"""Dedicated, bounded hosting of the Phase 3F engine chain; acquisition is opt-in."""
from contextlib import contextmanager, closing
from dataclasses import asdict
from pathlib import Path
import json
import os
import shutil
import sqlite3

from ..domain import digest, canonical
from ..realdata.config import NativeSpec
from ..realdata.runner import RealEngine
from ..realdata.store import RealStore
from ..worker import WorkerLock
from .collection import enabled, xau_spec, MobileCollector
from .xau_alignment_v1 import VERSION as ALIGNMENT_V1, MobileXauEngine


MIB = 1024 * 1024
DEFAULT_DB = 'backend/data/mobile-v2.sqlite3'


def candidates():
    return tuple(NativeSpec(name=vendor+'-'+channel, vendor=vendor, channel=channel, secret_env=secret)
                 for vendor, secret, channels in (
                     ('twelve_data', 'TWELVE_DATA_API_KEY', ('xau',)),
                     ('trading_economics', 'TRADING_ECONOMICS_API_KEY', ('dxy','us2y','us10y','calendar')),
                     ('finnhub', 'FINNHUB_API_KEY', ('news',))) for channel in channels)


def configured_path(path=None):
    # A credential or environment toggle must never silently turn on acquisition.
    enabled() # Strict toggle syntax; collection ownership belongs to the lifespan.
    value = Path(path or os.environ.get('MOBILE_DB_PATH', DEFAULT_DB))
    if os.environ.get('RENDER'):
        disk = Path(os.environ.get('MOBILE_DISK_PATH', '/var/data'))
        if not value.is_absolute() or not disk.is_mount() or not value.resolve().is_relative_to(disk.resolve()):
            raise RuntimeError('DEDICATED_PERSISTENT_DISK_REQUIRED')
        if os.environ.get('RENDER_SERVICE_NAME', 'dardania-xautrade-ai-v2') == 'xau-ai-trader-android':
            raise RuntimeError('LEGACY_SERVICE_FORBIDDEN')
    for name, default in (('DEMO_DB_PATH','backend/data/demo.sqlite3'),
                          ('PHASE2_DEMO_DB_PATH','backend/data/phase2.sqlite3'),
                          ('PHASE3A_DEMO_DB_PATH','backend/data/phase3a.sqlite3'),
                          ('PHASE3B_DEMO_DB_PATH','backend/data/phase3b.sqlite3'),
                          ('PHASE3C_DEMO_DB_PATH','backend/data/phase3c.sqlite3')):
        if value.resolve() == Path(os.environ.get(name, default)).resolve():
            raise RuntimeError('LEGACY_DATABASE_FORBIDDEN')
    return value.resolve()


class Runtime:
    """Own the database for the entire lifespan, including scheduler shutdown."""
    def __init__(self, path=None):
        self.collection_enabled=enabled()
        self.validator_version=os.environ.get('MOBILE_XAU_VALIDATOR','legacy')
        if self.validator_version not in ('legacy',ALIGNMENT_V1):raise RuntimeError('UNKNOWN_MOBILE_VALIDATOR')
        self.path = configured_path(path)
        self.limit = int(os.environ.get('MOBILE_MAX_DB_MIB', '512')) * MIB
        self.reserve = int(os.environ.get('MOBILE_MIN_FREE_MIB', '256')) * MIB
        if not 16*MIB <= self.limit <= 4096*MIB or self.reserve < 64*MIB:
            raise RuntimeError('INVALID_STORAGE_BUDGET')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = WorkerLock(str(self.path)+'.forward.lock')

    def storage(self):
        size = sum(p.stat().st_size for p in (self.path, Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')) if p.exists())
        free = shutil.disk_usage(self.path.parent).free
        return {'allocated_bytes':size, 'budget_bytes':self.limit, 'free_bytes':free,
                'reserve_bytes':self.reserve, 'within_budget':size < self.limit and free >= self.reserve,
                'retention':'STOP_AT_BUDGET_NO_EVIDENCE_PRUNING', 'collection_enabled':self.collection_enabled}

    def __enter__(self):
        self.lock.__enter__()
        try:
            if not self.storage()['within_budget']:
                raise RuntimeError('STORAGE_BUDGET_EXCEEDED')
            existed = self.path.exists()
            if existed:
                with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)) as conn:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mobile_metadata'").fetchone():
                        raise RuntimeError('DEDICATED_MOBILE_DATABASE_REQUIRED')
            self.specs = tuple(xau_spec(s) if s.channel=='xau' else s for s in candidates())
            self.store = RealStore(self.path, self.specs)
            self.store.verify()
            # Preserve legacy identity and make the operational pause toggle independent
            # of model identity. Approved provider configuration requires a new epoch.
            configuration={'providers':[asdict(s) for s in self.specs], 'collection_enabled':False,'service':'mobile-v2'}
            if self.validator_version!= 'legacy':configuration['validator_version']=self.validator_version
            config=digest(configuration)
            engine_class=RealEngine if self.validator_version=='legacy' else MobileXauEngine
            self.engine=engine_class(self.store,config)
            metadata={'engine':self.engine.forward_hash,'model':self.engine.model_identity,'configuration':config,'schema':1}
            if self.validator_version!='legacy':metadata['validator_version']=self.validator_version
            identity=canonical(metadata)
            with self.store.transaction() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS mobile_metadata(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)')
                prior = conn.execute('SELECT identity FROM mobile_metadata WHERE id=1').fetchone()
                if prior and prior[0] != identity:
                    raise RuntimeError('ORIGINAL_ENGINE_CONFIGURATION_REQUIRED')
                if not prior:conn.execute('INSERT INTO mobile_metadata VALUES(1,?)', (identity,))
                for table in ('decisions','trade_events','phase3b_shadow_decisions','phase3b_shadow_events',
                              'phase3b_context','meta_decisions','meta_outcomes','meta_position_events'):
                    for operation in ('UPDATE','DELETE'):
                        conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable evidence'); END")
                # A CAPTURED cycle is durable recovery state, not database corruption.
                # ForwardRunner._once() already calls _recover() before starting a new
                # acquisition cycle. Allow startup to reach the single-owner collector,
                # which deterministically completes any captured cycle from its archived
                # capture without making a provider request.
                pending = conn.execute(
                    "SELECT status FROM forward_cycles WHERE status!='COMPLETE'"
                ).fetchall()
                if any(row[0] != 'CAPTURED' for row in pending):
                    raise RuntimeError('PENDING_RECOVERY_REQUIRES_OFFLINE_REVIEW')
            # Read-only replay audit: do not alter decisions or invoke collection on restart.
            with self.store.connect() as conn:
                ids = [r[0] for r in conn.execute('SELECT id FROM decisions ORDER BY candle_close')]
            for identity in ids:
                if not self.engine.replay(identity)['matches'] or not self.engine.replay_meta(identity)['matches']:
                    raise RuntimeError('REPLAY_INTEGRITY_FAILURE')
            self.replay = {'checked_decisions':len(ids), 'status':'PASSED' if ids else 'NO_DECISIONS_YET'}
            self.store.verify()
            self.collector=MobileCollector(self)
            return self
        except BaseException:
            self.lock.__exit__()
            raise

    def __exit__(self, *args):
        if hasattr(self,'collector'):self.collector.close()
        self.lock.__exit__()

    @contextmanager
    def read(self):
        # Requests are read-only transactions, including when the disk reaches its budget.
        with self.store.connect() as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            yield conn
