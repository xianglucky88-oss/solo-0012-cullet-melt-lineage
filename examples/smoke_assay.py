"""Smoke tests for component-assay lineage, release verdicts and re-test impact.

The furnace is deliberately dust-heavy: the phase-separation stream carries far
more sulfur than a glass-gram pro-rata share would.  Every expectation is exact;
nothing is rounded.
"""
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cullet import publish, read_release

HERE = Path(__file__).resolve().parent


def load(name):
    return json.loads((HERE / name).read_text(encoding='utf-8'))


def variant(doc, name, parent='day1'):
    clone = deepcopy(doc)
    clone.update(revision=name, parent=parent)
    return clone


def expect_error(db, doc, fragment):
    try:
        publish(db, doc)
    except ValueError as exc:
        assert fragment in str(exc), f'{fragment!r} not in {exc!s}'
    else:
        raise AssertionError(f'accepted invalid document, expected {fragment!r}')


with TemporaryDirectory() as temp:
    db = Path(temp) / 'assay.sqlite'
    doc = load('assay.json')

    assert publish(db, doc)['status'] == 'published'
    assert publish(db, doc)['status'] == 'replay'
    day1 = read_release(db, 'dust-trial', 'day1')
    assay = day1['assay']
    materials = assay['materials']

    # Glass mass is still conserved exactly as before ...
    assert day1['materials']['first-melt']['output_g'] == 150
    assert day1['materials']['first-melt']['remaining_g'] == 30

    # ... while sulfur may be concentrated in the dust rather than the glass.
    first = materials['first-melt']
    assert first['streams']['material']['max_ppm'] == '200'
    assert first['streams']['loss']['min_ppm'] == '1000'
    # 150g glass + 50g dust, dust 1000..1200 ppm: the glass share is only 1/4.
    assert Fraction(first['streams']['loss']['min_g']) == Fraction(1000, 10**6) * 50

    # Both bounds rest on named records, including the weighing that splits them.
    assert first['support']['max']['sample'] == ['s-dust', 's-sand', 's-soda']
    assert first['support']['max']['weighing'] == ['first-melt']
    # Its signoff covers every interpretation, so it is released unconditionally.
    assert first['status'] == 'qualified'
    assert first['qualified_g'] == 150 and first['planned_g'] == 150

    # Two branches fed by the same dust stream share one pending re-test; their
    # upper bound cannot yet stay under the declared 180 ppm limit.
    second, third = materials['second-melt'], materials['third-melt']
    for branch in (second, third):
        assert branch['status'] == 'conditional'
        assert branch['conditions'] == ['rt-first']
        assert branch['planned_g'] == 60 and branch['qualified_g'] == 0
        assert branch['streams']['material']['max_ppm'] == '200'

    # A charge physically weighed but with no assay basis keeps its mass facts
    # and blocks release of what it feeds.
    fourth = materials['fourth-melt']
    assert day1['materials']['fourth-melt']['output_g'] == 30
    assert materials['raw-dust']['status'] == 'unqualified'
    assert materials['raw-dust']['qualified_g'] == 0
    assert materials['raw-dust']['streams']['material']['max_ppm'] == '1000000'
    assert fourth['status'] == 'unqualified'
    assert any(row['material'] == 'raw-dust' for row in fourth['blocked_by'])

    joint = {row['retest']: row for row in assay['joint_use']}
    shared = joint['rt-first']
    assert shared['branches'] == ['second-melt', 'third-melt']
    assert shared['pending'] is True and shared['jointly_usable'] is False
    assert 'pending' in shared['reason']
    for branch in shared['branch_status']:
        # Both the bound and the record that supports it are reported per branch.
        assert branch['max_support']['sample']
        assert branch['max_support']['weighing']

    # --- a document is not a measurement --------------------------------------
    forged = variant(doc, 'bad-forged')
    forged['samples'].append({'id': 's-fake', 'of': 'raw-dust',
                              'release_sheet': 'COA-2026-11'})
    expect_error(db, forged, 'document reference')

    borrowed = variant(doc, 'bad-borrowed')
    borrowed['samples'].append({'id': 's-copy', 'of': 'raw-dust',
                                'same_as': 's-sand'})
    expect_error(db, borrowed, 'cannot borrow')

    label_retest = variant(doc, 'bad-label')
    label_retest['samples'].append({'id': 's-dup', 'of': 'raw-dust', 'label': 'D',
                                    'min_ppm': 0, 'max_ppm': 10})
    label_retest['samples'].append({'id': 's-dup2', 'of': 'sand', 'label': 'D',
                                    'min_ppm': 0, 'max_ppm': 10})
    label_retest['retests'].append({'id': 'rt-label', 'condition': 'x', 'sample': 'D'})
    expect_error(db, label_retest, 'shared label')

    not_retained = variant(doc, 'bad-retained')
    not_retained['samples'][1]['retained'] = True
    not_retained['samples'][1]['retained'] = False
    not_retained['retests'].append({'id': 'rt-sand', 'condition': 'x', 'sample': 's-sand'})
    expect_error(db, not_retained, 'not marked retained')

    # A conditional signoff must name its re-test, and a release signoff cannot
    # be asserted while the risk is still open.
    premature = variant(doc, 'bad-premature')
    premature['signoffs'] = [{'melt': 'second-melt', 'verdict': 'released'}]
    expect_error(db, premature, 'cannot be released')

    # Mass is balanced here, but the charge draws a lot with no completed assay:
    # the physical draw stays in the facts while the release is refused.
    unbalanced = variant(doc, 'bad-mass')
    unbalanced['materials'][4]['inputs'][0]['grams'] = 61
    expect_error(db, unbalanced, 'charge must equal output plus loss')

    # A shorter path upstream does not hide a missing basis: a melt that consumes
    # the unqualified fourth melt cannot be released just because weighings balance.
    transitive = variant(doc, 'bad-transitive')
    transitive['materials'].append(
        {'id': 'fifth-melt', 'kind': 'melt', 'grams': 20, 'loss_g': 0,
         'inputs': [{'from': 'fourth-melt', 'grams': 20}], 'limit_ppm': 100})
    transitive['samples'].append({'id': 's-fifth', 'of': 'fifth-melt',
                                  'min_ppm': 0, 'max_ppm': 50})
    transitive['signoffs'].append({'melt': 'fifth-melt', 'verdict': 'released'})
    expect_error(db, transitive, 'cannot be released')

    # --- frozen evidence cannot be rewritten or dropped -----------------------
    rewritten = variant(doc, 'bad-rewrite-sample')
    rewritten['samples'][0]['max_ppm'] = 700
    rewritten['signoffs'] = [{'melt': 'first-melt', 'verdict': 'hold'}]
    expect_error(db, rewritten, 'cannot be rewritten')

    # A sample that the frozen record relies on cannot simply disappear.
    dropped = variant(doc, 'bad-drop-sample')
    dropped['samples'] = [row for row in dropped['samples'] if row['id'] != 's-sand']
    dropped['signoffs'] = [{'melt': 'first-melt', 'verdict': 'hold'}]
    expect_error(db, dropped, 'was dropped from the frozen assay record')

    switched = variant(doc, 'bad-component')
    switched['component'] = {'id': 'iron'}
    switched['materials'] = [m for m in switched['materials']]
    expect_error(db, switched, 'cannot be replaced')

    no_assay = variant(doc, 'bad-no-assay')
    for key in ('component', 'samples', 'retests', 'signoffs'):
        no_assay.pop(key)
    expect_error(db, no_assay, 'cannot silently drop it')

    # --- re-test arrives: one result frees both branches ---------------------
    retested = variant(doc, 'day2')
    retested['retests'] = [
        {'id': 'rt-first', 'condition': 're-sample settled dust after 24h',
         'sample': 's-dust', 'min_ppm': 1100, 'max_ppm': 1150},
    ]
    retested['signoffs'] = [
        {'melt': 'first-melt', 'verdict': 'released'},
        {'melt': 'second-melt', 'verdict': 'released'},
        {'melt': 'third-melt', 'verdict': 'released'},
    ]
    publish(db, retested)
    day2 = read_release(db, 'dust-trial', 'day2')
    day2_materials = day2['assay']['materials']
    assert day2_materials['first-melt']['status'] == 'qualified'
    assert day2_materials['second-melt']['status'] == 'qualified'
    assert day2_materials['third-melt']['status'] == 'qualified'
    assert Fraction(day2_materials['second-melt']['streams']['material']['max_ppm']) <= 180
    impact = day2['assay_changed']['retest_impact']
    assert impact[0]['jointly_usable'] is True
    assert {row['to'] for row in impact[0]['branches']} == {'qualified'}
    # The unqualified raw-dust charge is still not released downstream.
    assert day2_materials['fourth-melt']['status'] == 'unqualified'
    # Day one remains byte-identical under the basis it was signed on.
    assert read_release(db, 'dust-trial', 'day1') == day1
    assert day1['assay']['materials']['second-melt']['status'] == 'conditional'

    # --- re-signing must consume the result, not rewrite history -------------
    # Dropping the frozen re-test and claiming release from some other range is
    # refused: the completed result the parent was conditional on is the proof,
    # not a retroactive substitution.
    rewrite = variant(doc, 'bad-rewrite', parent='day1')
    rewrite['retests'] = []
    rewrite['samples'].append({'id': 's-second', 'of': 'second-melt',
                               'min_ppm': 100, 'max_ppm': 150})
    rewrite['signoffs'] = [
        {'melt': 'first-melt', 'verdict': 'released'},
        {'melt': 'second-melt', 'verdict': 'released'},
        {'melt': 'third-melt', 'verdict': 'released'},
    ]
    expect_error(db, rewrite, 'was dropped from the frozen record')

    print('component-assay smoke OK')
