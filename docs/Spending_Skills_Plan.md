# Groundhog Spending Skills Plan

## Purpose

Define the user-facing spending capabilities, their routing, and their safety
rules in one place. Personal spending data stays in local DuckDB and image
understanding uses the local Ollama vision model only.

The current upload path is an OpenClaw **plugin command**, not a model-selected
skill. That is intentional: `/expense` must always reach Groundhog directly and
must not depend on the chat model deciding which tool to call.

## Current Capabilities

### 1. Import a transaction screenshot

- Trigger: attach one Wallet or bank transaction-list screenshot to Telegram
  with `/expense` as its caption.
- OpenClaw owner: `groundhog-spending-router` command `expense`.
- Groundhog entrypoint: `python -m scripts.media_ingestion enqueue --kind expense`.
- Inputs: exact image path, stable attachment identity, and Phoenix-local upload
  date derived by the worker from the spooled file.
- Output: imported count, total, merchant, amount, category, short transaction
  ID, and skipped-row counts.
- Supported date labels: recent relative times, `Today`, `Yesterday`, weekday
  names, numeric dates, and English month-name dates.
- Posted rows require a merchant, charge amount, and resolvable date.
- Explicitly pending rows are skipped.
- Running balances and account balances are ignored.

### 2. Classify transactions

Allowed categories are:

- `groceries`
- `dining`
- `shopping`
- `entertainment`
- `beer`
- `other`

The vision model suggests a category. Deterministic merchant rules take
priority over that suggestion. The current rule normalizes the merchant name
and classifies every Circle K variant as `beer`.

### 3. Correct a category

- Trigger: `/expense-category <transaction-id> <category>`.
- The transaction ID may be the unique short prefix shown by `/expense`.
- Exactly one row must match; missing or ambiguous IDs fail without changing
  data.
- The category must be one of the allowed values above.

## Request Flow

```text
Telegram image + /expense
        ↓
OpenClaw registered command
        ↓
locate the attachment received with that message
        ↓
copy to durable spool and enqueue expense job
        ↓
immediately return the short job ID
        ↓
media worker runs Groundhog local vision extraction
        ↓
normalize dates, amounts, status, and category
        ↓
deduplicate and insert posted rows into DuckDB
        ↓
retain processed image for 15 days
        ↓
queue one concise terminal result for Telegram delivery
```

No LangGraph or MCP step is involved in the import path. The explicit command
prevents the bare-activity hook from claiming the same Telegram update.

## Data and Idempotency Design

The `spending` table stores:

- stable transaction ID
- resolved transaction date and original visible date label
- merchant, amount, payment method, and category
- source image hash and source row
- creation and update timestamps

Reprocessing is safe:

1. An image already represented by its source hash is skipped.
2. A merchant and amount already present within three calendar days is skipped,
   even when it came from a different screenshot.
3. Source images are archived by hash, so copying the same image again does not
   create another archive.

## Design Boundaries

- Personal images and extracted transactions never go to a hosted AI API.
- `/expense` remains a deterministic registered command; do not convert it to
  prompt-only routing.
- Do not add spending tools to `mcp_server/server.py` without explicit approval.
- Do not import pending transactions.
- Do not infer an unresolvable date merely to save a row.
- New merchant overrides must be deterministic and covered by tests.
- Logging or diagnostics must never expose the source image contents or full
  transaction history to an external service.

## Verification

Run locally:

```bash
python -m unittest tests.test_spending_ingestion
python -m unittest tests.test_media_ingestion
node --test deploy/openclaw/plugins/groundhog-spending-router/core.test.js
python -m unittest discover -s tests -p 'test_*.py'
```

Verify on Linux after deployment:

```bash
systemctl --user is-active openclaw-gateway.service
openclaw plugins inspect groundhog-spending-router --runtime --json
venv/bin/python -m unittest tests.test_spending_ingestion
```

Acceptance checks:

- A Wallet screenshot imports posted transaction rows with relative dates.
- A bank screenshot imports explicit dated rows and ignores running balances.
- A pending-only screenshot inserts nothing and reports the skipped rows.
- Re-uploading the same screenshot inserts nothing.
- The same posted charge shown by Wallet and a bank view is not duplicated.
- Circle K is stored as `beer`.
- `/expense-category` changes exactly one selected row.
- The OpenClaw runtime reports both commands and no plugin diagnostics.

## Planned Spending Skills

These capabilities are not implemented yet:

1. Spending queries such as totals by date range, merchant, or category.
2. Daily, weekly, and monthly spending summaries.
3. Category-budget comparisons and unusual-spending alerts.
4. Additional merchant overrides backed by explicit user rules and tests.
5. A review workflow for rows classified as `other`.

Before implementing those items, define whether they belong in a direct
OpenClaw command, a Groundhog MCP read tool, or the guarded `/ask` agent. Keep
writes deterministic; use MCP or the agent only when natural-language query
interpretation is actually needed.
