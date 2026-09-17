"""Isolated compressed content-addressed archive and append-only experiment ledger."""
from pathlib import Path
from contextlib import contextmanager
import json,sqlite3,zlib
from ..domain import canonical,digest


class Archive:
    def __init__(self,root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)

    def put(self,value):
        raw=canonical(value).encode(); key=digest(value)
        if len(raw)>32*1024*1024:raise ValueError('OBJECT_TOO_LARGE_USE_CHUNKED_ARCHIVE')
        path=self.root/(key+'.z')
        compressed=zlib.compress(raw,9)
        try:
            with path.open('xb') as f:f.write(compressed)
        except FileExistsError:
            if self.get(key)!=value:raise ValueError('ARCHIVE_COLLISION')
        return key

    def get(self,key):
        if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('INVALID_OBJECT_ID')
        compressed=(self.root/(key+'.z')).read_bytes()
        decoder=zlib.decompressobj()
        raw=decoder.decompress(compressed,32*1024*1024+1)
        if len(raw)>32*1024*1024 or not decoder.eof or decoder.unused_data:raise ValueError('ARCHIVE_SIZE_OR_STREAM')
        value=json.loads(raw)
        if digest(value)!=key:raise ValueError('ARCHIVE_CHECKSUM')
        return value

    def pack(self,value):
        if isinstance(value,dict) and (('t' in value and 'c' in value) or ('source' in value and 'id' in value)):
            node={'kind':'scalar','value':value}
        elif isinstance(value,dict):node={'kind':'dict','items':{k:self.pack(v) for k,v in sorted(value.items())}}
        elif isinstance(value,list):node={'kind':'list','items':[self.pack(v) for v in value]}
        else:node={'kind':'scalar','value':value}
        return self.put(node)

    def unpack(self,key,depth=0):
        if depth>100:raise ValueError('ARCHIVE_DEPTH')
        node=self.get(key)
        if node['kind']=='scalar':return node['value']
        if node['kind']=='list':return [self.unpack(k,depth+1) for k in node['items']]
        if node['kind']=='dict':return {k:self.unpack(v,depth+1) for k,v in node['items'].items()}
        raise ValueError('ARCHIVE_NODE')

    def put_document(self,value):
        """Chunk large containers without a giant decompression object."""
        if len(canonical(value).encode())<=8*1024*1024:
            return self.put({'document':'leaf','ref':self.put(value),'content_hash':digest(value)})
        if isinstance(value,dict):node={'document':'dict','items':{k:self.put_document(v) for k,v in value.items()}}
        elif isinstance(value,list):node={'document':'list','items':[self.put_document(v) for v in value]}
        else:raise ValueError('OVERSIZED_SCALAR')
        return self.put(dict(node,content_hash=digest(value)))

    def get_document(self,key,depth=0):
        if depth>100:raise ValueError('ARCHIVE_DEPTH')
        node=self.get(key)
        if node['document']=='leaf':value=self.get(node['ref'])
        elif node['document']=='dict':value={k:self.get_document(v,depth+1) for k,v in node['items'].items()}
        elif node['document']=='list':value=[self.get_document(v,depth+1) for v in node['items']]
        else:raise ValueError('INVALID_DOCUMENT_NODE')
        if digest(value)!=node['content_hash']:raise ValueError('DOCUMENT_CHECKSUM')
        return value


class Registry:
    def __init__(self,root):
        root=Path(root)
        root.mkdir(parents=True,exist_ok=True)
        self.path=root/'research-registry.sqlite3'
        self.archive=Archive(root/'objects')
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, checksum TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_entries_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_entries_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'immutable'); END;
            CREATE TABLE IF NOT EXISTS holdout_locks(dataset TEXT PRIMARY KEY, plan TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_lock_update BEFORE UPDATE ON holdout_locks BEGIN SELECT RAISE(ABORT,'immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_lock_delete BEFORE DELETE ON holdout_locks BEGIN SELECT RAISE(ABORT,'immutable'); END;
            ''')

    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.path)
        try:
            with conn:yield conn
        finally:conn.close()

    def entries(self,conn=None):
        if conn is None:
            with self.connect() as c:return self.entries(c)
        result=[]; previous=None
        for text,checksum in conn.execute('SELECT payload,checksum FROM entries ORDER BY seq'):
            row=json.loads(text)
            if digest(row)!=checksum or row['previous']!=previous:raise ValueError('REGISTRY_CORRUPT')
            result.append(row);previous=checksum
        return result

    def _append(self,c,row):
        rows=self.entries(c)
        record=dict(row,previous=digest(rows[-1]) if rows else None)
        c.execute('INSERT INTO entries(payload,checksum) VALUES (?,?)',(canonical(record),digest(record)))

    def begin(self,manifest):
        run=digest(manifest)
        self.archive.put(manifest)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=self.entries(c)
            dataset=manifest['dataset_id'];stage=manifest['stage'];plan=manifest['plan_hash']
            locked=c.execute('SELECT plan FROM holdout_locks WHERE dataset=?',(dataset,)).fetchone()
            reasons=[]
            if locked:reasons.append('HOLDOUT_ALREADY_OPENED_DATASET_FROZEN')
            if stage=='holdout':
                valid=any(r.get('status')=='COMPLETED' and r.get('stage')=='validation' and r.get('plan_hash')==plan
                          and r.get('dataset_id')==dataset for r in rows)
                if not valid:reasons.append('MATCHING_VALIDATION_REQUIRED')
            attempt=1+sum(r.get('event')=='START' for r in rows)
            self._append(c,{'event':'START','attempt':attempt,'run':run,'dataset_id':dataset,'plan_hash':plan,'stage':stage,
                            'variant_count':len(manifest['models'])*len(manifest['scenarios']),
                            'status':'REJECTED' if reasons else 'RUNNING','reasons':reasons})
            if not reasons and stage=='holdout':c.execute('INSERT INTO holdout_locks VALUES (?,?)',(dataset,plan))
        if reasons:raise ValueError(';'.join(reasons))
        return attempt,run

    def finish(self,attempt,manifest,status,result=None,error=None):
        if status not in ('COMPLETED','FAILED'):raise ValueError('INVALID_TERMINAL_STATUS')
        ref=self.archive.put_document(result) if result is not None else None
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=self.entries(c)
            starts=[r for r in rows if r.get('event')=='START' and r.get('attempt')==attempt]
            if len(starts)!=1 or starts[0]['status']!='RUNNING' or starts[0]['run']!=digest(manifest) or any(r.get('event')=='FINISH' and r.get('attempt')==attempt for r in rows):
                raise ValueError('INVALID_EXPERIMENT_COMPLETION')
            self._append(c,{'event':'FINISH','attempt':attempt,'run':digest(manifest),'dataset_id':manifest['dataset_id'],
                           'plan_hash':manifest['plan_hash'],'stage':manifest['stage'],'status':status,
                           'result':ref,'error':error})


def storage_profile(captures,archive):
    """Measured bytes for this corpus; extrapolation, not a production-size promise."""
    before=sum(len(canonical(x).encode()) for x in captures)
    roots=[archive.pack(x) for x in captures]
    for original,key in zip(captures,roots):
        if archive.unpack(key)!=original:raise ValueError('ARCHIVE_ROUNDTRIP')
    reachable=set()
    def visit(key):
        if key in reachable:return
        reachable.add(key);node=archive.get(key)
        if node['kind']=='dict':
            for child in node['items'].values():visit(child)
        if node['kind']=='list':
            for child in node['items']:visit(child)
    for key in roots:visit(key)
    objects=[archive.root/(key+'.z') for key in reachable]
    after=sum(p.stat().st_size for p in objects)+len(canonical(roots).encode())
    n=len(captures)
    breakdown={}
    for capture in captures:
        for key,value in capture.items():breakdown[key]=breakdown.get(key,0)+len(canonical(value).encode())
    return {'capture_count':n,'raw_payload_bytes':before,'compressed_deduplicated_bytes':after,
            'whole_capture_zlib_bytes':sum(len(zlib.compress(canonical(x).encode(),9)) for x in captures),
            'object_count':len(objects),'root_index_bytes':len(canonical(roots).encode()),'payload_breakdown':breakdown,
            'raw_mib_per_1440_ticks':before/n*1440/2**20 if n else None,
            'archive_mib_per_1440_ticks':after/n*1440/2**20 if n else None,
            'roundtrip_verified':True,'excludes':'filesystem allocation, SQLite indexes/WAL, decision and trade journals',
            'deployment':'ISOLATED_RESEARCH_ONLY','retention':'Never remove roots or reachable objects; no deletion implemented'}


def database_profile(conn):
    """Read-only page accounting of a disposable research engine database."""
    tables={row[0]:row[1] for row in conn.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name')}
    ticks=conn.execute('SELECT count(*) FROM snapshots').fetchone()[0]
    snapshot_bytes=conn.execute('SELECT coalesce(sum(length(cast(payload AS BLOB))),0) FROM snapshots').fetchone()[0]
    return {'ticks':ticks,'allocated_bytes_by_object':tables,'snapshot_payload_bytes':snapshot_bytes,
            'database_pages_bytes':sum(tables.values()),'mib_per_1440_ticks':sum(tables.values())/ticks*1440/2**20 if ticks else None,
            'excludes':'WAL/SHM files, filesystem allocation, nonstationary future history growth'}
