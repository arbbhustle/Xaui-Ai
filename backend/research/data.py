"""Point-in-time capture contract and strict chronological experiment partitions."""
from copy import deepcopy
from dataclasses import dataclass,asdict
from datetime import timedelta

from ..domain import INTERVALS,Policy,canonical,digest,parse,stamp,inspect_frames
from ..intelligence_providers import BUNDLE_KEY,MARKET_KEY,source_url
from ..slow_context import SLOW_KEY


@dataclass(frozen=True)
class Split:
    warmup_start: str
    train_start: str
    validation_start: str
    holdout_start: str
    end: str
    embargo_seconds: int = 3600

    def __post_init__(self):
        times=[parse(getattr(self,k)) for k in ('warmup_start','train_start','validation_start','holdout_start','end')]
        if any(b<=a for a,b in zip(times,times[1:])) or type(self.embargo_seconds) is not int or not 0<=self.embargo_seconds<=86400:
            raise ValueError('INVALID_CHRONOLOGICAL_SPLIT')

    def phase(self,at):
        at=parse(at) if isinstance(at,str) else at
        bounds=[parse(getattr(self,k)) for k in ('warmup_start','train_start','validation_start','holdout_start','end')]
        return next((name for name,a,b in zip(('warmup','training','validation','holdout'),bounds,bounds[1:]) if a<=at<b),None)

    def scoring_range(self,stage):
        if stage not in ('validation','holdout'):raise ValueError('INVALID_STAGE')
        return (parse(self.validation_start),parse(self.holdout_start)) if stage=='validation' else (parse(self.holdout_start),parse(self.end))


def ordered(value):
    """Canonical source ordering; duplicate records remain visible for integrity checks."""
    if isinstance(value,dict):return {k:ordered(v) for k,v in sorted(value.items())}
    if isinstance(value,list):return sorted((ordered(v) for v in value),key=canonical)
    return value


def protect_secrets(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if any(word in key.lower() for word in ('password','api_key','api-key','access_token','secret','authorization')):
                raise ValueError('CREDENTIAL_FIELD_REJECTED')
            if key=='url':source_url(item)
            protect_secrets(item)
    elif isinstance(value,list):
        for item in value:protect_secrets(item)


def validate_tape(tape,allow_test=False):
    """Require an archive of as-received captures, never a final revised time series.

    Future scheduled events are legal. Future observation/publication/reception is
    not. A changed source record needs explicit revision_received_at evidence.
    Provider attestation remains a trust boundary; timestamps cannot prove honesty.
    """
    protect_secrets(tape)
    if tape.get('schema')!='as-received-captures-v1' or not tape.get('dataset_id') or not tape.get('provider'):
        raise ValueError('MISSING_POINT_IN_TIME_PROVENANCE')
    if tape.get('mode') not in ('HISTORICAL','TEST_DATA') or (tape['mode']=='TEST_DATA' and not allow_test):
        raise ValueError('SYNTHETIC_DATA_DISABLED')
    if tape.get('point_in_time_attested') is not True:raise ValueError('POINT_IN_TIME_ATTESTATION_REQUIRED')
    ticks=[]; flags=[]; previous=None; versions={}; candle_versions={}
    for raw in tape['ticks']:
        at=parse(raw['at'])
        if previous is not None and at<=previous:raise ValueError('NONCHRONOLOGICAL_CAPTURE')
        previous=at
        if parse(raw['captured_at'])!=at:raise ValueError('CAPTURE_TIME_MISMATCH')
        frames=deepcopy(raw['frames'])
        for tf,seconds in INTERVALS.items():
            for row in frames.get(tf,[]):
                if parse(row['t'])+timedelta(seconds=seconds)>at:
                    raise ValueError('FUTURE_OR_FORMING_CANDLE')
                key=(tf,stamp(parse(row['t'])))
                fingerprint=digest(row)
                if key in candle_versions and candle_versions[key]!=fingerprint:
                    # A revised price archive cannot silently rewrite execution history.
                    raise ValueError('REVISED_CANDLE_REQUIRES_SEPARATE_DATASET')
                candle_versions[key]=fingerprint
        _,errors=inspect_frames({'observed_at':stamp(at),'frames':frames},Policy())
        flags.extend({'at':stamp(at),'code':e} for e in errors)
        def check(value):
            if isinstance(value,dict):
                mode=value.get('data_mode')
                if (mode in ('TEST_DATA','FIXTURE') or value.get('is_synthetic') is True) and tape['mode']!='TEST_DATA':
                    raise ValueError('SYNTHETIC_CONTAMINATION')
                for name in ('observed_at','published_at','retrieved_at','as_of','revision_received_at'):
                    if value.get(name) is not None and parse(value[name])>at:raise ValueError('FUTURE_SOURCE_VERSION')
                if value.get('id') and value.get('source'):
                    key=(value['source'],value['id'])
                    fp=digest(value)
                    old=versions.get(key)
                    if old and old[0]!=fp:
                        if not value.get('revision_received_at') or parse(value['revision_received_at'])<=old[1]:
                            raise ValueError('UNVERSIONED_SOURCE_REVISION')
                    versions[key]=(fp,at) if not old or old[0]!=fp else old
                for child in value.values():check(child)
            elif isinstance(value,list):
                ids=set()
                for child in value:
                    if isinstance(child,dict) and child.get('id') and child.get('source'):
                        key=(child['source'],child['id'])
                        if key in ids:flags.append({'at':stamp(at),'code':'DUPLICATE_SOURCE_RECORD'})
                        ids.add(key)
                    check(child)
        check(frames)
        market=frames.get(MARKET_KEY,{})
        if not market.get('provider') or market.get('data_mode') not in ('LIVE','DELAYED','FIXTURE','TEST_DATA'):
            raise ValueError('MISSING_XAU_PROVENANCE')
        ticks.append({'at':stamp(at),'captured_at':stamp(at),'frames':ordered(frames),
                      'source_errors':ordered(raw.get('source_errors',{}))})
    if not ticks:raise ValueError('EMPTY_ARCHIVE')
    normalized={k:tape[k] for k in ('schema','dataset_id','provider','mode','point_in_time_attested')}
    normalized['ticks']=ticks
    return normalized,sorted(flags,key=lambda f:(f['at'],f['code']))


def causal_labels(rows,feature_start,decision_at,embargo_seconds):
    cutoff=feature_start-timedelta(seconds=embargo_seconds)
    return [r for r in rows if parse(r['closed_at'])<cutoff and parse(r['known_at'])<cutoff
            and parse(r['known_at'])<decision_at]
