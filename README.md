# Entropy ZEC Market Maker

Inventory-aware two-sided market-making core and read-only Hyperliquid planner.

## Commands

```powershell
npm test
npm run probe
npm run plan
npm run stream
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/mm_dry_run.py
```

`scripts/mm_dry_run.py` fetches the live ZEC order book, account inventory and
open orders, then prints a cancel-first reconciliation plan. It cannot sign or
submit an order.

The Python quoting core provides:

- microprice-based fair value
- inventory-skewed reservation prices
- three-layer Post-Only bid/ask quotes
- long, short, net and gross exposure budgets
- elevated-volatility widening and shock suppression
- deterministic preservation, cancellation and placement planning
- minimum order lifetime and configurable reprice thresholds
- stale-book, position, margin, daily-loss and volatility risk gates
- transactional SQLite FIFO lot ledger with idempotent trade IDs and restart recovery
- cursor-based Hyperliquid fill synchronization with replay overlap and gap protection
- fail-closed Hyperliquid venue adapter with ALO-only quotes and a verified cancel-first barrier
- supervised read-only daemon with a process lock, cycle timeout, exponential backoff, signal handling and atomic health state
- post-execution persistence, forced fill refresh, exchange order snapshots, orphan detection, position recheck and cumulative dry-run metrics
- rolling latency p50/p95, quote-survival and cancel/place churn observations
- deterministic timeout, partial-execution and restart-recovery drills
- read-only dedicated-wallet and minimum-notional live preflight

Configuration can be supplied with the environment variables documented in
`.env.example`. The current Entropy builder attribution is displayed with every
generated plan so it can later be included in signed Hyperliquid order actions.

Do not put a main-wallet private key in this project. The live phase will use a
separately approved API/Agent wallet and an explicit live-trading switch.

Operational validation commands:

```bash
.venv/bin/python scripts/mm_resilience_drill.py
ENTROPY_ACCOUNT=0x... ENTROPY_OPERATOR_ADDRESS=0x... \
  .venv/bin/python scripts/mm_live_preflight.py
```

The preflight loads no private key and performs zero signed calls. The daemon live path remains locked.
