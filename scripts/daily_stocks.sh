#!/bin/bash
# Runs after market close: fetch prices → compute signals → fire alerts

set -e
cd "$(dirname "$0")/.."

PYTHON=venv/bin/python

echo "=== $(date) ==="
echo "--- Fetching prices ---"
$PYTHON ingestion/stocks.py

echo "--- Computing signals ---"
$PYTHON analytics/signals.py

echo "--- Checking alerts ---"
$PYTHON analytics/alerts.py
