"""Continuous-shift caliber for a re-melt line that keeps melting across handovers.

The batch caliber in ``balance.py`` treats every melt as a closed DAG.  Here a
vessel is never closed: the ending remainder of one shift is the opening
remainder of the next one.  Handover weighings are a shared ledger between two
adjacent shifts, not newly purchased material, so they create no mass and no
feed origin.  Origins that cannot be established (open residuals without an
adjacent handover basis) are booked as ``pending``: their mass is certified,
their source is not.
"""
from fractions import Fraction


class LineError(ValueError):
    pass


def _q(value):
    """Render an exact fraction the same way the batch facts render them."""
    value = value if isinstance(value, Fraction) else Fraction(value)
    return str(value.numerator) if value.denominator == 1 else f'{value.numerator}/{value.denominator}'


def _plain(value):
    """Coerce every Fraction in a fact tree to its exact string rendering."""
    if isinstance(value, Fraction):
        return _q(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _draw(composition, grams):
    """Remove grams from a fully mixed vessel, proportional to composition."""
    if grams == 0:
        return {}
    total = sum(composition.values(), Fraction(0))
    if grams > total:
        raise LineError(f'event draws {grams}g but vessel holds {_q(total)}g')
    ratio = Fraction(grams, total)
    taken = {lot: amount * ratio for lot, amount in composition.items()}
    for lot, amount in taken.items():
        composition[lot] -= amount
        if composition[lot] == 0:
            del composition[lot]
    return taken


def _origin_rows(parts):
    return [{'kind': kind, 'id': oid, 'grams': _q(grams)}
            for (kind, oid), grams in sorted(parts.items(), key=lambda item: (item[0][0], item[0][1]))]


def _event_summary(event):
    return {key: value for key, value in event.items() if key != 'type'}


def reconcile(line):
    """Merge the two boundary reports of every handover into one ledger row."""
    reports = {}
    vessel_of_shift = {shift['id']: shift['vessel'] for shift in line['shifts']}
    for index, shift in enumerate(line['shifts']):
        for boundary in ('start', 'end'):
            raw = shift[boundary]
            reports.setdefault(raw['handover'], []).append(
                {'shift': shift['id'], 'boundary': boundary,
                 'lo': raw['lo'], 'hi': raw['hi'], 'seq': index})
    ledger = {}
    for hid, info in line['handovers'].items():
        rows = reports.get(hid)
        if not rows:
            raise LineError(f'handover {hid}: never reported by any shift')
        vessels = {vessel_of_shift[r['shift']] for r in rows}
        if vessels != {info['vessel']}:
            raise LineError(f'handover {hid}: reports must stay on vessel {info["vessel"]}')
        if len(rows) > 2:
            raise LineError(f'handover {hid}: repeated weighing reports do not add mass')
        lo, hi = max(r['lo'] for r in rows), min(r['hi'] for r in rows)
        if lo > hi:
            raise LineError(f'handover {hid}: adjacent shifts report incompatible weighings')
        if info['open']:
            if len(rows) != 1:
                raise LineError(f'handover {hid}: an open residual has no adjacent shift to confirm it')
        elif len(rows) != 2:
            raise LineError(f'handover {hid}: internal handover needs both adjacent shift reports')
        ledger[hid] = {**info, 'lo': lo, 'hi': hi, 'resolved': lo == hi,
                       'reports': sorted(({k: r[k] for k in ('shift', 'boundary', 'lo', 'hi')}
                                          for r in rows), key=lambda r: (r['shift'], r['boundary']))}
    if set(reports) != set(line['handovers']):
        raise LineError('every ledger handover must be used by a shift boundary')
    chains = {}
    for shift in line['shifts']:
        chains.setdefault(shift['vessel'], []).append(shift)
    for vessel, chain in chains.items():
        for previous, current in zip(chain, chain[1:]):
            if previous['end']['handover'] != current['start']['handover']:
                raise LineError(
                    f'vessel {vessel}: shifts {previous["id"]} and {current["id"]} do not share a handover')
        for edge, boundary in ((chain[0], 'start'), (chain[-1], 'end')):
            if not ledger[edge[boundary]['handover']]['open']:
                raise LineError(
                    f'handover {edge[boundary]["handover"]}: a vessel edge boundary must be marked open')
    return ledger, chains


def check_transfers(line):
    """One transfer = one outbound draw plus exactly one inbound occupancy."""
    outbound, inbound = {}, {}
    shift_index = {shift['id']: i for i, shift in enumerate(line['shifts'])}
    for shift in line['shifts']:
        for event in shift['events']:
            if event['type'] == 'transfer':
                if event['id'] in outbound:
                    raise LineError(f'transfer {event["id"]}: only one actual outbound occupancy is allowed')
                outbound[event['id']] = (shift, event)
            elif event['type'] == 'transfer_in':
                if event['transfer'] in inbound:
                    raise LineError(
                        f'transfer {event["transfer"]}: only one vessel may occupy this draw')
                inbound[event['transfer']] = (shift, event)
    if set(outbound) != set(inbound):
        raise LineError('every transfer needs one outbound event and one inbound occupancy')
    for tid, (out_shift, out_event) in outbound.items():
        in_shift, in_event = inbound[tid]
        if out_shift['vessel'] == in_shift['vessel']:
            raise LineError(f'transfer {tid}: source and occupying vessel must differ')
        if shift_index[in_shift['id']] < shift_index[out_shift['id']]:
            raise LineError(f'transfer {tid}: inbound shift appears before its outbound shift')
        if 'grams' not in in_event:
            in_event['grams'] = out_event['grams']
        elif in_event['grams'] != out_event['grams']:
            raise LineError(f'transfer {tid}: inbound grams must equal the outbound weighing')
    return outbound, inbound


def feasibility(chains, ledger):
    """Propagate boundary intervals through the integer mass account of each vessel."""
    unresolved = set()
    for vessel, chain in chains.items():
        lo = ledger[chain[0]['start']['handover']]['lo']
        hi = ledger[chain[0]['start']['handover']]['hi']
        for shift in chain:
            for event in shift['events']:
                kind = event['type']
                if kind in ('feed', 'transfer_in'):
                    lo += event.get('grams', 0)
                    hi += event.get('grams', 0)
                elif kind in ('discharge', 'loss', 'cleanout', 'transfer'):
                    lo, hi = lo - event['grams'], hi - event['grams']
                    if hi < 0:
                        raise LineError(
                            f'shift {shift["id"]}: event of {event["grams"]}g exceeds vessel mass')
                    if lo < 0:
                        lo = 0
            bound = ledger[shift['end']['handover']]
            lo, hi = max(lo, bound['lo']), min(hi, bound['hi'])
            if lo > hi:
                raise LineError(
                    f'shift {shift["id"]}: mass account cannot satisfy handover {shift["end"]["handover"]}')
            if lo != hi:
                unresolved.add(shift['end']['handover'])
                unresolved.add(shift['start']['handover'])
    return sorted(unresolved)


def attribute_line(line):
    ledger, chains = reconcile(line)
    outbound, inbound = check_transfers(line)
    unresolved = feasibility(chains, ledger)
    if line['attestation'] == 'formal' and unresolved:
        raise LineError(
            'formal attestation requires every handover to be pinned by the mass account; '
            f'unresolved: {", ".join(unresolved)}')

    states = {vessel: {'lo': None, 'hi': None, 'attested': True, 'composition': {}}
              for vessel in chains}
    held_transfers, shift_facts, product_facts = {}, {}, {}
    degraded_shifts = set()
    global_seq = 0
    for shift in line['shifts']:
        global_seq += 1
        vessel, state = shift['vessel'], states[shift['vessel']]
        bound = ledger[shift['start']['handover']]
        if shift is chains[vessel][0]:
            state['lo'], state['hi'] = bound['lo'], bound['hi']
            if bound['resolved'] and bound['lo']:
                state['composition'] = {('pending', shift['start']['handover']): Fraction(bound['lo'])}
            if not bound['resolved']:
                state['attested'] = False
        shift_degraded = not state['attested'] or state['lo'] != state['hi']
        opening_rows = _origin_rows(state['composition']) if state['attested'] else []

        account = {'feed_g': 0, 'discharge_g': 0, 'loss_g': 0, 'transfer_in_g': 0,
                   'transfer_out_g': 0, 'cleanout_g': 0}
        event_facts, products, losses = [], [], []
        for seq, event in enumerate(shift['events'], 1):
            kind = event['type']
            fact = {'seq': seq, 'type': kind, **_event_summary(event)}
            if kind == 'feed':
                state['lo'] += event['grams']
                state['hi'] += event['grams']
                if state['attested']:
                    lot = ('feed', event['id'])
                    state['composition'][lot] = state['composition'].get(lot, 0) + event['grams']
                account['feed_g'] += event['grams']
            elif kind == 'changeover':
                pass
            elif kind in ('loss', 'discharge', 'cleanout', 'transfer'):
                grams = event['grams']
                if state['hi'] < grams:
                    raise LineError(f'shift {shift["id"]}: event of {grams}g exceeds vessel mass')
                state['lo'] -= grams
                state['hi'] -= grams
                taken = _draw(state['composition'], grams) if state['attested'] else None
                if kind == 'loss':
                    account['loss_g'] += grams
                    if taken is not None:
                        losses.append({'seq': seq, 'grams': _q(grams), 'origins': _origin_rows(taken)})
                else:
                    origins = _origin_rows(taken) if taken is not None else []
                    attested = state['attested'] and all(row['kind'] == 'feed' for row in origins)
                    record = {'kind': 'transfer' if kind == 'transfer' else kind,
                              'seq': seq, 'shift': shift['id'], 'vessel': vessel,
                              'grams': _q(grams), 'attested': attested, 'origins': origins}
                    if kind == 'transfer':
                        held_transfers[event['id']] = {
                            'composition': dict(taken) if taken else {},
                            'attested': state['attested'], 'grams': grams}
                        account['transfer_out_g'] += grams
                    else:
                        if kind == 'cleanout':
                            account['cleanout_g'] += grams
                            # Only a full emptying cuts the provenance chain; a partial
                            # cleanout keeps the remaining composition of the vessel.
                            if state['lo'] == 0 and state['hi'] == 0:
                                state['attested'], state['composition'] = True, {}
                        else:
                            account['discharge_g'] += grams
                            product_facts[event['id']] = record
                        products.append({'id': event['id'], **record})
            elif kind == 'transfer_in':
                tid, grams = event['transfer'], event['grams']
                parcel = held_transfers.get(tid)
                if parcel is None:
                    raise LineError(f'shift {shift["id"]}: transfer {tid} has not left its source vessel')
                state['lo'] += grams
                state['hi'] += grams
                if state['attested'] and parcel['attested']:
                    for lot, amount in parcel['composition'].items():
                        state['composition'][lot] = state['composition'].get(lot, 0) + amount
                else:
                    state['attested'], state['composition'] = False, {}
                account['transfer_in_g'] += grams
                fact['grams'] = grams
            event_facts.append(fact)

        end_bound = ledger[shift['end']['handover']]
        if state['hi'] < end_bound['lo'] or state['lo'] > end_bound['hi']:
            raise LineError(
                f'shift {shift["id"]}: mass account escapes handover {shift["end"]["handover"]} bounds')
        state['lo'], state['hi'] = max(state['lo'], end_bound['lo']), min(state['hi'], end_bound['hi'])
        if state['attested']:
            total = sum(state['composition'].values(), Fraction(0))
            if not (end_bound['lo'] <= total <= end_bound['hi']):
                raise LineError(
                    f'shift {shift["id"]}: measured ending {_q(total)}g disagrees with handover '
                    f'{shift["end"]["handover"]}')
            # The mass account pinned a bounded handover: both adjacent shifts share this value.
            end_bound['lo'] = end_bound['hi'] = total
            end_bound['resolved'] = True
            ledger[shift['end']['handover']]['lo'] = total
            ledger[shift['end']['handover']]['hi'] = total
            ledger[shift['end']['handover']]['resolved'] = True
        if not state['attested'] or state['lo'] != state['hi']:
            shift_degraded = True
            degraded_shifts.add(shift['id'])
        shift_facts[shift['id']] = {
            'vessel': vessel, 'seq': global_seq,
            'start_handover': shift['start']['handover'], 'end_handover': shift['end']['handover'],
            'exact': state['lo'] == state['hi'], 'attested': state['attested'],
            'opening_g': _q(state['lo']) if state['lo'] == state['hi'] else None,
            'ending_g': _q(state['lo']) if state['lo'] == state['hi'] else None,
            'opening_lo': None if state['lo'] == state['hi'] else state['lo'],
            'opening_hi': None if state['lo'] == state['hi'] else state['hi'],
            'ending_lo': state['lo'], 'ending_hi': state['hi'],
            'opening_composition': opening_rows,
            'ending_composition': _origin_rows(state['composition']) if state['attested'] else [],
            'mass_account': account, 'events': event_facts,
            'products': products, 'losses': losses}

    handover_facts = {
        hid: _plain({'vessel': row['vessel'], 'open': row['open'], 'lo': row['lo'], 'hi': row['hi'],
                     'grams': _q(row['lo']) if row['resolved'] else None,
                     'resolved': row['resolved'], 'status': _status(row), 'reports': row['reports']})
        for hid, row in ledger.items()}
    transfer_facts = {}
    for tid, (out_shift, out_event) in outbound.items():
        in_shift, _ = inbound[tid]
        parcel = held_transfers[tid]
        transfer_facts[tid] = {
            'shift': out_shift['id'], 'vessel': out_shift['vessel'],
            'in_shift': in_shift['id'], 'in_vessel': in_shift['vessel'],
            'grams': _q(out_event['grams']), 'attested': parcel['attested'],
            'origins': _origin_rows(parcel['composition']) if parcel['attested'] else []}
    return _plain({
        'attestation': line['attestation'],
        'vessels': [{'id': vessel, 'shift_order': [s['id'] for s in chain],
                     'handover_order': [chain[0]['start']['handover']]
                                        + [s['end']['handover'] for s in chain]}
                    for vessel, chain in chains.items()],
        'handovers': handover_facts,
        'shifts': shift_facts,
        'products': product_facts,
        'transfers': transfer_facts,
        'unresolved_handovers': unresolved,
        'degraded_shifts': sorted(degraded_shifts)})


def _status(row):
    if row['open']:
        return 'open'
    return 'confirmed' if row['resolved'] else 'bounded'


def line_report(facts, shift_ids=None):
    """Period-level totals; pending grams are never counted as verified recovery."""
    wanted = set(shift_ids) if shift_ids else set(facts['shifts'])
    unknown = wanted - set(facts['shifts'])
    if unknown:
        raise LineError(f'unknown shifts: {", ".join(sorted(unknown))}')
    attested_product = pending_product = Fraction(0)
    measured, loss, feed = 0, 0, 0
    for sid in sorted(wanted, key=lambda s: facts['shifts'][s]['seq']):
        shift = facts['shifts'][sid]
        feed += shift['mass_account']['feed_g']
        loss += shift['mass_account']['loss_g']
        for product in shift['products']:
            grams = Fraction(product['grams'])
            measured += grams
            if product['attested']:
                attested_product += grams
            else:
                pending_product += grams
    ending = []
    for vessel in facts['vessels']:
        last_sid = vessel['shift_order'][-1]
        if last_sid not in wanted:
            continue
        shift = facts['shifts'][last_sid]
        attested = sum((Fraction(row['grams']) for row in shift['ending_composition']
                        if row['kind'] == 'feed'), Fraction(0))
        pending = sum((Fraction(row['grams']) for row in shift['ending_composition']
                       if row['kind'] == 'pending'), Fraction(0))
        ending.append({'vessel': vessel['id'],
                       'grams': shift['ending_g'] if shift['exact'] else None,
                       'lo': shift['ending_lo'], 'hi': shift['ending_hi'],
                       'attested_g': _q(attested), 'pending_g': _q(pending)})
    return _plain({'shifts': sorted(wanted), 'attestation': facts['attestation'],
                   'measured_product_g': measured,
                   'attested_product_g': _q(attested_product),
                   'pending_product_g': _q(pending_product),
                   'loss_g': loss, 'feed_g': feed,
                   'ending_remainder': ending,
                   'unresolved_handovers': facts['unresolved_handovers']})


def _interval_text(lo, hi):
    return str(lo) if lo == hi else f'{lo}..{hi}'


def compatible_revision(parent, child_line):
    """A corrected middle-shift weighing cannot graft onto a frozen confirmed ending.

    ``child_line`` is the raw parsed line (before reconciliation), so this sees
    what each shift actually reported instead of the pinned shared ledger value.
    """
    parent_vessels = {v['id']: v for v in parent['vessels']}
    if set(parent_vessels) != set(child_line['vessels']):
        return ['vessel structure changed; publish a new series instead']
    parent_order = [sid for vessel in parent['vessels'] for sid in vessel['shift_order']]
    if [shift['id'] for shift in child_line['shifts']] != parent_order:
        return ['shift structure changed; publish a new series instead']
    if set(parent['handovers']) != set(child_line['handovers']):
        return ['handover ledger structure changed; publish a new series instead']
    raw_reports = {}
    for shift in child_line['shifts']:
        for boundary in ('start', 'end'):
            raw = shift[boundary]
            raw_reports.setdefault(raw['handover'], []).append(
                {'shift': shift['id'], 'boundary': boundary, 'lo': raw['lo'], 'hi': raw['hi']})
    if set(raw_reports) != set(parent['handovers']):
        return ['handover ledger structure changed; publish a new series instead']
    conflicts = []
    for hid, frozen in parent['handovers'].items():
        rows = raw_reports[hid]
        lo, hi = max(r['lo'] for r in rows), min(r['hi'] for r in rows)
        frozen_lo, frozen_hi = int(frozen['lo']), int(frozen['hi'])
        parent_reports = {(r['shift'], r['boundary']): (int(r['lo']), int(r['hi']))
                          for r in frozen['reports']}
        child_pairs = {(r['shift'], r['boundary']): (r['lo'], r['hi']) for r in rows}
        changed_sides = [key for key, interval in parent_reports.items()
                         if child_pairs.get(key) != interval]
        disjoint = lo > frozen_hi or hi < frozen_lo
        if disjoint and len(changed_sides) < 2:
            conflicts.append(
                f'handover {hid}: new weighing {_interval_text(lo, hi)} is incompatible with frozen '
                f'{_interval_text(frozen_lo, frozen_hi)}')
            continue
        if frozen['resolved'] and not frozen['open'] and changed_sides and len(changed_sides) < 2:
            conflicts.append(
                f'handover {hid}: confirmed weighing {frozen_lo}g changed without both '
                'adjacent shifts reconfirming it')
    return conflicts


def compare_line_releases(parent, child):
    """Locate source changes, mass-stable products and the cleanout that cuts propagation."""
    changed_handovers, source_changed, mass_changed = [], [], []
    for hid in sorted(set(parent['handovers']) | set(child['handovers'])):
        prow, crow = parent['handovers'].get(hid), child['handovers'].get(hid)
        if prow != crow:
            changed_handovers.append({'handover': hid,
                                      'parent_grams': prow and prow['grams'],
                                      'grams': crow and crow['grams'],
                                      'parent_lo': prow and prow['lo'],
                                      'parent_hi': prow and prow['hi'],
                                      'lo': crow and crow['lo'], 'hi': crow and crow['hi']})

    def product_index(facts):
        index = {}
        for shift in facts['shifts'].values():
            for product in shift['products']:
                index[product['id']] = product
        for tid, transfer in facts['transfers'].items():
            index[tid] = {'kind': 'transfer', 'seq': 0, 'shift': transfer['shift'],
                          'vessel': transfer['vessel'], 'grams': transfer['grams'],
                          'attested': transfer['attested'], 'origins': transfer['origins']}
        return index

    old, new = product_index(parent), product_index(child)
    for pid in sorted(set(old) | set(new)):
        before, after = old.get(pid), new.get(pid)
        if before is None or after is None:
            (source_changed if after else mass_changed).append(pid)
            continue
        if before['grams'] != after['grams'] or before['attested'] != after['attested']:
            mass_changed.append(pid)
        if before['origins'] != after['origins']:
            source_changed.append(pid)
    mass_unchanged = sorted(pid for pid in new if pid in old and pid not in mass_changed)

    positions = _positions(child)
    end_seq = {shift['end_handover']: shift['seq'] for shift in child['shifts'].values()}
    cutoffs = {}
    for vessel in child['vessels']:
        vid = vessel['id']
        touched = [(pos[1], pos[2]) for pid, pos in positions.items()
                   if pid in set(source_changed) | set(mass_changed) and pos[0] == vid]
        touched += [(end_seq[row['handover']], 0) for row in changed_handovers
                    if row['handover'] in vessel['handover_order']]
        if not touched:
            continue
        first_change = min(touched)
        cleanouts = [(pid, (pos[1], pos[2])) for pid, pos in positions.items()
                     if pos[0] == vid and new[pid]['kind'] == 'cleanout'
                     and (pos[1], pos[2]) > first_change]
        if cleanouts:
            cutoff_pid, cutoff_pos = min(cleanouts, key=lambda item: item[1])
            cutoffs[vid] = {
                'cleanout': cutoff_pid, 'after_change_seq': first_change[0],
                'unaffected_after': sorted(pid for pid, pos in positions.items()
                                           if pos[0] == vid and (pos[1], pos[2]) > cutoff_pos)}
    return {'changed_handovers': changed_handovers,
            'source_changed': sorted(set(source_changed)),
            'mass_changed': sorted(set(mass_changed)),
            'mass_unchanged': mass_unchanged,
            'cleanout_cutoffs': cutoffs}


def _positions(facts):
    positions = {}
    for shift in facts['shifts'].values():
        for product in shift['products']:
            positions[product['id']] = (shift['vessel'], shift['seq'], product['seq'])
    for tid, transfer in facts['transfers'].items():
        positions[tid] = (transfer['vessel'], facts['shifts'][transfer['shift']]['seq'], 0)
    return positions
