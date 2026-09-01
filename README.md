# Entropy ZEC Market Maker

Inventory-aware, toxicity-aware and execution-quality-aware market making for Hyperliquid, with a fail-closed read-only planner and a deliberately gated live strategy.

## Adaptive-v4 strategy

The strategy has been rebuilt around measured maker expectancy rather than fixed spread parameters. The current model includes:

- five-level, exponentially weighted L2 fair value bounded inside the live BBO
- capped microstructure influence so one distorted level cannot dominate fair value
- nonlinear inventory reservation-price skew and inventory-aware risk capacity
- continuous volatility widening and dynamic order-size reduction
- profitability edge requirements that include fees, minimum desired profit, volatility, directionality, book imbalance and historical adverse markout
- separate buy-fill and sell-fill 1s/5s/30s markout histories, so a toxic bid side does not unnecessarily disable the ask side
- restart-safe SQLite markout persistence with automatic migration from older databases
- persistent maker quote exposure history: distance from touch, displayed queue ahead, lifetime and final fill/cancel outcome
- empirical fill-rate and queue-quality learning that automatically reduces size when historical execution quality deteriorates
- directional and book-pressure penalties that widen or pause only the vulnerable side
- soft/hard stale quote thresholds, so queue-lifetime protection never preserves materially stale orders
- per-cycle quote epochs in Hyperliquid CLOIDs while retaining same-cycle retry idempotency
- explicit partial/unknown execution outcomes and fail-closed cancel-first placement
- stale-book, position, margin, daily-loss, volatility, inventory-age and adverse-move gates
- transactional FIFO ledger, fill replay protection, restart recovery and CI regression coverage

## Safety posture

Risk-increasing orders are fail-closed after every strategy generation change. Existing v2/v3 live environment settings do not authorize adaptive-v4 openings. The live strategy requires both:

```bash
ENTROPY_ALLOW_NEW_OPENINGS=true
ENTROPY_STRATEGY_RESET_ACK=adaptive-v4
```

Do not enable these merely because CI is green. Code correctness does not establish positive expectancy. First inspect dry-run behavior and collect enough live maker observations to evaluate net PnL after fees, side-specific 5-second markout, fill rate and queue quality.

Never put a main-wallet private key in this project. Use a separately approved API/Agent wallet.

## Learned diagnostics

`scripts/zec_neutral_mm.py` emits the variables needed to diagnose the strategy rather than only reporting PnL:

- `l2_fair` and `l2_spread_bps`
- `required_edge_bps` and `edge_score`
- `size_multiplier` and `learned_fill_multiplier`
- `directional_bps` and `book_imbalance`
- aggregate, buy-side and sell-side maker markout
- aggregate, buy-side and sell-side empirical fill rate
- mean queue-ahead ratio

Persistent learning state is stored separately from the accounting ledger:

```bash
ENTROPY_MARKOUT_PATH=runtime/entropy_markouts.sqlite3
ENTROPY_QUOTE_QUALITY_PATH=runtime/entropy_quote_quality.sqlite3
```

## Validation

```bash
npm ci
npm test
python -m unittest discover -s tests -v
python scripts/mm_dry_run.py
python scripts/mm_resilience_drill.py
```

GitHub Actions runs both Python and Node tests on every push to `main` and on pull requests.

## Read-only planner

`scripts/mm_dry_run.py` fetches the live ZEC book, account inventory, fills and open orders, then prints a cancel-first reconciliation plan without signed order calls. Risk limits and quote parameters are configurable through `.env.example`.

## Live strategy

`scripts/zec_neutral_mm.py` is the bounded adaptive live strategy. It learns from its own maker fills and quote outcomes. Negative markout raises the required edge; side-specific toxicity can disable only the affected side; poor empirical fill/queue quality reduces size. L2 depth is used for fair value rather than relying solely on top-of-book microprice.

Sizing should only be promoted after out-of-sample observations show acceptable net PnL after fees and non-negative or otherwise justified maker markout. If measured expectancy stays negative, the correct action is to leave openings disabled rather than increase capital.
