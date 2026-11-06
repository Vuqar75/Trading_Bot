"""Source-aware public prices for the research service, without networking."""
from decimal import Decimal,InvalidOperation

def quote(value,*,market,now_ms,max_age_ms=5000):
 """Reject future/stale events and never treat receipt time as event time."""
 if not isinstance(value,dict) or market not in ('spot','futures'):raise ValueError('quote_invalid')
 expected={'spot':'binance_spot_trade','futures':'binance_usdm_mark'}[market]
 if value.get('symbol')!='BTCUSDT' or value.get('source')!=expected:raise ValueError('quote_source_or_symbol_invalid')
 event=value.get('event_ms');received=value.get('received_ms')
 if type(event) is not int or type(received) is not int or type(now_ms) is not int or not 0<=now_ms-received<=max_age_ms or not 0<=received-event<=max_age_ms or not 0<=now_ms-event<=max_age_ms:raise ValueError('quote_stale_or_future')
 try:price=Decimal(str(value['price']))
 except (KeyError,ValueError,TypeError,InvalidOperation):raise ValueError('quote_price_invalid') from None
 if not price.is_finite() or price<=0:raise ValueError('quote_price_invalid')
 return {'symbol':'BTCUSDT','source':expected,'price':str(price),'event_ms':event,'received_ms':received}
