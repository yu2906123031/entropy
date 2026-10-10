# Safe deployment: io:SNDK / $12 / 1x

This repository ships two independent processes. The guardian is the primary
cancel-on-failure control for accounts that cannot arm Hyperliquid
`scheduleCancel`; the trading process refuses every placement until the
ready heartbeat is fresh.

## Preflight (offline)

```bash
cd /opt/entropy-bot/releases/NEW
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock  # when maintained by deployment
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m compileall -q entropy_bot tests
.venv/bin/pytest -q
```

Required live configuration:

```text
LIVE=1
COINS=io:SNDK
QUOTE_NOTIONAL_USD=12
MAX_LEVERAGE=1
ENTROPY_STATE_DIR=/var/lib/entropy-bot
ENTROPY_INTENT_DB=/var/lib/entropy-bot/intents.sqlite3
GUARDIAN_READY_PATH=/run/entropy-bot/guardian-ready.json
GUARDIAN_LEASE_PATH=/run/entropy-bot/main-lease.json
GUARDIAN_MAX_AGE=5
GUARDIAN_POLL_S=1
```

Inject the encrypted keystore password with systemd credentials. Keep the
keystore and credential paths outside the release. `status` and `paper` skip
private-key loading.

## Units and ordering

Install `deploy/entropy-guardian.service` and `deploy/entropy-live.service`.
Create the dedicated user and the state/runtime directories with mode 0700.
Start the guardian first. Observe at least two fresh heartbeat updates, then
start the live unit manually. Keep both units disabled during the first
observation window.

The live startup gate requires: authenticated state reads, complete WAL
reconciliation, zero external/non-EBOT orders, isolated 1x verification, a
fresh guardian heartbeat, and successful EBOT-only startup cleanup. Any
manual or other-strategy order blocks startup and runtime quoting.

## Shutdown and rollback

Stop the live unit first. Its shutdown path cancels EBOT orders, performs
bounded authoritative position reads, and submits reduce-only IOC flatten
orders until flat. A failure exits nonzero and leaves the guardian running.
After direct exchange reads prove zero EBOT orders and zero `io:SNDK`
position, stop the guardian. Roll back the `current` symlink only while both
processes are stopped and exchange state is flat.

Never start either unit as part of package installation or symlink switch.
