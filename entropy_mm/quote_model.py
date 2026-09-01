"""Adaptive inventory-aware market-making quote model for Entropy."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

@dataclass(frozen=True)
class Book:
    bid: float; ask: float; bid_size: float; ask_size: float
@dataclass(frozen=True)
class Inventory:
    long: float = 0.0; short: float = 0.0
    @property
    def net(self): return self.long-self.short
    @property
    def gross(self): return self.long+self.short
@dataclass(frozen=True)
class RiskLimits:
    max_long: float; max_short: float; max_net: float; max_gross: float
@dataclass(frozen=True)
class QuoteConfig:
    tick_size: float; lot_size: float; layers: int=3
    base_half_spread_bps: float=8.0; level_gap_bps: float=6.0
    inventory_skew_bps: float=12.0; order_size: float=0.2
    elevated_vol_bps: float=20.0; shock_vol_bps: float=50.0
    elevated_spread_multiplier: float=1.8; elevated_size_multiplier: float=0.5
    microprice_weight: float=0.65; max_microprice_edge_bps: float=4.0
    inventory_skew_power: float=1.5; capacity_skew_strength: float=0.85
@dataclass(frozen=True)
class Quote:
    level: int; side: str; price: float; size: float

def _finite_positive(name,value):
    if not math.isfinite(value) or value<=0: raise ValueError(f"{name} must be finite and positive")
def validate(book,inventory,limits,cfg):
    for n,v in vars(book).items(): _finite_positive(n,v)
    if book.bid>=book.ask: raise ValueError("book must have bid below ask")
    for n,v in vars(limits).items(): _finite_positive(n,v)
    for n in ("tick_size","lot_size","order_size"): _finite_positive(n,getattr(cfg,n))
    if not isinstance(cfg.layers,int) or cfg.layers<1: raise ValueError("layers must be a positive integer")
    if inventory.long<0 or inventory.short<0: raise ValueError("inventory magnitudes must be non-negative")
    if cfg.inventory_skew_power<=0 or not 0<=cfg.microprice_weight<=1 or not 0<=cfg.capacity_skew_strength<=1: raise ValueError("invalid adaptive quote configuration")
def microprice(book):
    for n in ("bid","ask","bid_size","ask_size"): _finite_positive(n,getattr(book,n))
    if book.bid>=book.ask: raise ValueError("book must have bid below ask")
    return (book.ask*book.bid_size+book.bid*book.ask_size)/(book.bid_size+book.ask_size)
def fair_value(book,cfg):
    mid=(book.bid+book.ask)/2
    raw=microprice(book)-mid
    cap=mid*cfg.max_microprice_edge_bps/10000
    return mid+cfg.microprice_weight*max(-cap,min(cap,raw))
def _floor_step(v,s): return float((Decimal(str(v))/Decimal(str(s))).to_integral_value(rounding=ROUND_FLOOR)*Decimal(str(s)))
def _ceil_step(v,s): return float((Decimal(str(v))/Decimal(str(s))).to_integral_value(rounding=ROUND_CEILING)*Decimal(str(s)))
def opening_capacities(inventory,limits,capacity_skew_strength=0.0):
    gross=max(0.0,limits.max_gross-inventory.gross)
    lc=max(0.0,min(limits.max_long-inventory.long,limits.max_net-inventory.net,gross))
    sc=max(0.0,min(limits.max_short-inventory.short,limits.max_net+inventory.net,gross))
    if lc>0 and sc>0 and lc+sc>gross:
        ratio=max(-1.0,min(1.0,inventory.net/limits.max_net))
        long_weight=max(0.05,1-ratio*capacity_skew_strength)
        short_weight=max(0.05,1+ratio*capacity_skew_strength)
        total=long_weight+short_weight
        lc=min(lc,gross*long_weight/total); sc=min(sc,gross*short_weight/total)
    return lc,sc
def build_quotes(book,inventory,limits,cfg,volatility_bps=0.0):
    validate(book,inventory,limits,cfg)
    if not math.isfinite(volatility_bps) or volatility_bps<0: raise ValueError("volatility_bps must be finite and non-negative")
    if volatility_bps>=cfg.shock_vol_bps: return []
    fair=fair_value(book,cfg)
    ratio=max(-1.0,min(1.0,inventory.net/limits.max_net))
    nonlinear=math.copysign(abs(ratio)**cfg.inventory_skew_power,ratio) if ratio else 0.0
    reservation=fair*(1-nonlinear*cfg.inventory_skew_bps/10000)
    vol_ratio=max(0.0,volatility_bps/max(cfg.elevated_vol_bps,1e-9))
    progress=min(1.0,vol_ratio*vol_ratio)
    spread_mult=1+(cfg.elevated_spread_multiplier-1)*progress
    size_mult=1-(1-cfg.elevated_size_multiplier)*progress
    layer_count=max(1,round(cfg.layers-(cfg.layers-1)*0.5*progress))
    half=fair*cfg.base_half_spread_bps*spread_mult/10000; gap=fair*cfg.level_gap_bps*spread_mult/10000
    lc,sc=opening_capacities(inventory,limits,cfg.capacity_skew_strength)
    desired=_floor_step(cfg.order_size*size_mult,cfg.lot_size); out=[]
    for level in range(layer_count):
        bid=min(reservation-half-level*gap,book.bid); ask=max(reservation+half+level*gap,book.ask)
        bs=_floor_step(min(desired,lc),cfg.lot_size); ss=_floor_step(min(desired,sc),cfg.lot_size)
        if bs>=cfg.lot_size: out.append(Quote(level,"buy",_floor_step(bid,cfg.tick_size),bs)); lc-=bs
        if ss>=cfg.lot_size: out.append(Quote(level,"sell",_ceil_step(ask,cfg.tick_size),ss)); sc-=ss
    return out
