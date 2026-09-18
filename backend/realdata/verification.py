"""Causal authenticity gates. Successful HTTP alone is never sufficient."""
from datetime import timedelta
from ..domain import parse,stamp,market_closed,digest


def verify(row,previous,spec,now):
    row=dict(row);native=dict(row['native']);proof=native['verification'];source_errors=list(row['errors']);errors=list(source_errors)
    incoming=row['data_mode'];observed=parse(row['observed_at'])
    # Trust fresh minute prices for exits independently of unavailable higher frames.
    partial_minute=spec.channel=='xau' and bool(row['value'].get('1min')) and 0<=(now-observed).total_seconds()<150 and all(
        e.rsplit(':',1)[-1] in ('5min','15min','1h','4h') or e=='CANDLE_GAP:1min' for e in source_errors)
    if partial_minute:errors=[]
    if proof.get('authenticated') is not True:errors.append('AUTHENTICATION_UNVERIFIED')
    if proof.get('real_transport') is not True:errors.append('NONREAL_TRANSPORT')
    if proof.get('provider_identity')!=spec.identity:errors.append('PROVENANCE_IDENTITY_MISMATCH')
    if not spec.approved_at(now) or not spec.live_entitled or proof.get('entitled') is not True:errors.append('ENTITLEMENT_UNVERIFIED')
    if proof.get('complete') is not True and not partial_minute:errors.append('INCOMPLETE_PROVIDER_COVERAGE')
    if incoming!='LIVE_DATA':errors.append('NONLIVE_SOURCE_MODE')
    if row['health']!='HEALTHY' and not partial_minute:errors.append('SOURCE_NOT_HEALTHY')
    if spec.channel=='xau' and market_closed(now):errors.append('GOLD_MARKET_CLOSED')
    older=[p for p in previous if parse(p['received_at'])<now]
    if older and observed<max(parse(p['observed_at']) for p in older):errors.append('OUT_OF_ORDER_SOURCE')
    if spec.channel=='xau' and older and row['value'].get('1min'):
        old=older[-1]
        if old['value'].get('1min'):
            a,b=old['value']['1min'][-1]['c'],row['value']['1min'][-1]['c']
            if abs(b-a)/a>.03:errors.append('ABNORMAL_GOLD_JUMP')
            def price_fingerprint(r):return digest({k:r['value']['1min'][-1][k] for k in ('o','h','l','c')})
            consecutive=[]
            for prior in reversed(older):
                if not prior['value'].get('1min') or price_fingerprint(prior)!=price_fingerprint(row):break
                consecutive.append(prior)
            if consecutive and now-parse(consecutive[-1]['received_at'])>=timedelta(minutes=10):errors.append('REPEATED_GOLD_PRICE_WINDOW')
    candidate=not errors
    eligible=[p for p in older if p.get('native',{}).get('candidate_verified')]
    points=sorted({parse(p['observed_at']) for p in eligible[-10:]}|({observed} if candidate else set()))
    cadence=False
    if spec.channel in ('calendar','news'):
        # Quiet event/news periods are legal: verify fresh successful polls across time.
        receipts=[parse(p['received_at']) for p in eligible[-10:]]+([now] if candidate else [])
        cadence=len(receipts)>=3 and receipts[-1]-receipts[-3]>=timedelta(seconds=120) and all(
            0<(b-a).total_seconds()<=spec.expected_cadence_seconds*2 for a,b in zip(receipts[-3:],receipts[-2:]))
    elif len(points)>=3:
        cadence=points[-1]-points[-3]>=timedelta(seconds=120) and all(
            0<(b-a).total_seconds()<=spec.expected_cadence_seconds*2 for a,b in zip(points[-3:],points[-2:]))
    if not cadence:errors.append('CADENCE_VERIFICATION_PENDING')
    verified=candidate and cadence
    mode=incoming if incoming in ('TEST_DATA','FIXTURE','HISTORICAL_POINT_IN_TIME','UNAVAILABLE') else 'LIVE_DATA' if verified else 'DELAYED_DATA'
    native.update(candidate_verified=candidate,live_verified=verified,checks=sorted(set(errors)),checked_at=stamp(now),
                  availability='VERIFIED' if verified else 'UNVERIFIED_OR_UNAVAILABLE')
    row.update(native=native,data_mode=mode,health='HEALTHY' if verified and not source_errors else 'STALE' if row['health']=='STALE' else 'DEGRADED',
               errors=sorted(set(errors+source_errors)))
    return row
