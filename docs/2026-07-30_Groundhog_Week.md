# A Week of Making Groundhog Operational

Over the past week, Groundhog moved from a collection of useful local scripts
into a more dependable personal-data system: it can receive data through
Telegram, explain what it knows through a guarded local agent, deliver stock
alerts, and report whether its scheduled work actually ran.

The goal was not to make a chatbot that sounds informed. It was to make a
local system that can collect personal data, preserve a clear audit trail, and
answer from facts stored in its own database.

## Every job now leaves a trail

The daily stock pipeline now records the beginning, outcome, and any traceback
for each run. It also emits durable events for completed and failed jobs, as
well as for stock-signal changes. Those events feed an outbox table, separating
the fact that something happened from the decision to deliver a message.

That separation turned out to be useful. Groundhog can create a deduplicated
stock alert without assuming that a desktop notification or a chat message was
actually delivered. OpenClaw reads the pending outbox item, sends it through
Telegram, and marks it delivered only after the send succeeds.

There is also now a small operational CLI for checking service status, schema,
and read-only database queries. When something goes wrong, the question is no
longer “did the script run?” but “which run failed, where, and what did it
record?”

## Telegram became a real intake and delivery channel

Telegram support grew in both directions.

On the inbound side, Groundhog can deterministically import activity-result,
workout-plan, and sleep screenshots that OpenClaw receives. A one-minute local
watcher processes new media once, checkpoints what it has seen, and keeps the
date anchored to reliable metadata rather than OCR guesses. Workout cards are
merged into a single plan when appropriate; pool-swim details and Telegram
caption hints improve activity imports.

On the outbound side, a separate OpenClaw job checks the Groundhog outbox every
15 minutes. The delivery bridge is deliberately narrow: read pending messages,
send them through the configured Telegram channel, then record success. It has
already been exercised end to end with both a real stock alert and a synthetic
delivery test.

## The local agent got more useful—and more constrained

Telegram text can now use `/ask` to reach the LangGraph-backed Groundhog agent.
The agent runs against local tools and the local Ollama models, with explicit
limits on tool calls, database-grounding retries, and a guard against exposing
internal implementation details.

It gained dedicated tools for activity summaries, sleep summaries, workout
lookup, data freshness, service status, recent alerts, and a market summary
that includes Bitcoin. The point of those tools is precision: a question about
the latest close or recent sleep should come from DuckDB, not from model
memory.

Local summary artifacts were added as well. Daily summaries and weekly reviews
are generated only from stored facts, saved for review, and never silently sent
as if they were source data.

## Market data now works across the whole week

Bitcoin no longer goes dark over the weekend. A dedicated weekend job stores a
current `BTC-USD` price on Saturday and Sunday without rerunning the entire
equity, signals, and alerts pipeline.

The weekday stock job remains focused on its normal work: fetch prices,
calculate SMA and Supertrend signals, and create deduplicated alerts on
direction changes. A read-only service CLI makes the latest job and database
state inspectable without opening a Python shell.

The market-summary tool brings the latest Bitcoin price and day-over-day change
together with current Supertrend direction and recent alerts. It is a compact
status view, not investment advice.

## Scheduling moved to the orchestration layer

The production weekday stock job now runs through OpenClaw cron at exactly
5:00 PM America/Phoenix, Monday through Friday. The former systemd stock timer
was disabled so the pipeline cannot run twice at the close. The underlying
systemd service remains available as a manual fallback.

This puts chat, scheduling, and delivery in one orchestration layer while
Groundhog stays responsible for local ingestion, analytics, and query tools.
The first OpenClaw-triggered run completed successfully, fetched the watchlist,
calculated signals, and recorded its result in DuckDB.

## What is next

The next idea is deliberately more personal: local semantic retrieval over
workout plans. Rather than treating “leg-heavy,” “HYROX-style,” or
“lower-body conditioning” as a rigid set of tags, Groundhog can eventually
rank stored workout descriptions with local embeddings. The proposed experience
is simple: ask for a kind of workout, receive a short numbered list, choose one,
and display the complete stored plan. It will be read-only—no hidden workout
scheduling or training-plan changes.

That work is still a plan, not a shipped feature. For now, the important gain is
more basic and more valuable: Groundhog has become a local system whose inputs,
scheduled work, alerts, and answers are easier to trust.
