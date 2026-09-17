"""Offline chronological lab orchestration. No network, service or promotion action."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json,platform,sqlite3
from importlib.metadata import version,PackageNotFoundError

from ..domain import canonical,digest,parse,stamp
from ..storage import Store
from ..meta_council import DemoCosts
from ..phase3b import SOURCE_FILES as BASE_FILES
from ..phase3c import SOURCE_FILES as META_FILES
from ..gold_shadows import VARIANTS
from . import VERSION
from .data import Split,validate_tape,protect_secrets,ordered
from .archive import Registry,Archive,storage_profile
from .models import MODELS,ABLATIONS,make_engine
from .execution import STRESSES,stress_tick,CounterfactualExecution
from .statistics import metrics,sequence_risk,cohorts,stability,compare

BASE_COMMIT='0bbfe2e162857064eae562dfe243521cb992fd1d'


def runtime_identity():
    try:tz=version('tzdata')
    except PackageNotFoundError:tz='SYSTEM_ZONEINFO_UNPINNED'
    return {'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'tzdata':tz}


def code_identity(root=None):
    root=Path(root) if root is not None else Path(__file__).parents[1]
    # Cover helpers (including news identity journalling) omitted by older engine
    # source lists. Runtime changes require the original environment for replay.
    paths=[*root.glob('*.py'),*(root/'research').glob('*.py'),*root.glob('requirements*.txt')]
    files={p.relative_to(root).as_posix():p.read_text(encoding='utf-8') for p in paths}
    return digest({'sources':files,'runtime':runtime_identity()})


def cohort(result,reference=None):
    ref=reference or result;context=ref.get('analytics_context',{})
    ratio=ref.get('timeframes',{}).get('5min',{}).get('atr_ratio')
    return {'hidden_state':context.get('hidden_state','UNAVAILABLE'),'session':ref.get('session','UNAVAILABLE'),
            'direction':result.get('candidate_direction',result['direction']),
            'volatility':'UNAVAILABLE' if ratio is None else 'HIGH' if ratio>=1.5 else 'LOW' if ratio<.75 else 'NORMAL',
            'macro':{'GOLD_SUPPORTIVE':'SUPPORTIVE','GOLD_ADVERSE':'ADVERSE','MIXED':'MIXED'}.get(context.get('macro_regime'),'UNAVAILABLE'),
            'alignment':context.get('timeframe_alignment','UNAVAILABLE')}


def manifest(tape,split,stage,models,scenarios,costs,seed,settings):
    from ..council import CouncilPolicy
    from ..domain import Policy
    from ..intelligence import IntelligencePolicy
    from ..hidden_state import HiddenPolicy
    from ..slow_context import SlowPolicy
    from ..meta_council import MetaPolicy
    protect_secrets(tape)
    protect_secrets(settings)
    plan={'version':VERSION,'base_commit':BASE_COMMIT,'code_hash':code_identity(),
          'runtime':runtime_identity(),
          'dataset_id':tape['dataset_id'],'data_hash':digest(ordered(tape)),'provider':tape['provider'],'mode':tape['mode'],
          'point_in_time_attested':tape.get('point_in_time_attested'),'split':asdict(split),
          'models':list(models),'scenarios':list(scenarios),'costs':asdict(costs),'seed':seed,'settings':settings,
          'default_policies':{cls.__name__:asdict(cls()) for cls in (Policy,CouncilPolicy,IntelligencePolicy,HiddenPolicy,SlowPolicy,MetaPolicy)},
          'execution':'NATIVE_MODELS_WITH_PURGED_CALIBRATION_AND_SEPARATE_EXECUTION_STRESSES',
          'thresholds':{'minimum_evidence':100,'target_win_rate_evaluation_only':.75,'max_drawdown_r':8,
                        'bootstrap_iterations':300,'bootstrap_block_size':5},
          'capture_range':[tape['ticks'][0]['at'],tape['ticks'][-1]['at']] if tape['ticks'] else []}
    return dict(plan,plan_hash=digest(plan),stage=stage)


def _extract(engine,name,decisions,contexts,costs):
    rows=[];unclosed=[]
    with engine.store.connect() as c:
        table='meta_positions' if name=='PHASE3C' else 'trades'
        for text, in c.execute('SELECT payload FROM '+table):
            trade=json.loads(text)
            if trade['status'] in ('PENDING','OPEN'):
                unclosed.append(dict(trade,cohort=contexts.get(trade['decision_id'],{})))
                continue
            if trade['status']!='CLOSED':continue
            dec=decisions.get(trade['decision_id'])
            if not dec:continue
            risk=trade.get('risk') or abs(trade['entry']-trade['sl'])
            if name=='PHASE3C':
                known=c.execute("SELECT at FROM meta_position_events WHERE position_id=? AND kind='CLOSED'",(trade['id'],)).fetchone()[0]
                # Event time is economic close; outcome first-known can be later.
                found=c.execute("SELECT payload FROM meta_outcomes WHERE json_extract(payload,'$.trade_id')=? AND json_extract(payload,'$.source')='ADAPTIVE_CHALLENGER'",(trade['id'],)).fetchone()
                if found:known=json.loads(found[0])['known_at']
            else:
                known=c.execute("SELECT s.observed_at FROM trade_events e JOIN snapshots s ON s.id=e.snapshot_id WHERE e.trade_id=? AND e.kind='CLOSED'",(trade['id'],)).fetchone()[0]
            rows.append({'id':trade['id'],'status':'CLOSED','created_at':trade['created_at'],'opened_at':trade['opened_at'],
                         'closed_at':trade['closed_at'],'known_at':known,'gross_r':trade['r_multiple'],
                         'net_r':costs.net(trade['r_multiple'],risk),'risk_points':risk,
                         'probability':dec.get('calibrated_confidence'),'predicted_at':dec['timestamp_utc'],
                         'probability_target':'NET' if name=='PHASE3C' else 'GROSS','cohort':contexts[trade['decision_id']]})
    return rows,unclosed


def _report(rows,decisions,unclosed,split,stage,seed,observed_until):
    start,end=split.scoring_range(stage)
    # Cross-boundary labels never contaminate a scoring fold. No forced endpoint exits.
    selected=[r for r in rows if start<=parse(r['created_at'])<=parse(r['opened_at'])<parse(r['closed_at'])<end and parse(r['known_at'])<end]
    selected_decisions=[r for r in decisions if start<=parse(r['timestamp_utc'])<end]
    duration=(end-start).total_seconds()
    summary=metrics(selected,selected_decisions,duration)
    exposure=0;group_exposure={}
    for row in rows+unclosed:
        if not row.get('opened_at'):continue
        a=max(start,parse(row['opened_at']));b=min(end,observed_until,parse(row['closed_at']) if row.get('closed_at') else observed_until)
        seconds=max(0,(b-a).total_seconds());exposure+=seconds
        for axis,value in row.get('cohort',{}).items():group_exposure[(axis,value)]=group_exposure.get((axis,value),0)+seconds
    summary.update(exposure_seconds=exposure,exposure_basis='OBSERVED_TIME_IN_FOLD_INCLUDING_OPEN_POSITIONS')
    summary['target_75_percent']['out_of_sample_survival']=(
        'OBSERVED_HOLDOUT_ONLY_FORWARD_REQUIRED' if stage=='holdout' else 'VALIDATION_ONLY_HOLDOUT_REQUIRED'
    ) if summary['trade_count']>=100 and summary['win_rate']>=.75 and summary['expectancy']>0 else 'NOT_ESTABLISHED'
    groups=cohorts(selected,selected_decisions,duration)
    for axis,values in groups.items():
        for value,metric in values.items():
            metric.update(exposure_seconds=group_exposure.get((axis,value),0),exposure_basis='OBSERVED_TIME_IN_FOLD_INCLUDING_OPEN_POSITIONS')
    return {'metrics':summary,'cohorts':groups,
            'sequence_risk':sequence_risk(selected,seed),'stability':stability(selected),
            'unclosed_positions_at_cutoff':len(unclosed),'cross_boundary_or_other_period_outcomes':len(rows)-len(selected),
            'outcomes':selected,'decision_hash':digest(selected_decisions),'decisions':selected_decisions}


def evaluate_variant(tape,split,stage,name,scenario,costs,seed,settings):
    cutoff=split.scoring_range(stage)[1]
    selected=[t for t in tape['ticks'] if parse(split.warmup_start)<=parse(t['at'])<cutoff]
    if not selected:raise ValueError('EMPTY_EXPERIMENT_WINDOW')
    stress_costs=DemoCosts(costs.spread_points*(3 if scenario=='WIDE_SPREAD' else 1),
                           costs.slippage_points_per_side*(3 if scenario=='HIGH_SLIPPAGE' else 1),costs.latency_penalty_points)
    decisions={};contexts={};shadow_decisions={v:[] for v in VARIANTS};flags=[]
    execution=CounterfactualExecution(1 if scenario=='DELAY_1M' else 2 if scenario=='DELAY_2M' else 0,
                                      .5 if scenario=='ENTRY_DRIFT' else 0) if scenario in ('DELAY_1M','DELAY_2M','ENTRY_DRIFT') else None
    shadow_execution={v:CounterfactualExecution(execution.delay,execution.drift) for v in VARIANTS} if execution and name=='PHASE3B' else {}
    with TemporaryDirectory(prefix='dardania-research-') as directory:
        e=make_engine(name,Store(Path(directory)/'model.sqlite3'),split,tape['mode']=='TEST_DATA',stress_costs,settings)
        # Independent reference classifier supplies common point-in-time cohorts.
        ref=make_engine('PHASE3B',Store(Path(directory)/'reference.sqlite3'),split,tape['mode']=='TEST_DATA',costs)
        for index,original in enumerate(selected):
            tick=stress_tick(original,scenario,index);now=parse(tick['at'])
            if execution:execution.monitor(tick)
            for executor in shadow_execution.values():executor.monitor(tick)
            reference=ref.tick(deepcopy(tick['frames']),now,tick['source_errors'])
            result=e.tick(deepcopy(tick['frames']),now,tick['source_errors'])
            if not result:continue
            data_flags=[v for v in result.get('veto_codes',[]) if any(s in v for s in ('STALE','CONFLICT','PROVIDER','MISSING','INVALID','UNAVAILABLE','GAP','FIXTURE','FUTURE'))]
            flags.extend({'at':tick['at'],'code':v} for v in data_flags)
            if not result.get('decision_id'):continue
            if name=='PHASE3C' and now>=parse(split.train_start):
                meta=e.latest_meta()
                if 'challenger' not in meta:raise ValueError('RESEARCH_META_FAILURE')
                result=dict(result,direction=meta['challenger']['direction'],calibrated_confidence=meta['challenger']['calibrated_confidence'],
                            research_meta_vetoes=meta['challenger']['vetoes'],research_probability_target='NET')
            ctx=cohort(result,reference);contexts[result['decision_id']]=ctx
            decisions[result['decision_id']]=dict(result,cohort=ctx)
            if execution:execution.submit(result,ctx)
            if shadow_execution:
                with e.store.connect() as c:
                    for variant,payload in c.execute('SELECT variant,payload FROM phase3b_shadow_decisions WHERE decision_id=?',(result['decision_id'],)):
                        evaluation=json.loads(payload)
                        if evaluation['direction'] not in ('BUY','SELL'):continue
                        risk=max(result['atr'],result['entry']*.0008);sign=1 if evaluation['direction']=='BUY' else -1
                        decision=dict(result,direction=evaluation['direction'],sl=result['entry']-sign*risk,
                                      tp2=result['entry']+sign*2*risk,calibrated_confidence=None)
                        shadow_execution[variant].submit(decision,dict(ctx,direction=evaluation['direction']))
        rows,unclosed=_extract(e,name,decisions,contexts,stress_costs)
        if execution:
            rows=[dict(r,net_r=stress_costs.net(r['gross_r'],r['risk'])) for r in execution.closed]
            unclosed=[execution.active] if execution.active else []
        observed_until=parse(selected[-1]['at'])
        report=_report(rows,list(decisions.values()),unclosed,split,stage,seed,observed_until)
        if name=='PHASE3B':
            report['shadows']={}
            with e.store.connect() as c:
                for variant in VARIANTS:
                    records=[];ds=[];opened=[]
                    for decision_id,payload in c.execute('SELECT decision_id,payload FROM phase3b_shadow_decisions WHERE variant=?',(variant,)):
                        item=json.loads(payload)
                        d=decisions.get(decision_id)
                        if d:ds.append(dict(d,direction=item.get('direction','NO_TRADE')))
                    for payload, in c.execute('SELECT payload FROM phase3b_shadows WHERE variant=?',(variant,)):
                        t=json.loads(payload)
                        if t['status'] in ('OPEN','PENDING'):opened.append(dict(t,cohort=dict(contexts[t['decision_id']],direction=t['direction'])))
                        if t['status']!='CLOSED':continue
                        d=decisions[t['decision_id']]
                        known=c.execute("SELECT s.observed_at FROM phase3b_shadow_events e JOIN snapshots s ON s.id=e.snapshot_id WHERE e.shadow_id=? AND json_extract(e.payload,'$.event')='CLOSED'",(t['id'],)).fetchone()[0]
                        records.append({'id':t['id'],'status':'CLOSED','created_at':t['created_at'],'opened_at':t['opened_at'],'closed_at':t['closed_at'],
                                        'known_at':known,'gross_r':t['r_multiple'],'net_r':stress_costs.net(t['r_multiple'],t['risk']),
                                        'probability':None,'predicted_at':d['timestamp_utc'],'cohort':dict(contexts[t['decision_id']],direction=t['direction'])})
                    if variant in shadow_execution:
                        executor=shadow_execution[variant]
                        records=[dict(r,net_r=stress_costs.net(r['gross_r'],r['risk'])) for r in executor.closed]
                        opened=[executor.active] if executor.active else []
                    report['shadows'][variant]=_report(records,ds,opened,split,stage,seed,observed_until)
                    report['shadows'][variant]['execution_basis']='SIGNAL_FIXED_EXECUTION_COUNTERFACTUAL_NO_LEARNING_FEEDBACK' if execution else 'NATIVE_SHADOW_EXECUTION'
    report.update(model=name,scenario=scenario,costs=asdict(stress_costs),integrity_flags=flags,
                  execution_basis='SIGNAL_FIXED_EXECUTION_COUNTERFACTUAL_NO_LEARNING_FEEDBACK' if execution else 'NATIVE_DEMO_EXECUTION',
                  edge_after_costs=report['metrics']['expectancy']>0 if report['metrics']['expectancy'] is not None else None)
    return report


def promotion_evidence(result,mode,stage):
    champion=result.get('PHASE3B:BASE');challenger=result.get('PHASE3C:BASE')
    if mode!='HISTORICAL' or not champion or not challenger:return 'INSUFFICIENT_EVIDENCE'
    for report in (champion,challenger):
        m=report['metrics'];risk=report['sequence_risk']
        if m['trade_count']<100 or m['expectancy']<=0 or m['max_drawdown_r']>8 or report['integrity_flags'] or report['unclosed_positions_at_cutoff']:
            return 'INSUFFICIENT_EVIDENCE'
        if m['calibration']['count']<60 or m['calibration']['ece'] is None or m['calibration']['ece']>.1 or m['calibration']['brier']>.25:
            return 'INSUFFICIENT_EVIDENCE'
        if risk['expectancy']['ci95'][0]<=0 or report['stability']['flags']:return 'INSUFFICIENT_EVIDENCE'
        states=report['cohorts']['hidden_state'].values()
        if sum(m['trade_count']>=20 and m['expectancy']>0 for m in states)<3:return 'INSUFFICIENT_EVIDENCE'
        if sum(m['trade_count']>=20 for m in report['cohorts']['session'].values())<2:return 'INSUFFICIENT_EVIDENCE'
    for name in ('PHASE3B','PHASE3C'):
        for scenario in STRESSES:
            report=result.get(name+':'+scenario)
            if not report or report['metrics']['trade_count']<100 or report['metrics']['expectancy']<=0:
                return 'INSUFFICIENT_EVIDENCE'
    return 'FORWARD_DEMO_REQUIRED' if stage=='holdout' else 'RESEARCH_VALIDATION_PASSED'


def run(tape,split,root,stage='validation',models=MODELS,scenarios=('BASE',),costs=DemoCosts(),seed=173,settings=None,allow_test=False):
    settings=settings or {}
    if stage not in ('validation','holdout') or not models or len(set(models))!=len(models) or not scenarios or len(set(scenarios))!=len(scenarios) or any(s not in STRESSES for s in scenarios) or type(seed) is not int:raise ValueError('INVALID_EXPERIMENT_CONFIG')
    registry=Registry(root)
    frozen=manifest(tape,split,stage,models,scenarios,costs,seed,settings)
    attempt,identity=registry.begin(frozen)
    try:
        cutoff=split.scoring_range(stage)[1]
        prefix=dict(tape,ticks=[t for t in tape['ticks'] if parse(t['at'])<cutoff])
        normalized,flags=validate_tape(prefix,allow_test)
        # Archive the exact submitted capture corpus; later corrections are new runs.
        archive_id=registry.archive.put_document(tape)
        results={}
        for name in models:
            for scenario in scenarios:
                results[name+':'+scenario]=evaluate_variant(normalized,split,stage,name,scenario,costs,seed,settings.get(name,{}))
        ablations={component:compare(results['PHASE3B:BASE'],results['WITHOUT_'+component+':BASE'])
                   for component in ABLATIONS if component!='adaptive_meta_council' and 'PHASE3B:BASE' in results and 'WITHOUT_'+component+':BASE' in results}
        if 'PHASE3C:BASE' in results and 'WITHOUT_adaptive_meta_council:BASE' in results:
            ablations['adaptive_meta_council']=compare(results['PHASE3C:BASE'],results['WITHOUT_adaptive_meta_council:BASE'])
        attempts=[r for r in registry.entries() if r['event']=='START']
        result={'version':VERSION,'manifest_id':identity,'archive_id':archive_id,'mode':normalized['mode'],
                'stage':stage,'results':results,'ablations':ablations,'integrity_flags':flags,
                'evaluated_prefix_hash':digest(normalized),
                'experiment_count':len(attempts),'variant_count_this_run':len(results),
                'variant_count_all_attempts':sum(r['variant_count'] for r in attempts),
                'validation_reuse_count':sum(r['stage']=='validation' and r['dataset_id']==normalized['dataset_id'] for r in attempts),
                'research_bias_warning':'ALL_ATTEMPTS_RETAINED_NO_AUTOMATIC_WINNER_MULTIPLE_COMPARISONS_DESCRIPTIVE',
                'promotion_evidence':'INSUFFICIENT_EVIDENCE' if flags else promotion_evidence(results,normalized['mode'],stage),
                'champion':'PHASE3B','automatic_promotion':False}
        result['scorecard']={k:v['metrics'] for k,v in results.items() if k.endswith(':BASE')}
        if 'PHASE3B:BASE' in results:
            result['scorecard'].update({'SHADOW:'+k:v['metrics'] for k,v in results['PHASE3B:BASE']['shadows'].items()})
        registry.finish(attempt,frozen,'COMPLETED',result)
        return result
    except Exception as exc:
        # Failure existence persists; provider text/credentials never enter the ledger.
        registry.finish(attempt,frozen,'FAILED',error=type(exc).__name__)
        raise


def sensitivity(tape,split,root,model='PHASE3B',parameter='entropy_veto',values=(75,78,81),allow_test=False):
    """Validation only; no best-value selection and no holdout argument exists."""
    reports=[]
    for value in values:
        settings={model:{'hidden':{parameter:value}}}
        result=run(tape,split,root,models=(model,),settings=settings,allow_test=allow_test)
        report=result['results'][model+':BASE'];reports.append({'value':value,'manifest_id':result['manifest_id'],'metrics':report['metrics']})
    means=[r['metrics']['expectancy'] for r in reports if r['metrics']['expectancy'] is not None]
    return {'parameter':parameter,'variants':reports,'selected_value':None,
            'fragile':min(means)<=0<max(means) or max(means)-min(means)>.5 if len(means)>1 else None,
            'status':'LOW_SAMPLE' if any(r['metrics']['trade_count']<100 for r in reports) else 'DESCRIPTIVE_VALIDATION_ONLY'}


def reproduce(root,attempt):
    """Replay a completed frozen experiment; never a way to change holdout settings."""
    registry=Registry(root)
    entry=next((r for r in registry.entries() if r.get('event')=='FINISH' and r.get('attempt')==attempt and r['status']=='COMPLETED'),None)
    if entry is None:raise ValueError('NO_COMPLETED_EXPERIMENT')
    frozen=registry.archive.get(entry['run'])
    if frozen['code_hash']!=code_identity():raise ValueError('ORIGINAL_RESEARCH_CODE_REQUIRED')
    saved=registry.archive.get_document(entry['result'])
    tape=registry.archive.get_document(saved['archive_id'])
    if digest(ordered(tape))!=frozen['data_hash']:raise ValueError('ARCHIVE_DATA_HASH_MISMATCH')
    split=Split(**frozen['split']);stage=frozen['stage'];cutoff=split.scoring_range(stage)[1]
    normalized,flags=validate_tape(dict(tape,ticks=[t for t in tape['ticks'] if parse(t['at'])<cutoff]),tape['mode']=='TEST_DATA')
    if digest(normalized)!=saved['evaluated_prefix_hash']:raise ValueError('RESEARCH_PREFIX_MISMATCH')
    results={name+':'+scenario:evaluate_variant(normalized,split,stage,name,scenario,DemoCosts(**frozen['costs']),
                                             frozen['seed'],frozen['settings'].get(name,{}))
             for name in frozen['models'] for scenario in frozen['scenarios']}
    return {'attempt':attempt,'manifest_id':entry['run'],'matches':results==saved['results'] and flags==saved['integrity_flags'],
            'champion':'PHASE3B','automatic_promotion':False}
