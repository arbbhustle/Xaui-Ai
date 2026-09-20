"""Versioned Twelve Data validation, isolated from every frozen backend module.

Load the unchanged engine sources in a private package namespace. Only that copy's
4h validator is extended, and only inside a context backed by provider evidence.
No sys.modules entry or function in the normal backend namespace is replaced.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from types import ModuleType
import math
import re
import sys

from ..domain import canonical, digest, parse, stamp

VERSION='mobile-twelve-xau-4h-v1'
META='mobile_4h_alignment'
_anchor=ContextVar('mobile_twelve_4h_v1',default=None)
_package=__package__+'._frozen_xau_alignment_v1'
_root=Path(__file__).resolve().parents[1]
_namespace=ModuleType(_package)
_namespace.__path__=[str(_root)]
_namespace.__package__=_package
sys.modules[_package]=_namespace
_domain=import_module(_package+'.domain')
_legacy_closed=_domain.closed_frame


@contextmanager
def alignment_scope(anchor):
    token=_anchor.set(anchor)
    try:yield
    finally:_anchor.reset(token)


def closed_frame(rows,interval,now,minimum=60,continuity=True):
    offset=_anchor.get()
    if interval!='4h' or offset is None or offset==0:
        return _legacy_closed(rows,interval,now,minimum,continuity)
    # Same checks as frozen closed_frame; only the modulo origin differs. Never
    # shift stored timestamps or the clock (that would distort market/session gaps).
    candles=[];seen=set();errors=[];seconds=14400
    try:
        if not isinstance(rows,list):raise ValueError('INVALID_FRAME')
        for row in rows:
            opened=parse(row['t'])
            if opened.utcoffset()!=timedelta(0) or opened.microsecond or (int(opened.timestamp())-offset)%seconds:
                raise ValueError('UNALIGNED_CANDLE')
            if opened>now:raise ValueError('FUTURE_CANDLE')
            if opened+timedelta(seconds=seconds)>now:continue
            candle=_domain.Candle(**row)
            if opened in seen:raise ValueError('DUPLICATE_CANDLE')
            seen.add(opened)
            prices=[candle.o,candle.h,candle.l,candle.c]
            if not all(math.isfinite(v) and v>0 for v in prices):raise ValueError('INVALID_PRICE')
            if not candle.l<=min(candle.o,candle.c)<=max(candle.o,candle.c)<=candle.h:raise ValueError('INVALID_OHLC')
            candles.append(candle)
        candles.sort(key=lambda c:parse(c.t))
        if len(candles)<minimum:errors.append('INSUFFICIENT_DATA:4h')
        for a,b in zip(candles,candles[1:]):
            expected=parse(a.t)+timedelta(seconds=seconds)
            if parse(b.t)<expected:raise ValueError('OVERLAPPING_CANDLES')
            if continuity and parse(b.t)>expected and not _domain.allowed_session_gap(expected,parse(b.t),seconds):
                errors.append('CANDLE_GAP:4h');break
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):return [],['INVALID_DATA:4h']
    return candles,errors


_domain.closed_frame=closed_frame # Private copy only; normal backend.domain is untouched.
_contracts=import_module(_package+'.forward.contracts')
_normalize=_contracts.normalize


def validate_evidence(spec,evidence,now):
    if (spec.vendor!='twelve_data' or spec.channel!='xau' or not spec.approved_at(now)
            or not spec.live_entitled or spec.source_timezone!='UTC' or spec.unit!='USD_per_troy_ounce'):
        raise ValueError('ALIGNMENT_PROVIDER_NOT_APPROVED')
    if not isinstance(evidence,dict) or evidence.get('version')!=VERSION or evidence.get('provider_identity')!=spec.identity:
        raise ValueError('ALIGNMENT_EVIDENCE_REQUIRED')
    anchor=evidence.get('anchor_seconds')
    if type(anchor) is not int or anchor not in (0,3600,7200,10800):raise ValueError('UNSUPPORTED_4H_ANCHOR')
    at=parse(evidence['bootstrap_at'])
    if at>now or not spec.approved_at(at):raise ValueError('NONCAUSAL_BOOTSTRAP')
    if not re.fullmatch('[0-9a-f]{64}',evidence.get('raw_hash','')):raise ValueError('BOOTSTRAP_HASH_REQUIRED')
    rows=evidence.get('native_bars')
    if not isinstance(rows,list) or not 60<=len(rows)<=125:raise ValueError('BOOTSTRAP_COVERAGE_REQUIRED')
    if any(parse(r['t'])+timedelta(hours=4)>at for r in rows):raise ValueError('UNCLOSED_BOOTSTRAP')
    with alignment_scope(anchor):bars,errors=closed_frame(rows,'4h',at)
    if errors or (at-parse(bars[-1].t)-timedelta(hours=4)).total_seconds()>=14490:
        raise ValueError('INVALID_BOOTSTRAP_CANDLES')
    return anchor


def normalize_acquisition(spec,acquisition,received):
    evidence=acquisition.details.get(META)
    anchor=validate_evidence(spec,evidence,received)
    proof=acquisition.verification
    if (not all(proof.get(k) is True for k in ('authenticated','real_transport','entitled','complete'))
            or proof.get('provider_identity')!=spec.identity or proof.get('provider')!='twelve_data'
            or proof.get('instrument')!='XAU/USD' or proof.get('source_timezone')!='UTC'
            or proof.get('unit')!='USD_per_troy_ounce' or acquisition.envelope.get('data_mode')!='LIVE_DATA'):
        raise ValueError('UNVERIFIED_ALIGNMENT_PROVENANCE')
    with alignment_scope(anchor):return _normalize(spec.legacy,acquisition.envelope,received,acquisition.raw_hash)


# Engine imports now bind to the isolated domain's validator. Other validation,
# scoring, timeframes, timestamps and execution behavior remain the frozen sources.
_RealEngine=import_module(_package+'.realdata.runner').RealEngine


class MobileXauEngine(_RealEngine):
    validator_version=VERSION

    def __init__(self,store,configuration):
        super().__init__(store,configuration)
        self.forward_hash=digest({'base':self.forward_hash,'validator':VERSION,
            'source':Path(__file__).read_text(encoding='utf-8')})
        self.model_identity=digest([self.model_identity,self.forward_hash])

    @contextmanager
    def capture_scope(self,capture_id,frames,now):
        with self.store.connect() as conn:capture=self.store.get_capture(conn,capture_id)
        if parse(capture['observed_at'])!=now:raise ValueError('CAPTURE_CLOCK_MISMATCH')
        selected=capture['selection']['selected'].get('xau')
        anchor=None
        if selected:
            spec=self.store.specs[selected['provider']]
            proof=selected['native']['verification']
            if (selected['data_mode']!='LIVE_DATA' or selected['native'].get('live_verified') is not True
                    or not all(proof.get(k) is True for k in ('authenticated','real_transport','entitled','complete'))
                    or proof.get('provider_identity')!=spec.identity
                    or proof.get('provider')!='twelve_data' or proof.get('instrument')!='XAU/USD'
                    or proof.get('source_timezone')!='UTC' or proof.get('unit')!='USD_per_troy_ounce'
                    or not parse(selected['observed_at'])<=parse(selected['received_at'])<=now):
                raise ValueError('UNVERIFIED_CAPTURE_ALIGNMENT')
            anchor=validate_evidence(spec,selected['native']['details'].get(META),parse(selected['received_at']))
            if frames.get('4h')!=selected['value']['4h']:raise ValueError('CAPTURE_FRAME_MISMATCH')
        elif frames.get('4h'):raise ValueError('ALIGNMENT_CAPTURE_REQUIRED')
        with alignment_scope(anchor):yield

    def tick(self,frames,now,source_errors=None):
        with self.capture_scope(frames['__forward__']['capture_id'],frames,now):
            return super().tick(frames,now,source_errors)

    def snapshot_context(self,conn,snapshot,checked):
        snapshot['mobile_validator_version']=VERSION
        return super().snapshot_context(conn,snapshot,checked)

    def evaluate_snapshot(self,snapshot,policy):
        if snapshot.get('mobile_validator_version')!=VERSION:raise ValueError('VALIDATOR_VERSION_REQUIRED')
        with self.capture_scope(snapshot['forward']['capture_id'],snapshot['frames'],parse(snapshot['observed_at'])):
            return super().evaluate_snapshot(snapshot,policy)
