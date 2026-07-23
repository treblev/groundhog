# Groundhog Long-Running Service Roadmap

This roadmap turns Groundhog from a collection of local scripts into a durable
personal data service. Work stops at the end of each phase for review before the
next phase starts.

## Operating Boundary

Groundhog owns:

- personal data ingestion
- deterministic analytics
- DuckDB schema and durable facts
- event and outbox records
- MCP tools over local data

OpenClaw owns:

- chat
- model/tool orchestration
- user-facing scheduling
- delivery policy and wording
- notification channels

Working rule:

```text
Groundhog decides what happened.
OpenClaw decides how to explain or deliver it.
```

## Phase 0: Migration Stabilization

Status: Done

### Goal

The migrated Linux stock pipeline is reliable, tested, and able to reach the
local Ollama runtime without per-command environment hacks.

### Tasks

- [x] Add Linux systemd user timer/service for daily stocks.
- [x] Run the stock job under the `openclaw` service user.
- [x] Keep the Groundhog DuckDB file outside the repo.
- [x] Make alert delivery configurable for Linux local-only operation.
- [x] Use `GROUNDHOG_ALERT_BACKEND=none` when OpenClaw owns delivery.
- [x] Fix Linux dependency resolution for the Python venv.
- [x] Add incremental stock ingestion after the latest stored ticker date.
- [x] Add regression coverage for the yfinance 500-row response case.
- [x] Add regression coverage for idempotent stock inserts.
- [x] Add live smoke tests for imports, DB tools, and memory tools.
- [x] Configure the Ollama base URL explicitly in `config/settings.py`.
- [x] Remove the need for `OLLAMA_HOST` when running Linux smoke tests.
- [x] Verify offline regression tests on Linux.
- [x] Verify live smoke tests on Linux.

### Verification

```bash
venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
venv/bin/python tests/smoke_test.py
systemctl --user start groundhog-stocks.service
journalctl --user -u groundhog-stocks.service -n 100 --no-pager
```

### Review Gate

User confirms the Linux migration is stable before Phase 1 begins.

## Phase 1: Service Run Tracking

Status: Planned

### Goal

Every Groundhog job records when it started, when it finished, whether it
succeeded, and what happened.

### Tasks

- [ ] Design `agent_runs` schema.
- [ ] Add `agent_runs` table to `ingestion/schema.py`.
- [ ] Keep schema creation idempotent.
- [ ] Add a small helper module for creating and finalizing run records.
- [ ] Record successful job completion.
- [ ] Record failed job completion with error text.
- [ ] Add tests for successful run tracking.
- [ ] Add tests for failed run tracking.
- [ ] Wrap the daily stock pipeline with run tracking.
- [ ] Update Linux operations docs.
- [ ] Run offline regression tests.
- [ ] Run live smoke tests.

### Verification

```sql
SELECT *
FROM agent_runs
ORDER BY started_at DESC
LIMIT 10;
```

### Review Gate

User can answer: "Did Groundhog run today, and did it fail?"

## Phase 2: Events Table

Status: Planned

### Goal

Groundhog records important facts in one normalized event stream, regardless of
which source table produced them.

### Tasks

- [ ] Design `events` schema.
- [ ] Add `events` table to `ingestion/schema.py`.
- [ ] Define initial event kinds.
- [ ] Add event writer helper.
- [ ] Emit job failure events.
- [ ] Emit stock signal flip events.
- [ ] Emit stock alert created events.
- [ ] Add tests for event idempotency.
- [ ] Add tests for event payload shape.
- [ ] Update docs with event kind conventions.
- [ ] Run offline regression tests.
- [ ] Run live smoke tests.

### Initial Event Kinds

- `job_failed`
- `job_completed`
- `stock_signal_flipped`
- `stock_alert_created`
- `sleep_data_imported`
- `workout_data_imported`
- `health_metric_changed`

### Review Gate

User can query recent important events without knowing which source table
created them.

## Phase 3: Outbox Table

Status: Planned

### Goal

Separate "something happened" from "tell the user about it."

### Tasks

- [ ] Design `outbox` schema.
- [ ] Add `outbox` table to `ingestion/schema.py`.
- [ ] Define delivery statuses.
- [ ] Add helper for creating outbox rows from events.
- [ ] Add tests for pending outbox rows.
- [ ] Add tests for delivery status updates.
- [ ] Decide which stock alerts create outbox rows.
- [ ] Update docs with Groundhog/OpenClaw delivery boundary.
- [ ] Run offline regression tests.
- [ ] Run live smoke tests.

### Review Gate

User can see pending Groundhog messages before OpenClaw delivers them.

## Phase 4: Groundhog Service Runner

Status: Planned

### Goal

Replace loose script chaining with one stable operational command surface.

### Tasks

- [ ] Design CLI shape for `groundhog_service.py`.
- [ ] Add `run daily-stocks` command.
- [ ] Add `status` command.
- [ ] Call existing ingestion/analytics modules without changing their core logic.
- [ ] Record `agent_runs` around each service task.
- [ ] Emit events and outbox rows where appropriate.
- [ ] Update `scripts/daily_stocks.sh` to call the service runner.
- [ ] Update systemd service documentation.
- [ ] Add tests for runner success and failure paths.
- [ ] Run offline regression tests.
- [ ] Run live smoke tests.
- [ ] Manually run `groundhog-stocks.service`.

### Review Gate

The systemd timer calls one Groundhog command, and failures are visible in the
database and journal.

## Phase 5: MCP Service Tools

Status: Planned

### Goal

OpenClaw can inspect Groundhog's service state through MCP tools.

### Tasks

- [ ] Design tool contracts before editing `mcp_server/server.py`.
- [ ] Add `get_recent_events`.
- [ ] Add `get_pending_outbox`.
- [ ] Add `get_agent_run_status`.
- [ ] Add `get_latest_alerts`.
- [ ] Add `mark_outbox_delivered`.
- [ ] Add smoke coverage for new MCP tools.
- [ ] Update OpenClaw MCP notes.
- [ ] Run offline regression tests.
- [ ] Run live smoke tests.

### Review Gate

OpenClaw can answer: "What happened today?" using Groundhog tools.

## Phase 6: Always-On Mode

Status: Planned

### Goal

Groundhog can optionally run as a long-lived local process.

### Tasks

- [ ] Decide whether daemon mode is needed beyond systemd timers.
- [ ] Design `groundhog_service.py daemon`.
- [ ] Add due-task polling.
- [ ] Add clean shutdown handling.
- [ ] Add systemd user service template for daemon mode.
- [ ] Add journal-friendly logs.
- [ ] Add tests for due-task selection.
- [ ] Verify restart behavior.
- [ ] Verify reboot behavior under linger.

### Review Gate

Groundhog can run continuously and recover cleanly after restart.

## Phase 7: Agentic Reasoning

Status: Planned

### Goal

Use the local LLM for explanation, summarization, prioritization, and natural
language interaction without letting it silently mutate core data.

### Tasks

- [ ] Define allowed LLM actions.
- [ ] Define forbidden LLM actions.
- [ ] Add daily summary generation over events.
- [ ] Add weekly review generation over events and metrics.
- [ ] Add outbox prioritization.
- [ ] Log LLM-generated summaries as derived artifacts.
- [ ] Keep all LLM calls local via configured Ollama.
- [ ] Add evaluation prompts for summaries.
- [ ] Add review workflow before automated delivery.

### Review Gate

LLM behavior is local-only, tool-mediated, logged, and reversible.

## Phase Discipline

For every phase:

1. Design the schema or API.
2. Write focused tests.
3. Implement the smallest useful version.
4. Run offline regression tests.
5. Run live smoke tests when relevant.
6. Update docs.
7. Stop for user review.
