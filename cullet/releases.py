"""Freeze full material snapshots and make parent-to-child changes inspectable."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

from .balance import attribute
from .line import attribute_line, compare_line_releases, compatible_revision, line_report
from .recipe import decode, decode_line, identifier

SCHEMA = '''
CREATE TABLE IF NOT EXISTS release (
 series TEXT NOT NULL, revision TEXT NOT NULL, parent TEXT,
 sha TEXT NOT NULL, source_json TEXT NOT NULL, facts_json TEXT NOT NULL,
 caliber TEXT NOT NULL DEFAULT 'batch', line_json TEXT,
 PRIMARY KEY (series, revision), FOREIGN KEY(series, parent) REFERENCES release(series, revision)
);
'''


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _ensure_schema(conn):
    conn.execute(SCHEMA)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(release)')}
    if 'caliber' not in columns:
        conn.execute("ALTER TABLE release ADD COLUMN caliber TEXT NOT NULL DEFAULT 'batch'")
    if 'line_json' not in columns:
        conn.execute('ALTER TABLE release ADD COLUMN line_json TEXT')


def publish(path, doc):
    is_line = 'line' in doc
    if is_line:
        series, revision, parent = (identifier(doc['series']), identifier(doc['revision']),
                                    doc.get('parent'))
        if parent is not None:
            parent = identifier(parent)
            if parent == revision:
                raise ValueError('a release cannot parent itself')
        line = decode_line(doc)
        caliber = 'line'
    else:
        series, revision, parent, materials = decode(doc)
        caliber = 'batch'
    source = _json(doc)
    digest = sha256(source.encode()).hexdigest()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        _ensure_schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute(
            'SELECT sha, caliber FROM release WHERE series=? AND revision=?', (series, revision)
        ).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError('release revision already frozen')
            return {'status': 'replay', 'sha256': digest, 'caliber': old[1]}
        existing = conn.execute(
            'SELECT caliber FROM release WHERE series=? LIMIT 1', (series,)).fetchone()
        if existing and parent is None:
            raise ValueError('a successor must identify a parent release')
        if existing and existing[0] != caliber:
            raise ValueError('a series keeps one caliber; start a new series for the other one')
        if parent is not None:
            parent_row = conn.execute(
                'SELECT line_json FROM release WHERE series=? AND revision=?', (series, parent)
            ).fetchone()
            if not parent_row:
                raise ValueError('unknown parent release')
            if is_line and parent_row[0] is not None:
                # Compare the raw new reports with the frozen ledger before reconciliation,
                # so a one-sided correction of a confirmed handover reports the right rejection.
                conflicts = compatible_revision(json.loads(parent_row[0]), line)
                if conflicts:
                    raise ValueError('incompatible revision: ' + '; '.join(conflicts))
        if is_line:
            line_facts = attribute_line(line)
            facts = {}
        else:
            facts, line_facts = attribute(materials), None
        conn.execute(
            'INSERT INTO release VALUES(?,?,?,?,?,?,?,?)',
            (series, revision, parent, digest, source, _json(facts), caliber,
             _json(line_facts) if line_facts is not None else None))
    return {'status': 'published', 'sha256': digest, 'caliber': caliber}


def read_release(path, series, revision):
    with closing(sqlite3.connect(path)) as conn:
        _ensure_schema(conn)
        row = conn.execute('''SELECT r.sha, r.parent, r.facts_json, r.line_json,
            p.facts_json, p.line_json
            FROM release r LEFT JOIN release p ON p.series=r.series AND p.revision=r.parent
            WHERE r.series=? AND r.revision=?''',
            (identifier(series), identifier(revision))).fetchone()
    if row is None:
        raise ValueError('unknown release')
    sha, parent, facts_raw, line_raw, parent_facts_raw, parent_line_raw = row
    if line_raw is not None:
        current, previous = json.loads(line_raw), json.loads(parent_line_raw) if parent_line_raw else {}

        def line_sections(facts):
            return {'shift': facts.get('shifts', {}), 'handover': facts.get('handovers', {}),
                    'product': facts.get('products', {}), 'transfer': facts.get('transfers', {})}

        changed = []
        if previous:
            old_sections, new_sections = line_sections(previous), line_sections(current)
            for section in new_sections:
                keys = old_sections[section].keys() | new_sections[section].keys()
                changed.extend(f'{section}:{key}' for key in sorted(keys)
                               if old_sections[section].get(key) != new_sections[section].get(key))
        result = {'sha256': sha, 'parent': parent, 'caliber': 'line',
                  'line': current, 'changed': sorted(changed)}
        if previous:
            result['impact'] = compare_line_releases(previous, current)
        return result
    current, previous = json.loads(facts_raw), json.loads(parent_facts_raw) if parent_facts_raw else {}
    return {'sha256': sha, 'parent': parent, 'caliber': 'batch', 'materials': current,
            'changed': sorted(key for key in current.keys() | previous.keys()
                              if current.get(key) != previous.get(key))}


def report_release(path, series, revision, shift_ids=None):
    with closing(sqlite3.connect(path)) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            'SELECT line_json FROM release WHERE series=? AND revision=?',
            (identifier(series), identifier(revision))).fetchone()
    if row is None:
        raise ValueError('unknown release')
    if row[0] is None:
        raise ValueError('period reports only apply to continuous-line releases')
    return line_report(json.loads(row[0]), shift_ids)
