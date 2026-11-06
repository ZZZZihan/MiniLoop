#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

OFFLINE_MODE=0
for arg in "$@"; do
  case "${arg}" in
    --offline)
      OFFLINE_MODE=1
      ;;
    *)
      echo "Unknown option: ${arg}" >&2
      echo "Usage: ./scripts/verify.sh [--offline]" >&2
      exit 2
      ;;
  esac
done

echo "==> [1/4] Running ruff check..."
uv run ruff check src tests

echo "==> [2/4] Running mypy strict check..."
uv run mypy src tests

echo "==> [3/4] Running pytest suite (including MANIFEST.json and 6 reference-fix checks)..."
uv run pytest -q

echo "==> [4/4] Running 10 deterministic protocol verification scenarios..."
uv run python -m miniloop.cli verify-protocol --check

if [[ "${OFFLINE_MODE}" -eq 1 ]]; then
  echo "==> Offline verification passed."
  exit 0
fi

echo "==> [Live Smoke] Running real-model read-only verification via OpenAI-compatible gateway..."
PROVIDER="${MINILOOP_MODEL_PROVIDER:-openai_compatible}"
BASE_URL="${MINILOOP_MODEL_BASE_URL:-http://127.0.0.1:3425/v1}"
MODEL_NAME="${MINILOOP_MODEL_NAME:-remote-magpie/auto/claude-sonnet-5-5}"
if [[ "${BASE_URL}" == *"127.0.0.1:3425"* && "${MODEL_NAME}" != *"/"* ]]; then
  MODEL_NAME="remote-magpie/auto/${MODEL_NAME}"
fi

SMOKE_TRAJ="$(mktemp /tmp/miniloop_verify_smoke_XXXXXX.jsonl)"
trap 'rm -f "${SMOKE_TRAJ}"' EXIT

uv run python -m miniloop.cli run \
  --provider "${PROVIDER}" \
  --base-url "${BASE_URL}" \
  --model "${MODEL_NAME}" \
  --workspace ./workspaces/demo_readonly \
  --task "Use read_file to inspect README_DEMO.md and config_sample.ini, then state the service codename and listen_port." \
  --output "${SMOKE_TRAJ}"

echo "==> [Live Smoke] Replaying recorded trajectory in pure read-only mode..."
uv run python -m miniloop.cli replay "${SMOKE_TRAJ}"

echo "==> All offline and real-model smoke checks passed."
