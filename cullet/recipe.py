"""Parse a full release of weighed feed lots and melt operations."""
from dataclasses import dataclass
import re


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('invalid material or release identifier')
    return value


def mass(value, *, allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 10**12:
        raise ValueError('mass must be integer grams in range')
    return value


@dataclass(frozen=True)
class Material:
    row: int
    grams: int
    loss: int = 0
    inputs: tuple = ()


def decode(doc):
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
        if item['kind'] == 'feed':
            material = Material(row, grams)
        elif item['kind'] == 'melt':
            loss = mass(item['loss_g'], allow_zero=True)
            inputs = item['inputs']
            if not isinstance(inputs, list) or not inputs:
                raise ValueError(f'{key}: melt requires input draws')
            draws = tuple((identifier(draw['from']), mass(draw['grams'])) for draw in inputs)
            if sum(amount for _, amount in draws) != grams + loss:
                raise ValueError(f'{key}: charge must equal output plus loss')
            material = Material(row, grams, loss, draws)
        else:
            raise ValueError(f'{key}: expected feed or melt')
        materials[key] = material
    return series, revision, parent, materials


def _boundary(raw):
    """A shift boundary weighing: one exact mass or one shared closed interval."""
    handover = identifier(raw['handover'])
    if 'grams' in raw:
        lo = hi = mass(raw['grams'], allow_zero=True)
    elif 'min_g' in raw and 'max_g' in raw:
        lo, hi = mass(raw['min_g'], allow_zero=True), mass(raw['max_g'], allow_zero=True)
        if lo > hi:
            raise ValueError(f'handover {handover}: inverted bounds')
    else:
        raise ValueError(f'handover {handover}: expected grams or min_g/max_g')
    return {'handover': handover, 'lo': lo, 'hi': hi}


def _line_event(raw, lot_ids, product_ids, transfer_ids):
    kind = raw.get('type')
    if kind == 'feed':
        event = {'type': 'feed', 'id': identifier(raw['id']), 'grams': mass(raw['grams'])}
        if event['id'] in lot_ids:
            raise ValueError(f"duplicate feed lot {event['id']}")
        lot_ids.add(event['id'])
    elif kind == 'loss':
        event = {'type': 'loss', 'grams': mass(raw['grams'], allow_zero=True)}
    elif kind == 'discharge':
        event = {'type': 'discharge', 'id': identifier(raw['id']), 'grams': mass(raw['grams'])}
        if event['id'] in product_ids:
            raise ValueError(f"duplicate product {event['id']}")
        product_ids.add(event['id'])
    elif kind == 'cleanout':
        event = {'type': 'cleanout', 'id': identifier(raw['id']),
                 'grams': mass(raw['grams'], allow_zero=True)}
        if event['id'] in product_ids:
            raise ValueError(f"duplicate product {event['id']}")
        product_ids.add(event['id'])
    elif kind == 'transfer':
        event = {'type': 'transfer', 'id': identifier(raw['id']), 'grams': mass(raw['grams'])}
        if event['id'] in transfer_ids or event['id'] in product_ids:
            raise ValueError(f"duplicate transfer {event['id']}")
        transfer_ids.add(event['id'])
        product_ids.add(event['id'])
    elif kind == 'transfer_in':
        event = {'type': 'transfer_in', 'transfer': identifier(raw['transfer'])}
        if 'grams' in raw:
            event['grams'] = mass(raw['grams'], allow_zero=True)
    elif kind == 'changeover':
        event = {'type': 'changeover'}
    else:
        raise ValueError(f'unknown line event type {kind!r}')
    return event


def decode_line(doc):
    """Parse the continuous-shift section. Batch documents never reach this path."""
    attestation = doc.get('attestation', 'formal')
    if attestation not in ('formal', 'provisional'):
        raise ValueError('attestation must be formal or provisional')
    if 'materials' in doc:
        raise ValueError('a release carries either a batch materials graph or a continuous line')
    raw = doc.get('line')
    if not isinstance(raw, dict):
        raise ValueError('expected a line section for the continuous-shift caliber')
    vessels = [identifier(v) for v in raw.get('vessels', [])]
    if not vessels or len(set(vessels)) != len(vessels):
        raise ValueError('line requires unique nonempty vessels')
    handovers = {}
    for item in raw.get('handovers', []):
        hid, vessel = identifier(item['id']), identifier(item['vessel'])
        if vessel not in vessels:
            raise ValueError(f'handover {hid}: unknown vessel {vessel}')
        if hid in handovers:
            raise ValueError(f'duplicate handover {hid}')
        handovers[hid] = {'id': hid, 'vessel': vessel, 'open': bool(item.get('open', False))}
    if not handovers:
        raise ValueError('line requires a handover ledger')
    shifts, shift_ids, lot_ids, product_ids, transfer_ids = [], set(), set(), set(), set()
    for item in raw.get('shifts', []):
        sid, vessel = identifier(item['id']), identifier(item['vessel'])
        if vessel not in vessels:
            raise ValueError(f'shift {sid}: unknown vessel {vessel}')
        if sid in shift_ids:
            raise ValueError(f'duplicate shift {sid}')
        shift_ids.add(sid)
        events = [_line_event(event, lot_ids, product_ids, transfer_ids)
                  for event in item.get('events', [])]
        shifts.append({'id': sid, 'vessel': vessel,
                       'start': _boundary(item['start']), 'end': _boundary(item['end']),
                       'events': events})
    if not shifts:
        raise ValueError('line requires at least one shift')
    used_vessels = {shift['vessel'] for shift in shifts}
    idle = [vessel for vessel in vessels if vessel not in used_vessels]
    if idle:
        raise ValueError(f'vessel {idle[0]} has no shifts')
    for event in (e for s in shifts for e in s['events'] if e['type'] == 'transfer_in'):
        if event['transfer'] not in transfer_ids:
            raise ValueError(f"transfer_in references unknown transfer {event['transfer']}")
    return {'attestation': attestation, 'vessels': vessels,
            'handovers': handovers, 'shifts': shifts}
