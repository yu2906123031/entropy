from __future__ import annotations

import os
import json
import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from eth_account import Account

from dotenv import load_dotenv

from entropy_bot.coins import ALLOWED_COINS, validate_coin_list
from entropy_bot.errors import ConfigError, LiveGuardError
from entropy_bot.fees import (
    DEFAULT_ENTROPY_REFERRAL_REWARD,
    DEFAULT_ENTROPY_REFERRED_USER_BENEFIT,
    DEFAULT_ENTROPY_SELF_REBATE,
    DEFAULT_ENTROPY_TIER,
    DEFAULT_FEE_TIER,
    entropy_program,
    rates_for_tier,
)

DEFAULT_API_URL = "https://api.hyperliquid.xyz"
DEFAULT_WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_NOTIONAL = 12.0
DEFAULT_OFFSET_TICKS = 2
DEFAULT_MAX_LEVERAGE = 1
DEFAULT_MIN_REPLACE_S = 12.0
DEFAULT_FLATTEN_FAR_S = 60.0
DEFAULT_FLATTEN_TAKE_S = 120.0
DEFAULT_MIN_EXIT_EDGE_TICKS = 2
MIN_ORDER_USD = 10.0
DEFAULT_INTENT_DB_PATH = "~/.local/state/entropy-bot/intents.sqlite3"


@dataclass(frozen=True)
class Settings:
    live: bool
    private_key: str | None
    account: str | None
    coins: tuple[str, ...]
    quote_notional_usd: float
    quote_offset_ticks: int
    max_leverage: int
    api_url: str
    ws_url: str
    fee_tier: int = DEFAULT_FEE_TIER
    referral_discount: float = 0.0
    maker_rebate_bps: float | None = None
    entropy_tier: int = DEFAULT_ENTROPY_TIER
    entropy_self_rebate: float = DEFAULT_ENTROPY_SELF_REBATE
    entropy_referral_reward: float = DEFAULT_ENTROPY_REFERRAL_REWARD
    entropy_referred_user_benefit: float = DEFAULT_ENTROPY_REFERRED_USER_BENEFIT
    min_replace_s: float = DEFAULT_MIN_REPLACE_S
    intent_db_path: str = DEFAULT_INTENT_DB_PATH
    state_dir: str = "~/.local/state/entropy-bot"
    guardian_ready_path: str = "~/.local/state/entropy-bot/guardian-ready.json"
    guardian_lease_path: str = "~/.local/state/entropy-bot/main-lease.json"
    guardian_token_path: str = "~/.local/state/entropy-bot/guardian-session.token"
    guardian_max_age: float = 5.0
    guardian_poll_s: float = 1.0
    flatten_far_s: float = DEFAULT_FLATTEN_FAR_S
    flatten_take_s: float = DEFAULT_FLATTEN_TAKE_S
    position_reconcile_s: float = 10.0
    open_orders_reconcile_s: float = 15.0
    fill_reconcile_s: float = 60.0
    deadman_retry_s: float = 300.0
    shutdown_cancel_attempts: int = 3
    shutdown_retry_s: float = 2.0
    info_min_interval_s: float = 0.5
    info_cooldown_base_s: float = 15.0
    info_cooldown_max_s: float = 120.0
    min_exit_edge_ticks: int = DEFAULT_MIN_EXIT_EDGE_TICKS

    @property
    def paper(self) -> bool:
        return not self.live


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _load_private_key() -> str | None:
    raw = _optional_env("HYPERLIQUID_PRIVATE_KEY")
    if raw:
        return raw
    keystore_path = _optional_env("ENTROPY_KEYSTORE_PATH")
    credential_path = _optional_env("ENTROPY_CREDENTIAL_FILE")
    if not keystore_path and not credential_path:
        return None
    if not keystore_path or not credential_path:
        raise ConfigError("encrypted signer requires ENTROPY_KEYSTORE_PATH and ENTROPY_CREDENTIAL_FILE")
    try:
        encrypted = json.loads(Path(keystore_path).read_text(encoding="utf-8"))
        password = Path(credential_path).read_text(encoding="utf-8").rstrip("\r\n")
        if not password:
            raise ValueError("empty credential")
        return "0x" + Account.decrypt(encrypted, password).hex()
    except Exception as exc:
        raise ConfigError("could not load encrypted signer") from exc


def _official_endpoint(url: str, scheme: str, path: str = "") -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == scheme
        and parsed.hostname == "api.hyperliquid.xyz"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path == path
    )


def load_settings(dotenv_path: str | Path | None = None, *, load_private_key: bool = True) -> Settings:
    if dotenv_path is not None:
        load_dotenv(dotenv_path, override=False)
    coins_raw = os.environ.get("COINS", "io:SNDK")
    coins = validate_coin_list(part.strip() for part in coins_raw.split(",") if part.strip())
    try:
        notional = float(os.environ.get("QUOTE_NOTIONAL_USD", DEFAULT_NOTIONAL))
        offset = int(os.environ.get("QUOTE_OFFSET_TICKS", DEFAULT_OFFSET_TICKS))
        max_lev = int(os.environ.get("MAX_LEVERAGE", DEFAULT_MAX_LEVERAGE))
        fee_tier = int(os.environ.get("FEE_TIER", DEFAULT_FEE_TIER))
        referral = float(os.environ.get("REFERRAL_DISCOUNT", "0") or 0)
        rebate_raw = _optional_env("MAKER_REBATE_BPS")
        rebate_bps = float(rebate_raw) if rebate_raw is not None else None
        entropy_tier = int(os.environ.get("ENTROPY_TIER", DEFAULT_ENTROPY_TIER))
        self_rebate_raw = _optional_env("ENTROPY_SELF_REBATE")
        entropy_self_rebate = (
            float(self_rebate_raw) if self_rebate_raw is not None else DEFAULT_ENTROPY_SELF_REBATE
        )
        reward_raw = _optional_env("ENTROPY_REFERRAL_REWARD")
        entropy_referral_reward = (
            float(reward_raw) if reward_raw is not None else DEFAULT_ENTROPY_REFERRAL_REWARD
        )
        referred_raw = _optional_env("ENTROPY_REFERRED_USER_BENEFIT")
        entropy_referred = (
            float(referred_raw) if referred_raw is not None else DEFAULT_ENTROPY_REFERRED_USER_BENEFIT
        )
        min_replace_s = float(os.environ.get("MIN_REPLACE_S", DEFAULT_MIN_REPLACE_S))
        guardian_max_age = float(os.environ.get("GUARDIAN_MAX_AGE", "5"))
        guardian_poll_s = float(os.environ.get("GUARDIAN_POLL_S", "1"))
        flatten_far_s = float(os.environ.get("FLATTEN_FAR_S", DEFAULT_FLATTEN_FAR_S))
        flatten_take_s = float(os.environ.get("FLATTEN_TAKE_S", DEFAULT_FLATTEN_TAKE_S))
        position_reconcile_s = float(os.environ.get("POSITION_RECONCILE_S", "10"))
        open_orders_reconcile_s = float(os.environ.get("OPEN_ORDERS_RECONCILE_S", "15"))
        fill_reconcile_s = float(os.environ.get("FILL_RECONCILE_S", "60"))
        deadman_retry_s = float(os.environ.get("DEADMAN_RETRY_S", "300"))
        shutdown_cancel_attempts = int(os.environ.get("SHUTDOWN_CANCEL_ATTEMPTS", "3"))
        shutdown_retry_s = float(os.environ.get("SHUTDOWN_RETRY_S", "2"))
        info_min_interval_s = float(os.environ.get("INFO_MIN_INTERVAL_S", "0.5"))
        info_cooldown_base_s = float(os.environ.get("INFO_COOLDOWN_BASE_S", "15"))
        info_cooldown_max_s = float(os.environ.get("INFO_COOLDOWN_MAX_S", "120"))
        min_exit_edge_ticks = int(os.environ.get("MIN_EXIT_EDGE_TICKS", DEFAULT_MIN_EXIT_EDGE_TICKS))
    except ValueError as exc:
        raise ConfigError(f"invalid numeric config: {exc}") from exc
    floats = (notional, referral, min_replace_s, entropy_self_rebate, entropy_referral_reward, entropy_referred, guardian_max_age, guardian_poll_s, flatten_far_s, flatten_take_s, position_reconcile_s, open_orders_reconcile_s, fill_reconcile_s, deadman_retry_s, shutdown_retry_s, info_min_interval_s, info_cooldown_base_s, info_cooldown_max_s)
    if rebate_bps is not None:
        floats += (rebate_bps,)
    if not all(math.isfinite(value) for value in floats):
        raise ConfigError("numeric config values must be finite")
    if notional <= 0 or offset < 0 or max_lev < 1:
        raise ConfigError("QUOTE_NOTIONAL_USD, QUOTE_OFFSET_TICKS, MAX_LEVERAGE must be positive")
    if min_replace_s < 0:
        raise ConfigError("MIN_REPLACE_S must be >= 0")
    if guardian_max_age <= 0 or guardian_poll_s <= 0:
        raise ConfigError("guardian timing must be finite and positive")
    if flatten_far_s < 0 or flatten_take_s <= flatten_far_s:
        raise ConfigError("FLATTEN_TAKE_S must be greater than FLATTEN_FAR_S >= 0")
    if min(position_reconcile_s, open_orders_reconcile_s, fill_reconcile_s,
           deadman_retry_s, shutdown_retry_s) <= 0 or shutdown_cancel_attempts < 1:
        raise ConfigError("reconciliation, retry, and shutdown timing must be positive")
    if info_min_interval_s < 0 or info_cooldown_base_s <= 0 or info_cooldown_max_s < info_cooldown_base_s:
        raise ConfigError("info pacing/cooldown values are inconsistent")
    if min_exit_edge_ticks < 0:
        raise ConfigError("MIN_EXIT_EDGE_TICKS must be >= 0")
    rates_for_tier(fee_tier)
    entropy_program(entropy_tier)
    if not 0.0 <= referral < 1.0:
        raise ConfigError("REFERRAL_DISCOUNT must be in [0, 1)")
    if entropy_self_rebate < 0 or entropy_referral_reward < 0 or entropy_referred < 0:
        raise ConfigError("Entropy rebate rates must be >= 0")
    api_url = _optional_env("HYPERLIQUID_API_URL") or DEFAULT_API_URL
    ws_url = _optional_env("HYPERLIQUID_WS_URL") or DEFAULT_WS_URL
    if not _official_endpoint(api_url, "https") or not _official_endpoint(ws_url, "wss", "/ws"):
        raise ConfigError("only official Hyperliquid HTTP/WS endpoints are allowed")
    live = _truthy(os.environ.get("LIVE"))
    if live and (fee_tier != 0 or referral != 0 or rebate_bps is not None or
                 entropy_self_rebate != 0 or entropy_referred != 0):
        raise ConfigError("production fee estimates require conservative gross Tier0 rates")
    if live and _optional_env("HYPERLIQUID_PRIVATE_KEY"):
        raise ConfigError("live mode requires the encrypted keystore credential path")
    intent_path = Path(_optional_env("ENTROPY_INTENT_DB") or DEFAULT_INTENT_DB_PATH).expanduser()
    if not intent_path.is_absolute() or len(str(intent_path)) > 4096 or intent_path.name in {"", ".", ".."}:
        raise ConfigError("ENTROPY_INTENT_DB must be a bounded absolute file path")
    return Settings(
        live=live,
        private_key=_load_private_key() if load_private_key else None,
        account=_optional_env("HYPERLIQUID_ACCOUNT"),
        coins=coins,
        quote_notional_usd=notional,
        quote_offset_ticks=offset,
        max_leverage=max_lev,
        api_url=api_url.rstrip("/"),
        ws_url=ws_url,
        fee_tier=fee_tier,
        referral_discount=referral,
        maker_rebate_bps=rebate_bps,
        entropy_tier=entropy_tier,
        entropy_self_rebate=entropy_self_rebate,
        entropy_referral_reward=entropy_referral_reward,
        entropy_referred_user_benefit=entropy_referred,
        min_replace_s=min_replace_s,
        intent_db_path=str(intent_path),
        state_dir=str(Path(_optional_env("ENTROPY_STATE_DIR") or "~/.local/state/entropy-bot").expanduser()),
        guardian_ready_path=str(Path(_optional_env("GUARDIAN_READY_PATH") or "~/.local/state/entropy-bot/guardian-ready.json").expanduser()),
        guardian_lease_path=str(Path(_optional_env("GUARDIAN_LEASE_PATH") or "~/.local/state/entropy-bot/main-lease.json").expanduser()),
        guardian_token_path=str(Path(_optional_env("GUARDIAN_TOKEN_PATH") or "~/.local/state/entropy-bot/guardian-session.token").expanduser()),
        guardian_max_age=guardian_max_age,
        guardian_poll_s=guardian_poll_s,
        flatten_far_s=flatten_far_s,
        flatten_take_s=flatten_take_s,
        position_reconcile_s=position_reconcile_s,
        open_orders_reconcile_s=open_orders_reconcile_s,
        fill_reconcile_s=fill_reconcile_s,
        deadman_retry_s=deadman_retry_s,
        shutdown_cancel_attempts=shutdown_cancel_attempts,
        shutdown_retry_s=shutdown_retry_s,
        info_min_interval_s=info_min_interval_s,
        info_cooldown_base_s=info_cooldown_base_s,
        info_cooldown_max_s=info_cooldown_max_s,
        min_exit_edge_ticks=min_exit_edge_ticks,
    )


def require_live(settings: Settings) -> None:
    if not settings.live:
        raise LiveGuardError("live trading refused: set LIVE=1 (paper is the default)")
    if not settings.private_key:
        raise LiveGuardError("live trading refused: HYPERLIQUID_PRIVATE_KEY is not set")


def require_signer(settings: Settings) -> None:
    if not settings.private_key:
        raise LiveGuardError("signing refused: HYPERLIQUID_PRIVATE_KEY is not set")


def live_notional(settings: Settings) -> float:
    """Default ~$50/side, never below the $10 exchange minimum."""
    return max(MIN_ORDER_USD, settings.quote_notional_usd)
