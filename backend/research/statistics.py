"""Descriptive net-R metrics and seeded dependence-aware uncertainty. No optimizer."""
from collections import defaultdict
from datetime import timedelta
from math import sqrt,isfinite
from random import Random
from statistics import mean
from ..domain import parse
from ..hidden_state import STATES


def quantile(values,p):
    if not values:return None
    values=sorted(values);x=(len(values)-1)*p;i=int(x)
    return values[i]+(values[min(i+1,len(values)-1)]-values[i])*(x-i)


def wilson(wins,n,z=1.95996398454):
    if not n:return [None,None]
    p=wins/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den;half=z*sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0,center-half),min(1,center+half)]


def calibration(rows):
    pairs=[]
    for row in rows:
        p=row.get('probability')
        if p is None:continue
        if not isinstance(p,(float,int)) or not isfinite(p) or not 0<=p<=1:raise ValueError('INVALID_PROBABILITY')
        if parse(row['predicted_at'])>=parse(row['opened_at']):continue
        outcome=row['gross_r']>0 if row.get('probability_target')=='GROSS' else row['net_r']>0
        pairs.append((p,int(outcome)))
    bins=[]
    for i in range(10):
        group=[(p,y) for p,y in pairs if min(9,int(p*10))==i]
        bins.append({'lower':i/10,'upper':(i+1)/10,'count':len(group),
                     'predicted':mean(p for p,y in group) if group else None,'observed':mean(y for p,y in group) if group else None})
    n=len(pairs)
    return {'count':n,'brier':mean((p-y)**2 for p,y in pairs) if n else None,
            'ece':sum(b['count']*abs(b['predicted']-b['observed']) for b in bins if b['count'])/n if n else None,
            'overconfidence':sum(b['count']*max(0,b['predicted']-b['observed']) for b in bins if b['count'])/n if n else None,
            'underconfidence':sum(b['count']*max(0,b['observed']-b['predicted']) for b in bins if b['count'])/n if n else None,
            'mean_calibration_bias':mean(p-y for p,y in pairs) if n else None,'bins':bins,
            'targets':sorted({r.get('probability_target','NET') for r in rows if r.get('probability') is not None})}


def metrics(rows,decisions=(),duration_seconds=0):
    rows=sorted(rows,key=lambda r:(parse(r['closed_at']),r['id']))
    if any(r['status']!='CLOSED' or not parse(r['opened_at'])<parse(r['closed_at'])<=parse(r['known_at'])
           or not all(type(r[k]) in (int,float) and isfinite(r[k]) for k in ('gross_r','net_r')) for r in rows):
        raise ValueError('INVALID_CLOSED_OUTCOME')
    values=[r['net_r'] for r in rows];n=len(values)
    winners=[x for x in values if x>0];losers=[x for x in values if x<0]
    profit=sum(winners);loss=-sum(losers)
    equity=peak=dd=0;winning=losing=maxwin=maxloss=0
    for value in values:
        equity+=value;peak=max(peak,equity);dd=max(dd,peak-equity)
        winning=winning+1 if value>0 else 0;losing=losing+1 if value<0 else 0
        maxwin=max(maxwin,winning);maxloss=max(maxloss,losing)
    signals=sum(d['direction'] in ('BUY','SELL') for d in decisions)
    cal=calibration(rows)
    return {'trade_count':n,'win_rate':len(winners)/n if n else None,'loss_rate':len(losers)/n if n else None,
            'breakeven_rate':sum(x==0 for x in values)/n if n else None,
            'gross_r':sum(r['gross_r'] for r in rows),'net_simulated_r':sum(values),'expectancy':mean(values) if n else None,
            'average_winner':mean(winners) if winners else None,'average_loser':mean(losers) if losers else None,
            'payoff_ratio':mean(winners)/-mean(losers) if winners and losers else None,
            'profit_factor':profit/loss if loss else None,'profit_factor_status':'DEFINED' if loss else 'NO_LOSSES' if n else 'NO_TRADES',
            'max_drawdown_r':dd,'longest_winning_streak':maxwin,'longest_losing_streak':maxloss,
            'recovery_factor':sum(values)/dd if dd else None,'decision_count':len(decisions),
            'signals_per_day':signals/duration_seconds*86400 if duration_seconds else None,
            'exposure_seconds':sum((parse(r['closed_at'])-parse(r['opened_at'])).total_seconds() for r in rows),
            'exposure_basis':'CLOSED_TRADES_ONLY_OPEN_EXPOSURES_REPORTED_SEPARATELY',
            'no_trade_percent':100*(len(decisions)-signals)/len(decisions) if decisions else None,
            'win_rate_ci95':wilson(len(winners),n),'low_sample':n<100,'calibration':cal,
            'target_75_percent':{'achieved_in_sample':len(winners)/n>=.75 if n else None,'sample_count':n,
                                 'ci95':wilson(len(winners),n),'net_expectancy':mean(values) if n else None,
                                 'out_of_sample_survival':'NOT_ESTABLISHED_BY_THIS_METRIC'}}


def sequence_risk(rows,seed=173,iterations=300,block_size=5):
    if type(seed) is not int or not 50<=iterations<=5000 or type(block_size) is not int or block_size<1:
        raise ValueError('INVALID_BOOTSTRAP_CONFIG')
    observed=metrics(rows)
    if len(rows)<2:return {'status':'INSUFFICIENT_EVIDENCE','sample_count':len(rows)}
    rows=sorted(rows,key=lambda r:(parse(r['closed_at']),r['id']));rng=Random(seed);n=len(rows)
    samples=[]
    # Circular blocks preserve local trade-sequence dependence; no calendar forecast.
    for _ in range(iterations):
        chosen=[]
        while len(chosen)<n:
            start=rng.randrange(n);chosen.extend(rows[(start+j)%n] for j in range(min(block_size,n)))
        chosen=chosen[:n]
        # Sequence metrics must not re-sort resampled outcomes back into original order.
        equity=peak=dd=0;streak=longest=0
        for r in chosen:
            equity+=r['net_r'];peak=max(peak,equity);dd=max(dd,peak-equity)
            streak=streak+1 if r['net_r']<0 else 0;longest=max(longest,streak)
        profit=sum(max(0,r['net_r']) for r in chosen);loss=-sum(min(0,r['net_r']) for r in chosen)
        samples.append({'drawdown':dd,'losing_streak':longest,'expectancy':equity/n,'win_rate':mean(r['net_r']>0 for r in chosen),
                        'profit_factor':profit/loss if loss else None,'ece':calibration(chosen)['ece']})
    def interval(key):
        values=[s[key] for s in samples if s[key] is not None]
        return {'ci95':[quantile(values,.025),quantile(values,.975)],'defined_replicates':len(values)}
    return {'status':'LOW_SAMPLE' if n<100 else 'ESTIMATED','sample_count':n,'seed':seed,'iterations':iterations,
            'block_size':block_size,'method':'CIRCULAR_BLOCK_BOOTSTRAP_NOT_A_FORECAST',
            'drawdown':interval('drawdown'),'losing_streak':interval('losing_streak'),
            'expectancy':interval('expectancy'),'profit_factor':interval('profit_factor'),'calibration_error':interval('ece'),'win_rate':interval('win_rate'),
            'probability_drawdown_over_125pct_observed':mean(s['drawdown']>max(observed['max_drawdown_r']*1.25,.01) for s in samples),
            'probability_nonpositive_expectancy':mean(s['expectancy']<=0 for s in samples)}


def cohorts(rows,decisions,duration):
    required={'hidden_state':STATES,'session':('ASIA','LONDON','LONDON / NEW YORK','NEW YORK'),
              'direction':('BUY','SELL'),'volatility':('HIGH','NORMAL','LOW'),
              'macro':('SUPPORTIVE','ADVERSE','MIXED'),'alignment':('ALIGNED','MIXED','OPPOSED')}
    return {axis:{value:metrics([r for r in rows if r['cohort'].get(axis)==value],
                               [d for d in decisions if d['cohort'].get(axis)==value],duration)
                  for value in sorted(set(values)|{r['cohort'].get(axis,'UNAVAILABLE') for r in [*rows,*decisions]})}
            for axis,values in required.items()}


def stability(rows):
    periods=defaultdict(list)
    for row in rows:periods[parse(row['opened_at']).strftime('%Y-%m')].append(row)
    blocks={k:metrics(v) for k,v in sorted(periods.items())};flags=[]
    if len(blocks)<3:flags.append('INSUFFICIENT_CHRONOLOGICAL_PERIODS')
    if rows and max(map(len,periods.values()))/len(rows)>.5:flags.append('ONE_PERIOD_DOMINANCE')
    means=[b['expectancy'] for b in blocks.values()]
    if len(means)>1 and means[-1]<0 and means[0]-means[-1]>.5:flags.append('DEGRADATION')
    if means and min(means)<0<max(means):flags.append('UNSTABLE_ACROSS_PERIODS')
    return {'periods':blocks,'flags':flags}


def compare(full,removed):
    a,b=full['metrics'],removed['metrics']
    if min(a['trade_count'],b['trade_count'])<100:return {'assessment':'INSUFFICIENT_EVIDENCE'}
    delta=a['expectancy']-b['expectancy']
    ci_a=full['sequence_risk']['expectancy']['ci95'];ci_b=removed['sequence_risk']['expectancy']['ci95']
    clear=ci_a[0]>ci_b[1] or ci_b[0]>ci_a[1]
    return {'assessment':('IMPROVES_RESULTS' if delta>0 else 'MAKES_RESULTS_WORSE') if clear else 'NO_MEASURABLE_VALUE_AT_THIS_SAMPLE',
            'expectancy_delta':delta,'reduces_drawdown':a['max_drawdown_r']<b['max_drawdown_r'],
            'improves_calibration':a['calibration']['ece']<b['calibration']['ece'] if a['calibration']['ece'] is not None and b['calibration']['ece'] is not None else None,
            'multiple_testing_warning':'DESCRIPTIVE_NONSELECTION_COMPARISON_NOT_SIGNIFICANCE_PROOF'}
