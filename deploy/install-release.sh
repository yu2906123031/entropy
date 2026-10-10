#!/usr/bin/env bash
set -euo pipefail

# Build and install one immutable git commit. This script never starts or enables units.
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
PREFIX=${PREFIX:-/opt/entropy-bot}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
PYTHON=${PYTHON:-python3}
COMMIT=${1:-HEAD}

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
"$STAGE/venv/bin/python" -m pip install --disable-pip-version-check --require-hashes -r "$STAGE/app/requirements.lock"
"$STAGE/venv/bin/python" -m pip install --disable-pip-version-check "$STAGE/app[dev]"
"$STAGE/venv/bin/python" -m compileall -q "$STAGE/app/entropy_bot"
"$STAGE/venv/bin/python" -m pytest -q "$STAGE/app/tests"
printf '%s\n' "$GIT_COMMIT" > "$STAGE/COMMIT"
chmod -R a-w "$STAGE/app" "$STAGE/venv" "$STAGE/COMMIT"
mv "$STAGE" "$RELEASE"
trap - EXIT
ln -sfn "$RELEASE" "$PREFIX/.current-$GIT_COMMIT"
mv -Tf "$PREFIX/.current-$GIT_COMMIT" "$PREFIX/current"

install -D -m 0644 "$RELEASE/app/deploy/entropy-guardian.service" "$UNIT_DIR/entropy-guardian.service"
install -D -m 0644 "$RELEASE/app/deploy/entropy-live.service" "$UNIT_DIR/entropy-live.service"
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$UNIT_DIR/entropy-guardian.service" "$UNIT_DIR/entropy-live.service"
fi
printf 'installed immutable release %s; units remain stopped and disabled\n' "$RELEASE"
