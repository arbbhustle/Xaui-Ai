"""Allowlisted read-only HTTPS, bounded bodies and secrets in headers only."""
from dataclasses import dataclass
from hashlib import sha256
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import json,os,time
import httpx
from ..forward.providers import ProviderFailure
from .config import HOSTS


@dataclass(frozen=True)
class Response:
    payload:object
    raw_hash:str
    real_transport:bool
    server_date:str|None


def credential(spec,environ=None):
    value=(os.environ if environ is None else environ).get(spec.secret_env,'')
    if not isinstance(value,str) or not value.strip():raise ProviderFailure('CREDENTIAL_UNAVAILABLE')
    if any(c in value for c in ('\r','\n','\x00')):raise ProviderFailure('INVALID_CREDENTIAL_FORMAT')
    return value


class NativeHTTP:
    def __init__(self,transport=None):self.transport=transport

    def read(self,spec,path,params,now,environ=None):
        if not spec.approved_at(now):raise ProviderFailure('PROVIDER_NOT_APPROVED')
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:raise ProviderFailure('UNSAFE_PATH')
        token=credential(spec,environ)
        if any(k.lower() in ('c','client','apikey','api_key','token','key') for k in params):raise ProviderFailure('SECRET_IN_QUERY')
        headers={'Authorization':'apikey '+token} if spec.vendor=='twelve_data' else {'Authorization':token} if spec.vendor=='trading_economics' else {'X-Finnhub-Token':token}
        deadline=time.monotonic()+spec.timeout_seconds
        try:
            with httpx.Client(transport=self.transport,trust_env=False,follow_redirects=False,timeout=spec.timeout_seconds) as client:
                with client.stream('GET',HOSTS[spec.vendor]+path,params=params,headers=headers) as response:
                    if response.status_code in (401,403):raise ProviderFailure('AUTH_OR_ENTITLEMENT_DENIED')
                    if response.status_code==429:
                        try:retry=int(response.headers.get('Retry-After','60'))
                        except ValueError:
                            try:retry=int((parsedate_to_datetime(response.headers['Retry-After'])-now).total_seconds())
                            except Exception:retry=60
                        raise ProviderFailure('RATE_LIMITED',max(1,min(900,retry)))
                    if response.status_code!=200:raise ProviderFailure('HTTP_PROVIDER_FAILURE')
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body)>4*1024*1024:raise ProviderFailure('RESPONSE_TOO_LARGE')
                        if time.monotonic()>deadline:raise ProviderFailure('PROVIDER_TIMEOUT')
                    raw=json.loads(body)
                    if not isinstance(raw,(dict,list)):raise ProviderFailure('MALFORMED_PAYLOAD')
                    if isinstance(raw,dict) and (raw.get('error') or raw.get('status')=='error'):
                        raise ProviderFailure('PROVIDER_REJECTED')
                    return Response(raw,sha256(body).hexdigest(),self.transport is None,response.headers.get('Date'))
        except ProviderFailure:raise
        except httpx.TimeoutException:raise ProviderFailure('PROVIDER_TIMEOUT') from None
        except Exception:raise ProviderFailure('PROVIDER_READ_FAILED') from None
