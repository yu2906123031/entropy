"""Deterministic order reconciliation for the Entropy market maker."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from .quote_model import Quote
@dataclass(frozen=True)
class LiveOrder:
    order_id:int; side:str; price:float; size:float; reduce_only:bool=False; created_at_ms:int|None=None
@dataclass(frozen=True)
class Cancel: order_id:int; reason:str
@dataclass(frozen=True)
class Place:
    level:int; side:str; price:float; size:float; post_only:bool=True; reduce_only:bool=False
@dataclass(frozen=True)
class ReconcilePlan:
    cancels:tuple[Cancel,...]; places:tuple[Place,...]; kept_order_ids:tuple[int,...]
def _same_number(a,b): return Decimal(str(a))==Decimal(str(b))
def _distance_bps(order,quote): return abs(order.price-quote.price)/quote.price*10000
def reconcile_orders(existing:Iterable[LiveOrder],desired:Iterable[Quote],*,now_ms:int|None=None,min_order_lifetime_ms:int=0,reprice_threshold_bps:float=0.0,hard_reprice_threshold_bps:float|None=None)->ReconcilePlan:
    if min_order_lifetime_ms<0 or reprice_threshold_bps<0: raise ValueError("reconciliation thresholds must be non-negative")
    hard=max(reprice_threshold_bps, hard_reprice_threshold_bps if hard_reprice_threshold_bps is not None else max(8.0,reprice_threshold_bps*4))
    remaining=list(desired); cancels=[]; kept=[]
    for order in existing:
        if order.reduce_only: kept.append(order.order_id); continue
        candidates=[(i,q) for i,q in enumerate(remaining) if q.side==order.side and _same_number(q.size,order.size)]
        soft=next((i for i,q in candidates if _distance_bps(order,q)<=reprice_threshold_bps),None)
        young=now_ms is not None and order.created_at_ms is not None and 0<=now_ms-order.created_at_ms<min_order_lifetime_ms
        match=soft
        if match is None and young and candidates:
            best_i,best_q=min(candidates,key=lambda x:_distance_bps(order,x[1]))
            if _distance_bps(order,best_q)<hard: match=best_i
        if match is None: cancels.append(Cancel(order.order_id,"hard_stale" if candidates and min(_distance_bps(order,q) for _,q in candidates)>=hard else "stale_or_duplicate")); continue
        remaining.pop(match); kept.append(order.order_id)
    return ReconcilePlan(tuple(cancels),tuple(Place(q.level,q.side,q.price,q.size) for q in remaining),tuple(kept))
