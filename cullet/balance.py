"""Allocate weighed draws through a DAG using exact proportional attribution."""
from collections import Counter
from fractions import Fraction
from graphlib import CycleError, TopologicalSorter


def attribute(materials):
    drawn = Counter()
    graph = TopologicalSorter()
    for key, material in materials.items():
        graph.add(key, *(source for source, _ in material.inputs))
        for source, amount in material.inputs:
            if source not in materials:
                raise ValueError(f'{key}: unknown material {source}')
            drawn[source] += amount
    for key, amount in drawn.items():
        if amount > materials[key].grams:
            raise ValueError(f'{key}: downstream draws overdraw measured output')
    try:
        order = tuple(graph.static_order())
    except CycleError as error:
        raise ValueError('material ancestry contains a cycle') from error
    composition, facts = {}, {}
    for key in order:
        material = materials[key]
        charge = Counter()
        for source, amount in material.inputs:
            ratio = Fraction(amount, materials[source].grams)
            for lot, grams in composition[source].items():
                charge[lot] += grams * ratio
        if material.inputs:
            yield_ratio = Fraction(material.grams, material.grams + material.loss)
            product = {lot: grams * yield_ratio for lot, grams in charge.items()}
            loss = {lot: charge[lot] - amount for lot, amount in product.items() if material.loss}
        else:
            product, loss = {key: Fraction(material.grams)}, {}
        composition[key] = product
        facts[key] = {
            'source_row': material.row,
            'output_g': material.grams,
            'remaining_g': material.grams - drawn[key],
            'input_draws': [{'from': source, 'grams': amount} for source, amount in material.inputs],
            'origins': [{'feed': lot, 'row': materials[lot].row, 'output_g': str(product[lot]),
                         'loss_g': str(loss.get(lot, 0))} for lot in sorted(product)],
        }
    return facts
