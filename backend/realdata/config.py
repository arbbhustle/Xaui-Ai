"""Explicit per-channel approval and entitlement, independent of environment secrets."""
from dataclasses import dataclass,asdict
import re
from ..domain import digest,parse
from ..forward.contracts import ProviderSpec

VENDORS={'twelve_data':('xau',),'trading_economics':('dxy','us2y','us10y','calendar'), 'finnhub':('news',)}
HOSTS={'twelve_data':'https://api.twelvedata.com','trading_economics':'https://api.tradingeconomics.com','finnhub':'https://finnhub.io'}
MAPPINGS={'xau':'XAU/USD','dxy':'DXY:CUR','us2y':'USGG2YR:IND','us10y':'USGG10YR:IND','calendar':'US_CALENDAR','news':'GOLD_USD_NEWS'}


@dataclass(frozen=True)
class NativeSpec:
    name:str
    vendor:str
    channel:str
    secret_env:str
    approved:bool=False
    approval_ref:str=''
    entitlement_ref:str=''
    entitlement_until:str=''
    live_entitled:bool=False
    source_timezone:str=''
    unit:str=''
    priority:int=0
    timeout_seconds:float=8
    expected_cadence_seconds:int=60
    calendar_complete:bool=False

    def __post_init__(self):
        if self.vendor not in VENDORS or self.channel not in VENDORS[self.vendor]:raise ValueError('UNSUPPORTED_NATIVE_CHANNEL')
        for value in (self.approved,self.live_entitled,self.calendar_complete):
            if type(value) is not bool:raise ValueError('INVALID_APPROVAL_FLAG')
        if not re.fullmatch('[A-Z][A-Z0-9_]{1,100}',self.secret_env):raise ValueError('INVALID_SECRET_REFERENCE')
        for value in (self.approval_ref,self.entitlement_ref):
            if value and not re.fullmatch('[A-Za-z0-9_.:-]{1,100}',value):raise ValueError('INVALID_APPROVAL_REFERENCE')
        if self.entitlement_until:parse(self.entitlement_until)
        if self.source_timezone not in ('','UTC'):raise ValueError('UNVERIFIED_SOURCE_TIMEZONE')
        if self.unit not in ('','percent','index_points','USD_per_troy_ounce'):raise ValueError('INVALID_UNIT_MAPPING')
        if type(self.expected_cadence_seconds) is not int or not 1<=self.expected_cadence_seconds<=900:raise ValueError('INVALID_CADENCE')
        self.legacy  # Reuse baseline validation for name, timeout and priority.

    @property
    def identity(self):return digest(asdict(self))

    @property
    def legacy(self):
        return ProviderSpec(self.name,self.channel,HOSTS[self.vendor],self.secret_env,
                            mode='LIVE_DATA',approval=self.identity,priority=self.priority,timeout_seconds=self.timeout_seconds)

    def approved_at(self,at):
        return bool(self.approved and self.approval_ref and self.entitlement_ref and self.entitlement_until
                    and at<parse(self.entitlement_until))
