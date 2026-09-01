#!/usr/bin/env python3
"""Adaptive bounded ZEC market maker with profitability and toxicity gates."""
from __future__ import annotations

import getpass, json, math, os, signal, statistics, time
from collections import deque
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from entropy_mm.edge import directional_pressure_bps, dynamic_order_size, profitability_edge
from entropy_mm.toxicity import FillObservation, MarkoutStore, MarkoutTracker

ACCOUNT=os.getenv("ENTROPY_ACCOUNT","0x78605485604BA45ce0eF860DB1594ec810154477")
AGENT=os.getenv("ENTROPY_AGENT","0x3B4347B99BB749eBdD6DE720736796E1b7Dfe4a6")
COIN=os.getenv("ENTROPY_COIN","ZEC")
BASE_ORDER_SIZE=float(os.getenv("ENTROPY_ORDER_SIZE","0.02")); LEVEL_SIZES=(BASE_ORDER_SIZE,)
LEVEL_SPREAD_BPS=(float(os.getenv("ENTROPY_BASE_HALF_SPREAD_BPS","35")),)
CAPITAL_BUDGET_USD=float(os.getenv("ENTROPY_CAPITAL_BUDGET_USD","20")); MAX_NET_SIZE=float(os.getenv("ENTROPY_MAX_NET_SIZE","0.0201")); MAX_LOSS_USD=float(os.getenv("ENTROPY_MAX_LOSS_USD","1.0"))
REFRESH_SECONDS=2; REQUOTE_THRESHOLD_BPS=5.0; HARD_REQUOTE_THRESHOLD_BPS=12.0; SAFE_CYCLES_TO_QUOTE=5; TOXIC_CYCLES_TO_HALT=2; MINIMUM_QUOTE_LIFETIME_SECONDS=8; POST_FILL_COOLDOWN_SECONDS=120
ROUND_TRIP_MAKER_FEE_BPS=3.0; MINIMUM_NET_PROFIT_BPS=8.0; FAST_MARKET_THRESHOLD_BPS=25.0; MEDIUM_TREND_THRESHOLD_BPS=45.0; TOXIC_BOOK_IMBALANCE=0.80; VOLATILITY_SPREAD_MULTIPLIER=2.5
TOXIC_WIDEN_THRESHOLD=0.45; TOXIC_PAUSE_THRESHOLD=0.80; MAX_TOXIC_BUFFER_BPS=15.0; INVENTORY_SKEW_BPS_AT_MAX=20.0; INVENTORY_SKEW_POWER=1.5; MICROPRICE_WEIGHT=0.65; MAX_MICROPRICE_EDGE_BPS=4.0
SOFT_EXIT_AGE_SECONDS=15; MAX_INVENTORY_AGE_SECONDS=35; MAX_INVENTORY_LOSS_USD=0.035; MAX_ADVERSE_MOVE_BPS=8.0; DAILY_PROFIT_LOCK_USD=0.10; MARKET_CLOSE_SLIPPAGE=0.005
MARKOUT_HORIZON_MS=5_000; TOXIC_MARKOUT_MEAN_BPS=-2.0; TOXIC_MARKOUT_MIN_SAMPLES=10; MARKOUT_PATH=os.getenv("ENTROPY_MARKOUT_PATH","runtime/entropy_markouts.sqlite3")
LOT_SIZE=float(os.getenv("ENTROPY_LOT_SIZE","0.01")); STRATEGY_RESET_ACK="adaptive-v3"
KEYSTORE=Path(__file__).resolve().parents[1]/"secrets"/"agent-keystore.json"; BUILDER={"b":"0xcd254d2a328f7f67c7c6fef930a4757516f7b601","f":0}; running=True

def stop(*_):
    global running; running=False

def read_password():
    path=os.environ.get("ENTROPY_CREDENTIAL_FILE"); return Path(path).read_text().rstrip("\r\n") if path else getpass.getpass("Keystore password: ")

def zec_position(info):
    for item in info.user_state(ACCOUNT).get("assetPositions",[]):
        p=item.get("position",{})
        if p.get("coin")==COIN and abs(float(p.get("szi",0) or 0))>0: return p
    return None

def zec_open_orders(info): return [o for o in info.frontend_open_orders(ACCOUNT) if o.get("coin")==COIN]
def cancel_orders(exchange,orders):
    for o in orders: print(json.dumps({"event":"cancel","oid":o["oid"],"result":exchange.cancel(COIN,int(o["oid"]))}),flush=True)
def cancel_all(exchange,info): cancel_orders(exchange,zec_open_orders(info))
def order_price_distance_bps(order,price):
    live=float(order.get("limitPx",0) or 0); return math.inf if live<=0 or price<=0 else abs(live/price-1)*10000
def order_matches(order,buy,size,price,reduce_only,threshold_bps=REQUOTE_THRESHOLD_BPS):
    return order.get("side")==("B" if buy else "A") and abs(float(order.get("sz",0) or 0)-size)<1e-9 and bool(order.get("reduceOnly",False))==reduce_only and order_price_distance_bps(order,price)<threshold_bps
def quote_pair_matches(orders,size,bid,ask): return len(orders)==2 and any(order_matches(o,True,size,bid,False) for o in orders) and any(order_matches(o,False,size,ask,False) for o in orders)
def opening_orders_match(orders,desired): return len(orders)==len(desired) and all(any(order_matches(o,b,s,p,False) for o in orders) for b,s,p in desired)
def opening_orders_hard_stale(orders,desired,threshold_bps=HARD_REQUOTE_THRESHOLD_BPS):
    for o in orders:
        if o.get("reduceOnly"): continue
        side=o.get("side"); candidates=[p for b,s,p in desired if side==("B" if b else "A") and abs(float(o.get("sz",0) or 0)-s)<1e-9]
        if not candidates or min(order_price_distance_bps(o,p) for p in candidates)>=threshold_bps: return True
    return False
def minimum_quote_lifetime_elapsed(orders,now_ms=None,minimum_seconds=MINIMUM_QUOTE_LIFETIME_SECONDS):
    if not orders:return True
    ts=[int(o.get("timestamp",0) or 0) for o in orders]
    if any(x<=0 for x in ts):return True
    now=int(time.time()*1000) if now_ms is None else now_ms; return all(now-x>=minimum_seconds*1000 for x in ts)
def exit_order_matches(orders,buy,size,price): return len(orders)==1 and order_matches(orders[0],buy,size,price,True)

def place(exchange,buy,size,price,reduce_only):
    result=exchange.order(COIN,buy,size,price,{"limit":{"tif":"Alo"}},reduce_only,builder=BUILDER); status=result.get("response",{}).get("data",{}).get("statuses",[{}])[0]
    print(json.dumps({"event":"place","side":"buy" if buy else "sell","size":size,"price":price,"reduce_only":reduce_only,"status":status}),flush=True)
    if isinstance(status,dict) and "resting" in status:return True
    err=status.get("error","") if isinstance(status,dict) else ""
    if "Post only order would have immediately matched" in err:return False
    raise RuntimeError(f"order rejected: {status}")

def passive_quotes(info,strategy_bid,strategy_ask):
    levels=info.l2_snapshot(COIN).get("levels",[])
    if len(levels)<2 or not levels[0] or not levels[1]:raise RuntimeError("ZEC order book has no two-sided BBO")
    bb=Decimal(str(levels[0][0]["px"])); ba=Decimal(str(levels[1][0]["px"])); tick=Decimal(os.getenv("ENTROPY_PRICE_TICK","0.1"))
    return float(min(Decimal(str(strategy_bid)),bb).quantize(tick,rounding=ROUND_FLOOR)),float(max(Decimal(str(strategy_ask)),ba).quantize(tick,rounding=ROUND_CEILING))
def inventory_exit_quotes(entry_price,szi,round_trip_fee_bps=ROUND_TRIP_MAKER_FEE_BPS,minimum_profit_bps=MINIMUM_NET_PROFIT_BPS):
    edge=(Decimal(str(round_trip_fee_bps))+Decimal(str(minimum_profit_bps)))/Decimal("10000"); entry=Decimal(str(entry_price))
    if szi>0:return None,float(entry*(Decimal("1")+edge))
    if szi<0:return float(entry*(Decimal("1")-edge)),None
    return None,None
def inventory_exit_strategy_price(entry_price,szi,best_bid,best_ask,age_seconds):
    bid,ask=inventory_exit_quotes(entry_price,szi)
    if age_seconds>=SOFT_EXIT_AGE_SECONDS:return best_ask if szi>0 else best_bid
    target=ask if szi>0 else bid
    if target is None:raise ValueError("inventory exit requires a non-zero position")
    return target
def adaptive_spread_bps(mids,base_spread_bps,round_trip_fee_bps=ROUND_TRIP_MAKER_FEE_BPS,minimum_profit_bps=MINIMUM_NET_PROFIT_BPS,volatility_multiplier=VOLATILITY_SPREAD_MULTIPLIER):
    floor=round_trip_fee_bps+minimum_profit_bps
    if len(mids)<2:return max(base_spread_bps,floor)
    r=[abs((b/a-1)*10000) for a,b in zip(mids,mids[1:]) if a>0]; return max(base_spread_bps,floor,(statistics.fmean(r)*volatility_multiplier if r else 0))
def rms_returns_bps(mids):
    r=[(b/a-1)*10000 for a,b in zip(mids,mids[1:]) if a>0]; return (sum(x*x for x in r)/len(r))**0.5 if r else 0.0
def microprice(best_bid,best_ask,bid_size,ask_size):
    if best_bid<=0 or best_ask<=best_bid:raise ValueError("invalid two-sided book")
    total=bid_size+ask_size; return (best_bid+best_ask)/2 if bid_size<=0 or ask_size<=0 or total<=0 else (best_ask*bid_size+best_bid*ask_size)/total
def capped_fair_value(best_bid,best_ask,bid_size,ask_size):
    mid=(best_bid+best_ask)/2; edge=microprice(best_bid,best_ask,bid_size,ask_size)-mid; cap=mid*MAX_MICROPRICE_EDGE_BPS/10000; return mid+MICROPRICE_WEIGHT*max(-cap,min(cap,edge))
def reservation_price(fair,inventory,max_inventory,skew_bps_at_max=INVENTORY_SKEW_BPS_AT_MAX):
    ratio=max(-1,min(1,inventory/max_inventory)) if max_inventory>0 else 0; nonlinear=math.copysign(abs(ratio)**INVENTORY_SKEW_POWER,ratio) if ratio else 0; return fair*(1-nonlinear*skew_bps_at_max/10000)
def book_toxicity_signal(bid_size,ask_size,widen_threshold=TOXIC_WIDEN_THRESHOLD,pause_threshold=TOXIC_PAUSE_THRESHOLD,max_buffer_bps=MAX_TOXIC_BUFFER_BPS):
    total=bid_size+ask_size
    if bid_size<=0 or ask_size<=0 or total<=0:return 0,0,0,False,False
    im=max(-1,min(1,(bid_size-ask_size)/total)); mag=abs(im); span=max(1e-9,pause_threshold-widen_threshold); buf=0 if mag<=widen_threshold else max_buffer_bps*min(1,(mag-widen_threshold)/span)
    return im,(buf if im<0 else 0),(buf if im>0 else 0),(im<0 and mag>=pause_threshold),(im>0 and mag>=pause_threshold)
def lighter_style_quotes(best_bid,best_ask,bid_size,ask_size,inventory,max_inventory,volatility_bps,bid_adverse_buffer_bps,ask_adverse_buffer_bps,base_half_spread_bps=LEVEL_SPREAD_BPS[0],required_edge_bps=0):
    fair=capped_fair_value(best_bid,best_ask,bid_size,ask_size); center=reservation_price(fair,inventory,max_inventory); mid=(best_bid+best_ask)/2; observed=(best_ask-best_bid)/mid*5000
    vol_ratio=max(0,volatility_bps/max(FAST_MARKET_THRESHOLD_BPS,1e-9)); dynamic_vol=volatility_bps*(1+min(2,vol_ratio*vol_ratio)); half=max(base_half_spread_bps,observed+ROUND_TRIP_MAKER_FEE_BPS+MINIMUM_NET_PROFIT_BPS,required_edge_bps,dynamic_vol)
    return min(center*(1-(half+bid_adverse_buffer_bps)/10000),best_bid),max(center*(1+(half+ask_adverse_buffer_bps)/10000),best_ask)
def directional_efficiency(mids):
    if len(mids)<2 or any(x<=0 for x in mids):return 0
    path=sum(abs((b/a-1)*10000) for a,b in zip(mids,mids[1:])); return 0 if path<=0 else min(1,abs((mids[-1]/mids[0]-1)*10000)/path)
def fast_market_active(mids,threshold_bps=FAST_MARKET_THRESHOLD_BPS,trend_efficiency=0.70): return len(mids)>=3 and mids[0]>0 and abs((mids[-1]/mids[0]-1)*10000)>=threshold_bps and directional_efficiency(mids)>=trend_efficiency
def medium_trend_active(mids,threshold_bps=MEDIUM_TREND_THRESHOLD_BPS,trend_efficiency=0.55): return len(mids)>=20 and mids[0]>0 and abs((mids[-1]/mids[0]-1)*10000)>=threshold_bps and directional_efficiency(mids)>=trend_efficiency
def toxic_order_book(bid_size,ask_size,threshold=TOXIC_BOOK_IMBALANCE):
    total=bid_size+ask_size
    if total<=0:return True
    ratio=bid_size/total; return ratio>=threshold or ratio<=1-threshold
def top_book_sizes(info,depth_levels=5):
    levels=info.l2_snapshot(COIN).get("levels",[])
    if len(levels)<2 or not levels[0] or not levels[1]:raise RuntimeError("ZEC order book has no two-sided liquidity")
    return sum(float(x.get("sz",0) or 0) for x in levels[0][:depth_levels]),sum(float(x.get("sz",0) or 0) for x in levels[1][:depth_levels])
def inventory_hard_exit_required(unrealized_pnl,age_seconds,adverse_move_bps=0,max_loss_usd=MAX_INVENTORY_LOSS_USD,max_age_seconds=MAX_INVENTORY_AGE_SECONDS,max_adverse_move_bps=MAX_ADVERSE_MOVE_BPS): return unrealized_pnl<=-abs(max_loss_usd) or age_seconds>=max_age_seconds or adverse_move_bps>=max_adverse_move_bps
def inventory_adverse_move_bps(entry_price,mark_price,szi):
    if entry_price<=0 or szi==0:return 0
    r=(mark_price/entry_price-1)*10000; return max(0,-r if szi>0 else r)
def inventory_age_seconds(fills,now_ms=None):
    now=int(time.time()*1000) if now_ms is None else now_ms; latest=max((int(f.get("time",0) or 0) for f in fills),default=now); return max(0,(now-latest)/1000)
def opening_gate_enabled(): return os.getenv("ENTROPY_ALLOW_NEW_OPENINGS","false").lower()=="true"
def strategy_reset_ack_enabled(): return os.getenv("ENTROPY_STRATEGY_RESET_ACK","")==STRATEGY_RESET_ACK
def post_fill_cooldown_active(fills,now_ms=None):
    if not fills:return False
    now=int(time.time()*1000) if now_ms is None else now_ms; return now-max(int(f.get("time",0) or 0) for f in fills)<POST_FILL_COOLDOWN_SECONDS*1000
def session_net_pnl(fills,unrealized_pnl=0): return sum(float(f.get("closedPnl",0) or 0)-float(f.get("fee",0) or 0) for f in fills)+unrealized_pnl
def loss_limit_reached(net_pnl,limit_usd=MAX_LOSS_USD): return net_pnl<=-abs(limit_usd)
def persistent_risk_halt_required(szi,net_pnl,profit_locked,max_net_size=MAX_NET_SIZE,max_loss_usd=MAX_LOSS_USD): return abs(szi)>max_net_size or loss_limit_reached(net_pnl,max_loss_usd) or profit_locked
def profit_lock_reached(net_pnl,target_usd=DAILY_PROFIT_LOCK_USD): return net_pnl>=abs(target_usd)
def trailing_24h_start_ms(): return int(time.time()*1000)-86400000
def flatten(exchange,info):
    p=zec_position(info)
    if p:
        szi=float(p["szi"]); print(json.dumps({"event":"flatten","szi":szi,"result":exchange.market_close(COIN,abs(szi),None,MARKET_CLOSE_SLIPPAGE,builder=BUILDER)}),flush=True)
def _fill_observation(fill):
    try:
        raw=str(fill.get("side","")); side="buy" if raw in {"B","buy","Buy"} else "sell" if raw in {"A","sell","Sell"} else ""; price=float(fill.get("px",fill.get("price",0)) or 0); size=float(fill.get("sz",fill.get("size",0)) or 0); t=int(fill.get("time",0) or 0); tid=str(fill.get("tid",fill.get("hash",f"{t}:{side}:{price}:{size}")))
        return FillObservation(tid,side,price,size,t) if side and price>0 and size>0 and t>0 else None
    except (TypeError,ValueError):return None

def main():
    key=Account.decrypt(json.loads(KEYSTORE.read_text()),read_password()); agent=Account.from_key(key)
    if agent.address.lower()!=AGENT.lower():raise SystemExit("agent mismatch")
    info=Info(constants.MAINNET_API_URL,skip_ws=True,timeout=30); role=info.user_role(agent.address)
    if role.get("role")!="agent" or role.get("data",{}).get("user","").lower()!=ACCOUNT.lower():raise SystemExit("agent authorization mismatch")
    exchange=Exchange(agent,constants.MAINNET_API_URL,account_address=ACCOUNT,timeout=30); exchange.update_leverage(1,COIN,True); signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    session_started=int(time.time()*1000); mids=deque(maxlen=31); tracker=MarkoutTracker(); store=MarkoutStore(MARKOUT_PATH); seen=set(); safe_cycles=0; toxic_cycles=0; opening_gate=opening_gate_enabled() and strategy_reset_ack_enabled(); risk_halted=False
    print(json.dumps({"event":"start","coin":COIN,"strategy":STRATEGY_RESET_ACK,"opening_gate":opening_gate}),flush=True)
    try:
        while running:
            now=int(time.time()*1000); p=zec_position(info); orders=zec_open_orders(info); mid=float(info.all_mids()[COIN]); mids.append(mid)
            szi=float(p["szi"]) if p else 0; upnl=float(p.get("unrealizedPnl",0)) if p else 0; fills=[f for f in info.user_fills_by_time(ACCOUNT,trailing_24h_start_ms()) if f.get("coin")==COIN]
            for f in fills:
                obs=_fill_observation(f)
                if obs and obs.time_ms>=session_started and obs.trade_id not in seen:seen.add(obs.trade_id);tracker.add_fill(obs)
            produced=tracker.observe(now,mid)
            if produced:store.record(produced,recorded_at_ms=now)
            toxicity=store.summary(MARKOUT_HORIZON_MS,last_n=100,toxic_mean_bps=TOXIC_MARKOUT_MEAN_BPS,min_samples=TOXIC_MARKOUT_MIN_SAMPLES)
            net=session_net_pnl(fills,upnl); cooldown=post_fill_cooldown_active(fills,now); age=inventory_age_seconds(fills,now) if p else 0; fast=fast_market_active(list(mids)); medium=medium_trend_active(list(mids)); history=len(mids)>=20
            book=info.l2_snapshot(COIN).get("levels",[])
            if len(book)<2 or not book[0] or not book[1]:raise RuntimeError("ZEC order book has no two-sided liquidity")
            bb=float(book[0][0]["px"]); ba=float(book[1][0]["px"]); bid_size=sum(float(x.get("sz",0) or 0) for x in book[0][:5]); ask_size=sum(float(x.get("sz",0) or 0) for x in book[1][:5])
            imbalance,bid_buf,ask_buf,raw_bid,raw_ask=book_toxicity_signal(bid_size,ask_size); toxic_cycles=toxic_cycles+1 if raw_bid or raw_ask else 0; persistent_book_toxic=toxic_cycles>=TOXIC_CYCLES_TO_HALT
            vol=rms_returns_bps(list(mids)); direction=directional_pressure_bps(list(mids)); edge=profitability_edge(volatility_bps=vol,book_imbalance=imbalance,markout_mean_bps=toxicity.mean_markout_bps,markout_negative_rate=toxicity.negative_rate,directional_bps=direction,round_trip_fee_bps=ROUND_TRIP_MAKER_FEE_BPS,minimum_profit_bps=MINIMUM_NET_PROFIT_BPS)
            pause_bid=(persistent_book_toxic and raw_bid) or edge.pause_bid; pause_ask=(persistent_book_toxic and raw_ask) or edge.pause_ask
            market_safe=history and not fast and not medium and not toxicity.toxic and not (pause_bid and pause_ask); safe_cycles=safe_cycles+1 if market_safe and not p else 0
            profit_locked=profit_lock_reached(net); adverse=inventory_adverse_move_bps(float(p["entryPx"]),mid,szi) if p else 0; action="none"
            if persistent_risk_halt_required(szi,net,profit_locked):risk_halted=True
            if p and inventory_hard_exit_required(upnl,age,adverse):cancel_orders(exchange,orders);flatten(exchange,info);action="hard_exit"
            elif szi>0:
                _,target=inventory_exit_quotes(float(p["entryPx"]),szi); target=ba if age>=SOFT_EXIT_AGE_SECONDS else target; _,ask=passive_quotes(info,mid,target)
                if exit_order_matches(orders,False,abs(szi),ask):action="retain_exit"
                else:cancel_orders(exchange,orders);place(exchange,False,abs(szi),ask,True);action="replace_exit"
            elif szi<0:
                target,_=inventory_exit_quotes(float(p["entryPx"]),szi); target=bb if age>=SOFT_EXIT_AGE_SECONDS else target; bid,_=passive_quotes(info,target,mid)
                if exit_order_matches(orders,True,abs(szi),bid):action="retain_exit"
                else:cancel_orders(exchange,orders);place(exchange,True,abs(szi),bid,True);action="replace_exit"
            elif not opening_gate or risk_halted or cooldown or not market_safe or safe_cycles<SAFE_CYCLES_TO_QUOTE:
                if orders:
                    urgent=risk_halted or cooldown or fast or medium or toxicity.toxic or (pause_bid and pause_ask)
                    if urgent or minimum_quote_lifetime_elapsed(orders):cancel_orders(exchange,orders);action="cancel_unsafe"
                    else:action="retain_unsafe_grace"
            else:
                size=dynamic_order_size(BASE_ORDER_SIZE,LOT_SIZE,edge.size_multiplier)
                if size<LOT_SIZE: desired=[]; action="edge_size_zero"
                else:
                    strategy_bid,strategy_ask=lighter_style_quotes(bb,ba,bid_size,ask_size,szi,MAX_NET_SIZE,vol,bid_buf+edge.bid_extra_bps,ask_buf+edge.ask_extra_bps,required_edge_bps=edge.required_edge_bps); bid,ask=passive_quotes(info,strategy_bid,strategy_ask); desired=[]
                    if not pause_bid:desired.append((True,size,bid))
                    if not pause_ask:desired.append((False,size,ask))
                    if mid*size>CAPITAL_BUDGET_USD:desired=[];action="capital_gate"
                if opening_orders_match(orders,desired):action="retain_quotes"
                elif orders and not opening_orders_hard_stale(orders,desired) and not minimum_quote_lifetime_elapsed(orders):action="retain_requote_grace"
                else:
                    cancel_orders(exchange,orders)
                    for buy,sz,px in desired:place(exchange,buy,sz,px,False)
                    if desired:action="replace_quotes"
            print(json.dumps({"event":"cycle","mid":mid,"szi":szi,"net_pnl":net,"quote_action":action,"volatility_bps":vol,"directional_bps":direction,"book_imbalance":imbalance,"required_edge_bps":edge.required_edge_bps,"edge_score":edge.score,"size_multiplier":edge.size_multiplier,"pause_bid":pause_bid,"pause_ask":pause_ask,"markout_count":toxicity.count,"markout_mean_bps":toxicity.mean_markout_bps,"markout_negative_rate":toxicity.negative_rate}),flush=True)
            for _ in range(REFRESH_SECONDS):
                if not running:break
                time.sleep(1)
    finally:
        cancel_all(exchange,info); print(json.dumps({"event":"stopped"}),flush=True)

if __name__=="__main__":main()
