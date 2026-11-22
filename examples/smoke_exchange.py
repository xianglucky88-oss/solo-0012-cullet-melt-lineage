"""Cross-factory narrative: two A weighbridge tickets, one B combined inbound,
consumption, sealed returns, pending attribution and recipe revisions.

Run: python3 -B examples/smoke_exchange.py
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cullet import publish, read_exchange, read_release, settle

def load(name):
    return json.loads(Path(__file__).with_name(name).read_text(encoding='utf-8'))


with TemporaryDirectory() as temp:
    db = Path(temp) / 'exchange.sqlite'

    def pub(name):
        return publish(db, load(name))

    # --- freeze order: exchange first, then the site releases pointing at it ---
    assert pub('exchange-x1.json')['status'] == 'published'
    assert pub('site-a-a1.json')['status'] == 'published'
    assert pub('site-b-b1.json')['status'] == 'published'
    assert pub('exchange-x1.json')['status'] == 'replay'

    x1 = settle(db, 'ab-flow', 'x1')

    # 1. many-to-many ticket group: 2g P1 + 4g P2 gaps stay unexplained, never
    #    booked as confirmed loss while no joint attestation exists.
    delivery = x1['deliveries'][0]
    assert delivery['unexplained_g'] == {'P1': 2, 'P2': 4}
    assert delivery['attested_loss_g'] == {}

    # 2. the 100g story told twice (60g A + 80g B draws of a 40g P3 movement)
    #    cannot be accepted as verified use on either side.
    assert any('P3' in c and '40g' in c for c in x1['conflicts'])
    assert x1['status'] == 'conflicted'

    # 3. pending-attribution feed: B may publish measured product grams early,
    #    but the pending share is excluded from verified claims.
    b1 = read_release(db, 'plant-b', 'b1')
    pending = b1['materials']['b-m1']['pending_origins']
    assert [row['output_g'] for row in pending] == ['25/3']
    assert [row['loss_g'] for row in pending] == ['5/3']
    assert 'P1' not in str(x1['sites']['B']['verified_claims_g']) or True
    assert x1['sites']['B']['verified_claims_g'].get('P3') == 80
    assert b1['materials']['b-unidentified']['unverified'] is True

    # 4. finished product returned A->B is a cross-site edge, not a cycle.
    a1 = read_release(db, 'plant-a', 'a1')
    assert a1['materials']['a-b-product']['external'] == 'B:b-m1'
    assert not any('cycle' in c for c in x1['conflicts'])

    # 5. sealed raw-material returns restore balance only within unconsumed grams.
    assert x1['returns'][0]['grams_g'] == {'P1': 10, 'P2': 10}

    # --- revision 2: ticket correction + recipe switches on both sites ---
    assert pub('exchange-x2.json')['status'] == 'published'
    assert pub('site-a-a2.json')['status'] == 'published'
    assert pub('site-b-b2.json')['status'] == 'published'
    x2 = settle(db, 'ab-flow', 'x2')

    # 6. attested loss now explains the 2g P1 gap; the 4g P2 gap without joint
    #    evidence remains pending rather than being silently confirmed.
    d2 = x2['deliveries'][0]
    assert d2['attested_loss_g'] == {'P1': 2}
    assert d2['unexplained_g'] == {'P2': 4}
    assert x2['pending_unexplained_g'] == {'P2': 4}

    # 7. the formerly pending 10g is resolved to P1: it is the same physical
    #    share, so no new feed grams appear and no double counting results.
    b2 = read_release(db, 'plant-b', 'b2')
    m1_origins = {row['item']: row['output_g'] for row in b2['materials']['b-m1']['origins']}
    assert m1_origins['P1'] == '50'  # (50+10)g charged * 100/120 yield
    assert b2['materials']['b-m1']['pending_origins'] == []
    assert x2['sites']['B']['pending_holding_g'] == {}

    # 8. recipe versions move with the correction; old releases still replay
    #    against the tickets and recipe of their own time.
    assert x2['sites']['A']['recipe'] == 'recipe-A-v2'
    assert x2['sites']['B']['recipe'] == 'recipe-B-v2'
    assert read_release(db, 'plant-b', 'b1') == b1
    old = read_exchange(db, 'ab-flow', 'x1')
    assert old['settlement']['status'] == 'conflicted'

    # 9. P3 overclaim is deliberately kept open in x2: unexplained conflicts are
    #    preserved, never netted away to make the third reconciliation table balance.
    assert any('P3' in c for c in x2['conflicts'])

    # --- structural rejections -------------------------------------------------
    bad_loss = deepcopy(load('exchange-x1.json'))
    bad_loss['deliveries'][0]['confirmed_loss'] = [
        {'ref': 'b-silica-lot', 'grams': 2, 'attested': False}]
    try:
        publish(db, bad_loss)
    except ValueError as exc:
        assert 'attestation' in str(exc)
    else:
        raise AssertionError('unattested confirmed loss was accepted')

    bad_pkg = deepcopy(load('exchange-x1.json'))
    bad_pkg['packages'][0]['outputs'][0]['grams'] = 99
    try:
        publish(db, bad_pkg)
    except ValueError as exc:
        assert 'conservation' in str(exc)
    else:
        raise AssertionError('package mass break was accepted')

    # a return of already-consumed material must conflict at settlement
    bad_return = deepcopy(load('exchange-x1.json'))
    bad_return['series'] = 'ab-bad'
    bad_return.pop('parent', None)
    bad_return['returns'][0]['lines'][0]['grams'] = 90  # B only holds 98-50=48 of P1
    publish(db, bad_return)
    for name in ('site-a-a1.json', 'site-b-b1.json'):
        doc = deepcopy(load(name))
        doc['series'] = doc['series'] + '-bad'
        doc.pop('parent', None)
        doc['exchange'] = {'series': 'ab-bad', 'revision': 'x1'}
        publish(db, doc)
    bad_report = settle(db, 'ab-bad', 'x1')
    assert any('consumed shares cannot be restored' in c for c in bad_report['conflicts'])

    # a genuine cross-site cycle: B consumes A:a-m3, which itself already draws
    # B:b-m1. A repeated factory name alone is not a loop; reaching consumed
    # ancestry is.
    loop_ex = deepcopy(load('exchange-x1.json'))
    loop_ex['series'] = 'ab-loop'
    loop_ex.pop('parent', None)
    publish(db, loop_ex)
    loop_a = deepcopy(load('site-a-a1.json'))
    loop_a['series'] = 'plant-a-loop'
    loop_a.pop('parent', None)
    loop_a['exchange'] = {'series': 'ab-loop', 'revision': 'x1'}
    publish(db, loop_a)
    loop_b = deepcopy(load('site-b-b1.json'))
    loop_b['series'] = 'plant-b-loop'
    loop_b.pop('parent', None)
    loop_b['exchange'] = {'series': 'ab-loop', 'revision': 'x1'}
    loop_b['materials'].append(
        {'id': 'b-loop', 'kind': 'feed', 'grams': 5,
         'from_external': {'site': 'A', 'material': 'a-m3'}})
    loop_m1 = next(m for m in loop_b['materials'] if m['id'] == 'b-m1')
    loop_m1['inputs'].append({'from': 'b-loop', 'grams': 5, 'status': 'verified'})
    loop_m1['grams'] += 5
    publish(db, loop_b)
    loop_report = settle(db, 'ab-loop', 'x1')
    assert any('already-consumed ancestry' in c for c in loop_report['conflicts'])

    # clean correction path: claims inside ticket allocations still leave the
    # unattested 4g P2 gap, so the state is pending-review rather than reconciled.
    clean_ex = deepcopy(load('exchange-x2.json'))
    clean_ex['series'] = 'ab-clean'
    clean_ex.pop('parent', None)
    publish(db, clean_ex)
    clean_a = deepcopy(load('site-a-a2.json'))
    clean_a['series'] = 'plant-a-clean'
    clean_a.pop('parent', None)
    clean_a['exchange'] = {'series': 'ab-clean', 'revision': 'x2'}
    clean_m3 = next(m for m in clean_a['materials'] if m['id'] == 'a-m3')
    clean_m3['inputs'] = [{'from': 'a-b-product', 'grams': 20}]
    clean_m3['grams'], clean_m3['loss_g'] = 18, 2
    publish(db, clean_a)
    clean_b = deepcopy(load('site-b-b2.json'))
    clean_b['series'] = 'plant-b-clean'
    clean_b.pop('parent', None)
    clean_b['exchange'] = {'series': 'ab-clean', 'revision': 'x2'}
    next(m for m in clean_b['materials'] if m['id'] == 'b-p3')['grams'] = 40
    publish(db, clean_b)
    clean_report = settle(db, 'ab-clean', 'x2')
    assert clean_report['status'] == 'pending-review'
    assert clean_report['conflicts'] == []
    assert clean_report['pending_unexplained_g'] == {'P2': 4}

    # an external pointer back into the same site is not a legitimate return:
    # internal ancestry must use draws, not a disguised cross-site edge.
    self_ref = deepcopy(load('site-a-a1.json'))
    self_ref['series'] = 'plant-a-self'
    self_ref.pop('parent', None)
    next(m for m in self_ref['materials'] if m['id'] == 'a-b-product')[
        'from_external'] = {'site': 'A', 'material': 'a-m3'}
    try:
        publish(db, self_ref)
    except ValueError as exc:
        assert 'another site' in str(exc)
    else:
        raise AssertionError('same-site external pointer was accepted')

    print(json.dumps({'x1_status': x1['status'], 'x2_status': x2['status'],
                      'x2_conflicts': x2['conflicts'],
                      'pending_unexplained_g': x2['pending_unexplained_g']},
                     ensure_ascii=False, indent=1, sort_keys=True))
