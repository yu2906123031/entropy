import json
import time
from types import SimpleNamespace

import pytest

import entropy_bot.guardian as guardian_mod
import entropy_bot.live as live_mod
from entropy_bot.errors import LiveGuardError
from entropy_bot.guardian import run_guardian
from entropy_bot.intents import IntentWAL
from entropy_bot.live import LiveQuoter, RestSlot
from entropy_bot.quoting import BookTop, QuoteIntent, QuotePlan


COIN = "io:SNDK"
OK = {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
ERR = {"status": "err", "response": "rejected"}


def settings(tmp_path):
    return SimpleNamespace(
        private_key="0x01", account="0xabc", api_url="https://api.hyperliquid.xyz",
        state_dir=str(tmp_path), guardian_token_path=str(tmp_path / "token"),
        guardian_ready_path=str(tmp_path / "ready.json"), guardian_lease_path=str(tmp_path / "lease.json"),
        guardian_max_age=1.0, guardian_poll_s=1.0, max_leverage=1,
        min_replace_s=0.0, maker_rebate_bps=None, fee_tier=0, referral_discount=0,
        entropy_tier=1, entropy_self_rebate=0, entropy_referral_reward=0,
        entropy_referred_user_benefit=0, quote_notional_usd=12,
    )


def market():
    return SimpleNamespace(
        coin=COIN, asset_id=200002, max_leverage=10, sz_decimals=2,
        deployer_fee_scale=1.0, growth_mode=True,
    )


class Signer:
    account = "0xabc"
    def __init__(self, *_a, **_k): pass
    def signed_cancel_cloids(self, pairs): return {"cancel": pairs}
    def signed_update_leverage(self, _market, value): return {"leverage": value}
    def signed_schedule_cancel(self, deadline): return {"deadline": deadline}
    def signed_cancel_oids(self, pairs): return {"cancel_oids": pairs}
    def signed_orders(self, specs): return {"orders": specs}
    def next_cloid(self, *_a): return "0x45424f54000000000000000000000001"


class GuardianClient:
    def __init__(self, responses): self.responses = iter(responses); self.closed = False
    def open_orders(self, *_a, **_k):
        return [{"coin": COIN, "cloid": "0x45424f54000000000000000000000001"}]
    def post_exchange(self, _payload): return next(self.responses)
    def close(self): self.closed = True


def run_guardian_case(monkeypatch, tmp_path, responses):
    clock = {"now": 100.0}
    readiness = []
    client = GuardianClient(responses)
    monkeypatch.setattr(guardian_mod, "load_io_markets", lambda *_a: ({}, {COIN: market()}, {}))
    monkeypatch.setattr(guardian_mod, "lease_fresh", lambda *_a: False)
    monkeypatch.setattr(guardian_mod.time, "time", lambda: clock["now"])
    real_atomic = guardian_mod.atomic_json
    def record_atomic(path, row):
        if "ready" in row: readiness.append(row["ready"])
        real_atomic(path, row)
    monkeypatch.setattr(guardian_mod, "atomic_json", record_atomic)
    def sleep(_seconds): clock["now"] += 1
    rc = run_guardian(settings(tmp_path), seconds=20, client_factory=lambda _url: client,
                      signer_factory=Signer, sleep=sleep)
    return rc, json.loads((tmp_path / "ready.json").read_text()), readiness


def test_run_guardian_transient_failure_recovers(monkeypatch, tmp_path):
    # startup, failed cycle, recovered cycle, then three failures to terminate, final cancel succeeds.
    rc, ready, readiness = run_guardian_case(monkeypatch, tmp_path, [OK, ERR, OK, ERR, ERR, ERR, OK])
    assert rc == 2
    assert readiness[:2] == [False, True]
    assert ready["ready"] is False


def test_run_guardian_final_cancel_failure_is_nonzero(monkeypatch, tmp_path):
    rc, _, _ = run_guardian_case(monkeypatch, tmp_path, [OK, ERR, ERR, ERR, ERR])
    assert rc == 2


def quoter(tmp_path, client):
    q = LiveQuoter(settings(tmp_path), {COIN: market()}, client, Signer())
    q.position_known = True
    q.open_orders_known = True
    q._fee_banner = lambda _coin: None
    return q


def flat_state(): return {"assetPositions": []}
def position_state(leverage="isolated", value=1):
    return {"assetPositions": [{"position": {"coin": COIN, "szi": "1", "leverage": {"type": leverage, "value": value}}}]}


class Client:
    def __init__(self):
        self.posts = []
        self.state = flat_state()
        self.opens = []
    def post_exchange(self, payload): self.posts.append(payload); return OK
    def clearinghouse_state(self, *_a, **_k): return self.state
    def open_orders(self, *_a, **_k): return self.opens
    def user_fills(self, *_a, **_k): return []
    def order_status(self, *_a, **_k): return {"status": "canceled"}
    def l2_book(self, _coin):
        return {"time": int(time.time() * 1000), "levels": [[{"px": "9.9", "sz": "5"}], [{"px": "10.1", "sz": "5"}]]}


def test_leverage_update_error_blocks_and_flat_response_persists_proof(tmp_path):
    client = Client(); q = quoter(tmp_path, client)
    client.post_exchange = lambda _payload: ERR
    with pytest.raises(LiveGuardError, match="leverage update rejected"):
        q.bootstrap_isolated()
    assert not (tmp_path / "leverage-proof.json").exists()

    client.post_exchange = lambda payload: (client.posts.append(payload) or OK)
    q.bootstrap_isolated()
    proof = json.loads((tmp_path / "leverage-proof.json").read_text())
    assert proof["proofs"][COIN]["account"] == "0xabc"
    assert proof["proofs"][COIN]["value"] == 1
    assert proof["proofs"][COIN]["response_timestamp"] > 0
    assert not q._isolated_leverage_verified(flat_state())


def test_first_position_leverage_mismatch_immediately_ioc_flattens_and_aborts(tmp_path):
    client = Client(); client.state = position_state("cross", 1)
    q = quoter(tmp_path, client)
    q._feed = None
    q.cancel_coin_rests = lambda **_k: {"fetched": True}
    placed = []
    original = q._place
    q._place = lambda coin, plan: (placed.append(plan) or {"resp": OK})
    assert q.refresh_positions(force=True)
    assert q.leverage_blocked
    with pytest.raises(LiveGuardError, match="emergency flatten"):
        q.enforce_position_leverage()
    assert len(placed) == 1 and placed[0].take and placed[0].intents[0].reduce_only
    q._place = original


def maker_plan():
    intent = QuoteIntent(COIN, 200002, True, 9.9, 1.0, "B")
    return QuotePlan(COIN, "flat", "flat", "Alo", False, [intent], 0, 0, buy_px=9.9)


def test_place_blocks_missing_guardian_unknown_position_and_external_orders(tmp_path):
    client = Client(); q = quoter(tmp_path, client)
    q.guardian_ready = lambda: False
    assert q._place(COIN, maker_plan())["blocked"] == "safetyProtectionUnavailable"
    q.guardian_ready = lambda: True; q.position_known = False
    assert q._place(COIN, maker_plan())["blocked"] == "safetyProtectionUnavailable"
    q.position_known = True; q.external_orders = [{"oid": 7}]
    assert q._place(COIN, maker_plan())["blocked"] == "openOrdersUnknown"


def test_reconcile_external_order_blocks_and_unknown_submit_recovers_from_wal(tmp_path):
    client = Client(); q = quoter(tmp_path, client)
    client.opens = [{"coin": COIN, "oid": 7, "side": "B", "cloid": "external"}]
    assert not q.reconcile_orders()

    client.opens = []
    assert q.reconcile_orders(force=True)
    wal = IntentWAL(tmp_path / "intents.db"); q.intent_wal = wal
    q.guardian_ready = lambda: True
    client.post_exchange = lambda _payload: (_ for _ in ()).throw(ConnectionError("unknown submit"))
    with pytest.raises(ConnectionError): q._place(COIN, maker_plan())
    pending = wal.pending(); assert len(pending) == 1
    client.opens = [{"coin": COIN, "oid": 99, "side": "B", "cloid": pending[0]["cloid"]}]
    assert q.reconcile_orders(force=True)
    assert wal.pending() == []
    wal.close()


def test_cancel_deadman_and_shutdown_failure_paths(tmp_path, monkeypatch):
    client = Client(); q = quoter(tmp_path, client)
    q.rests[COIN]["B"] = RestSlot(oid=7, cloid="0x45424f54000000000000000000000001")
    client.post_exchange = lambda _payload: ERR
    result = q.cancel_coin_rests(refetch=False)
    assert result["rateLimited"] and q.rests[COIN]["B"].occupied()
    with pytest.raises(LiveGuardError): q.schedule_deadman()

    q.cancel_coin_rests = lambda **_k: {"rateLimited": False}
    q.refresh_positions = lambda **_k: True
    q.pos = {COIN: 1.0}
    q.book_for = lambda *_a: None
    monkeypatch.setattr("entropy_bot.live.time.sleep", lambda _s: None)
    assert q.shutdown_cancels() is False


def test_run_live_directly_blocks_external_orders(monkeypatch, tmp_path):
    cfg = settings(tmp_path)
    cfg.live = True; cfg.coins = (COIN,); cfg.ws_url = "wss://api.hyperliquid.xyz/ws"
    cfg.intent_db_path = str(tmp_path / "run-live.db")
    class RunSigner(Signer):
        signer_address = "0xabc"; is_agent = False
    class RunClient(Client):
        def __init__(self, _url, **_kwargs): super().__init__(); self.opens = [{"coin": COIN, "cloid": "external"}]
        def perp_dexs(self): return [{"name": "io", "fullName": "EntropyIO"}]
        def close(self): pass
    class Lock:
        def __init__(self, *_a): pass
        def close(self): pass
    class Wal:
        def __init__(self, *_a): pass
        def prune_terminal(self, *_a): return 0
        def reconcile_open_orders(self, *_a): return set()
        def close(self): pass
    monkeypatch.setattr(live_mod, "LiveSigner", RunSigner)
    monkeypatch.setattr(live_mod, "InfoClient", RunClient)
    monkeypatch.setattr(live_mod, "AccountLock", Lock)
    monkeypatch.setattr(live_mod, "IntentWAL", Wal)
    monkeypatch.setattr(live_mod, "load_io_markets", lambda *_a: ({}, {COIN: market()}, {}))
    monkeypatch.setattr(live_mod, "perp_dex_info", lambda *_a: {"name": "io", "fullName": "EntropyIO"})
    with pytest.raises(LiveGuardError, match="external open orders block startup"):
        live_mod.run_live(cfg, seconds=1)
