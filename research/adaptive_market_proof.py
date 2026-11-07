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

def futures_trade_quote(value,*,now_ms,not_before_ms,max_age_ms=5000):
 """Validate a post-decision USD-M aggregate trade; never substitute mark."""
 if not isinstance(value,dict) or value.get('symbol')!='BTCUSDT' or value.get('source')!='binance_usdm_trade':raise ValueError('trade_source_invalid')
 if type(value.get('trade_id')) is not int or value['trade_id']<0:raise ValueError('trade_id_invalid')
 event=value.get('event_ms');received=value.get('received_ms')
 if any(type(x) is not int for x in (event,received,now_ms,not_before_ms)) or event<not_before_ms or not 0<=now_ms-received<=max_age_ms or not 0<=received-event<=max_age_ms or not 0<=now_ms-event<=max_age_ms:raise ValueError('trade_stale_future_or_predates_signal')
 try:price=Decimal(str(value['price']))
 except (KeyError,ValueError,TypeError,InvalidOperation):raise ValueError('trade_price_invalid') from None
 if not price.is_finite() or price<=0:raise ValueError('trade_price_invalid')
 return {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':value['trade_id'],'price':str(price),'event_ms':event,'received_ms':received}
