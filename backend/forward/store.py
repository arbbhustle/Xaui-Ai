"""Separate crash-safe forward store with compressed snapshots and immutable evidence."""
from contextlib import contextmanager
from pathlib import Path
import json,sqlite3,zlib

from ..domain import canonical,digest,parse,stamp
from ..storage import Store

MAGIC=b'F3E1\x00'


def encode(value):
    raw=value.encode('utf-8')
    if len(raw)>32*1024*1024:raise ValueError('PAYLOAD_TOO_LARGE')
    return MAGIC+zlib.compress(raw,9)


def decode(value):
    if not isinstance(value,bytes):return value
    if not value.startswith(MAGIC):raise ValueError('UNKNOWN_COMPRESSED_PAYLOAD')
    decoder=zlib.decompressobj();raw=decoder.decompress(value[len(MAGIC):],32*1024*1024+1)
    if len(raw)>32*1024*1024 or not decoder.eof or decoder.unused_data:raise ValueError('COMPRESSED_PAYLOAD_INVALID')
    return raw.decode('utf-8')


class CompressedConnection(sqlite3.Connection):
    def execute(self,sql,parameters=()):
        normalized=' '.join(sql.split()).upper()
        if normalized=='INSERT OR IGNORE INTO SNAPSHOTS VALUES (?,?,?)':
            parameters=(*parameters[:2],encode(parameters[2]))
        elif normalized=='INSERT INTO META_DECISIONS VALUES (?,?,?,?,?,?,?)':
            parameters=(*parameters[:4],encode(parameters[4]),encode(parameters[5]),parameters[6])
        return super().execute(sql,parameters)


def row_factory(cursor,row):return sqlite3.Row(cursor,tuple(decode(v) if isinstance(v,bytes) and v.startswith(MAGIC) else v for v in row))


FORWARD_SCHEMA='''
CREATE TABLE IF NOT EXISTS forward_metadata(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO forward_metadata VALUES(1);
CREATE TABLE IF NOT EXISTS forward_objects(id TEXT PRIMARY KEY,payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS forward_observations(id TEXT PRIMARY KEY,provider TEXT NOT NULL,channel TEXT NOT NULL,
 observed_at TEXT NOT NULL,revision TEXT NOT NULL,content_id TEXT NOT NULL REFERENCES forward_objects(id),payload_id TEXT NOT NULL REFERENCES forward_objects(id));
CREATE INDEX IF NOT EXISTS forward_observation_order ON forward_observations(provider,channel,observed_at);
CREATE TABLE IF NOT EXISTS forward_receipts(cycle TEXT NOT NULL,provider TEXT NOT NULL,observation TEXT NOT NULL REFERENCES forward_observations(id),
 received_at TEXT NOT NULL,payload_id TEXT NOT NULL REFERENCES forward_objects(id),PRIMARY KEY(cycle,provider));
CREATE TABLE IF NOT EXISTS forward_ledger(seq INTEGER PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,
 at TEXT NOT NULL,payload_id TEXT NOT NULL REFERENCES forward_objects(id),previous TEXT,checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forward_control(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forward_cycles(id TEXT PRIMARY KEY,scheduled_at TEXT NOT NULL,status TEXT NOT NULL,capture_id TEXT REFERENCES forward_objects(id),observed_at TEXT);
CREATE TABLE IF NOT EXISTS forward_requests(id TEXT PRIMARY KEY,provider TEXT NOT NULL,cycle TEXT NOT NULL,at TEXT NOT NULL,status TEXT NOT NULL);
'''


class ForwardStore(Store):
    def __init__(self,path):
        path=Path(path)
        if path.exists():
            conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            try:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='forward_metadata'").fetchone():
                    raise ValueError('DEDICATED_FORWARD_DATABASE_REQUIRED')
            finally:conn.close()
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(path) as conn:
                conn.executescript(FORWARD_SCHEMA)
        super().__init__(path)
        with self.connect() as conn:
            conn.executescript(FORWARD_SCHEMA)
            if [r[0] for r in conn.execute('SELECT version FROM forward_metadata')]!=[1]:raise ValueError('FORWARD_SCHEMA_VERSION')
            for table in ('forward_objects','forward_observations','forward_receipts','forward_ledger','snapshots'):
                for operation in ('UPDATE','DELETE'):
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable evidence'); END")

    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.path,timeout=15,isolation_level=None,factory=CompressedConnection)
        conn.row_factory=row_factory
        conn.execute('PRAGMA foreign_keys=ON');conn.execute('PRAGMA synchronous=FULL')
        try:yield conn
        finally:conn.close()

    @staticmethod
    def put(conn,value):
        identity=digest(value)
        row=conn.execute('SELECT payload FROM forward_objects WHERE id=?',(identity,)).fetchone()
        if row:
            if digest(json.loads(row[0]))!=identity:raise ValueError('OBJECT_CORRUPTION')
        else:conn.execute('INSERT INTO forward_objects VALUES (?,?)',(identity,encode(canonical(value))))
        return identity

    @staticmethod
    def get(conn,identity):
        row=conn.execute('SELECT payload FROM forward_objects WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('MISSING_FORWARD_OBJECT')
        value=json.loads(row[0])
        if digest(value)!=identity:raise ValueError('OBJECT_CORRUPTION')
        return value

    @classmethod
    def put_observation_document(cls,conn,row):
        document=dict(row);value=dict(document.pop('value'))
        if 'retrieved_at' in value:document['value_retrieved_at']=value.pop('retrieved_at')
        document['value_ref']=cls.put(conn,value)
        return cls.put(conn,document)

    @classmethod
    def get_observation_document(cls,conn,identity):
        document=cls.get(conn,identity)
        value=cls.get(conn,document.pop('value_ref'))
        if 'value_retrieved_at' in document:value['retrieved_at']=document.pop('value_retrieved_at')
        return dict(document,value=value)

    @classmethod
    def put_capture(cls,conn,capture):
        compact=dict(capture)
        compact['observations']=[cls.put_observation_document(conn,r) for r in capture['observations']]
        selection=dict(capture['selection'])
        selection['selected']={k:cls.put_observation_document(conn,r) for k,r in selection['selected'].items()}
        selection['health']={k:dict(v,providers=[cls.put_observation_document(conn,r) for r in v['providers']])
                             for k,v in selection['health'].items()}
        compact['selection']=selection;compact['format']='shared-observation-capture-v1'
        return cls.put(conn,compact)

    @classmethod
    def get_capture(cls,conn,identity):
        capture=cls.get(conn,identity)
        if capture.pop('format')!='shared-observation-capture-v1':raise ValueError('UNKNOWN_CAPTURE_FORMAT')
        capture['observations']=[cls.get_observation_document(conn,r) for r in capture['observations']]
        selection=capture['selection']
        selection['selected']={k:cls.get_observation_document(conn,r) for k,r in selection['selected'].items()}
        selection['health']={k:dict(v,providers=[cls.get_observation_document(conn,r) for r in v['providers']])
                             for k,v in selection['health'].items()}
        return capture

    @classmethod
    def event(cls,conn,key,kind,at,value):
        payload=cls.put(conn,value)
        existing=conn.execute('SELECT payload_id FROM forward_ledger WHERE event_key=?',(key,)).fetchone()
        if existing:
            if existing[0]!=payload:raise ValueError('EVIDENCE_ID_REUSED')
            return
        old=conn.execute('SELECT checksum FROM forward_ledger ORDER BY seq DESC LIMIT 1').fetchone()
        previous=old[0] if old else None
        checksum=digest([key,kind,at,payload,previous])
        conn.execute('INSERT INTO forward_ledger(event_key,kind,at,payload_id,previous,checksum) VALUES (?,?,?,?,?,?)',
                     (key,kind,at,payload,previous,checksum))

    def verify(self,full=True):
        # Immutable tables permit incremental verification between full startup/status audits.
        # This is tamper evidence, not protection against a privileged database administrator.
        start=(0,0,0,None) if full else getattr(self,'_audit_cursor',(0,0,0,None))
        object_end,snapshot_end,ledger_end,previous=start
        with self.connect() as conn:
            conn.execute('BEGIN')
            if full:
                if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('STORAGE_INTEGRITY')
                if conn.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('REFERENCE_INTEGRITY')
                for row in conn.execute('SELECT payload FROM decisions'):
                    decision=json.loads(row[0]);checksum=decision.pop('decision_checksum',None)
                    if checksum is None or digest(decision)!=checksum:raise ValueError('CHAMPION_JOURNAL_CORRUPTION')
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta_decisions'").fetchone():
                    for row in conn.execute('SELECT inputs,result,checksum FROM meta_decisions'):
                        if digest({'inputs':json.loads(row[0]),'result':json.loads(row[1])})!=row[2]:raise ValueError('CHALLENGER_JOURNAL_CORRUPTION')
                    for table in ('meta_outcomes','meta_positions','meta_position_events'):
                        for row in conn.execute(f'SELECT payload,checksum FROM {table}'):
                            if digest(json.loads(row[0]))!=row[1]:raise ValueError('COMPANION_HISTORY_CORRUPTION')
                for cycle in conn.execute('SELECT * FROM forward_cycles'):
                    entry=conn.execute('SELECT payload_id FROM forward_ledger WHERE event_key=?',('capture:'+cycle['id'],)).fetchone()
                    if not entry or self.get(conn,entry[0]).get('capture_id')!=cycle['capture_id']:raise ValueError('CAPTURE_REFERENCE_CORRUPTION')
                    capture=self.get_capture(conn,cycle['capture_id'])
                    if capture['observed_at']!=cycle['observed_at']:raise ValueError('CAPTURE_CLOCK_CORRUPTION')
                    if cycle['status']=='COMPLETE':
                        decision=conn.execute('SELECT payload_id FROM forward_ledger WHERE event_key=?',('decision:'+cycle['id'],)).fetchone()
                        if not decision or self.get(conn,decision[0]).get('capture_id')!=cycle['capture_id']:raise ValueError('CHECKPOINT_CORRUPTION')
            for row in conn.execute('SELECT rowid,id FROM forward_objects WHERE rowid>? ORDER BY rowid',(object_end,)):
                self.get(conn,row[1]);object_end=row[0]
            for row in conn.execute('SELECT event_key,kind,at,payload_id,previous,checksum,seq FROM forward_ledger WHERE seq>? ORDER BY seq',(ledger_end,)):
                if row['previous']!=previous or digest(list(row)[:5])!=row['checksum']:raise ValueError('LEDGER_CHAIN_CORRUPTION')
                self.get(conn,row['payload_id']);previous=row['checksum'];ledger_end=row['seq']
            for row in conn.execute('SELECT rowid,id,payload FROM snapshots WHERE rowid>? ORDER BY rowid',(snapshot_end,)):
                if digest(json.loads(row['payload']))!=row['id']:raise ValueError('SNAPSHOT_CORRUPTION')
                snapshot_end=row[0]
        self._audit_cursor=(object_end,snapshot_end,ledger_end,previous)
        return {'ledger_entries':ledger_end,'storage_healthy':True,'compressed_snapshot_integrity':True}

    @classmethod
    def archive_observation(cls,conn,cycle,row):
        row=dict(row)
        value=dict(row['value'])
        value.pop('retrieved_at',None)
        content=cls.put(conn,value)
        prior=conn.execute('SELECT observed_at,revision,content_id FROM forward_observations WHERE provider=? AND channel=? ORDER BY observed_at DESC LIMIT 1',
                           (row['provider_identity'],row['channel'])).fetchone()
        if prior and parse(row['observed_at'])<parse(prior['observed_at']):
            row.update(health='DEGRADED',errors=sorted(set(row['errors']+['OUT_OF_ORDER_OBSERVATION'])))
        editions=conn.execute('SELECT content_id FROM forward_observations WHERE provider=? AND channel=? AND observed_at=? AND revision=?',
                             (row['provider_identity'],row['channel'],row['observed_at'],row['revision_id'])).fetchall()
        if any(content!=r[0] for r in editions):
            row.update(health='CONFLICTING',errors=sorted(set(row['errors']+['REVISION_ID_REUSED'])))
        identity=digest([row['provider_identity'],row['channel'],row['observed_at'],row['revision_id'],content,
                         row['data_mode'],row['published_at'],row['raw_hash']])
        payload=cls.put_observation_document(conn,row)
        conn.execute('INSERT OR IGNORE INTO forward_observations VALUES (?,?,?,?,?,?,?)',
                     (identity,row['provider_identity'],row['channel'],row['observed_at'],row['revision_id'],content,payload))
        conn.execute('INSERT OR IGNORE INTO forward_receipts VALUES (?,?,?,?,?)',(cycle,row['provider_identity'],identity,row['received_at'],payload))
        return dict(row,observation_id=identity)
