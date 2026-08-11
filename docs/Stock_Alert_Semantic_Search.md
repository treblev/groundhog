# Stock Alert Semantic Search Design

## Goal

Retrieve stored weekly Supertrend flip alerts by meaning while keeping market
prices, indicator state, and alert rows structured and authoritative in DuckDB.
The feature is read-only and local-only.

## Source and index

- `stock_alerts` is canonical. Index only rows whose `alert_type` is
  `supertrend_weekly_bullish` or `supertrend_weekly_bearish`.
- Each qualifying row produces one derived `semantic_chunks` record in the
  `stock_alert` domain. Its embedding text includes ticker, weekly timeframe,
  direction, alert type, date, and stored message.
- Metadata supports exact case-insensitive ticker and direction filters;
  `start_date` and `end_date` filter the canonical alert date.
- Synchronization uses content hashes and embedding-model names to reuse
  unchanged chunks, re-embed changed/model-mismatched chunks, and remove stale
  chunks. It performs Ollama calls outside DuckDB write transactions.

## Retrieval boundary

Use MCP `search_documents` with `domain="stock_alert"` for semantic historical
questions such as “which weekly bearish turns did I have?” or “find bullish
weekly Supertrend flips for MSFT.” Results include the stable alert ID, ticker,
date, direction, alert type, message, and cosine-similarity score.

Use structured data tools or SQL for current prices, current Supertrend state,
exact alert listings, counts, aggregates, and OHLCV calculations. Do not embed
price bars, create investment advice, or treat the derived index as a source of
truth.

## Operations and verification

Search refreshes this small derived index automatically. It can also be refreshed
or inspected explicitly:

```bash
python scripts/index_semantic_documents.py --domain stock_alert
python scripts/index_semantic_documents.py --domain stock_alert --dry-run
```

Offline regression coverage verifies weekly-only selection, idempotent refresh,
changed and deleted source handling, semantic ranking, filters, and MCP argument
forwarding.
