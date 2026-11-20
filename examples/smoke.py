from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cullet import publish, read_release

with TemporaryDirectory() as temp:
    db = Path(temp) / 'cullet.sqlite'
    doc = json.loads(Path(__file__).with_name('batch.json').read_text(encoding='utf-8'))
    assert publish(db, doc)['status'] == 'published'
    assert publish(db, doc)['status'] == 'replay'
    opening = read_release(db, 'amber-trial', 'bench-01')
    assert opening['materials']['first-melt']['remaining_g'] == 30
    assert [r['output_g'] for r in opening['materials']['remelt']['origins']] == ['41/2', '41/2']
    correction = deepcopy(doc)
    correction.update(revision='bench-02', parent='bench-01')
    correction['materials'][2]['inputs'][0]['grams'] = 40
    correction['materials'][2]['inputs'][1]['grams'] = 60
    invalid = deepcopy(correction)
    invalid['materials'][0]['inputs'][0]['grams'] = 90
    invalid['materials'][0]['grams'] = 86
    try:
        publish(db, invalid)
    except ValueError as exc:
        assert 'overdraw' in str(exc)
    else:
        raise AssertionError('overdraw was accepted')
    for extra, expected in [(
        [{'id': 'loop', 'kind': 'melt', 'grams': 1, 'loss_g': 0,
          'inputs': [{'from': 'loop', 'grams': 1}]}], 'cycle'), (
        [{'id': 'orphan', 'kind': 'melt', 'grams': 1, 'loss_g': 0,
          'inputs': [{'from': 'missing', 'grams': 1}]}], 'unknown material')]:
        try:
            publish(db, dict(correction, materials=correction['materials'] + extra))
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f'bad melt accepted: {expected}')
    publish(db, correction)
    out = read_release(db, 'amber-trial', 'bench-02')
    assert [r['output_g'] for r in out['materials']['remelt']['origins']] == ['123/5', '82/5']
    assert out['changed'] == ['amber-cut', 'clear-cut', 'first-melt', 'remelt', 'side-stream']
    assert read_release(db, 'amber-trial', 'bench-01') == opening
    print(json.dumps(out, sort_keys=True))
