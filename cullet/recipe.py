"""Parse site furnace releases and cross-site exchange (ticket) contracts."""
from __future__ import annotations

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


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty string')
    return value


DRAW_STATUSES = ('verified', 'pending')


@dataclass(frozen=True)
class Draw:
    source: str
    grams: int
    status: str = 'verified'


@dataclass(frozen=True)
class Material:
    row: int
    grams: int
    loss: int = 0
    inputs: tuple = ()
    item: str = None
    unverified: bool = False
    external: str = None
    evidence: str = None


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    site: str
    direction: str
    packing: str
    lines: tuple  # (ref, item, grams)


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    outbound: tuple
    inbound: tuple
    retained: tuple  # (item, grams)
    losses: tuple  # (item, grams, evidence)


@dataclass(frozen=True)
class PackageEvent:
    event_id: str
    action: str
    inputs: tuple  # (ref, grams)
    outputs: tuple


@dataclass(frozen=True)
class Return:
    return_id: str
    delivery: str
    site_from: str
    site_to: str
    lines: tuple  # (ref, item, grams, opened)


@dataclass(frozen=True)
class Exchange:
    items: dict
    alias: dict
    tickets: dict
    deliveries: dict
    packages: tuple
    returns: tuple


def _header(doc):
    site = identifier(doc['site']) if 'site' in doc else None
    recipe = identifier(doc['recipe']) if 'recipe' in doc else None
    exchange = None
    if 'exchange' in doc and doc['exchange'] is not None:
        pointer = doc['exchange']
        if not isinstance(pointer, dict):
            raise ValueError('exchange must point at series/revision')
        exchange = (identifier(pointer['series']), identifier(pointer['revision']))
    return {'site': site, 'recipe': recipe, 'exchange': exchange}


def decode(doc):
    series, revision = identifier(doc['series']), identifier(doc['revision'])
    parent = doc.get('parent')
    if parent is not None:
        parent = identifier(parent)
        if parent == revision:
            raise ValueError('a release cannot parent itself')
    header = _header(doc)
    rows, materials = doc['materials'], {}
    if not isinstance(rows, list) or not rows:
        raise ValueError('expected nonempty materials array')
    for row, item in enumerate(rows, 1):
        key, grams = identifier(item['id']), mass(item['grams'])
        if key in materials:
            raise ValueError(f'duplicate material {key}')
        bound = identifier(item['item']) if 'item' in item and item['item'] is not None else None
        unverified = bool(item.get('unverified', False))
        external = None
        if 'from_external' in item and item['from_external'] is not None:
            ref = item['from_external']
            if not isinstance(ref, dict):
                raise ValueError(f'{key}: from_external needs site/material')
            external_site = identifier(ref['site'])
            if header.get('site') is not None and external_site == header['site']:
                raise ValueError(f'{key}: external return must point at another site; internal ancestry uses draws')
            external = f"{external_site}:{identifier(ref['material'])}"
        evidence = text(item['evidence'], f'{key}.evidence') if 'evidence' in item else None
        if unverified and (bound or external):
            raise ValueError(f'{key}: pending-attribution feed cannot bind an item or external source')
        if external and bound:
            raise ValueError(f'{key}: external product feed is traced via from_external, not item')
        if item['kind'] == 'feed':
            material = Material(row, grams, item=bound, unverified=unverified,
                                external=external, evidence=evidence)
        elif item['kind'] == 'melt':
            loss = mass(item['loss_g'], allow_zero=True)
            inputs = item['inputs']
            if not isinstance(inputs, list) or not inputs:
                raise ValueError(f'{key}: melt requires input draws')
            draws = []
            for draw in inputs:
                status = draw.get('status', 'verified')
                if status not in DRAW_STATUSES:
                    raise ValueError(f'{key}: draw status must be verified or pending')
                draws.append(Draw(identifier(draw['from']), mass(draw['grams']), status))
            if sum(draw.grams for draw in draws) != grams + loss:
                raise ValueError(f'{key}: charge must equal output plus loss')
            material = Material(row, grams, loss, tuple(draws))
        else:
            raise ValueError(f'{key}: expected feed or melt')
        materials[key] = material
    for key, material in materials.items():
        for draw in material.inputs:
            source = materials.get(draw.source)
            if source is None:
                raise ValueError(f'{key}: unknown material {draw.source}')
            if draw.status == 'pending' and not source.unverified:
                raise ValueError(f'{key}: pending draw must come from a pending-attribution feed')
            if draw.status == 'verified' and source.unverified:
                raise ValueError(f'{key}: unverified feed requires a pending draw')
            if source.external is not None and draw.status == 'pending':
                raise ValueError(f'{key}: returned product is identified and cannot be pending')
    return series, revision, parent, header, materials


def _resolve(alias, ref):
    if ref not in alias:
        raise ValueError(f'unknown item alias or package {ref}')
    return alias[ref]


def decode_exchange(doc):
    series, revision = identifier(doc['series']), identifier(doc['revision'])
    parent = doc.get('parent')
    if parent is not None:
        parent = identifier(parent)
        if parent == revision:
            raise ValueError('a release cannot parent itself')
    items, alias = {}, {}
    for entry in doc.get('items', []):
        item_id = identifier(entry['id'])
        if item_id in items:
            raise ValueError(f'duplicate item {item_id}')
        names = [item_id]
        for name in entry.get('aliases', []):
            name = identifier(name)
            if name in alias:
                raise ValueError(f'alias {name} is used more than once')
            alias[name] = item_id
            names.append(name)
        alias[item_id] = item_id
        items[item_id] = names
    tickets = {}
    for entry in doc.get('tickets', []):
        ticket_id = identifier(entry['id'])
        if ticket_id in tickets:
            raise ValueError(f'duplicate ticket {ticket_id}')
        if entry['direction'] not in ('in', 'out'):
            raise ValueError(f'{ticket_id}: direction must be in or out')
        lines = []
        for line in entry['lines']:
            ref = identifier(line['ref'])
            if ref not in alias:
                raise ValueError(f'{ticket_id}: ticket line ref {ref} is not a registered item alias')
            lines.append((ref, _resolve(alias, ref), mass(line['grams'])))
        tickets[ticket_id] = Ticket(ticket_id, identifier(entry['site']), entry['direction'],
                                    entry.get('packing', 'single'), tuple(lines))
    deliveries = {}
    referenced = set()
    for entry in doc.get('deliveries', []):
        delivery_id = identifier(entry['id'])
        if delivery_id in deliveries:
            raise ValueError(f'duplicate delivery {delivery_id}')
        outbound, inbound = tuple(identifier(v) for v in entry['outbound']), \
            tuple(identifier(v) for v in entry['inbound'])
        if not outbound or not inbound:
            raise ValueError(f'{delivery_id}: delivery needs outbound and inbound tickets')
        for ticket_id in outbound + inbound:
            if ticket_id not in tickets:
                raise ValueError(f'{delivery_id}: unknown ticket {ticket_id}')
            if ticket_id in referenced:
                raise ValueError(f'{delivery_id}: ticket {ticket_id} settled in another delivery')
            referenced.add(ticket_id)
        for ticket_id in outbound:
            if tickets[ticket_id].direction != 'out':
                raise ValueError(f'{delivery_id}: {ticket_id} is not an outbound ticket')
        for ticket_id in inbound:
            if tickets[ticket_id].direction != 'in':
                raise ValueError(f'{delivery_id}: {ticket_id} is not an inbound ticket')
        retained = tuple((_resolve(alias, identifier(line['ref'])), mass(line['grams']))
                         for line in entry.get('retained', []))
        losses = []
        for line in entry.get('confirmed_loss', []):
            if not line.get('attested') or not isinstance(line.get('evidence'), str) \
                    or not line['evidence'].strip():
                raise ValueError(f'{delivery_id}: confirmed loss needs joint attestation and evidence')
            losses.append((_resolve(alias, identifier(line['ref'])), mass(line['grams']),
                           line['evidence'].strip()))
        deliveries[delivery_id] = Delivery(delivery_id, outbound, inbound, retained, tuple(losses))
    packages = []
    packages_known = set()
    for entry in doc.get('packages', []):
        event_id = identifier(entry['id'])
        if event_id in packages_known:
            raise ValueError(f'duplicate package event {event_id}')
        if entry['action'] not in ('merge', 'split'):
            raise ValueError(f'{event_id}: package action must be merge or split')
        inputs = tuple((identifier(v['ref']), mass(v['grams'])) for v in entry['inputs'])
        outputs = tuple((identifier(v['ref']), mass(v['grams'])) for v in entry['outputs'])
        if not inputs or not outputs:
            raise ValueError(f'{event_id}: package event needs inputs and outputs')
        for ref, _ in inputs:
            if ref not in alias and ref not in packages_known:
                raise ValueError(f'{event_id}: unknown input ref {ref}')
        for ref, _ in outputs:
            if ref in alias:
                raise ValueError(f'{event_id}: output package {ref} shadows an item alias')
        output_names = {ref for ref, _ in outputs}
        if len(output_names) != len(outputs):
            raise ValueError(f'{event_id}: duplicate output package {ref}')
        for ref in output_names:
            if ref in alias or ref in packages_known:
                raise ValueError(f'{event_id}: output package {ref} already exists')
        packages.append(PackageEvent(event_id, entry['action'], inputs, outputs))
        packages_known.add(event_id)
        for ref, _ in outputs:
            packages_known.add(ref)
    returns = []
    for entry in doc.get('returns', []):
        return_id = identifier(entry['id'])
        delivery_id = identifier(entry['delivery'])
        if delivery_id not in deliveries:
            raise ValueError(f'{return_id}: unknown delivery {delivery_id}')
        lines = []
        for line in entry['lines']:
            ref = identifier(line['ref'])
            if ref not in alias:
                raise ValueError(f'{return_id}: return line {ref} must reference a registered item alias')
            if bool(line.get('opened', False)):
                raise ValueError(f'{return_id}: opened raw material cannot return to stock as sealed lot')
            lines.append((ref, _resolve(alias, ref), mass(line['grams']), False))
        returns.append(Return(return_id, delivery_id, identifier(entry['from']),
                              identifier(entry['to']), tuple(lines)))
    if not deliveries:
        raise ValueError('exchange contract needs at least one delivery')
    _validate_package_conservation(packages)
    return series, revision, parent, Exchange(items, alias, tickets, deliveries,
                                               tuple(packages), tuple(returns))


def _validate_package_conservation(packages):
    for event in packages:
        total_in = sum(grams for _, grams in event.inputs)
        total_out = sum(grams for _, grams in event.outputs)
        if total_in != total_out:
            raise ValueError(f'{event.event_id}: package {event.action} breaks mass conservation')
