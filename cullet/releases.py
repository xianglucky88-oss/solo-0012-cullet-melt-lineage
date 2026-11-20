"""Freeze full material snapshots and make parent-to-child changes inspectable."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

from .balance import attribute
from .recipe import decode, identifier

SCHEMA = '''
CREATE TABLE IF NOT EXISTS release (
 series TEXT NOT NULL, revision TEXT NOT NULL, parent TEXT,
 sha TEXT NOT NULL, source_json TEXT NOT NULL, facts_json TEXT NOT NULL,
 PRIMARY KEY (series, revision), FOREIGN KEY(series, parent) REFERENCES release(series, revision)
);
'''


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def publish(path, doc):
    series, revision, parent, materials = decode(doc)
    source = _json(doc)
    digest = sha256(source.encode()).hexdigest()
    facts = attribute(materials)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT sha FROM release WHERE series=? AND revision=?', (series, revision)).fetchone()
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
        conn.execute('INSERT INTO release VALUES(?,?,?,?,?,?)', (series, revision, parent, digest, source, _json(facts)))
    return {'status': 'published', 'sha256': digest}


def read_release(path, series, revision):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(SCHEMA)
        row = conn.execute('''SELECT r.sha, r.parent, r.facts_json, p.facts_json
            FROM release r LEFT JOIN release p ON p.series=r.series AND p.revision=r.parent
            WHERE r.series=? AND r.revision=?''', (identifier(series), identifier(revision))).fetchone()
    if row is None:
        raise ValueError('unknown release')
    current, previous = json.loads(row[2]), json.loads(row[3]) if row[3] else {}
    return {'sha256': row[0], 'parent': row[1], 'materials': current,
            'changed': sorted(key for key in current.keys() | previous.keys() if current.get(key) != previous.get(key))}
