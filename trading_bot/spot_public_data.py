"""Public Binance Spot market data only; no account or order endpoints."""
import json
from urllib.request import urlopen
from .testnet_observer import SpotTestnetReader,ObservationError

class SpotPublicData(SpotTestnetReader):
    HOST='https://data-api.binance.vision'
    PATHS={'/api/v3/time','/api/v3/exchangeInfo','/api/v3/ticker/price','/api/v3/klines','/api/v3/trades'}
    def __init__(self,*,transport=None,clock=None):
        super().__init__(transport=transport,**({'clock':clock} if clock is not None else {}))
    @staticmethod
    def _http(request):
        try:
            with urlopen(request,timeout=10) as response:return json.load(response)
        except Exception:raise ObservationError('Public Spot data unavailable') from None
    def get(self,path,params=None,signed=False):
        if signed or path not in self.PATHS:raise ObservationError('Public market data only')
        return super().get(path,params,signed=False)
