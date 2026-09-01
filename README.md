# Entropy ZEC Market Maker

Inventory-aware, toxicity-aware two-sided market-making core for Hyperliquid, with a fail-closed read-only planner and a deliberately gated live strategy.

## What changed in adaptive-v2

The strategy was rewritten after sustained negative testing. The new model adds:

- capped L1 microprice influence instead of trusting raw top-of-book imbalance
- nonlinear inventory reservation-price skew
- inventory-aware allocation of remaining long/short gross capacity
- continuous volatility widening, size reduction and layer reduction
- soft and hard reprice thresholds so minimum quote lifetime never protects a materially stale quote
- 1s/5s/30s maker-fill markout tracking and a negative-markout toxicity halt
- five-level depth toxicity filtering and adverse-side-only quote suppression
- per-cycle quote epochs in Hyperliquid CLOIDs while preserving retry idempotency
- explicit execution result categories and partial-placement state
- stale-book, position, margin, daily-loss, volatility, inventory-age and adverse-move gates
- transactional SQLite FIFO lot ledger, fill replay protection and restart recovery
- cancel-first execution verification and ALO-only opening orders
- rolling latency, quote-survival and order-churn observations

## Safety posture

New risk-increasing orders are fail-closed. The legacy live environment is not enough to resume openings after the adaptive-v2 reset. The live ZEC script additionally requires:

```bash
ENTROPY_ALLOW_NEW_OPENINGS=true
ENTROPY_STRATEGY_RESET_ACK=adaptive-v2
```

Do not enable those values merely because the code starts successfully. First validate dry-run quote behavior and maker-fill markouts. A strategy with persistently negative 5-second markout should remain disabled even if operational checks are green.

Never put a main-wallet private key in this project. Use a separately approved API/Agent wallet.

## Validation

```bash
npm ci
npm test
python -m unittest discover -s tests -v
python scripts/mm_dry_run.py
python scripts/mm_resilience_drill.py
```

The repository includes GitHub Actions CI for both the Python and Node test suites.

## Read-only planner

`scripts/mm_dry_run.py` fetches the live ZEC book, account inventory, fills and open orders, then prints a cancel-first reconciliation plan. It performs zero signed order calls. Risk limits and adaptive quote parameters are configurable through `.env.example` variables instead of using the old oversized planner defaults.

## Live strategy

`scripts/zec_neutral_mm.py` is the bounded adaptive live strategy. It uses five-level book depth, capped fair value, nonlinear inventory skew, continuous volatility protection, hard stale-quote cancellation, short inventory holding limits and in-process fill markouts. If 5-second maker markout becomes persistently negative, new inventory is suppressed.

The live phase remains operator-controlled; code changes cannot establish that the strategy has positive expectancy. Promote sizing only after out-of-sample net PnL after fees and fill markouts are acceptable.
