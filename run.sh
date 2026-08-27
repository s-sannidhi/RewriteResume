#!/usr/bin/env bash
# Local-only launcher. Backend binds to 127.0.0.1 — never exposed off this machine.
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "No virtualenv found. Run this first:"
  echo "    python3 setup.py"
  exit 1
fi

exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765 "$@"
