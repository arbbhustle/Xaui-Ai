"""Read-only evidence reports with provenance-qualified cohorts and minimum samples."""
from collections import Counter,defaultdict
from datetime import timedelta
from dataclasses import dataclass
from statistics import mean
import json
from ..domain import parse,stamp
from ..forward.contracts import CRITICAL
from ..meta_evaluation import performance
from ..meta_council import independent
from ..forward.profile import measure
from .adapters import synthetic_payload


def real_capture(capture):
    if capture['selection']['vetoes']:return False
    selected=capture['selection']['selected']
    for channel in CRITICAL:
        row=selected.get(channel,{})
        native=row.get('native',{});proof=native.get('verification',{})
        if row.get('data_mode')!='LIVE_DATA' or row.get('health')!='HEALTHY' or not native.get('live_verified'):return False
        if synthetic_payload(row['value']):return False
        if not all(proof.get(k) is True for k in ('authenticated','real_transport','entitled','complete')):return False
        if not row.get('approval') or proof.get('provider_identity')!=row['approval']:return False
        at=parse(capture['observed_at'])
        if not parse(row['observed_at'])<=parse(row['received_at'])<=at:return False
        if parse(native['checked_at'])>at or parse(proof['entitlement_until'])<=at:return False
    return True


@dataclass(frozen=True)
class Qualification:
    closed_trades:int=100
    sessions:int=20
    regimes:int=3
    calendar_days:int=30


def qualify(rows,now,policy=Qualification()):
    samples=independent(rows);reasons=[]
    sessions={(parse(r['opened_at']).date().isoformat(),r['regime'].get('session')) for r in samples}
    regimes={r['regime'].get('hidden_state') for r in samples if r['regime'].get('hidden_state')}
    elapsed=(max(parse(r['closed_at']) for r in samples)-min(parse(r['opened_at']) for r in samples)).total_seconds()/86400 if samples else 0
    if len(samples)<policy.closed_trades:reasons.append('MINIMUM_INDEPENDENT_CLOSED_TRADES')
    if len(sessions)<policy.sessions:reasons.append('MINIMUM_SESSIONS')
    if len(regimes)<policy.regimes:reasons.append('MINIMUM_REGIMES')
    if elapsed<policy.calendar_days:reasons.append('MINIMUM_CALENDAR_TIME')
    return {'status':'INSUFFICIENT_FORWARD_DATA' if reasons else 'SUFFICIENT_FOR_RESEARCH_REVIEW_ONLY',
            'reasons':reasons,'independent_closed_trades':len(samples),'sessions':len(sessions),'regimes':len(regimes),
            'elapsed_calendar_days':elapsed,'thresholds':vars(policy),'profitability_established':False,'automatic_promotion':False}


def metrics(rows):
    result=performance(rows);gains=sum(max(0,r['net_r']) for r in rows);losses=-sum(min(0,r['net_r']) for r in rows)
    result.update(wins=sum(r['net_r']>0 for r in rows),losses=sum(r['net_r']<0 for r in rows),
                  breakeven=sum(r['net_r']==0 for r in rows),profit_factor=gains/losses if losses else None,
                  profit_factor_note=None if losses else 'UNDEFINED_WITHOUT_LOSSES')
    return result


def report(store,now):
    store.verify();decisions={};challengers={};counts=Counter();health=[];all_outcomes=[];real_cycles=0;seen_decisions=set()
    with store.connect() as conn:
        conn.execute('BEGIN')
        for row in conn.execute("SELECT at,payload_id FROM forward_ledger WHERE kind='DECISION' AND at<=? ORDER BY seq",(stamp(now),)):
            evidence=store.get(conn,row['payload_id']);capture=store.get_capture(conn,evidence['capture_id'])
            health.append({'at':row['at'],'channels':{k:v['status'] for k,v in capture['selection']['health'].items()},
                           'provider_audit':capture['provider_audit']})
            if not real_capture(capture):continue
            real_cycles+=1;identity=evidence['engine_decision_id']
            if identity and identity not in seen_decisions:
                # A later real monitor tick must not launder an earlier nonreal 5m decision.
                original=conn.execute('SELECT s.payload FROM decisions d JOIN snapshots s ON s.id=d.snapshot_id WHERE d.id=?',(identity,)).fetchone()
                if not original:continue
                original_capture=store.get_capture(conn,json.loads(original[0])['forward']['capture_id'])
                if not real_capture(original_capture):continue
                decisions[identity]=evidence['champion'];counts[evidence['champion']['direction']]+=1;seen_decisions.add(identity)
                challengers[identity]=evidence['challenger']
        for row in conn.execute("SELECT payload_id FROM forward_ledger WHERE kind='NATIVE_OUTCOME' AND at<=? ORDER BY seq",(stamp(now),)):
            outcome=store.get(conn,row[0]);trade=outcome['trade'];decision=decisions.get(trade.get('decision_id',trade['id']))
            if decision is None or parse(outcome['known_at'])>now:continue
            # No outcome from TEST, unverified or missing-entry provenance enters real cohorts.
            record={'id':outcome['source']+':'+trade['id'],'source':outcome['source'],'opened_at':trade['opened_at'],
                    'closed_at':trade['closed_at'],'gross_r':outcome['gross_r'],'net_r':outcome['simulated_net_r'],
                    'regime':trade.get('analytics_context',decision.get('analytics_context',{})),
                    'prediction':None,'prediction_at':decision['timestamp_utc'],'prediction_target':'POSITIVE_GROSS_R'}
            record['variant']=trade.get('variant',outcome['source'])
            if outcome['source']=='CHAMPION' and decision.get('confidence_kind')=='EMPIRICAL_DEMO_BETA_BIN':
                record['prediction']=decision.get('calibrated_confidence')
            if outcome['source']=='ADAPTIVE_CHALLENGER':
                meta=challengers.get(decision['decision_id'],{})
                record.update(prediction=meta.get('challenger',{}).get('calibrated_confidence'),
                              prediction_at=meta.get('at',decision['timestamp_utc']),prediction_target='POSITIVE_NET_R')
            all_outcomes.append(record)
    cohorts={}
    for source in ('CHAMPION','ADAPTIVE_CHALLENGER','SHADOW'):
        rows=[r for r in all_outcomes if r['source']==source];by_regime=defaultdict(list);by_session=defaultdict(list)
        for r in rows:
            by_regime[r['regime'].get('hidden_state','UNKNOWN')].append(r)
            by_session[r['regime'].get('session','UNKNOWN')].append(r)
        cohorts[source]={'metrics':metrics(rows),'qualification':qualify(rows,now),
                         'by_regime':{k:metrics(v) for k,v in by_regime.items()},'by_session':{k:metrics(v) for k,v in by_session.items()}}
        cohorts[source]['by_variant']={variant:metrics([r for r in rows if r['variant']==variant]) for variant in sorted({r['variant'] for r in rows})}
    return {'real_forward_evaluation_cycles':real_cycles,'real_five_minute_decisions':len(decisions),
            'decision_counts':{k:counts[k] for k in ('BUY','SELL','NO_TRADE')},'cohorts':cohorts,
            'provider_health_history':health,'research_status':cohorts['CHAMPION']['qualification']['status'],
            'champion':'PHASE3B_CHAMPION','challenger':'ISOLATED_DEMO_ONLY','automatic_promotion':False,
            'no_predictive_edge_claim':True}


def capacity(store):
    result=measure(store);daily=result['full_database_mib_per_1440']
    result.update(mib_per_hour=daily/24,mib_per_day=daily,mib_per_30_days=daily*30,mib_per_365_days=daily*365,
                  comparison_phase3e_mib_per_day=79.03,scenario='LINEAR_NATIVE_SHAPE_SAMPLE_NOT_REAL_CAPACITY_FORECAST')
    return result
