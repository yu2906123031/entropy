#!/usr/bin/env bash
set -euo pipefail

# Build the platform-specific, hash-constrained wheelhouse used by install-release.sh.
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
PYTHON=${PYTHON:-python3}
WHEELHOUSE=${1:-"$REPO/wheelhouse"}

mkdir -p "$WHEELHOUSE"
if find "$WHEELHOUSE" -mindepth 1 -maxdepth 1 -type f -print -quit | grep -q .; then
  printf 'wheelhouse must be empty: %s\n' "$WHEELHOUSE" >&2
  exit 2
fi

"$PYTHON" -m pip download --disable-pip-version-check \
  --require-hashes --only-binary=:all: \
  --dest "$WHEELHOUSE" -r "$REPO/requirements.lock"
printf 'built hash-constrained wheelhouse: %s\n' "$WHEELHOUSE"