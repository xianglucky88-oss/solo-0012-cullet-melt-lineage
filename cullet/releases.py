"""Freeze full material snapshots and make parent-to-child changes inspectable.

Mass-only releases are stored exactly as before.  A release that declares an
assay component also freezes its assay facts in a separate column, so the mass
facts read back byte-identically and earlier published revisions stay reviewable
under the basis that applied when they were signed.
"""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

from .balance import attribute
from .component import analyse, compare_assay
from .recipe import decode, decode_component, identifier

SCHEMA = '''
CREATE TABLE IF NOT EXISTS release (
 series TEXT NOT NULL, revision TEXT NOT NULL, parent TEXT,
 sha TEXT NOT NULL, source_json TEXT NOT NULL, facts_json TEXT NOT NULL,
 assay_json TEXT,
 PRIMARY KEY (series, revision), FOREIGN KEY(series, parent) REFERENCES release(series, revision)
);
'''


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _ensure_schema(conn):
    conn.execute(SCHEMA)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(release)')}
    if 'assay_json' not in columns:
        conn.execute('ALTER TABLE release ADD COLUMN assay_json TEXT')


def publish(path, doc):
    series, revision, parent, materials = decode(doc)
    component = decode_component(doc, materials)
    source = _json(doc)
    digest = sha256(source.encode()).hexdigest()
    facts = attribute(materials)
    assay = analyse(materials, component)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        _ensure_schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT sha FROM release WHERE series=? AND revision=?',
                           (series, revision)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError('release revision already frozen')
            return {'status': 'replay', 'sha256': digest}
        existing = conn.execute('SELECT 1 FROM release WHERE series=? LIMIT 1', (series,)).fetchone()
        if existing and parent is None:
            raise ValueError('a successor must identify a parent release')
        if parent is not None and not conn.execute(
            'SELECT 1 FROM release WHERE series=? AND revision=?', (series, parent)
        ).fetchone():
            raise ValueError('unknown parent release')
        if assay is not None and parent is not None:
            frozen = conn.execute('SELECT assay_json FROM release WHERE series=? AND revision=?',
                                  (series, parent)).fetchone()
            if frozen is not None:
                if frozen[0] is None:
                    raise ValueError(f'{revision}: the parent release has no assay record; '
                                     'keep the assay section out or publish a new series')
                _check_history(json.loads(frozen[0]), assay, revision)
        elif assay is None and parent is not None:
            frozen = conn.execute('SELECT assay_json FROM release WHERE series=? AND revision=?',
                                  (series, parent)).fetchone()
            if frozen is not None and frozen[0] is not None:
                raise ValueError(f'{revision}: the parent release declares an assay component; '
                                 'a successor cannot silently drop it')
        conn.execute('INSERT INTO release VALUES(?,?,?,?,?,?,?)',
                     (series, revision, parent, digest, source, _json(facts),
                      _json(assay) if assay is not None else None))
    return {'status': 'published', 'sha256': digest}


def _check_history(parent, child, revision):
    """A later revision may add evidence but never rewrite the frozen basis.

    A re-test result is fresh evidence: a conditional melt may be re-signed as
    released only when the completed result is present for the *same* condition.
    A content correction may invalidate an earlier release; that is allowed and
    reported as a status change.  What is forbidden is dropping evidence or a
    signoff that the earlier revision relied on, or quietly retargeting a sample.
    """
    old_evidence = parent.get('evidence', {})
    new_evidence = child.get('evidence', {})
    for section in ('samples', 'composites'):
        for key, old in old_evidence.get(section, {}).items():
            new = new_evidence.get(section, {}).get(key)
            if new is None:
                raise ValueError(f'{revision}: {section[:-1]} {key} was dropped from the '
                                 'frozen assay record')
            if section == 'samples' and old['pending'] and not new['pending']:
                # A pending specimen receiving its first result is fresh evidence;
                # only an already measured range is immutable.
                continue
            if new != old:
                raise ValueError(f'{revision}: measured {section[:-1]} {key} cannot be '
                                 'rewritten; append a re-test or a new sample instead')
    # Weighing corrections are allowed (the mass contract is a full snapshot), but
    # they are reported through the mass ``changed`` set rather than hidden here.
    old_component = parent.get('component')
    new_component = child.get('component')
    if old_component != new_component:
        raise ValueError(f'{revision}: component {old_component!r} cannot be replaced by '
                         f'{new_component!r} in the same series')

    for rid, old in old_evidence.get('retests', {}).items():
        new = new_evidence.get('retests', {}).get(rid)
        if new is None:
            raise ValueError(f'{revision}: re-test {rid} was dropped from the frozen record')
        if old['condition'] != new['condition']:
            raise ValueError(f'{revision}: re-test {rid} changed its condition; the frozen '
                             'condition still governs and a new re-test needs a new id')
        if new['pending'] and not old['pending']:
            raise ValueError(f'{revision}: completed re-test {rid} cannot revert to pending')
        if not new['pending'] and not old['pending'] and new != old:
            raise ValueError(f'{revision}: completed re-test {rid} cannot be rewritten')

    for key, old in parent.get('signoffs', {}).items():
        new = child.get('signoffs', {}).get(key)
        if new is None:
            raise ValueError(f'{revision}: signoff for {key} was dropped from the frozen record')
        if old['verdict'] == 'conditional' and new['verdict'] == 'released':
            results = new_evidence.get('retests', {})
            for rid in old['retests']:
                if results.get(rid, {}).get('pending', True):
                    raise ValueError(
                        f'{revision}: {key} was conditional on {rid}; a release signoff '
                        'needs that re-test completed, not a retrospective rewrite')


def read_release(path, series, revision):
    with closing(sqlite3.connect(path)) as conn:
        _ensure_schema(conn)
        row = conn.execute('''SELECT r.sha, r.parent, r.facts_json, r.assay_json,
            p.facts_json, p.assay_json
            FROM release r LEFT JOIN release p ON p.series=r.series AND p.revision=r.parent
            WHERE r.series=? AND r.revision=?''', (identifier(series), identifier(revision))).fetchone()
    if row is None:
        raise ValueError('unknown release')
    sha, parent, facts_raw, assay_raw, parent_facts_raw, parent_assay_raw = row
    current = json.loads(facts_raw)
    previous = json.loads(parent_facts_raw) if parent_facts_raw else {}
    result = {'sha256': sha, 'parent': parent, 'materials': current,
              'changed': sorted(key for key in current.keys() | previous.keys()
                                if current.get(key) != previous.get(key))}
    if assay_raw is not None:
        assay = json.loads(assay_raw)
        previous_assay = json.loads(parent_assay_raw) if parent_assay_raw else None
        result['assay'] = assay
        result['assay_changed'] = compare_assay(previous_assay, assay)
    return result
