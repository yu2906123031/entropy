import logging
import time
from types import SimpleNamespace

import pytest

from entropy_bot.config import load_settings
from entropy_bot.errors import RateLimited
from entropy_bot.live import LiveQuoter, RestSlot
from entropy_bot.coins import Market
from entropy_bot.quoting import BookTop, QuoteIntent, QuotePlan, flatten_stage, quote_plan
from entropy_bot.rest import InfoClient

COIN = "io:SNDK"


class Response:
    def __init__(self, status, *, headers=None, data=None):
        self.status_code = status
        self.headers = headers or {}
        self._data = data
        self.content = b"{}" if data is not None else b""
        self.url = "https://api.hyperliquid.xyz/info"
        self.ok = status < 400
        self.text = "rate limited" if status == 429 else ""

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.trust_env = True

    def post(self, *_a, **_k):
        self.calls += 1
        return next(self.responses)

    def close(self):
        pass


def test_info_client_retry_after_stops_all_optional_calls(monkeypatch, caplog):
    now = {"v": 100.0}
    monkeypatch.setattr("entropy_bot.rest.time.monotonic", lambda: now["v"])
    client = InfoClient("https://api.hyperliquid.xyz", info_min_interval_s=0,
                        cooldown_base_s=5, cooldown_max_s=60)
    client.session = Session([Response(429, headers={"Retry-After": "30"})])
    with caplog.at_level(logging.WARNING):
        assert client.user_fills("0xabc", optional=True) is None
        assert client.open_orders("0xabc", optional=True) is None
    assert client.session.calls == 1
    assert client.info_cooldown_active()
    assert sum("info 429" in row.message for row in caplog.records) == 1
    with pytest.raises(RateLimited):
        client.clearinghouse_state("0xabc")
    now["v"] += 31
    assert not client.info_cooldown_active()


def test_info_client_exponential_cooldown_without_retry_after(monkeypatch):
    now = {"v": 1.0}
    monkeypatch.setattr("entropy_bot.rest.time.monotonic", lambda: now["v"])
    client = InfoClient("https://api.hyperliquid.xyz", info_min_interval_s=0,
                        cooldown_base_s=7, cooldown_max_s=60)
    client.session = Session([Response(429), Response(429)])
    assert client.post_info({"type": "x"}, optional=True) is None
    assert client.info_cooldown_remaining() == pytest.approx(7)
    now["v"] += 8
    assert client.post_info({"type": "x"}, optional=True) is None
    assert client.info_cooldown_remaining() == pytest.approx(14)


def test_flatten_ladder_defaults_and_configuration(monkeypatch, tmp_path):
    assert flatten_stage(59_999, 1, far_ms=60_000, take_ms=120_000) == "far"
    assert flatten_stage(60_000, 1, far_ms=60_000, take_ms=120_000) == "mid"
    assert flatten_stage(119_999, 1, far_ms=60_000, take_ms=120_000) == "mid"
    assert flatten_stage(120_000, 1, far_ms=60_000, take_ms=120_000) == "take"
    monkeypatch.setenv("ENTROPY_INTENT_DB", str(tmp_path / "intents.db"))
    monkeypatch.setenv("FLATTEN_FAR_S", "75")
    monkeypatch.setenv("FLATTEN_TAKE_S", "110")
    settings = load_settings(load_private_key=False)
    assert settings.flatten_far_s == 75
    assert settings.flatten_take_s == 110


def test_maker_exit_requires_two_tick_edge_then_ioc_can_cross():
    market = Market(COIN, 200002, 0, 10, 4, 10, True, "isolated", True, 1.0, False, 0)
    top = BookTop(COIN, 1588.8, 1588.9, 1, 1, 1588.85, 1,
                  bid_px="1588.8", ask_px="1588.9")
    maker = quote_plan(market, top, szi=0.0075, age_ms=60_000,
                       notional_usd=12, entry_px=1588.8, min_exit_edge_ticks=2)
    assert maker.tif == "Alo"
    assert maker.sell_px == pytest.approx(1589.0)
    take = quote_plan(market, top, szi=0.0075, age_ms=120_000,
                      notional_usd=12, entry_px=1588.8, min_exit_edge_ticks=2)
    assert take.tif == "Ioc"
    assert take.sell_px == pytest.approx(1588.8)


def test_soft_ioc_waits_on_large_loss_and_hard_deadline_exits():
    market = Market(COIN, 200002, 0, 10, 4, 10, True, "isolated", True, 1.0, False, 0)
    top = BookTop(COIN, 1588.0, 1588.1, 1, 1, 1588.05, 1,
                  bid_px="1588.0", ask_px="1588.1")
    waiting = quote_plan(
        market, top, szi=0.0075, age_ms=900_000, notional_usd=20,
        flatten_far_ms=120_000, flatten_take_ms=900_000,
        flatten_hard_ms=3_600_000, ioc_max_loss_ticks=2,
        entry_px=1588.8, min_exit_edge_ticks=1,
    )
    assert waiting.tif == "Alo"
    assert not waiting.take
    assert waiting.sell_px == pytest.approx(1588.9)
    hard = quote_plan(
        market, top, szi=0.0075, age_ms=3_600_000, notional_usd=20,
        flatten_far_ms=120_000, flatten_take_ms=900_000,
        flatten_hard_ms=3_600_000, ioc_max_loss_ticks=2,
        entry_px=1588.8, min_exit_edge_ticks=1,
    )
    assert hard.tif == "Ioc"
    assert hard.take
    assert hard.sell_px == pytest.approx(1588.0)


def test_hard_ioc_can_exit_one_quarter_of_cycle_notional():
    market = Market(COIN, 200002, 0, 10, 4, 10, True, "isolated", True, 1.0, False, 0)
    top = BookTop(COIN, 2000.0, 2000.1, 1, 1, 2000.05, 1,
                  bid_px="2000.0", ask_px="2000.1")
    plan = quote_plan(
        market, top, szi=0.05, age_ms=3_600_000, notional_usd=100,
        flatten_far_ms=120_000, flatten_take_ms=900_000,
        flatten_hard_ms=3_600_000, hard_ioc_fraction=0.25,
        ioc_max_loss_ticks=2, entry_px=2010.0, min_exit_edge_ticks=1,
    )
    assert plan.take
    assert plan.tif == "Ioc"
    assert plan.intents[0].reduce_only
    assert plan.intents[0].sz == pytest.approx(0.0125)


def test_stop_loss_ignores_hard_ioc_tranche_fraction():
    market = Market(COIN, 200002, 0, 10, 4, 10, True, "isolated", True, 1.0, False, 0)
    top = BookTop(COIN, 1979.8, 1979.9, 1, 1, 1979.85, 1,
                  bid_px="1979.8", ask_px="1979.9")
    plan = quote_plan(
        market, top, szi=0.05, age_ms=1_000, notional_usd=100,
        flatten_far_ms=120_000, flatten_take_ms=900_000,
        flatten_hard_ms=3_600_000, hard_ioc_fraction=0.25,
        stop_loss_bps=100, entry_px=2000.0, min_exit_edge_ticks=1,
    )
    assert plan.take
    assert plan.intents[0].sz == pytest.approx(0.05)


def test_stop_loss_forces_immediate_reduce_only_ioc():
    market = Market(COIN, 200002, 0, 10, 4, 10, True, "isolated", True, 1.0, False, 0)
    top = BookTop(COIN, 1572.8, 1572.9, 1, 1, 1572.85, 1,
                  bid_px="1572.8", ask_px="1572.9")
    plan = quote_plan(
        market, top, szi=0.01, age_ms=1_000, notional_usd=100,
        flatten_far_ms=120_000, flatten_take_ms=900_000,
        flatten_hard_ms=3_600_000, ioc_max_loss_ticks=2,
        stop_loss_bps=100, entry_px=1588.8, min_exit_edge_ticks=1,
    )
    assert plan.take
    assert plan.stop_loss
    assert plan.tif == "Ioc"
    assert plan.intents[0].reduce_only
    assert plan.sell_px == pytest.approx(1572.8)


def test_opening_blocked_during_info_cooldown_reduce_only_allowed(tmp_path):
    class Client:
        def info_cooldown_active(self): return True
        def post_exchange(self, _payload):
            return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 7}}]}}}
    class Signer:
        account = "0xabc"
        def next_cloid(self, *_a): return "0x45424f54000000000000000000000001"
        def signed_orders(self, specs): return {"orders": specs}
    settings = SimpleNamespace(
        quote_notional_usd=12, max_leverage=1, min_replace_s=15,
        flatten_far_s=60, flatten_take_s=120, position_reconcile_s=10,
        open_orders_reconcile_s=15, fill_reconcile_s=60, deadman_retry_s=300,
        shutdown_cancel_attempts=3, shutdown_retry_s=2, maker_rebate_bps=None,
        fee_tier=0, referral_discount=0, entropy_tier=1, entropy_self_rebate=0,
        entropy_referral_reward=0, entropy_referred_user_benefit=0,
        guardian_ready_path=str(tmp_path / "ready"), guardian_token_path=str(tmp_path / "token"),
        guardian_lease_path=str(tmp_path / "lease"), guardian_max_age=5, state_dir=str(tmp_path),
    )
    market = SimpleNamespace(coin=COIN, asset_id=200002, max_leverage=10, sz_decimals=2,
                             deployer_fee_scale=1.0, growth_mode=True)
    q = LiveQuoter(settings, {COIN: market}, Client(), Signer())
    q.position_known = q.open_orders_known = True
    q.guardian_ready = lambda: True
    q._fee_banner = lambda _coin: None
    opening = QuotePlan(COIN, "flat", "flat", "Alo", False,
                        [QuoteIntent(COIN, 200002, True, 9.9, 1, "B")], 0, 0)
    assert q._place(COIN, opening)["blocked"] == "infoCooldown"
    closing = QuotePlan(COIN, "long", "take", "Ioc", True,
                        [QuoteIntent(COIN, 200002, False, 9.9, 1, "A", "Ioc", True)], 120000, 1)
    assert "resp" in q._place(COIN, closing)


def test_ws_fill_marks_position_unknown_for_immediate_reconcile(tmp_path):
    q = object.__new__(LiveQuoter)
    q.diag = None
    q._fill_reconcile_pending = False
    q.position_known = True
    q.on_user_fills({"fills": [{"coin": COIN}], "isSnapshot": False})
    assert q._fill_reconcile_pending
    assert not q.position_known
    q._fill_reconcile_pending = False
    q.position_known = True
    q.on_user_fills({"fills": [{"coin": COIN}], "isSnapshot": True})
    assert not q._fill_reconcile_pending
    assert q.position_known


def test_opening_notional_is_sampled_inside_configured_range():
    q = object.__new__(LiveQuoter)
    q.settings = SimpleNamespace(
        quote_notional_usd=25,
        quote_notional_min_usd=20,
        quote_notional_max_usd=30,
    )
    q._notional_rng = __import__("random").Random(7)
    q.notional = 0
    values = []
    for _ in range(20):
        q._sample_opening_notional()
        values.append(q.notional)
    assert all(20 <= value <= 30 for value in values)
    assert len(set(values)) > 1


def test_deadman_unavailable_uses_long_retry_interval(tmp_path, monkeypatch):
    class Client:
        def post_exchange(self, _payload): return {"status": "err"}
    class Signer:
        account = "0xabc"
        def signed_schedule_cancel(self, deadline): return {"deadline": deadline}
    settings = SimpleNamespace(quote_notional_usd=12, min_replace_s=15, deadman_retry_s=300)
    q = object.__new__(LiveQuoter)
    q.settings = settings; q.client = Client(); q.signer = Signer()
    q._weight_backoff_until = 0; q._weight_logged = False; q.deadman_until = 0; q.last_deadman = 0
    q._deadman_retry_at = 0; q._deadman_unavailable_logged = False
    now = time.time()
    q.maybe_deadman()
    assert q._deadman_retry_at >= now + 299
    q.maybe_deadman()
    assert q._deadman_retry_at >= now + 299
