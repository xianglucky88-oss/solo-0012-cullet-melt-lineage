"""Freeze site furnace snapshots and cross-site exchange contracts.

Site releases and exchange settlements live in separate tables. A site release
may reference a frozen exchange; settle() loads the current releases of every
participating site and reconciles them against one shared exchange state, so
both sites always read the same verified claims, balances and pending gaps.
"""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

from .balance import attribute
from .exchange import reconcile
from .recipe import decode, decode_exchange, identifier

RELEASE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS release (
 series TEXT NOT NULL, revision TEXT NOT NULL, parent TEXT,
 site TEXT, recipe TEXT, exchange_series TEXT, exchange_revision TEXT,
 sha TEXT NOT NULL, source_json TEXT NOT NULL, facts_json TEXT NOT NULL,
 PRIMARY KEY (series, revision), FOREIGN KEY(series, parent) REFERENCES release(series, revision)
);
'''
EXCHANGE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS exchange (
 series TEXT NOT NULL, revision TEXT NOT NULL, parent TEXT,
 sha TEXT NOT NULL, source_json TEXT NOT NULL,
 PRIMARY KEY (series, revision),
 FOREIGN KEY(series, parent) REFERENCES exchange(series, revision)
);
'''
SETTLEMENT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS settlement (
 exchange_series TEXT NOT NULL, exchange_revision TEXT NOT NULL,
 report_json TEXT NOT NULL,
 PRIMARY KEY (exchange_series, exchange_revision)
);
'''


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute(RELEASE_SCHEMA)
    conn.execute(EXCHANGE_SCHEMA)
    conn.execute(SETTLEMENT_SCHEMA)
    return conn


def publish(path, doc):
    if doc.get('kind') == 'exchange':
        return _publish_exchange(path, doc)
    return _publish_release(path, doc)


def _publish_release(path, doc):
    series, revision, parent, header, materials = decode(doc)
    source = _json(doc)
    digest = sha256(source.encode()).hexdigest()
    facts = attribute(materials)
    with closing(_connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT sha FROM release WHERE series=? AND revision=?', (series, revision)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError('release revision already frozen')
            return {'status': 'replay', 'sha256': digest}
        if conn.execute('SELECT 1 FROM release WHERE series=? LIMIT 1', (series,)).fetchone() and parent is None:
            raise ValueError('a successor must identify a parent release')
        if parent is not None and not conn.execute(
            'SELECT 1 FROM release WHERE series=? AND revision=?', (series, parent)
        ).fetchone():
            raise ValueError('unknown parent release')
        pointer = header['exchange']
        if pointer is not None and not conn.execute(
            'SELECT 1 FROM exchange WHERE series=? AND revision=?', pointer
        ).fetchone():
            raise ValueError('release points at an exchange that is not frozen')
        conn.execute(
            'INSERT INTO release VALUES(?,?,?,?,?,?,?,?,?,?)',
            (series, revision, parent, header['site'], header['recipe'],
             pointer[0] if pointer else None, pointer[1] if pointer else None,
             digest, source, _json(facts)))
    return {'status': 'published', 'sha256': digest}


def _publish_exchange(path, doc):
    series, revision, parent, exchange = decode_exchange(doc)
    source = _json(doc)
    digest = sha256(source.encode()).hexdigest()
    with closing(_connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT sha FROM exchange WHERE series=? AND revision=?', (series, revision)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError('exchange revision already frozen')
            return {'status': 'replay', 'sha256': digest}
        if conn.execute('SELECT 1 FROM exchange WHERE series=? LIMIT 1', (series,)).fetchone() and parent is None:
            raise ValueError('a successor must identify a parent exchange')
        if parent is not None and not conn.execute(
            'SELECT 1 FROM exchange WHERE series=? AND revision=?', (series, parent)
        ).fetchone():
            raise ValueError('unknown parent exchange')
        conn.execute('INSERT INTO exchange VALUES(?,?,?,?,?)', (series, revision, parent, digest, source))
    return {'status': 'published', 'sha256': digest, 'note': 'run settle after the site releases are published'}


def settle(path, exchange_series, exchange_revision):
    with closing(_connect(path)) as conn, conn:
        row = conn.execute('SELECT source_json FROM exchange WHERE series=? AND revision=?',
                           (identifier(exchange_series), identifier(exchange_revision))).fetchone()
        if row is None:
            raise ValueError('unknown exchange')
        _, _, _, exchange = decode_exchange(json.loads(row[0]))
        sites = {ticket.site for ticket in exchange.tickets.values()}
        site_releases = {}
        for site in sorted(sites):
            release_row = conn.execute(
                '''SELECT source_json FROM release
                   WHERE site=? AND exchange_series=? AND exchange_revision=?
                   ORDER BY series DESC, revision DESC LIMIT 1''',
                (site, exchange_series, exchange_revision)).fetchone()
            if release_row is None:
                raise ValueError(f'no site release found for {site}')
            series_r, revision_r, _parent, header, materials = decode(json.loads(release_row[0]))
            facts = attribute(materials)
            site_releases[site] = (materials, facts, header, (series_r, revision_r))
        report = reconcile(exchange, site_releases)
        conn.execute('INSERT OR REPLACE INTO settlement VALUES(?,?,?)',
                     (exchange_series, exchange_revision, _json(report)))
    return report


def read_release(path, series, revision):
    with closing(_connect(path)) as conn:
        row = conn.execute('''SELECT r.sha, r.parent, r.facts_json, p.facts_json
            FROM release r LEFT JOIN release p ON p.series=r.series AND p.revision=r.parent
            WHERE r.series=? AND r.revision=?''', (identifier(series), identifier(revision))).fetchone()
    if row is None:
        raise ValueError('unknown release')
    current, previous = json.loads(row[2]), json.loads(row[3]) if row[3] else {}
    return {'sha256': row[0], 'parent': row[1], 'materials': current,
            'changed': sorted(key for key in current.keys() | previous.keys() if current.get(key) != previous.get(key))}


def read_exchange(path, series, revision):
    with closing(_connect(path)) as conn:
        row = conn.execute('SELECT sha, parent FROM exchange WHERE series=? AND revision=?',
                           (identifier(series), identifier(revision))).fetchone()
        if row is None:
            raise ValueError('unknown exchange')
        settlement = conn.execute(
            'SELECT report_json FROM settlement WHERE exchange_series=? AND exchange_revision=?',
            (series, revision)).fetchone()
    return {'sha256': row[0], 'parent': row[1],
            'settlement': json.loads(settlement[0]) if settlement else None}
