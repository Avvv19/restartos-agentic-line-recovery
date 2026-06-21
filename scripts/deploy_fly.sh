#!/usr/bin/env bash
# scripts/deploy_fly.sh — One-shot Fly.io deploy for RestartOS.
# Prereqs:
#   1. flyctl installed   → see https://fly.io/docs/hands-on/install-flyctl/
#   2. flyctl auth login  → opens browser, one-time
#   3. .env populated with NIM_API_KEY + GROQ_API_KEY (optional but recommended)
set -euo pipefail

APP_ENGINE="restartos-mvp"
APP_QDRANT="restartos-qdrant"
PG_NAME="restartos-postgres"
REGION="iad"

if ! command -v flyctl >/dev/null 2>&1; then
  echo "✗ flyctl not found. Install: https://fly.io/docs/hands-on/install-flyctl/"
  exit 1
fi

if ! flyctl auth whoami >/dev/null 2>&1; then
  echo "✗ Not authenticated. Run: flyctl auth login"
  exit 1
fi

echo "▸ Creating Qdrant companion app (if not present)..."
flyctl apps create "$APP_QDRANT" --org personal 2>/dev/null || echo "  (exists)"
flyctl deploy --app "$APP_QDRANT" --image qdrant/qdrant:v1.12.4 \
              --internal-port 6333 --regions "$REGION" \
              --vm-memory 512 --no-public-ips || true

echo "▸ Creating Postgres cluster (if not present)..."
flyctl postgres create --name "$PG_NAME" --region "$REGION" \
                       --vm-size shared-cpu-1x --initial-cluster-size 1 \
                       --volume-size 1 2>/dev/null || echo "  (exists)"

echo "▸ Creating engine app..."
flyctl apps create "$APP_ENGINE" --org personal 2>/dev/null || echo "  (exists)"

echo "▸ Attaching Postgres..."
flyctl postgres attach --app "$APP_ENGINE" "$PG_NAME" 2>/dev/null || true

if [ -f .env ]; then
  echo "▸ Pushing API keys from .env as Fly secrets..."
  set +e
  for var in NIM_API_KEY GROQ_API_KEY GEMINI_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY; do
    val=$(grep -E "^${var}=" .env | cut -d= -f2-)
    if [ -n "$val" ] && [ "$val" != "" ]; then
      flyctl secrets set --app "$APP_ENGINE" "${var}=${val}" >/dev/null
      echo "    + $var"
    fi
  done
  set -e
fi

echo "▸ Deploying engine..."
flyctl deploy --app "$APP_ENGINE"

echo ""
echo "✓ Deployed."
flyctl status --app "$APP_ENGINE"
url=$(flyctl info --app "$APP_ENGINE" --json | python -c "import json,sys; print('https://'+json.load(sys.stdin)['Hostname'])")
echo ""
echo "Public URL: ${url}"
echo "  cockpit : ${url}/cockpit"
echo "  healthz : ${url}/healthz"
echo "  metrics : ${url}/metrics"
