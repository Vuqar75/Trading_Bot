"""Explicit offline estimates; neither exchange liquidation nor live control."""
from decimal import Decimal

def funding(direction,quantity,mark,rate):
 if direction not in ('LONG','SHORT'):raise ValueError('invalid direction')
 q,p,r=map(lambda x:Decimal(str(x)),(quantity,mark,rate))
 if q<=0 or p<=0 or not all(x.is_finite() for x in (q,p,r)):raise ValueError('invalid funding input')
 return q*p*r*(Decimal(-1) if direction=='LONG' else Decimal(1))

def funding_belongs(event_ms,entry_ms,exit_ms):
 if any(type(x) is not int for x in (event_ms,entry_ms,exit_ms)) or exit_ms<entry_ms:raise ValueError('invalid times')
 return entry_ms<=event_ms<exit_ms

def liquidation(direction,quantity,entry,mark,leverage=1,*,funding_total=0,maintenance='0.005',entry_fee='0.0005'):
 if direction not in ('LONG','SHORT') or leverage not in (1,2,3):raise ValueError('invalid liquidation input')
 q,p,m,rate,fee,paid=map(lambda x:Decimal(str(x)),(quantity,entry,mark,maintenance,entry_fee,funding_total))
 if q<=0 or p<=0 or m<=0 or not (0<=rate<1 and 0<=fee<1) or not all(x.is_finite() for x in (q,p,m,rate,fee,paid)):raise ValueError('invalid liquidation values')
 collateral=q*p/Decimal(leverage)-q*p*fee+paid
 equity=collateral+(m-p)*q*(1 if direction=='LONG' else -1)
 return {'liquidated':equity<=q*m*rate,'isolated_equity':equity,'maintenance':q*m*rate,'initial_margin':q*p/Decimal(leverage)}
