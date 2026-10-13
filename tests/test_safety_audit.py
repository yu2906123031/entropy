import json
import math
import os
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from entropy_bot.cli import build_parser
from entropy_bot.config import load_settings
from entropy_bot.errors import LiveGuardError
from entropy_bot.fees import estimate_fees, estimate_from_settings
from entropy_bot.guardian import cancel_ebot, final_return_code, health_transition
from entropy_bot.intents import IntentWAL
from entropy_bot.live import LiveQuoter, RestSlot, apply_open_orders
from entropy_bot.orders import action_statuses_ok
from entropy_bot.precision import ceil_to_tick, floor_size, floor_to_tick, round_price, tick_size, wire_number
from entropy_bot.safety import AccountLock, NonceStore, atomic_json, fresh_guardian
from entropy_bot.ws import BookFeed


def clean_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("HYPERLIQUID_", "ENTROPY_", "GUARDIAN_", "OUTBOUND_", "QUOTE_", "MAX_", "MIN_", "COINS", "LIVE", "FEE_", "REFERRAL_", "MAKER_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENTROPY_INTENT_DB", str(tmp_path / "intents.db"))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_precision_rejects_nonfinite(value):
    for fn, args in [(tick_size, (value, 2)), (round_price, (value, 2)), (floor_size, (value, 2)),
                     (wire_number, (value,)), (floor_to_tick, (value, 0.1)), (ceil_to_tick, (value, 0.1))]:
        with pytest.raises(ValueError):
            fn(*args)


@pytest.mark.parametrize("cmd", ["paper", "live", "guardian"])
@pytest.mark.parametrize("raw", ["nan", "inf", "-1", "0"])
def test_seconds_rejects_invalid(cmd, raw):
    with pytest.raises(SystemExit):
        build_parser().parse_args([cmd, "--seconds", raw])


def test_public_commands_skip_key_decryption(monkeypatch, tmp_path):
    clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ENTROPY_KEYSTORE_PATH", "/missing")
    monkeypatch.setenv("ENTROPY_CREDENTIAL_FILE", "/missing")
    assert load_settings(load_private_key=False).private_key is None
    with pytest.raises(Exception):
        load_settings(load_private_key=True)


def test_safe_defaults(monkeypatch, tmp_path):
    clean_env(monkeypatch, tmp_path)
    s = load_settings(load_private_key=False)
    assert s.coins == ("io:SNDK",)
    assert s.quote_notional_usd == 12
    assert s.max_leverage == 1
    assert s.fee_tier == 0


def test_nonce_persists_and_coordinates(tmp_path):
    a = NonceStore("0xabc", tmp_path).next(100)
    b = NonceStore("0xabc", tmp_path).next(100)
    assert b == a + 1


def test_account_lock_is_account_dex_scoped(tmp_path):
    first = AccountLock("0xabc", tmp_path)
    try:
        with pytest.raises(LiveGuardError):
            AccountLock("0xabc", tmp_path)
        other = AccountLock("0xdef", tmp_path)
        other.close()
    finally:
        first.close()


def test_guardian_heartbeat_requires_ready_matching_and_sane_time(tmp_path):
    p = tmp_path / "ready.json"
    token = tmp_path / "token"
    token.write_text("secret")
    pid = os.getpid()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    start = Path(f"/proc/{pid}/stat").read_text().split()[21]
    row = {"ready": True, "account": "0xAbC", "dex": "io", "heartbeat": 100.0,
           "pid": pid, "boot_id": boot, "start_id": start, "session_token": "secret"}
    atomic_json(p, row)
    assert fresh_guardian(p, "0xabc", max_age=5, token_path=token, now=103)
    assert not fresh_guardian(p, "0xabc", max_age=5, now=110)
    row["heartbeat"] = 999.0
    atomic_json(p, row)
    assert not fresh_guardian(p, "0xabc", max_age=5, now=100)
    row.update(heartbeat=100.0, session_token="old")
    atomic_json(p, row)
    assert not fresh_guardian(p, "0xabc", max_age=5, token_path=token, now=100)


def test_ws_only_book_message_refreshes_book_freshness():
    seen = []
    feed = BookFeed("wss://api.hyperliquid.xyz/ws", ("io:SNDK",), lambda c, d: seen.append(c))
    feed._connected = True
    feed._on_message(None, json.dumps({"channel": "pong", "data": {}}))
    assert feed.latest("io:SNDK") is None
    now_ms = int(time.time() * 1000)
    feed._on_message(None, json.dumps({"channel": "l2Book", "data": {"coin": "io:SNDK", "time": now_ms, "levels": [[], []]}}))
    assert seen == ["io:SNDK"]


def test_future_book_does_not_poison_sequence():
    feed = BookFeed("wss://api.hyperliquid.xyz/ws", ("io:SNDK",), lambda *_: None)
    future = int((time.time() + 100) * 1000)
    feed._on_message(None, json.dumps({"channel": "l2Book", "data": {"coin": "io:SNDK", "time": future}}))
    assert feed.latest("io:SNDK") is None


def test_cancel_semantics_require_every_status():
    assert action_statuses_ok({"status": "ok", "response": {"data": {"statuses": ["success"]}}}, 1)
    assert not action_statuses_ok({"status": "ok", "response": {"data": {"statuses": [{"error": "x"}]}}}, 1)
    assert not action_statuses_ok({"status": "err"}, 1)


def test_wal_prunes_only_terminal(tmp_path):
    wal = IntentWAL(tmp_path / "wal.db")
    try:
        wal.add_pending("a", "io:SNDK", "B", {})
        wal.add_pending("b", "io:SNDK", "A", {})
        wal.resolve("b", "cancelled")
        assert wal.prune_terminal(time.time() + 1) == 1
        assert [x["cloid"] for x in wal.pending()] == ["a"]
    finally:
        wal.close()


def test_wal_unknown_oid_resolves_never_accepted_intent(tmp_path):
    wal = IntentWAL(tmp_path / "wal.db")
    try:
        wal.add_pending("missing", "io:SNDK", "B", {})
        unresolved = wal.reconcile_history(
            ["missing"], [], lambda _cloid: {"status": "unknownOid"}
        )
        assert unresolved == set()
        assert wal.pending() == []
    finally:
        wal.close()


def test_fee_is_conservative_and_never_claims_negative():
    fee = estimate_fees(tier=0, entropy_self_rebate=100)
    assert fee.taker_pct == 0
    assert fee.maker_pct >= 0


def test_live_rejects_unverified_fee_discount(monkeypatch, tmp_path):
    clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("REFERRAL_DISCOUNT", ".1")
    with pytest.raises(Exception, match="conservative gross Tier0"):
        load_settings(load_private_key=False)


def test_fee_from_settings_forces_gross_tier0():
    market = SimpleNamespace(deployer_fee_scale=1.0, growth_mode=True)
    fee = estimate_from_settings(market, SimpleNamespace(
        fee_tier=6, referral_discount=.9, maker_rebate_bps=10,
        entropy_tier=4, entropy_self_rebate=10, entropy_referral_reward=1,
        entropy_referred_user_benefit=10))
    assert fee.tier == 0 and fee.taker_pct == fee.gross_taker_pct
    assert fee.maker_rebate_bps is None


def test_flat_leverage_requires_current_update_proof_and_existing_rows_are_strict():
    q = object.__new__(LiveQuoter)
    q.settings = SimpleNamespace(max_leverage=1)
    q.markets = {"io:SNDK": SimpleNamespace(max_leverage=20)}
    q.leverage_proof = {}
    assert not q._isolated_leverage_verified({"assetPositions": []})
    assert not q._isolated_leverage_verified({"assetPositions": []}, allow_flat_proof=True)
    q.leverage_proof = {"io:SNDK": {"value": 1}}
    assert q._isolated_leverage_verified({"assetPositions": []}, allow_flat_proof=True)
    assert q._isolated_leverage_verified({"assetPositions": [{"position": {
        "coin": "io:SNDK", "leverage": {"type": "isolated", "value": 1}}}]})
    assert not q._isolated_leverage_verified({"assetPositions": [{"position": {
        "coin": "io:SNDK", "leverage": {"type": "cross", "value": 1}}}]})


def test_rest_book_requires_fresh_exchange_timestamp():
    q = object.__new__(LiveQuoter)
    now = int(time.time() * 1000)
    class Client:
        value = {}
        def l2_book(self, _coin): return self.value
    q.client = Client(); q.books = {}; q._lock = __import__("threading").Lock()
    for bad in ({"levels": [[], []]}, {"time": now - 60_000, "levels": [[], []]},
                {"time": now + 6_000, "levels": [[], []]}):
        q.client.value = bad
        q.books = {}
        assert q.book_for("io:SNDK", None) is None


def test_guardian_cancels_all_allowed_markets_despite_coin_scope():
    rows = [{"coin": "io:ANTH", "cloid": "0x45424f54000000000000000000000001"},
            {"coin": "io:SNDK", "cloid": "0x45424f54000000000000000000000002"}]
    class Client:
        def open_orders(self, *_a, **_k): return rows
        def post_exchange(self, payload): self.payload = payload; return {
            "status": "ok", "response": {"data": {"statuses": ["success", "success"]}}}
    signer = SimpleNamespace(account="0xabc", signed_cancel_cloids=lambda pairs: {"pairs": pairs})
    markets = {coin: SimpleNamespace(asset_id=i) for i, coin in enumerate(("io:ANTH", "io:SNDK"), 1)}
    client = Client()
    assert cancel_ebot(client, signer, markets)
    assert len(client.payload["pairs"]) == 2


def test_guardian_transient_failure_recovers_and_consecutive_failures_exit():
    rc, failures, exhausted = health_transition(False, 0)
    assert (rc, failures, exhausted) == (2, 1, False)
    assert health_transition(True, failures) == (0, 0, False)
    state = (0, 0, False)
    for _ in range(3):
        state = health_transition(False, state[1])
    assert state == (2, 3, True)


def test_guardian_final_cancel_failure_always_returns_nonzero():
    assert final_return_code(0, False) == 2
    assert final_return_code(2, True) == 2
