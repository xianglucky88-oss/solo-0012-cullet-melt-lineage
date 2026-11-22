"""Component-assay lineage layered on top of the batch mass DAG.

Glass mass conservation says nothing about how a trace component splits between
product glass and the dust drawn off during melting.  This module keeps a
separate concentration for every physical stream:

* the ``material`` stream of a lot, and
* the phase-separation ``loss`` stream of a melt.

Concentrations are bounded by laboratory specimen ranges, blended-specimen
proportions and completed re-tests, and are tied together by component
conservation at every melt::

    sum(draw_g * c[source]) == output_g * c[material] + loss_g * c[loss]

The loss term is a free variable, so dust may carry far more of the component
than the glass-gram pro-rata share.  Every concentration is an exact
``Fraction`` and every bound is a linear programme over *all* compatible
interpretations, because an unconditional release is only honest when no
interpretation breaches the declared limit.

Qualification travels along the *actual draws* in the weighing record, never
along a batch number or an attached report.  When a charge is physically
present but its basis is missing, the mass facts stay untouched and the assay
status becomes ``unqualified``: the next furnace inherits the gap rather than
an automatic release.  A completed re-test is new evidence, not a rewrite: a
revision may re-sign a conditional melt, but the earlier revision keeps the
basis it was signed under.
"""
from fractions import Fraction
from graphlib import CycleError, TopologicalSorter

from .assay import Infeasible, LinearProgram, Unbounded, explain_bounds
from .recipe import PPM_MAX

PPM = PPM_MAX
QUALIFIED = 'qualified'
CONDITIONAL = 'conditional'
HOLD = 'hold'
UNQUALIFIED = 'unqualified'
UNSIGNED = 'unsigned'


def _q(value):
    value = value if isinstance(value, Fraction) else Fraction(value)
    return str(value.numerator) if value.denominator == 1 else f'{value.numerator}/{value.denominator}'


def _order(materials):
    graph = TopologicalSorter()
    for key, material in materials.items():
        graph.add(key, *(source for source, _ in material.inputs))
    try:
        return tuple(graph.static_order())
    except CycleError as error:
        raise ValueError('material ancestry contains a cycle') from error


def _unit(width, index):
    row = [Fraction(0)] * width
    row[index] = Fraction(1)
    return row


def _declared(material):
    return material.limit_min is not None or material.limit_max is not None


def _outside(material, low, high):
    broken = []
    if material.limit_max is not None and high > material.limit_max:
        broken.append(f'upper bound {_q(high)} ppm exceeds limit {material.limit_max} ppm')
    if material.limit_min is not None and low < material.limit_min:
        broken.append(f'lower bound {_q(low)} ppm is under limit {material.limit_min} ppm')
    return broken


def _retest_overrides(component):
    """Completed re-tests replace first-round ranges on the same physical stream."""
    samples, streams = set(), set()
    for retest in component['retests'].values():
        if retest['pending']:
            continue
        if retest['sample'] is not None:
            samples.add(retest['sample'])
        else:
            streams.add((retest['stream'], retest['of']))
    return samples, streams


class _Model:
    def __init__(self, materials, component):
        self.materials = materials
        self.component = component
        self.index = {}
        names = []
        for key in materials:
            self.index[('material', key)] = len(names)
            names.append(f'{key}:material')
            if materials[key].loss:
                self.index[('loss', key)] = len(names)
                names.append(f'{key}:loss')
        self.lp = LinearProgram(names)
        self.width = len(names)
        self._add_purity()
        self._add_samples()
        self._add_composites()
        self._add_retests()
        self._add_balance()

    def stream_index(self, stream, key):
        return self.index[(stream, key)]

    def _add_purity(self):
        for (stream, key), index in self.index.items():
            self.lp.add(_unit(self.width, index), '<=', PPM,
                        {'kind': 'purity', 'stream': stream, 'material': key})

    def _add_samples(self):
        overridden, streams = _retest_overrides(self.component)
        for sample in self.component['samples'].values():
            if sample['pending'] or sample['id'] in overridden:
                continue
            if (sample['stream'], sample['of']) in streams:
                continue
            index = self.stream_index(sample['stream'], sample['of'])
            tag = {'kind': 'sample', 'id': sample['id']}
            self.lp.add(_unit(self.width, index), '>=', sample['lo'], dict(tag, bound='min'))
            self.lp.add(_unit(self.width, index), '<=', sample['hi'], dict(tag, bound='max'))

    def _mix_index(self, entry):
        if entry['kind'] == 'sample':
            sample = self.component['samples'][entry['ref']]
            return self.stream_index(sample['stream'], sample['of'])
        return self.stream_index('material', entry['ref'])

    def _add_composites(self):
        for composite in self.component['composites'].values():
            row = [Fraction(0)] * self.width
            total = 0
            for entry in composite['mix']:
                row[self._mix_index(entry)] += entry['parts']
                total += entry['parts']
            tag = {'kind': 'composite', 'id': composite['id']}
            self.lp.add(row, '>=', Fraction(total * composite['lo']), dict(tag, bound='min'))
            self.lp.add(row, '<=', Fraction(total * composite['hi']), dict(tag, bound='max'))

    def _add_retests(self):
        for retest in self.component['retests'].values():
            if retest['pending']:
                continue
            index = self.stream_index(retest['stream'], retest['of'])
            tag = {'kind': 'retest', 'id': retest['id']}
            self.lp.add(_unit(self.width, index), '>=', retest['lo'], dict(tag, bound='min'))
            self.lp.add(_unit(self.width, index), '<=', retest['hi'], dict(tag, bound='max'))

    def weighing(self, key):
        material = self.materials[key]
        return {'melt': key, 'output_g': material.grams, 'loss_g': material.loss,
                'inputs': [{'from': source, 'grams': grams}
                           for source, grams in material.inputs]}

    def _add_balance(self):
        for key, material in self.materials.items():
            if not material.inputs:
                continue
            row = [Fraction(0)] * self.width
            for source, grams in material.inputs:
                row[self.stream_index('material', source)] += grams
            row[self.stream_index('material', key)] -= material.grams
            if material.loss:
                row[self.stream_index('loss', key)] -= material.loss
            self.lp.add(row, '==', 0, {'kind': 'weighing', 'weighing': self.weighing(key)})

    def stream_bounds(self):
        result = {}
        for (stream, key), index in self.index.items():
            try:
                low, high, low_tight, high_tight = explain_bounds(self.lp, index)
            except Infeasible as error:
                raise ValueError('assay constraints have no compatible interpretation '
                                 f'for {key}:{stream}') from error
            except Unbounded as error:
                raise ValueError('assay bounds are unbounded; tighten the component '
                                 'ranges') from error
            grams = self.materials[key].grams if stream == 'material' else self.materials[key].loss
            scale = Fraction(grams, PPM)
            result[(stream, key)] = {
                'min_ppm': _q(low), 'max_ppm': _q(high),
                'min_g': _q(low * scale), 'max_g': _q(high * scale), 'stream_g': grams,
                'min_support': _support(low_tight, self.lp),
                'max_support': _support(high_tight, self.lp)}
        return result


def _support(positions, program):
    """Rows essential to an extremum, grouped by the record a reviewer can open."""
    grouped = {'sample': [], 'retest': [], 'composite': [], 'weighing': []}
    for position in positions:
        tag = program.rows[position][3]
        kind = tag['kind']
        if kind == 'weighing':
            grouped['weighing'].append(tag['weighing']['melt'])
        elif kind == 'purity':
            continue
        else:
            grouped[kind].append(tag['id'])
    return {key: sorted(set(value)) for key, value in grouped.items() if value}


def _evidence(component, materials):
    """Index every measured record so a bound's support keys can be reviewed."""
    labels = {}
    for sample in component['samples'].values():
        if sample['label'] is not None:
            labels.setdefault(sample['label'], []).append(sample['id'])
    evidence = {'samples': {}, 'composites': {}, 'retests': {}, 'weighings': {},
                'shared_labels': []}
    for sid, sample in component['samples'].items():
        evidence['samples'][sid] = {
            'of': sample['of'], 'stream': sample['stream'], 'label': sample['label'],
            'retained': sample['retained'], 'pending': sample['pending'],
            'min_ppm': None if sample['pending'] else sample['lo'],
            'max_ppm': None if sample['pending'] else sample['hi']}
    for label, ids in labels.items():
        if len(ids) > 1:
            # A shared label is allowed but never unifies two specimens' measurements.
            evidence['shared_labels'].append({'label': label, 'samples': sorted(ids)})
    for cid, composite in component['composites'].items():
        evidence['composites'][cid] = {
            'mix': composite['mix'], 'min_ppm': composite['lo'], 'max_ppm': composite['hi']}
    for rid, retest in component['retests'].items():
        evidence['retests'][rid] = {
            'condition': retest['condition'], 'of': retest['of'],
            'stream': retest['stream'], 'sample': retest['sample'],
            'pending': retest['pending'],
            'min_ppm': None if retest['pending'] else retest['lo'],
            'max_ppm': None if retest['pending'] else retest['hi']}
    for key, material in materials.items():
        if material.inputs:
            evidence['weighings'][key] = {
                'output_g': material.grams, 'loss_g': material.loss,
                'inputs': [{'from': source, 'grams': grams}
                           for source, grams in material.inputs]}
    return evidence


def _measured_streams(component):
    """Physical streams backed by at least one completed measurement."""
    overridden, superseded = _retest_overrides(component)
    measured = set()
    for sample in component['samples'].values():
        if sample['pending'] or sample['id'] in overridden:
            continue
        if (sample['stream'], sample['of']) in superseded:
            continue
        measured.add((sample['stream'], sample['of']))
    for composite in component['composites'].values():
        for entry in composite['mix']:
            if entry['kind'] == 'sample':
                sample = component['samples'][entry['ref']]
                measured.add((sample['stream'], sample['of']))
            else:
                measured.add(('material', entry['ref']))
    for retest in component['retests'].values():
        if not retest['pending']:
            measured.add((retest['stream'], retest['of']))
    return measured


def _ancestors(materials):
    ancestors = {}
    for key in _order(materials):
        found = set()
        for source, _ in materials[key].inputs:
            found.add(source)
            found |= ancestors.get(source, frozenset())
        ancestors[key] = frozenset(found)
    return ancestors


def _conditions(component, key):
    signoff = component['signoffs'].get(key)
    if signoff is None or signoff['verdict'] != 'conditional':
        return set()
    return set(signoff['retests'])


def _blocker_rows(materials, key, component, statuses, reasons):
    """Direct drawn inputs not qualified under this melt's own basis."""
    allowed = _conditions(component, key)
    rows, seen = [], set()
    for source, grams in materials[key].inputs:
        if source in seen:
            continue
        seen.add(source)
        status = statuses[source]
        if status == QUALIFIED:
            continue
        if status == CONDITIONAL and _conditions(component, source) <= allowed:
            continue
        rows.append({'material': source, 'grams': grams, 'status': status,
                     'conditions': sorted(_conditions(component, source)),
                     'reasons': list(reasons.get(source, []))})
    return rows


def analyse(materials, component):
    """Exact bounds, verdicts and joint-use statements for one release."""
    if component is None:
        return None
    model = _Model(materials, component)
    bounds = model.stream_bounds()
    measured = _measured_streams(component)
    order = _order(materials)
    statuses, reasons, pending_conditions = {}, {}, {}

    for key in order:
        material = materials[key]
        stream = bounds[('material', key)]
        broken = _outside(material, Fraction(stream['min_ppm']), Fraction(stream['max_ppm']))
        within = not broken
        signoff = component['signoffs'].get(key)
        pending_conditions[key] = set()

        if material.inputs and signoff is not None and signoff['verdict'] == 'released':
            if broken:
                raise ValueError(f'signoff for {key} cannot be released: risk is not '
                                 f'excluded ({"; ".join(broken)})')
            if _blocker_rows(materials, key, component, statuses, reasons):
                raise ValueError(f'signoff for {key} cannot be released: a charged input '
                                 'lacks a qualification basis')

        if not material.inputs:
            if ('material', key) not in measured and not _has_composite(component, key):
                statuses[key] = UNQUALIFIED
                reasons[key] = ['the feed material stream has no completed assay sample']
            elif not within:
                statuses[key] = HOLD
                reasons[key] = list(broken)
            else:
                statuses[key] = UNSIGNED if _declared(material) else QUALIFIED
                reasons[key] = (['no release signoff has been recorded'] if _declared(material)
                                else [])
            continue

        blockers = _blocker_rows(materials, key, component, statuses, reasons)
        hard = [row for row in blockers if row['status'] != CONDITIONAL]
        soft = [row for row in blockers if row['status'] == CONDITIONAL]
        notes = [f"{row['material']} is {row['status']}, not qualified" for row in hard]
        notes += [f"{row['material']} is conditional on "
                  f"{', '.join(row['conditions']) or 'a pending re-test'}" for row in soft]
        notes.extend(broken)
        reasons[key] = notes

        own = signoff['retests'] if signoff and signoff['verdict'] == 'conditional' else []
        for rid in own:
            if not component['retests'][rid]['pending']:
                raise ValueError(
                    f'signoff for {key} is conditional but re-test {rid} already has a result; '
                    'publish the completed result as a released signoff instead')

        if hard:
            statuses[key] = UNQUALIFIED
        elif soft:
            statuses[key] = CONDITIONAL
            inherited = set().union(*(set(row['conditions']) for row in soft)) | set(own)
            pending_conditions[key] = {rid for rid in inherited
                                       if component['retests'][rid]['pending']}
        elif not within:
            if own:
                statuses[key] = CONDITIONAL
                pending_conditions[key] = set(own)
            else:
                statuses[key] = HOLD
        elif signoff is None:
            statuses[key] = UNSIGNED if _declared(material) else QUALIFIED
            if _declared(material):
                reasons[key].append('no release signoff has been recorded')
        elif signoff['verdict'] == 'released':
            statuses[key] = QUALIFIED
            reasons[key] = ['all compatible interpretations stay within the limit']
        elif signoff['verdict'] == 'conditional':
            statuses[key] = CONDITIONAL
            pending_conditions[key] = set(own)
        else:
            statuses[key] = HOLD
            if not reasons[key]:
                reasons[key] = ['held by QC under the signed verdict']

    facts = {}
    summary = {'by_status': {}, 'qualified_output_g': 0, 'conditional_planned_g': 0,
               'held_output_g': 0}
    for key in order:
        material, status = materials[key], statuses[key]
        streams = {'material': bounds[('material', key)]}
        if ('loss', key) in bounds:
            streams['loss'] = bounds[('loss', key)]
        qualified = material.grams if status == QUALIFIED else 0
        planned = material.grams if status in (QUALIFIED, CONDITIONAL) else 0
        signoff = component['signoffs'].get(key)
        facts[key] = {
            'kind': 'feed' if not material.inputs else 'melt',
            'limit': None if not _declared(material) else {
                'min_ppm': material.limit_min, 'max_ppm': material.limit_max},
            'streams': streams,
            'status': status,
            'reasons': reasons[key],
            'conditions': sorted(pending_conditions[key]) if status == CONDITIONAL else [],
            'physical_output_g': material.grams,
            'input_draws': [{'from': source, 'grams': grams}
                            for source, grams in material.inputs],
            'qualified_g': qualified,
            'planned_g': planned,
            'support': {'min': streams['material']['min_support'],
                        'max': streams['material']['max_support']},
            'signoff': None if signoff is None else {
                'verdict': signoff['verdict'], 'retests': signoff['retests'],
                'note': signoff['note']},
            'blocked_by': [] if not material.inputs
            else _blocker_rows(materials, key, component, statuses, reasons)}
        summary['by_status'][status] = summary['by_status'].get(status, 0) + 1
        if material.inputs:
            summary['qualified_output_g'] += qualified
            if status == CONDITIONAL:
                summary['conditional_planned_g'] += material.grams
            if status not in (QUALIFIED, CONDITIONAL):
                summary['held_output_g'] += material.grams

    return {
        'component': component['id'],
        'materials': facts,
        'evidence': _evidence(component, materials),
        'signoffs': {key: dict(row) for key, row in component['signoffs'].items()},
        'joint_use': _joint_use(materials, component, statuses, reasons, bounds,
                                pending_conditions),
        'summary': summary,
    }


def _has_composite(component, material):
    return any(entry['kind'] == 'material' and entry['ref'] == material
               for composite in component['composites'].values()
               for entry in composite['mix'])


def _joint_use(materials, component, statuses, reasons, bounds, pending_conditions):
    """Explain, per re-test, why branches may or may not share its conclusion."""
    ancestors = _ancestors(materials)
    limited = [key for key in _order(materials)
               if materials[key].inputs and _declared(materials[key])]
    rows = []
    for rid in sorted(component['retests']):
        retest = component['retests'][rid]
        branches = sorted({key for key in limited
                           if retest['of'] in ancestors[key]
                           or rid in _conditions(component, key)})
        if len(branches) < 2:
            continue
        released = [key for key in branches if statuses[key] == QUALIFIED]
        usable = len(released) == len(branches) and not retest['pending']
        target = f"{retest['of']}:{retest['stream']}"
        if retest['pending']:
            reason = (f're-test {rid} of {target} is pending; no branch may be used as '
                      'qualified feed until it passes')
        elif usable:
            reason = (f'one re-test {rid} of {target} supports every branch, so the branches '
                      'may be used together under the same result')
        else:
            blocked = [key for key in branches if statuses[key] != QUALIFIED]
            detail = '; '.join(f"{key} is {statuses[key]}"
                               f" ({'; '.join(reasons[key]) or 'no basis'})" for key in blocked)
            reason = (f're-test {rid} of {target} changes every branch, but they cannot share '
                      f'one conclusion: {detail}')
        rows.append({
            'retest': rid, 'condition': retest['condition'],
            'upstream': retest['of'], 'stream': retest['stream'],
            'pending': retest['pending'], 'branches': branches,
            'jointly_usable': usable, 'reason': reason,
            'branch_status': [{
                'melt': key, 'status': statuses[key],
                'conditions': sorted(pending_conditions[key]),
                'min_ppm': bounds[('material', key)]['min_ppm'],
                'max_ppm': bounds[('material', key)]['max_ppm'],
                'min_support': bounds[('material', key)]['min_support'],
                'max_support': bounds[('material', key)]['max_support'],
                'reasons': reasons[key]} for key in branches]})
    return rows


def compare_assay(parent, child):
    """Locate changed evidence and the verdict consequences of a new re-test."""
    parent = parent or {}
    changed = []
    old_evidence, new_evidence = parent.get('evidence', {}), child.get('evidence', {})
    for section in ('samples', 'composites', 'retests', 'weighings'):
        before, after = old_evidence.get(section, {}), new_evidence.get(section, {})
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                changed.append(f'{section[:-1]}:{key}')
    old_materials, new_materials = parent.get('materials', {}), child.get('materials', {})
    for key in sorted(set(old_materials) | set(new_materials)):
        if old_materials.get(key) != new_materials.get(key):
            changed.append(f'material:{key}')
    old_signoffs, new_signoffs = parent.get('signoffs', {}), child.get('signoffs', {})
    for key in sorted(set(old_signoffs) | set(new_signoffs)):
        if old_signoffs.get(key) != new_signoffs.get(key):
            changed.append(f'signoff:{key}')

    status_changed = []
    for key in sorted(set(old_materials) | set(new_materials)):
        before, after = old_materials.get(key), new_materials.get(key)
        if before is None or after is None:
            continue
        if before['status'] != after['status']:
            status_changed.append({'material': key, 'from': before['status'],
                                   'to': after['status'], 'reasons': after['reasons']})

    previous_joint = {row['retest']: row for row in parent.get('joint_use', [])}
    retest_impact = []
    for row in child.get('joint_use', []):
        before = {branch['melt']: branch['status']
                  for branch in previous_joint.get(row['retest'], {}).get('branch_status', [])}
        retest_impact.append({
            'retest': row['retest'], 'condition': row['condition'],
            'upstream': row['upstream'], 'stream': row['stream'],
            'pending': row['pending'], 'jointly_usable': row['jointly_usable'],
            'reason': row['reason'],
            'branches': [{'melt': branch['melt'], 'from': before.get(branch['melt']),
                          'to': branch['status'], 'min_ppm': branch['min_ppm'],
                          'max_ppm': branch['max_ppm']}
                         for branch in row['branch_status']]})
    return {'changed': sorted(changed), 'status_changed': status_changed,
            'retest_impact': retest_impact, 'joint_use': child.get('joint_use', [])}
