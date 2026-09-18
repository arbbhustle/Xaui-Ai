"""Dedicated Phase 3F database with immutable release/revision and verification evidence."""
from pathlib import Path
import sqlite3,json
from ..domain import digest,parse,stamp
from ..forward.store import ForwardStore
from .verification import verify


class RealStore(ForwardStore):
    def put_capture(self,conn,capture):
        from .forexfactory import cross_check
        row=conn.execute("SELECT payload_id FROM forward_ledger WHERE kind='SECONDARY_CALENDAR' AND at<=? ORDER BY seq DESC LIMIT 1",(capture['observed_at'],)).fetchone()
        if row:
            secondary=self.get(conn,row[0])
            # A previous successful export cannot remain fresh after a failed cycle.
            age=(parse(capture['observed_at'])-parse(secondary.get('source_updated_at',capture['observed_at']))).total_seconds()
            if age>3600:secondary=dict(secondary,status='STALE')
            audit=cross_check(capture['selection']['selected'].get('calendar'),secondary,parse(capture['observed_at']))
            capture=dict(capture,secondary_calendar={'payload_id':row[0],'cross_check':audit},
                         vetoes=sorted(set(capture['vetoes']+audit['vetoes'])))
            capture['selection']=dict(capture['selection'],vetoes=sorted(set(capture['selection']['vetoes']+audit['vetoes'])))
        return super().put_capture(conn,capture)

    def __init__(self,path,specs=()):
        path=Path(path)
        if path.exists():
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_metadata'").fetchone():raise ValueError('DEDICATED_PHASE3F_DATABASE_REQUIRED')
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(path) as conn:
                conn.executescript('CREATE TABLE native_metadata(version INTEGER PRIMARY KEY); INSERT INTO native_metadata VALUES(1); CREATE TABLE forward_metadata(version INTEGER PRIMARY KEY); INSERT INTO forward_metadata VALUES(1);')
        self.specs={s.name:s for s in specs}
        super().__init__(path)
        with self.connect() as conn:
            if [r[0] for r in conn.execute('SELECT version FROM native_metadata')]!=[1]:raise ValueError('NATIVE_SCHEMA_VERSION')
            conn.executescript('''CREATE TABLE IF NOT EXISTS native_editions(
                provider TEXT NOT NULL,channel TEXT NOT NULL,entity TEXT NOT NULL,revision TEXT NOT NULL,
                received_at TEXT NOT NULL,payload_id TEXT NOT NULL REFERENCES forward_objects(id),
                PRIMARY KEY(provider,channel,entity,revision));
                CREATE INDEX IF NOT EXISTS native_editions_time ON native_editions(provider,channel,entity,received_at);''')
            for op in ('UPDATE','DELETE'):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_native_editions_{op} BEFORE {op} ON native_editions BEGIN SELECT RAISE(ABORT,'immutable edition'); END")

    def archive_observation(self,conn,cycle,row):
        spec=self.specs[row['provider']]
        previous=[]
        for r in conn.execute('SELECT payload_id FROM forward_receipts WHERE provider=? ORDER BY received_at DESC LIMIT 20',(row['provider_identity'],)):
            previous.append(self.get_observation_document(conn,r[0]))
        row=verify(row,list(reversed(previous)),spec,parse(row['received_at']))
        for key,identifier in (('calendar_editions','event_id'),('news_editions','article_id')):
            for edition in row['native']['details'].get(key,[]):
                keys=(row['provider_identity'],row['channel'],edition[identifier])
                if conn.execute('SELECT 1 FROM native_editions WHERE provider=? AND channel=? AND entity=? AND revision=?',(*keys,edition['revision_id'])).fetchone():continue
                prior=conn.execute(
                    'SELECT payload_id FROM native_editions WHERE provider=? AND channel=? AND entity=? AND received_at<=? ORDER BY received_at DESC,revision DESC LIMIT 1',
                    (*keys,row['received_at'])).fetchone()
                previous=self.get(conn,prior[0]) if prior else {}
                first_actual=previous.get('first_observed_actual')
                release={'first_observed_actual':first_actual if first_actual is not None else edition.get('Actual'),
                         'release_quality':'AS_RECEIVED_NOT_VERIFIED_ORIGINAL_RELEASE'} if key=='calendar_editions' else {}
                payload=self.put(conn,dict(edition,**release,received_at=row['received_at'],data_mode=row['data_mode']))
                conn.execute('INSERT OR IGNORE INTO native_editions VALUES (?,?,?,?,?,?)',
                    (row['provider_identity'],row['channel'],edition[identifier],edition['revision_id'],row['received_at'],payload))
        return super().archive_observation(conn,cycle,row)

    def editions_as_of(self,provider,channel,entity,at):
        with self.connect() as conn:
            return [self.get(conn,r[0]) for r in conn.execute('SELECT payload_id FROM native_editions WHERE provider=? AND channel=? AND entity=? AND received_at<=? ORDER BY received_at,revision',
                                                          (provider,channel,entity,stamp(at)))]
