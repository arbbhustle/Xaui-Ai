"""Execution-only counterfactuals, isolated from every model's learning history."""
from copy import deepcopy
from datetime import timedelta
from ..domain import closed_frame,parse,stamp,allowed_session_gap
from ..intelligence_providers import BUNDLE_KEY

STRESSES=('BASE','WIDE_SPREAD','HIGH_SLIPPAGE','DELAY_1M','DELAY_2M','MISSED_CANDLES',
          'STALE_MACRO','DELAYED_NEWS','MISSING_DXY','MISSING_YIELDS','PROVIDER_OUTAGE','ENTRY_DRIFT')


def stress_tick(tick,scenario,index):
    result=deepcopy(tick);frames=result['frames'];now=parse(tick['at'])
    if scenario=='MISSED_CANDLES' and index%5==0 and frames.get('1min'):
        frames['1min']=sorted(frames['1min'],key=lambda r:parse(r['t']))[:-1]
    bundle=frames.get(BUNDLE_KEY,{})
    if scenario in ('MISSING_DXY','MISSING_YIELDS'):
        channel='usd' if scenario=='MISSING_DXY' else 'yields'
        bundle[channel]={'status':'UNAVAILABLE'}
    if scenario=='PROVIDER_OUTAGE' and index%10 in (0,1):
        result['source_errors']={tf:'RESEARCH_OUTAGE' for tf in ('1min','5min','15min','1h','4h')}
    if scenario=='STALE_MACRO':
        for name in ('usd','yields','macro'):
            if name in bundle:
                bundle[name]['as_of']=stamp(now-timedelta(days=2))
                for row in bundle[name].get('records',[]):
                    for key in ('observed_at','published_at'):
                        if key in row:row[key]=stamp(parse(row[key])-timedelta(days=2))
    if scenario=='DELAYED_NEWS' and 'news' in bundle:
        # Withhold recent news rather than pretend its delayed content was known.
        bundle['news']['records']=[r for r in bundle['news'].get('records',[]) if parse(r['published_at'])<=now-timedelta(minutes=30)]
    return result


class CounterfactualExecution:
    """Same recorded signals, changed execution only; never a retrained strategy."""
    def __init__(self,delay_minutes=0,entry_drift_points=0):
        self.delay=delay_minutes;self.drift=entry_drift_points;self.active=None;self.closed=[]

    def submit(self,decision,cohort):
        if self.active or decision['direction'] not in ('BUY','SELL'):return
        now=parse(decision['timestamp_utc']);eligible=now.replace(second=0,microsecond=0)
        if eligible<now:eligible+=timedelta(minutes=1)
        eligible+=timedelta(minutes=self.delay)
        risk=abs(decision['entry']-decision['sl'])
        self.active={'id':decision['decision_id'],'status':'PENDING','direction':decision['direction'],
                     'checkpoint':stamp(eligible),'opened_at':None,'created_at':stamp(now),
                     'expires_at':decision['expires_at'],'entry':decision['entry'],'sl':decision['sl'],
                     'tp2':decision['tp2'],'risk':risk,'cohort':cohort,
                     'probability':decision.get('calibrated_confidence'),'predicted_at':stamp(now),
                     'probability_target':decision.get('research_probability_target','GROSS')}

    def monitor(self,tick):
        if not self.active or '1min' in tick['source_errors']:return
        trade=self.active;now=parse(tick['at']);expected=parse(trade['checkpoint'])
        rows=[r for r in tick['frames'].get('1min',[]) if parse(r['t'])>=expected]
        bars,errors=closed_frame(rows,'1min',now,minimum=0,continuity=False)
        if errors:return
        for bar in bars:
            at=parse(bar.t)
            if at>expected and allowed_session_gap(expected,at,60):expected=at
            if at!=expected:break
            sign=1 if trade['direction']=='BUY' else -1
            if trade['status']=='PENDING':
                if at>=parse(trade['expires_at']):self.active=None;break
                # Drift worsens the actual fill, not the fixed signal's stop/target.
                trade.update(status='OPEN',opened_at=stamp(at),entry=bar.o+sign*self.drift)
            stop=bar.l<=trade['sl'] if sign==1 else bar.h>=trade['sl']
            target=bar.h>=trade['tp2'] if sign==1 else bar.l<=trade['tp2']
            if stop or target:
                exit_price=(min(bar.o,trade['sl']) if sign==1 else max(bar.o,trade['sl'])) if stop else trade['tp2']
                trade.update(status='CLOSED',closed_at=stamp(at+timedelta(minutes=1)),known_at=stamp(now),
                             gross_r=(exit_price-trade['entry'])*sign/trade['risk'])
                self.closed.append(deepcopy(trade));self.active=None;break
            expected=at+timedelta(minutes=1);trade['checkpoint']=stamp(expected)
