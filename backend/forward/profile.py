"""Read-only byte accounting; no retention or deletion of evidence."""
import json,zlib
from ..domain import canonical


def measure(store):
    with store.connect() as conn:
        conn.execute('BEGIN')
        snapshots=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM snapshots')]
        count=len(snapshots)
        raw=sum(len(canonical(s).encode()) for s in snapshots)
        compressed=conn.execute('SELECT coalesce(sum(length(cast(payload AS BLOB))),0) FROM snapshots').fetchone()[0]
        objects=conn.execute('SELECT count(*),coalesce(sum(length(payload)),0) FROM forward_objects').fetchone()
        pages={r[0]:r[1] for r in conn.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name')}
        page_total=sum(pages.values())
    scale=1440/count/2**20 if count else 0
    return {'ticks':count,'raw_snapshot_bytes':raw,'compressed_snapshot_bytes':compressed,
            'forward_object_count':objects[0],'forward_object_bytes':objects[1],
            'database_allocated_bytes':page_total,'allocated_by_table':pages,
            'raw_snapshot_mib_per_1440':round(raw*scale,3),'compressed_snapshot_mib_per_1440':round(compressed*scale,3),
            'full_database_mib_per_1440':round(page_total*scale,3),
            'limitations':'Linear sample extrapolation; excludes WAL/SHM, backups, filesystem blocks and mature learning-history growth',
            'integrity':store.verify()}
