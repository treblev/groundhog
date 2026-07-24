#!/bin/bash
# Runs after market close and records one durable pipeline lifecycle.

set -e
cd "$(dirname "$0")/.."

PYTHON=venv/bin/python

echo "=== $(date) ==="
$PYTHON groundhog_service.py run daily-stocks
