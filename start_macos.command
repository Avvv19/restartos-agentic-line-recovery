#!/bin/bash
# ===== RestartOS one-click launcher (macOS/Linux) =====
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)"
command -v python3 >/dev/null 2>&1 || { echo "Install Python 3.10+ from https://python.org"; read -n1; exit 1; }
echo "Installing minimal dependency (PyYAML)..."
python3 -m pip install --quiet PyYAML >/dev/null 2>&1
[ -f _data/_manifest.json ] || { echo "Generating demo dataset (first run only)..."; python3 dataset/generate.py; }
( sleep 2; open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null ) &
echo "Starting RestartOS engine at http://localhost:8000  (Ctrl+C to stop)"
python3 -m restartos.server --port 8000
