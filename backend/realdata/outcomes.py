"""Causal finalized DEMO analytics; candle excursions are bounds, not tick precision."""
from datetime import timedelta
import json
from ..domain import parse,stamp


def excursions(trade,bars):
    opened,closed=parse(trade['opened_at']),parse(trade['closed_at'])
    entry=trade['entry'];risk=trade.get('risk',abs(entry-trade['sl']));sign=1 if trade['direction']=='BUY' else -1
    held=[b for b in bars if opened<=parse(b['t']) and parse(b['t'])+timedelta(minutes=1)<=closed]
    favorable=[0,max(0,sign*(trade['exit_price']-entry)/risk)]
    adverse=[0,max(0,-sign*(trade['exit_price']-entry)/risk)]
    upper_f=list(favorable);upper_a=list(adverse)
    for bar in held:
        f=max(0,sign*((bar['h'] if sign==1 else bar['l'])-entry)/risk)
        a=max(0,-sign*((bar['l'] if sign==1 else bar['h'])-entry)/risk)
        upper_f.append(f);upper_a.append(a)
        if parse(bar['t'])+timedelta(minutes=1)<closed:favorable.append(f);adverse.append(a)
    expected=int((closed-opened).total_seconds()/60)
    complete=len(held)==expected and all(parse(b['t'])==opened+timedelta(minutes=i) for i,b in enumerate(held))
    return {'mfe_r':round(max(favorable),8) if complete else None,'mae_r':round(max(adverse),8) if complete else None,
            'mfe_r_upper':round(max(upper_f),8) if complete else None,'mae_r_upper':round(max(upper_a),8) if complete else None,
            'excursion_quality':'BOUNDS_EXIT_MINUTE_ORDER_UNKNOWN' if complete else 'INCOMPLETE_CAUSAL_BARS',
            'duration_seconds':(closed-opened).total_seconds()}


def finalize(conn,store,now):
    rows=conn.execute("""SELECT event_key,payload_id FROM forward_ledger l WHERE kind='OUTCOME'
        AND NOT EXISTS(SELECT 1 FROM forward_ledger n WHERE n.event_key='native:'||l.event_key) ORDER BY seq""").fetchall()
    for key,payload_id in rows:
        outcome=store.get(conn,payload_id);trade=outcome['trade'];known=parse(outcome['known_at']);bars={}
        if known>now:raise ValueError('FUTURE_FINALIZATION')
        # First usable as-received closed candle, never a later corrected download.
        for snapshot in conn.execute('SELECT payload FROM snapshots WHERE observed_at>=? AND observed_at<=? ORDER BY observed_at',
                                     (trade['opened_at'],outcome['known_at'])):
            value=json.loads(snapshot[0])
            if '1min' in value['source_errors']:continue
            for bar in value['frames'].get('1min',[]):
                if (parse(trade['opened_at'])<=parse(bar['t'])<parse(trade['closed_at'])
                        and parse(bar['t'])+timedelta(minutes=1)<=parse(value['observed_at'])):
                    bars.setdefault(bar['t'],bar)
        extended=dict(outcome,**excursions(trade,[bars[k] for k in sorted(bars)]),outcome_timestamp=trade['closed_at'],
                      finalized_at=stamp(now),original_outcome_ref=payload_id,correction_policy='APPEND_EXPLICIT_AUDIT_ONLY')
        store.event(conn,'native:'+key,'NATIVE_OUTCOME',stamp(now),extended)
