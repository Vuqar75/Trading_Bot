"""Consistent SQLite backup of the new runtime only; safe with a running writer."""
import argparse
from contextlib import closing
import os
import sqlite3
import tempfile
from pathlib import Path


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or not source.is_file():
        raise ValueError('Existing source and distinct destination required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.backup-')
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader:
            if not reader.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_meta'").fetchone():
                raise ValueError('Not a runtime database')
            writer = sqlite3.connect(temporary)
            try:
                reader.backup(writer)
                if writer.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity failure')
            finally:
                writer.close()
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    backup(args.source, args.destination)


if __name__ == '__main__':
    main()
