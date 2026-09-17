"""Isolated research adapters; function globals are copied, never monkeypatched."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from types import FunctionType

from .. import council,phase3a,phase3b,hidden_state
from ..phase2 import Phase2Engine
from ..phase3a import Phase3AEngine
from ..phase3b import Phase3BEngine
from ..phase3c import Phase3CEngine,feature_start
from ..domain import inspect_frames,INTERVALS,parse,stamp,digest,apply_veto,canonical
from ..scoring import ema,rsi,atr
from ..intelligence import IntelligencePolicy
from ..intelligence_providers import MARKET_KEY
from ..slow_context import SlowPolicy
from ..meta_council import DemoCosts,MetaPolicy
from .data import causal_labels

BASELINES=('NO_TRADE','EMA_TREND','RSI','BREAKOUT','TREND_5M')
MODELS=('PHASE2','PHASE3A','PHASE3B','PHASE3C',*BASELINES)
ABLATIONS=('technical','momentum','structure','volatility','multi_timeframe','macro','news',
           'resilience','dgfe','liquidity','entropy','event_absorption','slow_regime','adaptive_meta_council')


def clone(function,**overrides):
    env=dict(function.__globals__);env.update(overrides)
    result=FunctionType(function.__code__,env,function.__name__,function.__defaults__,function.__closure__)
    result.__kwdefaults__=function.__kwdefaults__
    return result


def ablated_evaluator(component):
    if component not in ABLATIONS:raise ValueError('UNKNOWN_ABLATION')
    weights=dict(council.WEIGHTS)
    key={'structure':'market_structure','multi_timeframe':'multi_timeframe_alignment'}.get(component,component)
    if key in weights:
        weights[key]=0;total=sum(weights.values());weights={k:v/total for k,v in weights.items()}
    evaluate_council=clone(council.evaluate_council,WEIGHTS=weights)
    def assess(*args):
        result=deepcopy(phase3a.assess_snapshot(*args))
        names=('usd_score','yields_score','macro_score') if component=='macro' else ('news_sentiment_score',) if component=='news' else ()
        for name in names:
            if result['scores'][name] is not None:result['scores'][name]=0
        return result
    blend=dict(phase3a.BLEND)
    if component=='macro':
        for k in ('usd_score','yields_score','macro_score'):blend.pop(k)
    if component=='news':blend.pop('news_sentiment_score')
    phase_a=clone(phase3a.evaluate_phase3a,evaluate_council=evaluate_council,assess_snapshot=assess,BLEND=blend)
    overrides={}
    if component=='resilience':
        def resilience(*args):
            original=hidden_state.resilience(*args)
            return dict(original,gold_resilience_score=None,components={},reason='RESEARCH_ABLATION')
        overrides['resilience']=resilience
    if component=='liquidity':
        def liquidity(bars):
            original=hidden_state.liquidity(bars)
            return {k:(False if type(v) is bool else 0) if k not in ('swing_high','swing_low','baseline_atr') else v for k,v in original.items()}
        overrides['liquidity']=liquidity
    if component=='entropy':
        overrides['entropy']=lambda closes:dict(hidden_state.entropy(closes),score=0)
    if component=='event_absorption':overrides['event_absorption']=lambda *args:[]
    if component=='dgfe':
        overrides['classify_state']=lambda shock,exhaustion,expansion,fracture,liquid,compression,accumulation:hidden_state.classify_state(shock,exhaustion,0,0,liquid,compression,accumulation)
    hidden_fn=clone(hidden_state.analyze_hidden,**overrides)
    def analyze(*args):
        result=hidden_fn(*args)
        if component=='dgfe' and result['dgfe']:
            for name in ('fracture_score','directional_pressure','expansion_candidate_score'):result['dgfe'][name]=0
            result['vetoes']=[v for v in result['vetoes'] if v!='HIDDEN_COUNCIL_CONFLICT']
        return result
    slow=phase3b.assess_slow if component!='slow_regime' else lambda *args:{'vetoes':[],'data_mode':'UNAVAILABLE','reason':'RESEARCH_ABLATION'}
    return clone(phase3b.evaluate_phase3b,evaluate_phase3a=phase_a,analyze_hidden=analyze,assess_slow=slow)


def baseline(snapshot,policy,name,threshold=0):
    result=council.evaluate_council(snapshot,policy)
    frames,errors=inspect_frames(snapshot,policy)
    if errors:return result
    bars=frames['5min'][-120:];closes=[b.c for b in bars];value=rsi(closes)[-1]
    side='NO_TRADE'
    if name=='EMA_TREND':side='BUY' if ema(closes,8)[-1]>ema(closes,21)[-1]+threshold else 'SELL' if ema(closes,8)[-1]<ema(closes,21)[-1]-threshold else 'NO_TRADE'
    if name=='RSI':side='BUY' if value<30 else 'SELL' if value>70 else 'NO_TRADE'
    if name=='BREAKOUT':side='BUY' if closes[-1]>max(b.h for b in bars[-21:-1]) else 'SELL' if closes[-1]<min(b.l for b in bars[-21:-1]) else 'NO_TRADE'
    if name=='TREND_5M':side='BUY' if closes[-1]>closes[-2] else 'SELL' if closes[-1]<closes[-2] else 'NO_TRADE'
    risk=max(atr(bars)[-1]*1.25,closes[-1]*.0008);sign=1 if side=='BUY' else -1
    # Baselines intentionally omit council score/bias gates, but preserve data,
    # market-session and kill-switch vetoes. All are uncalibrated research indices.
    vetoes=[v for v in result['veto_codes'] if v in ('MARKET_CLOSED','KILL_SWITCH')]
    result.update(direction=side,candidate_direction=side,entry=closes[-1],sl=closes[-1]-sign*risk,
                  tp1=closes[-1]+sign*risk,tp2=closes[-1]+sign*2*risk,atr=atr(bars)[-1],
                  confidence=0,calibrated_confidence=None,confidence_kind='UNCALIBRATED_RESEARCH_BASELINE',
                  calibration=None,raw_score=50,buy_score=50,sell_score=50,risk_veto=False,veto_codes=[],
                  action=side,reasons=['FIXED_RESEARCH_BASELINE'],research_baseline=name)
    return apply_veto(result,vetoes)


def make_engine(name,store,split,allow_test,costs=DemoCosts(),settings=None):
    settings=settings or {}
    if set(settings)-{'council','hidden'}:raise ValueError('UNKNOWN_RESEARCH_SETTING')
    ablation=name[8:] if name.startswith('WITHOUT_') else None
    if ablation and ablation not in ABLATIONS:raise ValueError('UNKNOWN_ABLATION')
    if name not in MODELS and not ablation:raise ValueError('UNKNOWN_MODEL')
    base={'PHASE2':Phase2Engine,'PHASE3A':Phase3AEngine,'PHASE3B':Phase3BEngine,'PHASE3C':Phase3CEngine}.get(name,Phase3BEngine if ablation else Phase2Engine)
    evaluator=ablated_evaluator(ablation) if ablation and ablation!='adaptive_meta_council' else None
    class ResearchEngine(base):
        def snapshot_context(self,conn,snapshot,checked):
            context=super().snapshot_context(conn,snapshot,checked)
            if 'calibration_samples' in context:
                context['calibration_samples']=causal_labels(context['calibration_samples'],feature_start(snapshot),parse(snapshot['observed_at']),split.embargo_seconds)
            return context

        def evaluate_snapshot(self,snapshot,policy):
            if name in BASELINES:result=baseline(snapshot,policy,name)
            elif evaluator:result=evaluator(snapshot,policy)
            else:result=super().evaluate_snapshot(snapshot,policy)
            market=snapshot.get('market_provenance',snapshot['frames'].get(MARKET_KEY,{}))
            result['research_historical_evaluation']=True
            result['research_data_mode']='TEST_DATA' if market.get('data_mode') in ('FIXTURE','TEST_DATA') else 'HISTORICAL_CAPTURE'
            if result['research_data_mode']=='TEST_DATA' and result['data_status']=='LIVE_DATA':
                result['data_status']='FIXTURE_DATA'
            return apply_veto(result,['RESEARCH_WARMUP_ONLY']) if parse(snapshot['observed_at'])<parse(split.train_start) else result

        def after_tick(self,conn,snapshot,result,now,snapshot_id):
            if now<parse(split.train_start):
                # Warm indicators/context only: no outcome labels or shadow/meta trades.
                if issubclass(base,Phase3BEngine) and result.get('decision_id'):
                    payload={'at':stamp(now),'pressure':hidden_state.macro_pressure(result['intelligence'])}
                    conn.execute('INSERT OR IGNORE INTO phase3b_context VALUES (?,?,?)',(stamp(now),snapshot_id,canonical(payload)))
                return
            super().after_tick(conn,snapshot,result,now,snapshot_id)
    kwargs={}
    if issubclass(base,Phase3AEngine):kwargs['intelligence_policy']=IntelligencePolicy(allow_fixture_data=allow_test)
    if issubclass(base,Phase3BEngine):kwargs['slow_policy']=SlowPolicy(allow_test_data=allow_test)
    if issubclass(base,Phase3CEngine):
        kwargs['costs']=costs
        kwargs['meta_policy']=MetaPolicy(embargo_seconds=max(3600,split.embargo_seconds))
    if 'council' in settings:kwargs['council_policy']=council.CouncilPolicy(**settings['council'])
    if 'hidden' in settings:
        if not issubclass(base,Phase3BEngine):raise ValueError('HIDDEN_POLICY_NOT_APPLICABLE')
        kwargs['hidden_policy']=hidden_state.HiddenPolicy(**settings['hidden'])
    engine=ResearchEngine(store,**kwargs)
    engine.model_identity=digest({'original':engine.model_identity,'research_variant':name,'settings':settings,
                                  'embargo':split.embargo_seconds})
    return engine
