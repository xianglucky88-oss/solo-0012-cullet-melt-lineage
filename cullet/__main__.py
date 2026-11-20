import argparse
import json
from pathlib import Path
import sqlite3

from .releases import publish, read_release


def main():
    parser = argparse.ArgumentParser(description='Publish traced melt material balances')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('publish').add_argument('source', type=Path)
    read = commands.add_parser('read')
    read.add_argument('series')
    read.add_argument('revision')
    args = parser.parse_args()
    try:
        if args.command == 'publish':
            result = publish(args.db, json.loads(args.source.read_text(encoding='utf-8')))
        else:
            result = read_release(args.db, args.series, args.revision)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
