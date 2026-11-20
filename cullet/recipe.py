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
