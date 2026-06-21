#!/usr/bin/env bash
# scripts/snapshot_mvp.sh
# Copies the validated production tree to ../RestartOS-MVP/ — only the files
# that belong in the public release. Excludes secrets, generated state, caches,
# and dev junk. Idempotent: safe to re-run; overwrites the target.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$(cd "$SRC/.." && pwd)/RestartOS-MVP}"

echo "[snapshot] SRC  = $SRC"
echo "[snapshot] DEST = $DEST"

# Wipe the target so stale files don't linger
rm -rf "$DEST"
mkdir -p "$DEST"

# Production files (everything that belongs in the public repo)
INCLUDE=(
  "restartos"
  "ui"
  "config"
  "dataset"
  "tests"
  ".github"
  "Dockerfile"
  "docker-compose.yml"
  ".env.example"
  ".gitignore"
  ".mcp.json"
  "Makefile"
  "pyproject.toml"
  "requirements.txt"
  "README.md"
  "LICENSE"
  "SOLUTION.md"
  "start_macos.command"
  "start_windows.bat"
  "scripts"
)

for item in "${INCLUDE[@]}"; do
  if [ -e "$SRC/$item" ]; then
    cp -R "$SRC/$item" "$DEST/"
    echo "  + $item"
  fi
done

# Strip Python/IDE caches that may have slipped in
find "$DEST" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -type d -name ".pytest_cache" -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -type d -name ".ruff_cache" -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -type d -name ".mypy_cache" -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name "*.pyc" -delete 2>/dev/null || true
find "$DEST" -name "test_write.txt" -delete 2>/dev/null || true
# Do NOT carry over secrets or local state
rm -f "$DEST/.env" "$DEST/run.log" "$DEST/run_act.log"
rm -rf "$DEST/_it_state" "$DEST/_data" 2>/dev/null || true

echo "[snapshot] done. Run 'cd $DEST && docker compose up -d' to verify."

# Final secrets scan — fail loudly if anything leaked
echo "[snapshot] secret scan..."
if grep -rE "nvapi-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}" \
     --include="*.py" --include="*.md" --include="*.yaml" --include="*.yml" \
     --include="*.json" --include="*.html" --include="*.sh" \
     "$DEST" 2>/dev/null; then
  echo "[snapshot] !!! SECRETS DETECTED IN SNAPSHOT. ABORTING. !!!"
  exit 2
fi
echo "[snapshot] clean. No secrets in $DEST."
