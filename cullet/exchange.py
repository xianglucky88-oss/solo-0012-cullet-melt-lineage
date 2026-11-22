"""Reconcile multi-party exchange tickets against each site's furnace releases.

One delivery may bind several outbound weighbridge tickets to one combined
inbound ticket (many-to-many). Mass that cannot be attributed to an attested
loss stays *unexplained*; it is never booked as a confirmed loss. Verified
claims of the same physical item across both sites cannot exceed the grams the
exchange actually moved.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from fractions import Fraction

from .balance import item_claims, pending_holdings


def _ledger(exchange):
    """Resolved per-item composition of every alias and package ref."""
    ledger = {}
    for item_id, names in exchange.items.items():
        for name in names:
            ledger[name] = Counter({item_id: Fraction(1)})
    for event in exchange.packages:
        total_in = sum(grams for _, grams in event.inputs)
        mixed = Counter()
        for ref, grams in event.inputs:
            share = Fraction(grams, total_in)
            for item, part in ledger[ref].items():
                mixed[item] += part * share
        for ref, grams in event.outputs:
            ledger[ref] = Counter({item: part for item, part in mixed.items() if part})
    return ledger


def _ticket_items(ticket, ledger):
    """Expand ticket lines through package compositions into per-item fractions."""
    totals = Counter()
    for ref, _declared, grams in ticket.lines:
        for item, ratio in ledger[ref].items():
            totals[item] += Fraction(grams) * ratio
    return Counter({item: grams for item, grams in totals.items() if grams})


def _as_int(counter, context):
    out = {}
    for item, grams in counter.items():
        if grams.denominator != 1:
            raise ValueError(f'{context}: fractional grams for {item}; split needs explicit item lines')
        out[item] = grams.numerator
    return out


def reconcile(exchange, site_releases):
    """Return the full reconciliation report.

    site_releases maps site id -> (materials, facts, header, series/revision).
    Structural violations raise; claim/return conflicts are collected so both
    sites keep reading the same frozen state instead of one side failing open.
    """
    ledger = _ledger(exchange)
    conflicts, deliveries_report = [], []
    inbound_by_site = defaultdict(Counter)
    outbound_by_site = defaultdict(Counter)
    outbound_total = Counter()
    pending_unexplained = Counter()
    for delivery_id, delivery in exchange.deliveries.items():
        out, inbound = Counter(), Counter()
        for ticket_id in delivery.outbound:
            ticket = exchange.tickets[ticket_id]
            per = _ticket_items(ticket, ledger)
            for item, grams in per.items():
                out[item] += grams
                outbound_total[item] += grams
                outbound_by_site[ticket.site][item] += grams
        for ticket_id in delivery.inbound:
            ticket = exchange.tickets[ticket_id]
            per = _ticket_items(ticket, ledger)
            for item, grams in per.items():
                inbound[item] += grams
                inbound_by_site[ticket.site][item] += grams
        retained = Counter(dict(delivery.retained))
        attested = Counter({item: grams for item, grams, _ in delivery.losses})
        report = {'delivery': delivery_id, 'outbound_g': _as_int(out, delivery_id),
                  'inbound_g': _as_int(inbound, delivery_id),
                  'retained_g': dict(retained), 'attested_loss_g': dict(attested),
                  'unexplained_g': {}}
        for item, grams in out.items():
            if inbound.get(item, 0) > grams:
                conflicts.append(f'{delivery_id}: inbound {item} {inbound[item]}g exceeds outbound {grams}g')
            if retained[item] > grams - inbound.get(item, 0):
                conflicts.append(f'{delivery_id}: retained {item} exceeds outbound minus inbound')
            if attested[item] > grams - inbound.get(item, 0) - retained[item]:
                conflicts.append(f'{delivery_id}: attested loss {item} without matching physical gap')
            gap = grams - inbound.get(item, 0) - retained[item] - attested[item]
            if gap < 0:
                conflicts.append(f'{delivery_id}: {item} gap allocation overbooks the physical difference')
            elif gap:
                report['unexplained_g'][item] = gap.numerator if gap.denominator == 1 else str(gap)
                pending_unexplained[item] += gap
        for item in set(retained) | set(attested) | set(inbound):
            if item not in out:
                conflicts.append(f'{delivery_id}: {item} appears without an outbound ticket line')
        deliveries_report.append(report)
    # Package totals cannot exceed what inbound tickets actually received.
    package_inputs = Counter()
    for event in exchange.packages:
        if event.action != 'merge':
            continue
        for ref, grams in event.inputs:
            if len(ledger[ref]) == 1:
                item = next(iter(ledger[ref]))
                package_inputs[item] += grams
    for item, grams in package_inputs.items():
        received = sum(inbound_by_site[site].get(item, 0)
                       for site in inbound_by_site)
        if grams > received:
            conflicts.append(f'package merge draws {grams}g of {item} but inbound tickets show {received}g')
    # Per-site verified claims and pending holdings from each site's own release.
    claims, holdings, release_refs = defaultdict(Counter), {}, {}
    for site, payload in site_releases.items():
        materials, _facts, header, ref = payload
        claims[site] = item_claims(materials)
        holdings[site] = pending_holdings(materials)
        release_refs[site] = {'series': ref[0], 'revision': ref[1], 'recipe': header.get('recipe')}
    known_items = set(exchange.items)
    # Sealed raw-material returns restore the sender's balance but never spent grams.
    returns_report = []
    returned = defaultdict(Counter)
    for ret in exchange.returns:
        lines = {}
        for ref, item, grams, _opened in ret.lines:
            lines[item] = lines.get(item, 0) + grams
            returned[ret.site_from][item] += grams
            held = inbound_by_site[ret.site_from].get(item, 0)
            used = claims[ret.site_from].get(item, 0)
            if grams > held - used:
                conflicts.append(
                    f'{ret.return_id}: returns {grams}g of {item} but only {_g(held - used)}g '
                    'remain unconsumed; consumed shares cannot be restored')
        returns_report.append({'return': ret.return_id, 'delivery': ret.delivery,
                               'from': ret.site_from, 'to': ret.site_to, 'grams_g': lines})
    for site, site_claims in claims.items():
        for item, grams in site_claims.items():
            if item not in known_items:
                conflicts.append(f'{site}: verified claim on unknown item {item}')
                continue
            # The receiving site may use what its inbound tickets record; the
            # sending site keeps its outbound grams, which already cover returns.
            allocation = (inbound_by_site[site].get(item, 0)
                          if inbound_by_site.get(site) else outbound_by_site[site].get(item, 0))
            if grams > allocation:
                conflicts.append(
                    f'{site}: claims {_g(grams)}g of {item} but tickets give {allocation}g '
                    '(short receipt cannot be reported as verified use)')
    for item in known_items:
        total = sum(claims[site].get(item, 0) for site in site_releases)
        # Sealed returns restore the sender's available balance without creating
        # new material: the same grams may legitimately be reused after return.
        cap = outbound_total.get(item, 0) + sum(returned[site].get(item, 0) for site in returned)
        if total > cap:
            conflicts.append(
                f'item {item}: sites jointly claim {_g(total)}g but outbound tickets total '
                f'{outbound_total.get(item, 0)}g and sealed returns restore '
                f'{cap - outbound_total.get(item, 0)}g '
                '(same physical grams cannot be verified on both sides)')
    _external_product_checks(exchange, site_releases, conflicts)
    sites_report = {}
    for site, payload in site_releases.items():
        materials, _facts, header, ref = payload
        ticket_allocation = (inbound_by_site[site] if inbound_by_site.get(site)
                             else outbound_by_site[site])
        sites_report[site] = {
            'release': {k: release_refs[site][k] for k in ('series', 'revision')},
            'recipe': release_refs[site]['recipe'],
            'verified_claims_g': {item: _g(grams) for item, grams in sorted(claims[site].items())},
            'pending_holding_g': {feed: grams for feed, grams in sorted(holdings[site].items())},
            'ticket_available_g': {item: _g(grams) for item, grams in
                                   sorted(ticket_allocation.items())},
        }
    status = 'conflicted' if conflicts else ('pending-review' if pending_unexplained or any(
        holdings[site] for site in site_releases) else 'reconciled')
    return {
        'status': status,
        'deliveries': deliveries_report,
        'returns': returns_report,
        'sites': sites_report,
        'pending_unexplained_g': {item: _g(grams) for item, grams in sorted(pending_unexplained.items())},
        'conflicts': sorted(conflicts),
    }


def _g(value):
    return value.numerator if isinstance(value, Fraction) and value.denominator == 1 else str(value)


def _external_product_checks(exchange, site_releases, conflicts):
    """Returned finished goods form cross-site edges; detect real cycles/overdraw."""
    nodes, edges, capacities, draws = set(), defaultdict(set), {}, Counter()
    for site, (materials, _facts, header, _ref) in site_releases.items():
        for key, material in materials.items():
            node = f'{site}:{key}'
            nodes.add(node)
            capacities[node] = material.grams
            for draw in material.inputs:
                edges[node].add(f'{site}:{draw.source}')
            if material.external:
                # The feed node itself is the cross-site edge endpoint; the melt
                # drawing it reaches the external product through this node.
                edges[node].add(material.external)
    # External pointer targets are material nodes owned by another site release.
    for site, (materials, _facts, header, _ref) in site_releases.items():
        for material in materials.values():
            if material.external:
                target = material.external
                if target not in nodes:
                    conflicts.append(f'{site}: external return targets unknown material {target}')
                draws[target] += material.grams
    for node, grams in draws.items():
        if node in capacities and grams > capacities[node]:
            conflicts.append(f'external return overdraws {node}: {grams}g > {capacities[node]}g')
    cycle = _find_cycle(edges, nodes)
    if cycle:
        conflicts.append(
            'cross-site product return reaches an already-consumed ancestry: ' + ' -> '.join(cycle))


def _find_cycle(edges, nodes):
    """Return one cycle path; graphlib's TopologicalSorter misses self loops."""
    state = {}  # 0 = unvisited, 1 = on stack, 2 = done
    stack = []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt, 0) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if state.get(nxt, 0) == 0 and nxt in nodes:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(nodes):
        if state.get(node, 0) == 0:
            found = visit(node)
            if found:
                return found
    return None
