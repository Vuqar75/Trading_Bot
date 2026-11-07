"""Checks for one owner-authorized local paper test; no order submission."""
from decimal import Decimal,InvalidOperation

def validate(state,*,side,business_key,quantity,price,quote_at,now_ms):
 if not isinstance(state,dict) or state.get('schema')!=1 or state.get('authorization')!='owner_local_once':raise ValueError('check_not_authorized')
 ident=state.get('id')
 if not isinstance(ident,str) or not ident or len(ident)>64:raise ValueError('check_identity_invalid')
 expected='BUY_ARMED' if side=='BUY' else 'SELL_ARMED' if side=='SELL' else None
 attempt=state.get(side.lower()+'_attempt',0)
 if type(attempt) is not int or not 0<=attempt<=3:raise ValueError('check_attempt_invalid')
 suffix=':'+str(attempt) if attempt else ''
 if state.get('phase')!=expected or business_key!='paper-check:'+ident+':'+side.lower()+suffix:raise ValueError('check_phase_invalid')
 try:q=Decimal(str(quantity));p=Decimal(str(price))
 except (InvalidOperation,TypeError,ValueError):raise ValueError('check_amount_invalid') from None
 if not q.is_finite() or not p.is_finite() or q<=0 or p<=0:raise ValueError('check_amount_invalid')
 if side=='BUY' and q*p>Decimal('10'):raise ValueError('check_notional_exceeded')
 if side=='SELL' and q!=Decimal(str(state.get('quantity','0'))):raise ValueError('check_quantity_changed')
 event=state.get('quote_event_ms');received=state.get('quote_received_ms')
 if any(type(x) is not int for x in (event,received,now_ms)) or not 0<=now_ms-event<=5000 or not 0<=now_ms-received<=5000 or not event<=received<=now_ms:raise ValueError('check_quote_stale')
 if isinstance(quote_at,bool) or not isinstance(quote_at,(float,int)) or abs(int(quote_at*1000)-received)>2:raise ValueError('check_quote_invalid')
 return True
