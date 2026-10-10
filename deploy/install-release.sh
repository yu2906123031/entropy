#!/usr/bin/env bash
set -euo pipefail

# Build and install one immutable git commit. This script never starts or enables units.
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
PREFIX=${PREFIX:-/opt/entropy-bot}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x /opt/entropy-bot/python-runtime/bin/python3 ]]; then
    PYTHON=/opt/entropy-bot/python-runtime/bin/python3
  else
    PYTHON=python3
  fi
fi
WHEELHOUSE=${WHEELHOUSE:-}
COMMIT=${1:-HEAD}

if [[ -z "$WHEELHOUSE" || ! -d "$WHEELHOUSE" ]]; then
  printf 'WHEELHOUSE must name a populated wheelhouse directory\n' >&2
  exit 2
fi
WHEELHOUSE=$(cd "$WHEELHOUSE" && pwd)

cd "$REPO"
GIT_COMMIT=$(git rev-parse --verify "${COMMIT}^{commit}")
RELEASE="$PREFIX/releases/$GIT_COMMIT"
if [[ -e "$RELEASE" ]]; then
  printf 'release already exists: %s\n' "$RELEASE" >&2
  exit 1
fi

install -d -m 0755 "$PREFIX/releases"
STAGE=$(mktemp -d "$PREFIX/releases/.stage-${GIT_COMMIT}.XXXXXX")
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT
install -d -m 0755 "$STAGE/app"
git archive "$GIT_COMMIT" | tar -x -C "$STAGE/app"
"$PYTHON" -m venv "$STAGE/venv"
"$STAGE/venv/bin/python" -m pip install --disable-pip-version-check \
  --no-index --find-links "$WHEELHOUSE" --require-hashes --only-binary=:all: \
  -r "$STAGE/app/requirements.lock"
PYTHONPATH="$STAGE/app" "$STAGE/venv/bin/python" -m compileall -q "$STAGE/app/entropy_bot" "$STAGE/app/tests"
PYTHONPATH="$STAGE/app" "$STAGE/venv/bin/python" -m pytest -q "$STAGE/app/tests"
PYTHONPATH="$STAGE/app" "$STAGE/venv/bin/python" -m entropy_bot --help >/dev/null
printf '%s\n' "$GIT_COMMIT" > "$STAGE/COMMIT"
chmod -R a-w "$STAGE/app" "$STAGE/venv" "$STAGE/COMMIT"
mv "$STAGE" "$RELEASE"
trap - EXIT
# mktemp creates STAGE as 0700. The immutable release remains root-owned,
# while the dedicated service account needs read/traverse and executable bits.
chown -R root:root "$RELEASE"
find "$RELEASE" -type d -exec chmod 0755 {} +
find "$RELEASE/app" -type f -exec chmod 0644 {} +
find "$RELEASE/venv" -type f -exec chmod 0644 {} +
find "$RELEASE/venv/bin" -type f -exec chmod 0755 {} +
chmod 0444 "$RELEASE/COMMIT"
ln -sfn "$RELEASE" "$PREFIX/.current-$GIT_COMMIT"
mv -Tf "$PREFIX/.current-$GIT_COMMIT" "$PREFIX/current"

install -D -m 0644 "$RELEASE/app/deploy/entropy-guardian.service" "$UNIT_DIR/entropy-guardian.service"
install -D -m 0644 "$RELEASE/app/deploy/entropy-live.service" "$UNIT_DIR/entropy-live.service"
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$UNIT_DIR/entropy-guardian.service" "$UNIT_DIR/entropy-live.service"
fi
printf 'installed immutable release %s; units remain stopped and disabled\n' "$RELEASE"
