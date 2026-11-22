"""Allocate weighed draws through a DAG using exact proportional attribution.

Verified material keeps its physical-item identity; a pending-attribution feed
stays an unlabelled pending share through every downstream melt until the
exchange side resolves its identity. Shares are exact fractions, never rounded.
"""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
from graphlib import CycleError, TopologicalSorter


def build_order(materials):
    drawn = Counter()
    graph = TopologicalSorter()
    for key, material in materials.items():
        graph.add(key, *(draw.source for draw in material.inputs))
        for draw in material.inputs:
            drawn[draw.source] += draw.grams
    for key, amount in drawn.items():
        if amount > materials[key].grams:
            raise ValueError(f'{key}: downstream draws overdraw measured output')
    try:
        order = tuple(graph.static_order())
    except CycleError as error:
        raise ValueError('material ancestry contains a cycle') from error
    return order, drawn


def compositions(materials, order):
    """Verified item fractions and unlabelled pending fractions per node."""
    verified, pending = {}, {}
    for key in order:
        material = materials[key]
        if not material.inputs:
            if material.unverified:
                pending[key] = {key: Fraction(material.grams)}
                verified[key] = {}
            elif material.external is not None:
                # Returned finished goods keep their cross-site edge; they are
                # never a fresh claim on an outbound raw-material item.
                verified[key], pending[key] = {}, {}
            else:
                identity = material.item or key
                verified[key] = {identity: Fraction(material.grams)}
                pending[key] = {}
            continue
        charge, charge_pending = Counter(), Counter()
        for draw in material.inputs:
            source = materials[draw.source]
            ratio = Fraction(draw.grams, source.grams)
            for item, grams in verified[draw.source].items():
                charge[item] += grams * ratio
            for tag, grams in pending[draw.source].items():
                charge_pending[tag] += grams * ratio
        yield_ratio = Fraction(material.grams, material.grams + material.loss)
        verified[key] = {item: grams * yield_ratio for item, grams in charge.items()}
        pending[key] = {tag: grams * yield_ratio for tag, grams in charge_pending.items()}
    return verified, pending


def attribute(materials):
    order, drawn = build_order(materials)
    verified, pending = compositions(materials, order)
    roots = verified_roots(materials)
    facts = {}
    for key in order:
        material = materials[key]
        total_in = material.grams + material.loss
        if material.inputs:
            yield_ratio = Fraction(material.grams, total_in)
            loss_ratio = 1 - yield_ratio
            origins = []
            for item, grams in sorted(verified[key].items()):
                feed = _feed_of(materials, item, roots)
                origins.append({
                    'item': item, 'feed': feed, 'row': materials[feed].row,
                    'output_g': str(grams),
                    'loss_g': str(grams * loss_ratio / yield_ratio) if material.loss else '0'})
            pending_origins = [
                {'pending_feed': tag, 'row': materials[tag].row,
                 'output_g': str(grams),
                 'loss_g': str(grams * loss_ratio / yield_ratio) if material.loss else '0'}
                for tag, grams in sorted(pending[key].items())
            ]
        else:
            origins, pending_origins = [], []
        facts[key] = {
            'source_row': material.row,
            'output_g': material.grams,
            'remaining_g': material.grams - drawn[key],
            'item': material.item,
            'unverified': material.unverified,
            'external': material.external,
            'evidence': material.evidence,
            'input_draws': [{'from': draw.source, 'grams': draw.grams, 'status': draw.status}
                            for draw in material.inputs],
            'origins': origins,
            'pending_origins': pending_origins,
        }
    return facts


def verified_roots(materials):
    return {key: (material.item or key)
            for key, material in materials.items()
            if not material.inputs and not material.unverified and material.external is None}


def _feed_of(materials, item, roots):
    for key, identity in roots.items():
        if identity == item:
            return key
    raise KeyError(f'no verified feed root for item {item}')


def item_claims(materials):
    """Verified grams of each physical item charged in this release.

    Pending draws and external product feeds never count: pending grams have no
    proven identity yet, and returned products were already claimed upstream.
    """
    order, _ = build_order(materials)
    verified, _ = compositions(materials, order)
    claimed = Counter()
    for key, material in materials.items():
        for draw in material.inputs:
            source = materials[draw.source]
            if draw.status == 'pending' or source.external is not None:
                continue
            ratio = Fraction(draw.grams, source.grams)
            for item, grams in verified[draw.source].items():
                claimed[item] += grams * ratio
    return claimed


def pending_holdings(materials):
    """Unspent grams still held on each pending-attribution feed."""
    spent = Counter()
    for melt in materials.values():
        for draw in melt.inputs:
            if draw.status == 'pending':
                spent[draw.source] += draw.grams
    holdings = {}
    for key, material in materials.items():
        if material.unverified:
            left = material.grams - spent[key]
            if left > 0:
                holdings[key] = left
    return holdings
