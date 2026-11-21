"""Smoke tests for the continuous-shift caliber (handover ledger and mixed vessel remainders)."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cullet import publish, read_release, report_release

HERE = Path(__file__).resolve().parent


def load(name):
    return json.loads((HERE / name).read_text(encoding='utf-8'))


def expect_error(db, doc, fragment):
    try:
        publish(db, doc)
    except ValueError as exc:
        assert fragment in str(exc), f'{fragment!r} not in {exc!s}'
    else:
        raise AssertionError(f'accepted invalid document, expected {fragment!r}')


def variant(doc, name):
    clone = deepcopy(doc)
    clone.update(revision=name, parent='day1')
    return clone


with TemporaryDirectory() as temp:
    db = Path(temp) / 'line.sqlite'
    doc = load('line.json')

    # Handover weighings shared by two shifts are not purchased feed: no feed id equals a handover.
    result = publish(db, doc)
    assert result == {'status': 'published', 'sha256': result['sha256'], 'caliber': 'line'}
    assert publish(db, doc)['status'] == 'replay'

    day1 = read_release(db, 'cullet-line', 'day1')
    assert day1['caliber'] == 'line' and day1['parent'] is None
    line = day1['line']

    # The bounded handover H2 (50..60) is pinned to 55 by the single shared mass account;
    # adjacent shifts may not each pick their own convenient endpoint.
    assert line['handovers']['H1']['grams'] == '80'
    assert line['handovers']['H2']['grams'] == '55'
    assert line['handovers']['H2']['status'] == 'confirmed'
    assert line['handovers']['H0']['status'] == 'open'
    assert line['unresolved_handovers'] == [] and line['degraded_shifts'] == []

    s1 = line['shifts']['S1']
    # New feed after a discharge cannot serve earlier product: p1 only draws opening + lotA.
    p1 = line['products']['p1']
    assert p1['grams'] == '40' and p1['attested'] is False
    assert {r['id']: r['grams'] for r in p1['origins']} == {
        'H0': '80/3', 'lotA': '40/3'}
    # The opening residual proves mass but not source: pending origin, never a verified feed.
    assert {r['kind'] for r in p1['origins']} == {'pending', 'feed'}
    assert s1['ending_g'] == '80'

    p2 = line['products']['p2']
    assert {r['id']: r['grams'] for r in p2['origins']} == {
        'H0': '800/33', 'lotA': '400/33', 'lotC': '150/11'}

    # p3 happens before the cleanout, so it still carries the open residual in its origins.
    p3 = line['products']['p3']
    assert p3['grams'] == '35'
    assert any(r['kind'] == 'pending' for r in p3['origins'])
    assert line['products']['p4']['attested'] is True
    assert {r['id'] for r in line['products']['p4']['origins']} == {'lotF'}

    # One transfer, one actual occupancy; the inbound vessel keeps the source composition.
    t1 = line['transfers']['t1']
    assert t1['grams'] == '20' and t1['in_vessel'] == 'V2'
    assert {r['id'] for r in t1['origins']} == {'H0', 'lotA'}
    p5 = line['products']['p5']
    assert {r['id']: r['grams'] for r in p5['origins']} == {
        'H4': '160/13', 'H0': '320/39', 'lotA': '160/39', 'lotE': '200/13'}

    report = report_release(db, 'cullet-line', 'day1')
    assert report['measured_product_g'] == '250'
    assert report['attested_product_g'] == '25'
    assert report['pending_product_g'] == '225'
    v1_remainder = {row['vessel']: row for row in report['ending_remainder']}['V1']
    assert v1_remainder['pending_g'] == '0' and v1_remainder['attested_g'] == '35'
    partial = report_release(db, 'cullet-line', 'day1', ['S1', 'S2'])
    assert partial['measured_product_g'] == '90' and partial['pending_product_g'] == '90'

    # A second-day correction of the middle shift must not graft onto the frozen confirmed ending.
    one_sided = deepcopy(doc)
    one_sided.update(revision='day2-bad', parent='day1')
    one_sided['line']['shifts'][1]['start'] = {'handover': 'H1', 'min_g': 70, 'max_g': 80}
    expect_error(db, one_sided, 'without both adjacent shifts reconfirming')

    disjoint = deepcopy(doc)
    disjoint.update(revision='day2-disjoint', parent='day1')
    disjoint['line']['shifts'][1]['start']['grams'] = 40
    expect_error(db, disjoint, 'incompatible with frozen')

    # A bounded report on only one side is rejected too: the shared value needs both sides.
    single_bounded = deepcopy(doc)
    single_bounded.update(revision='day2-bounded', parent='day1')
    single_bounded['line']['shifts'][1]['start'] = {'handover': 'H1', 'min_g': 78, 'max_g': 82}
    expect_error(db, single_bounded, 'without both adjacent shifts reconfirming')

    # Joint reconfirmation of both sides of H1 (80 -> 70) is a valid full-snapshot correction;
    # S1's measured loss moves with it so the shared mass account stays balanced.
    correction = deepcopy(doc)
    correction.update(revision='day2', parent='day1')
    correction['line']['shifts'][0]['end']['grams'] = 70
    correction['line']['shifts'][1]['start']['grams'] = 70
    correction['line']['shifts'][0]['events'][2]['grams'] = 20
    # The correction propagates through S2's fixed measurements: H2 moves 55 -> 45.
    correction['line']['shifts'][1]['end'] = {'handover': 'H2', 'min_g': 40, 'max_g': 50}
    correction['line']['shifts'][2]['start'] = {'handover': 'H2', 'min_g': 40, 'max_g': 50}
    correction['line']['shifts'][2]['events'][2]['grams'] = 50
    publish(db, correction)
    day2 = read_release(db, 'cullet-line', 'day2')
    impact = day2['impact']
    assert day2['changed'] == ['handover:H1', 'handover:H2', 'product:p2',
                               'product:p3', 'shift:S1', 'shift:S2', 'shift:S3']
    assert [row['handover'] for row in impact['changed_handovers']] == ['H1', 'H2']
    assert 'p2' in impact['source_changed'] and 'p3' in impact['source_changed']
    assert 'p1' not in impact['source_changed']
    # Only the cleanout mass changes; every discharge keeps its measured grams.
    assert impact['mass_changed'] == ['c1']
    assert 'p2' in impact['mass_unchanged'] and 'p5' in impact['mass_unchanged']
    cutoff = impact['cleanout_cutoffs']['V1']
    assert cutoff['cleanout'] == 'c1'
    assert cutoff['unaffected_after'] == ['p4']
    # Old release remains fully reviewable, byte-identical in its facts.
    assert read_release(db, 'cullet-line', 'day1') == day1
    assert read_release(db, 'cullet-line', 'day1')['line']['handovers']['H1']['grams'] == '80'

    # --- structural and account rejections -------------------------------------
    bounded = load('line_bounded.json')
    formal_reject = deepcopy(bounded)
    formal_reject['attestation'] = 'formal'
    expect_error(db, formal_reject, 'formal attestation requires every handover')

    duplicate = variant(doc, 'bad-duplicate')
    duplicate['line']['handovers'].append({'id': 'H1', 'vessel': 'V1'})
    expect_error(db, duplicate, 'duplicate handover')

    occupied = variant(doc, 'bad-occupied')
    # A second outbound draw claims the same transfer id from another vessel:
    # one transfer may have only one actual occupying vessel.
    occupied['line']['shifts'][3]['events'].insert(
        0, {'type': 'transfer', 'id': 't1b', 'grams': 20})
    occupied['line']['shifts'][3]['events'].insert(
        1, {'type': 'transfer_in', 'transfer': 't1b'})
    expect_error(db, occupied, 'source and occupying vessel must differ')

    duplicate_id = variant(doc, 'bad-duplicate-id')
    duplicate_id['line']['shifts'][3]['events'].insert(
        0, {'type': 'transfer', 'id': 't1', 'grams': 20})
    expect_error(db, duplicate_id, 'duplicate transfer')

    repeated = variant(doc, 'bad-repeated')
    # A handover belongs to one vessel; another vessel cannot report the same weighing.
    repeated['line']['shifts'][3]['start'] = {'handover': 'H1', 'grams': 80}
    expect_error(db, repeated, 'handover ledger structure changed')

    cross_vessel = deepcopy(repeated)
    cross_vessel.update(series='cullet-cross', revision='day1', parent=None)
    expect_error(db, cross_vessel, 'reports must stay on vessel')

    open_reports = variant(doc, 'bad-open')
    open_reports['line']['shifts'][1]['start'] = {'handover': 'H0', 'grams': 100}
    open_reports.update(series='cullet-open', parent=None)
    expect_error(db, open_reports, 'no adjacent shift to confirm')

    mismatch = variant(doc, 'bad-account')
    mismatch['line']['shifts'][2]['events'][0]['grams'] = 90
    expect_error(db, mismatch, 'mass account cannot satisfy')

    overdraw = variant(doc, 'bad-overdraw')
    # The bounded end keeps the interval book feasible, but the event drains more than the
    # vessel holds at that point in the ordered sequence.
    overdraw['line']['shifts'][1]['end'] = {'handover': 'H2', 'min_g': 0, 'max_g': 60}
    overdraw['line']['shifts'][2]['start'] = {'handover': 'H2', 'min_g': 0, 'max_g': 60}
    overdraw['line']['shifts'][2]['end']['grams'] = 35
    overdraw['line']['shifts'][1]['events'][1]['grams'] = 120
    expect_error(db, overdraw, 'exceeds vessel mass')

    same_vessel = variant(doc, 'bad-same-vessel')
    same_vessel['line']['shifts'][0]['events'][3] = {
        'type': 'transfer', 'id': 't1', 'grams': 20}
    same_vessel['line']['shifts'][0]['events'].append(
        {'type': 'transfer_in', 'transfer': 't1'})
    same_vessel['line']['shifts'][3]['events'] = [
        e for e in same_vessel['line']['shifts'][3]['events']
        if e.get('transfer') != 't1']
    expect_error(db, same_vessel, 'source and occupying vessel must differ')

    # --- provisional caliber: certify the mass, leave unresolved sources open ---
    bounded_doc = load('line_bounded.json')
    publish(db, bounded_doc)
    provisional = read_release(db, 'cullet-bounded', 'day1')
    facts = provisional['line']
    assert facts['attestation'] == 'provisional'
    assert facts['unresolved_handovers'] == ['B0', 'B1', 'B2', 'B3']
    assert set(facts['degraded_shifts']) == {'Q1', 'Q2', 'Q3'}
    for pid in ('q1', 'q2', 'q3'):
        assert facts['products'][pid]['attested'] is False
        assert facts['products'][pid]['origins'] == []
    preport = report_release(db, 'cullet-bounded', 'day1')
    assert preport['attested_product_g'] == '0'
    assert preport['pending_product_g'] == '65'
    remainder = preport['ending_remainder'][0]
    assert remainder['grams'] is None and (remainder['lo'], remainder['hi']) == (25, 35)

    # Batch and line calibers cannot share a series.
    batch = load('batch.json')
    batch.update(series='cullet-line', revision='mix', parent='day1')
    expect_error(db, batch, 'a series keeps one caliber')

print('continuous-shift smoke OK')
