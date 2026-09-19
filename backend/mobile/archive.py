"""Offline complete audit archive; no pruning, lossy retention or public mutation API."""
import argparse
from pathlib import Path
import os
import shutil
import tempfile
import sqlite3
from contextlib import closing
from copy import copy

from .runtime import Runtime
from ..realdata.runner import RealEngine


def archive(database, output):
    destination=Path(output).resolve()
    if destination.exists() or destination==Path(database).resolve():
        raise ValueError('ARCHIVE_MUST_BE_NEW')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mobile-archive-',dir=destination.parent) as work:
        backup=Path(work)/'mobile.sqlite3'
        with Runtime(database) as source:
            if shutil.disk_usage(destination.parent).free < source.storage()['allocated_bytes']+source.reserve:
                raise ValueError('ARCHIVE_HEADROOM_REQUIRED')
            # sqlite3.Connection's context manager commits but does NOT close.
            with source.store.connect() as conn, closing(sqlite3.connect(backup)) as target:
                conn.backup(target)
            # Reuse the existing store type without invoking schema creation on the
            # archive; every verification/replay connection closes explicitly.
            restored=copy(source.store);restored.path=str(backup)
            restored.verify()
            engine=RealEngine(restored,source.engine.configuration)
            with restored.connect() as conn:ids=[r[0] for r in conn.execute('SELECT id FROM decisions')]
            for identity in ids:
                if not engine.replay(identity)['matches'] or not engine.replay_meta(identity)['matches']:
                    raise ValueError('ARCHIVE_REPLAY_FAILURE')
            replay={'status':'PASSED' if ids else 'NO_DECISIONS_YET','checked_decisions':len(ids)}
        # Same filesystem: atomic, exclusive publication of the verified complete file.
        # Unsupported hard-link filesystems fail closed; the source is never pruned.
        os.link(backup,destination)
    return {'status':'ARCHIVE_VERIFIED','replay':replay,'bytes':destination.stat().st_size,
            'source_pruned':False}


def main():
    parser=argparse.ArgumentParser(description='Offline replay-verified complete archive; stop API first')
    parser.add_argument('--database',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:print(archive(args.database,args.output))
    except Exception:raise SystemExit('ARCHIVE_FAILED_NO_SOURCE_PRUNING') from None


if __name__=='__main__':main()
