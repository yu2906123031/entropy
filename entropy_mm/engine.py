"""Single-cycle risk gate and quote planner for Entropy."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .quote_model import Book,Inventory,Quote,QuoteConfig,RiskLimits,build_quotes
from .reconcile import LiveOrder,ReconcilePlan,reconcile_orders
class RiskMode(str,Enum): NORMAL="normal"; ELEVATED="elevated"; SHOCK="shock"; HALTED="halted"
@dataclass(frozen=True)
class EngineConfig:
    max_book_age_ms:int=3000; position_mismatch_tolerance:float=0.001; max_margin_usage_ratio:float=0.50; daily_loss_limit:float=10.0; min_order_lifetime_ms:int=5000; reprice_threshold_bps:float=2.0; hard_reprice_threshold_bps:float=8.0
@dataclass(frozen=True)
class CycleInput:
    now_ms:int; book_time_ms:int; book:Book; venue_inventory:Inventory; ledger_inventory:Inventory; open_orders:tuple[LiveOrder,...]=(); volatility_bps:float=0.0; margin_usage_ratio:float=0.0; daily_pnl:float=0.0
@dataclass(frozen=True)
class CycleDecision:
    mode:RiskMode; reason:str; quotes:tuple[Quote,...]; plan:ReconcilePlan
def _inventory_mismatch(a,b): return max(abs(a.long-b.long),abs(a.short-b.short))
def _cancel(c): return reconcile_orders(c.open_orders,())
def plan_cycle(cycle,quote_config,risk_limits,engine_config=EngineConfig()):
    age=cycle.now_ms-cycle.book_time_ms
    if age<0 or age>engine_config.max_book_age_ms: return CycleDecision(RiskMode.HALTED,"stale_book",(),_cancel(cycle))
    if _inventory_mismatch(cycle.venue_inventory,cycle.ledger_inventory)>engine_config.position_mismatch_tolerance: return CycleDecision(RiskMode.HALTED,"position_mismatch",(),_cancel(cycle))
    if cycle.margin_usage_ratio>=engine_config.max_margin_usage_ratio: return CycleDecision(RiskMode.HALTED,"margin_limit",(),_cancel(cycle))
    if cycle.daily_pnl<=-engine_config.daily_loss_limit: return CycleDecision(RiskMode.HALTED,"daily_loss_limit",(),_cancel(cycle))
    if cycle.volatility_bps>=quote_config.shock_vol_bps: return CycleDecision(RiskMode.SHOCK,"volatility_shock",(),_cancel(cycle))
    mode=RiskMode.ELEVATED if cycle.volatility_bps>=quote_config.elevated_vol_bps else RiskMode.NORMAL
    quotes=tuple(build_quotes(cycle.book,cycle.venue_inventory,risk_limits,quote_config,cycle.volatility_bps))
    plan=reconcile_orders(cycle.open_orders,quotes,now_ms=cycle.now_ms,min_order_lifetime_ms=engine_config.min_order_lifetime_ms,reprice_threshold_bps=engine_config.reprice_threshold_bps,hard_reprice_threshold_bps=engine_config.hard_reprice_threshold_bps)
    return CycleDecision(mode,"quotes_active",quotes,plan)
