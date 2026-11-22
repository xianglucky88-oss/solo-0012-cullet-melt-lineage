"""Parse full releases of weighed feed lots, melt operations and assay records.

The mass section is unchanged from the original contract.  The assay section is
optional and is accepted only together with a ``component`` declaration; it
lists measured specimen ranges (``samples``), laboratory mixtures with their
sampling proportions (``composites``), pending or reported re-tests, and the QC
signoffs for limited melts.
"""
from dataclasses import dataclass
import re

PPM_MAX = 10**6

# A batch number, release sheet or attached certificate identifies paperwork; it
# never measures a physical specimen.  These fields are rejected as evidence.
_DOCUMENT_FIELDS = ('batch', 'batch_no', 'release', 'release_no', 'release_sheet',
                    'release_note', 'certificate', 'certificate_no', 'coa',
                    'report', 'reports', 'attached_report')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('invalid material or release identifier')
    return value


def mass(value, *, allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 10**12:
        raise ValueError('mass must be integer grams in range')
    return value


def ppm(value, name='component'):
    if type(value) is not int or not 0 <= value <= PPM_MAX:
        raise ValueError(f'{name} must be integer ppm in range')
    return value


def _ordered_pair(low, high, owner):
    if low > high:
        raise ValueError(f'{owner}: inverted concentration range')
    return low, high


@dataclass(frozen=True)
class Material:
    row: int
    grams: int
    loss: int = 0
    inputs: tuple = ()
    limit_min: object = None
    limit_max: object = None


def _limit(item, owner):
    """Declared component range for a material: ``limit_ppm`` or a ``limit`` object."""
    simple = item.get('limit_ppm')
    detailed = item.get('limit')
    if simple is not None and detailed is not None:
        raise ValueError(f'{owner}: declare limit_ppm or limit, not both')
    if simple is not None:
        return None, ppm(simple, f'{owner}: limit_ppm')
    if detailed is None:
        return None, None
    if not isinstance(detailed, dict):
        raise ValueError(f'{owner}: limit must be an object')
    if 'component' in detailed:
        identifier(detailed['component'])
    low = ppm(detailed['min_ppm'], f'{owner}: limit min_ppm') if 'min_ppm' in detailed else None
    high = ppm(detailed['max_ppm'], f'{owner}: limit max_ppm') if 'max_ppm' in detailed else None
    if low is None and high is None:
        raise ValueError(f'{owner}: limit needs min_ppm and/or max_ppm')
    if low is not None and high is not None and low > high:
        raise ValueError(f'{owner}: inverted limit range')
    return low, high


def decode(doc):
    _reject_documents(doc, 'release')
    series, revision = identifier(doc['series']), identifier(doc['revision'])
    parent = doc.get('parent')
    if parent is not None:
        parent = identifier(parent)
        if parent == revision:
            raise ValueError('a release cannot parent itself')
    rows, materials = doc['materials'], {}
    if not isinstance(rows, list) or not rows:
        raise ValueError('expected nonempty materials array')
    for row, item in enumerate(rows, 1):
        key, grams = identifier(item['id']), mass(item['grams'])
        if key in materials:
            raise ValueError(f'duplicate material {key}')
        _reject_documents(item, key)
        low, high = _limit(item, key)
        if item['kind'] == 'feed':
            material = Material(row, grams, limit_min=low, limit_max=high)
        elif item['kind'] == 'melt':
            loss = mass(item['loss_g'], allow_zero=True)
            inputs = item['inputs']
            if not isinstance(inputs, list) or not inputs:
                raise ValueError(f'{key}: melt requires input draws')
            draws = tuple((identifier(draw['from']), mass(draw['grams'])) for draw in inputs)
            for draw in inputs:
                _reject_documents(draw, f'{key}: draw from {draw.get("from")}')
            if sum(amount for _, amount in draws) != grams + loss:
                raise ValueError(f'{key}: charge must equal output plus loss')
            material = Material(row, grams, loss, draws, low, high)
        else:
            raise ValueError(f'{key}: expected feed or melt')
        materials[key] = material
    return series, revision, parent, materials


def _reject_documents(item, owner):
    """Reject document references masquerading as measurement evidence."""
    if not isinstance(item, dict):
        return
    for field in _DOCUMENT_FIELDS:
        if field in item:
            raise ValueError(
                f'{owner}: {field} is a document reference, not a measured specimen; '
                'record a sample range bound to a distinct specimen')


def _range(item, owner):
    if 'min_ppm' not in item or 'max_ppm' not in item:
        raise ValueError(f'{owner}: expected min_ppm and max_ppm')
    return _ordered_pair(ppm(item['min_ppm'], f'{owner}: min_ppm'),
                         ppm(item['max_ppm'], f'{owner}: max_ppm'), owner)


def _stream(value, owner):
    stream = 'material' if value is None else value
    if stream not in ('material', 'loss'):
        raise ValueError(f'{owner}: stream must be material or loss')
    return stream


def _check_stream(materials, material, stream, owner):
    if stream == 'loss' and materials[material].loss == 0:
        raise ValueError(f'{owner}: {material} has no phase-separation loss stream')


def decode_component(doc, materials):
    """Parse the optional assay section; ``None`` when the release is mass-only."""
    raw = doc.get('component')
    if raw is None:
        extra = set(doc) & {'samples', 'composites', 'retests', 'signoffs'}
        if extra:
            raise ValueError(f'{sorted(extra)[0]}: assay records require a component declaration')
        return None
    if isinstance(raw, str):
        component = identifier(raw)
    elif isinstance(raw, dict):
        component = identifier(raw['id'])
        if raw.get('unit', 'ppm') != 'ppm':
            raise ValueError('component unit must be ppm')
    else:
        raise ValueError('component must be an identifier')

    for item in doc['materials']:
        declared = item.get('limit')
        if isinstance(declared, dict) and 'component' in declared:
            if identifier(declared['component']) != component:
                raise ValueError(
                    f"{item['id']}: limit names a different component than {component!r}")

    samples, labels = {}, {}
    for item in doc.get('samples') or []:
        sid = identifier(item['id'])
        if sid in samples:
            raise ValueError(f'duplicate sample {sid}')
        for alias in ('same_as', 'alias_of', 'copy_of', 'duplicate_of', 'borrows', 'shares'):
            if alias in item:
                raise ValueError(f'sample {sid}: one specimen cannot borrow another '
                                 "specimen's measurement")
        _reject_documents(item, f'sample {sid}')
        of, stream = identifier(item['of']), _stream(item.get('stream'), f'sample {sid}')
        if of not in materials:
            raise ValueError(f'sample {sid}: unknown material {of}')
        _check_stream(materials, of, stream, f'sample {sid}')
        pending = item.get('pending', False)
        if type(pending) is not bool:
            raise ValueError(f'sample {sid}: pending must be boolean')
        retained = item.get('retained', False)
        if type(retained) is not bool:
            raise ValueError(f'sample {sid}: retained must be boolean')
        if pending:
            if 'min_ppm' in item or 'max_ppm' in item:
                raise ValueError(f'sample {sid}: a pending sample has no measured range')
            low = high = None
        else:
            low, high = _range(item, f'sample {sid}')
        label = item.get('label')
        if label is not None and (not isinstance(label, str) or not label):
            raise ValueError(f'sample {sid}: invalid label')
        if label is not None:
            labels.setdefault(label, []).append(sid)
        samples[sid] = {'id': sid, 'of': of, 'stream': stream, 'label': label,
                        'retained': retained, 'pending': pending, 'lo': low, 'hi': high}

    composites = {}
    for item in doc.get('composites') or []:
        cid = identifier(item['id'])
        if cid in composites or cid in samples:
            raise ValueError(f'duplicate assay record {cid}')
        _reject_documents(item, f'composite {cid}')
        low, high = _range(item, f'composite {cid}')
        mix = item.get('mix')
        if not isinstance(mix, list) or len(mix) < 2:
            raise ValueError(f'composite {cid}: mix needs at least two components')
        total = sum(part.get('parts') for part in mix
                    if type(part.get('parts')) is int)
        if total != 100:
            # Laboratory mixtures are specified in parts per hundred so that the
            # mixed range is directly comparable with a specimen range.
            raise ValueError(f'composite {cid}: mix parts must total 100')
        entries = []
        for part in mix:
            parts = part.get('parts')
            if type(parts) is not int or parts <= 0:
                raise ValueError(f'composite {cid}: parts must be a positive integer')
            if 'sample' in part and 'material' in part:
                raise ValueError(f'composite {cid}: each mix entry names one source')
            if 'sample' in part:
                ref, kind = identifier(part['sample']), 'sample'
                if ref not in samples:
                    raise ValueError(f'composite {cid}: unknown sample {ref}')
            elif 'material' in part:
                ref, kind = identifier(part['material']), 'material'
                if ref not in materials:
                    raise ValueError(f'composite {cid}: unknown material {ref}')
            else:
                raise ValueError(f'composite {cid}: mix entries name a sample or material')
            entries.append({'kind': kind, 'ref': ref, 'parts': parts})
        composites[cid] = {'id': cid, 'lo': low, 'hi': high, 'mix': entries}

    retests, targeted = {}, set()
    for item in doc.get('retests') or []:
        rid = identifier(item['id'])
        if rid in retests:
            raise ValueError(f'duplicate retest {rid}')
        _reject_documents(item, f'retest {rid}')
        condition = item.get('condition')
        if not isinstance(condition, str) or not condition.strip():
            raise ValueError(f'retest {rid}: a concrete re-test condition is required')
        if 'sample' in item:
            target = identifier(item['sample'])
            if target not in samples:
                if target in labels:
                    raise ValueError(
                        f'retest {rid}: {target!r} is a shared label, not a specimen; '
                        'name the distinct sample id')
                raise ValueError(f'retest {rid}: unknown sample {target}')
            if not samples[target]['retained']:
                raise ValueError(
                    f'retest {rid}: sample {target} is not marked retained, so it cannot be '
                    're-measured; a new specimen is a new sample, not a re-test of this one')
            of, stream = samples[target]['of'], samples[target]['stream']
            sample = target
        else:
            of, sample = identifier(item['of']), None
            stream = _stream(item.get('stream'), f'retest {rid}')
            if of not in materials:
                raise ValueError(f'retest {rid}: unknown material {of}')
            _check_stream(materials, of, stream, f'retest {rid}')
        # One re-test re-measures exactly one physical stream.
        key = ('sample', sample) if sample is not None else ('stream', of, stream)
        if key in targeted:
            raise ValueError(f'retest {rid}: another re-test already covers this stream')
        targeted.add(key)
        pending = item.get('pending', False)
        if type(pending) is not bool:
            raise ValueError(f'retest {rid}: pending must be boolean')
        if pending:
            if 'min_ppm' in item or 'max_ppm' in item:
                raise ValueError(f'retest {rid}: a pending re-test has no result range')
            low = high = None
        else:
            low, high = _range(item, f'retest {rid}')
        retests[rid] = {'id': rid, 'condition': condition, 'pending': pending,
                        'lo': low, 'hi': high, 'of': of, 'stream': stream, 'sample': sample}

    signoffs = {}
    for item in doc.get('signoffs') or []:
        _reject_documents(item, f'signoff {item.get("melt")}')
        melt = identifier(item['melt'])
        if melt not in materials or materials[melt].inputs == ():
            raise ValueError(f'signoff {melt}: expected a melt')
        if materials[melt].limit_min is None and materials[melt].limit_max is None:
            raise ValueError(f'signoff {melt}: melt does not declare a component limit')
        if melt in signoffs:
            raise ValueError(f'duplicate signoff for {melt}')
        verdict = item.get('verdict')
        if verdict not in ('released', 'hold', 'conditional'):
            raise ValueError(f'signoff {melt}: verdict must be released, hold or conditional')
        names = item.get('retests')
        if names is None:
            names = [item['retest']] if 'retest' in item else []
        if not isinstance(names, list):
            raise ValueError(f'signoff {melt}: retests must be an array')
        names = [identifier(rid) for rid in names]
        if verdict == 'conditional' and not names:
            raise ValueError(f'signoff {melt}: a conditional signoff names its re-test conditions')
        if verdict != 'conditional' and names:
            raise ValueError(f'signoff {melt}: only a conditional signoff names re-tests')
        for rid in names:
            if rid not in retests:
                raise ValueError(f'signoff {melt}: unknown retest {rid}')
        signoffs[melt] = {'melt': melt, 'verdict': verdict, 'retests': names,
                          'note': item.get('note')}

    return {'id': component, 'samples': samples, 'composites': composites,
            'retests': retests, 'signoffs': signoffs}
