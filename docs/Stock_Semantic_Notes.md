# Stock Semantic Notes

## Goal

Store short, user-authored research notes for any syntactically valid ticker and
make active notes available to the local semantic search system immediately.
Notes are distinct from machine-generated `stock_alerts`: they are canonical
user data, retain revisions, and never trigger a market alert or recommendation.

## Canonical records and derived index

- `stock_notes` holds the current text, uppercase ticker, lifecycle state, and
  timestamps. A soft-deleted note is retained but excluded from active retrieval.
- `stock_note_revisions` is append-only. Creation, each edit, and deletion write
  a numbered revision containing the contemporaneous note text.
- Active notes produce one derived `semantic_chunks` entry in the `stock_note`
  domain. Its metadata includes the ticker. Ollama embeddings are generated
  after every add, edit, or delete; a delete removes the derived chunk.
- The derived vector is not canonical. Rebuild all ticker-note chunks with
  `python scripts/index_semantic_documents.py --domain stock_note`.

## Telegram commands

The `groundhog-stock-notes-router` OpenClaw plugin owns writes directly; `/ask`
does not mutate notes.

```text
/stocks-add-notes SHOP Weekly bullish bicycle activation; watch follow-through.
/stocks-edit-notes <note-id> Weekly bullish bicycle activation confirmed.
/stocks-delete-notes <note-id>
/stocks-notes SHOP
```

The add and list replies show an eight-character ID prefix. Use the full ID for
edit/delete when possible; the command intentionally rejects ambiguous or
missing IDs. Tickers are normalized to uppercase and accept letters, numbers,
periods, and hyphens, so notes do not require membership in the current
watchlist.

## Retrieval boundary

Ask semantic questions through `/ask`, for example: “What did I note about the
SHOP weekly bullish setup?” The agent must call MCP `search_documents` with
`domain="stock_note"`, adding the exact ticker filter when a ticker is named.
Use current prices, signal state, exact counts, and alerts from structured tools
instead; notes are research context, not market facts or investment advice.

## Verification

```bash
python -m unittest tests.test_stock_notes tests.test_semantic_search tests.test_mcp_service_tools
python scripts/stock_notes.py add SHOP "Weekly bullish bicycle activation"
python scripts/stock_notes.py list SHOP
python scripts/index_semantic_documents.py --domain stock_note --dry-run
```
