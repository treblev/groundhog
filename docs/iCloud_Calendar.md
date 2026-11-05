# Calendar MCP integration

Groundhog and OpenClaw share a standalone calendar MCP server. Its tools are
provider-neutral; a Python CalDAV adapter connects to iCloud. iCloud remains the
source of truth. No calendar events are copied into DuckDB.

```text
OpenClaw MCP client ─────┐
                        ├→ calendar MCP server → CalDAV adapter → iCloud
Groundhog local agent ──┘          │
                                  ├→ encrypted credentials + OS keyring
                                  └→ private operation audit
```

This replaces the earlier native OpenClaw calendar plugin and JSON subprocess
bridge. The existing Groundhog data MCP server stays separate.

## Tools and behavior

| Tool | Purpose |
| --- | --- |
| `calendar_list` | Discover names, opaque IDs, and selected permissions |
| `calendar_events` | Live agenda with expanded recurring occurrences |
| `calendar_get_event` | Current event, opaque resource ID, and ETag |
| `calendar_find_free_slots` | Calculate availability across selected calendars |
| `calendar_create_event` | Create one event after human confirmation |
| `calendar_update_event` | Patch an event after human confirmation |
| `calendar_delete_event` | Delete one event after explicit human confirmation |

All three write tools request approval through MCP form elicitation. The
empty form becomes a native Allow/Decline card containing the exact calendar
and proposed change. Approval is not a
model-supplied tool argument. The trusted MCP client must present the request to
the human; it must never auto-approve. Unsupported, declined, or cancelled
approval prevents the write. Read/write calendar selection is enforced again by
the backend. These lists restrict this service, not the Apple credential's
underlying account access.

The confirmation uses a compact single-line representation bounded to 1,000
UTF-16 units. OpenClaw 2026.10.4 rejects multiline standard-form messages and
limits them to 1,024 units. Oversized changes fail before approval; they are
never truncated and then executed. Updates show changed fields plus the event's
identifying title/time, without repeating unchanged private notes.

Writes use a stable create request UUID and conditional HTTP requests to prevent
duplicate creation and lost concurrent edits. Update arguments are patches;
omitted fields, alarms, and unknown iCalendar properties are preserved. Fetch the
current ETag before editing or deleting. After a conflict, fetch again and
reassess. After a network timeout, inspect the event before retrying; retain the
same create request UUID.

Timed input requires an explicit ISO offset. Writes store UTC instants and reads
display the configured timezone. All-day dates use exclusive ends: November 2
is `start=2026-11-02`, `end=2026-11-03`, `all_day=true`.

Agenda windows are limited to 93 days. Availability windows are limited to 31
days and default to every selected calendar. The service combines overlapping
busy intervals, blocks all-day events, and ignores transparent events. It
returns the earliest candidate of the requested duration in each maximal free
interval. Truncated event retrieval fails availability calculation rather than
claiming missing events are free time. Availability is a snapshot, not a
reservation; another client can book the same time afterward.

Agenda results are filtered against the requested window after retrieval.
All-day dates use midnight in the configured timezone, with an exclusive end;
an adjacent day's all-day event must not appear merely because Apple's query
matched it using UTC boundaries.

Event descriptions are omitted by default. Request them explicitly through
`calendar_get_event(include_description=true)` when needed. Event IDs encode
provider resource URLs so updates do not depend on UID search; treat them as
opaque identifiers, not secrets or authorization tokens. The backend still
validates their calendar membership. Recurring events are readable; recurring
series/occurrence writes and invitation changes are outside this version.

## Install and configure

On the Linux host, run under the existing `openclaw` account:

```bash
cd /home/openclaw/apps/groundhog
venv/bin/python -m pip install -r requirements-calendar.txt
```

Create a private configuration outside Git from
`deploy/openclaw/calendar-config.example.json`. Replace its paths and account
label with deployment values. Start with no selected calendars:

```json
{
  "provider": "icloud",
  "provider_url": "https://caldav.icloud.com/",
  "timezone": "America/Phoenix",
  "credential_file": "/home/openclaw/.config/groundhog/calendar/credentials.enc",
  "keyring_service": "groundhog.calendar",
  "keyring_account": "personal-icloud",
  "audit_path": "/home/openclaw/data/groundhog/calendar-audit.jsonl",
  "actor": "openclaw-owner",
  "calendars": []
}
```

All deployment paths are explicit in this standalone service's configuration;
it neither imports Groundhog database settings nor requires `GROUNDHOG_DB_PATH`.
Keep the configuration and its parent directory private. Use a unique
keyring service/account pair for each connected account.

Generate a dedicated Apple app-specific password with two-factor authentication
enabled. Connect interactively on the host; passwords never belong in chat,
argv, model prompts, or configuration literals:

```bash
venv/bin/python -m calendar_mcp connect --config /home/openclaw/.config/groundhog/calendar/config.json
venv/bin/python -m calendar_mcp select --config /home/openclaw/.config/groundhog/calendar/config.json
```

The setup command encrypts the Apple email and password with Fernet. The key
lives in the OS keyring, separate from the encrypted file. There is no plaintext
or environment-key fallback. Linux needs an unlocked Secret Service/KWallet
keyring reachable from the Gateway's D-Bus session, including after restart.
macOS uses Keychain. Provision that keyring as the service user; a desktop
login's unlocked keyring may not be available to a headless Gateway.

`select` lists calendars in the private terminal, then asks which to read and
which selected calendars may accept writes. Empty write selection keeps all
read-only. It saves the selection with stable IDs and preserves other settings.
Operator-only `discover` remains available when actual URLs are needed. The saved
configuration has this shape:

```json
"calendars": [
  {
    "id": "personal",
    "url": "https://pNN-caldav.icloud.com/ACCOUNT/calendars/CALENDAR/",
    "writable": true
  }
]
```

Copy actual discovered URLs, not the placeholders. Calendar names can be
duplicated or renamed. Calendars with `writable=false` remain read-only.

## Connect OpenClaw

[OpenClaw documents](https://docs.openclaw.ai/tools/mcp) local MCP servers under
`mcp.servers`. Merge this definition into the existing Gateway configuration:

```json
{
  "mcp": {
    "servers": {
      "calendar": {
        "transport": "stdio",
        "command": "/home/openclaw/apps/groundhog/venv/bin/python",
        "args": ["-m", "calendar_mcp", "serve", "--config", "/home/openclaw/.config/groundhog/calendar/config.json"],
        "cwd": "/home/openclaw/apps/groundhog",
        "env": {
          "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/UID/bus",
          "XDG_RUNTIME_DIR": "/run/user/UID"
        },
        "enabled": true,
        "requestTimeoutMs": 240000,
        "toolFilter": {"include": ["calendar_*"]}
      }
    }
  }
}
```

Use the existing local Ollama agent; calendar data must not reach an external AI
provider. Preserve the current tool policy. If the previous plugin was installed,
disable/remove its `groundhog-icloud-calendar` entry and installation.

Replace `UID` with the service user's numeric UID, using the Gateway's actual
D-Bus session environment. OpenClaw's stdio transport inherits a small default
environment; it does not automatically forward these keyring variables. An
SSH-shell credential check and `mcp probe` can pass while a real calendar call
fails. Keep Apple credentials and encryption keys out of this environment.

Copy `deploy/openclaw/skills/groundhog-calendar/` into the intended OpenClaw
workspace's skills directory. It explains when to use calendar tools and how to
handle availability, approval, and conflicts. Then check:

```bash
openclaw --version
openclaw mcp doctor calendar
openclaw mcp probe calendar --json
```

Verify that the installed runtime displays MCP form elicitations to the owner.
The installed OpenClaw 2026.10.4 standard MCP handler and native card renderer
were checked using synthetic event details and a fake Gateway: Allow, Decline,
expiry, and complete preview visibility passed. This check sends no Telegram
messages and makes no iCloud requests. Live Telegram delivery and owner responses
still need an end-to-end check after account setup. A runtime that cannot present
confirmation can still read calendars; writes return `approval_required`.

The isolated renderer check lives in `scripts/check_calendar_mcp_approval.mjs`.
It reads an elicitation request as JSON from stdin, accepts `--openclaw-root`,
and imports that installation's form compiler and card builder. It is a
version-sensitive diagnostic, not a runtime dependency or an approval bypass.

Verify real reads through the installed OpenClaw transport too:

```bash
node scripts/check_calendar_mcp_connection.mjs \
  --openclaw-root /home/openclaw/.npm-global/lib/node_modules/openclaw \
  --config /home/openclaw/.openclaw/openclaw.json \
  --start 2026-11-05T00:00:00-07:00 --end 2026-11-06T00:00:00-07:00
```

Use a suitable one-day window. This diagnostic calls only read tools and prints
counts and elapsed time. It uses the configured environment and OpenClaw's
actual stdio launcher, without sending Telegram messages or involving a model.
It is version-sensitive like the renderer check. A successful result verifies
transport and calendar access, not the local model's answer latency. Calendar
requests must stop on access errors; shell-based repairs and direct provider
fallbacks do not belong in a scheduling turn.

## Connect Groundhog's agent

Set `GROUNDHOG_CALENDAR_CONFIG` to the same private config path in the Groundhog
process environment. `/ask` then discovers tools from the separate calendar MCP
server, alongside the existing database server. Calendar scheduling requests use
the model's tool selection instead of the workout-history routing shortcut.

The terminal REPL and interactive one-question CLI display write confirmation
directly to the human. For example:

```bash
GROUNDHOG_CALENDAR_CONFIG=/home/openclaw/.config/groundhog/calendar/config.json venv/bin/python langgraph_client/client.py
venv/bin/python scripts/ask_groundhog.py --calendar-config /home/openclaw/.config/groundhog/calendar/config.json "What is on my calendar tomorrow?"
```

Programmatic callers can pass `calendar_config` and a trusted
`calendar_elicitation_callback` to `ask_question`. That callback must obtain the
human's decision, not generate it with a model. Noninteractive Telegram `/ask`
supports calendar reads; it has no confirmation callback and cannot commit
writes. Use OpenClaw's direct MCP route with a supported approval UI for writes.

## Audit, isolation, and disconnect

The private audit records actor, operation, local calendar ID, a hashed event or
create-request identifier, correlation ID, time, and outcome. It excludes titles,
notes, locations, account URLs, and credentials. The actor comes from the trusted
process configuration. An unavailable audit log prevents writes. A failure after
a remote request starts is reported as uncertain, never as confirmed success.

This deployment is single-user local stdio. The OS user and trusted MCP client
are the authorization boundary. For another user/account, run a separately
configured process under its own OS identity and secret store. There is no
unauthenticated remote HTTP listener and no claim of multi-user authorization.

To disconnect locally:

```bash
venv/bin/python -m calendar_mcp disconnect --config /home/openclaw/.config/groundhog/calendar/config.json
```

This removes the encrypted credentials and their keyring key. Revoke the
app-specific password at Apple to revoke account access. No background sync or
reminder schedule is installed. Add proactive monitoring later through the
existing OpenClaw scheduling mechanism.

## Verification

```bash
venv/bin/python -m unittest tests.test_icloud_calendar tests.test_calendar_mcp tests.test_ask_groundhog
venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Tests use synthetic calendars and fake credentials. They cover the real MCP
handshake, catalog, human approval, decline, unsupported approval, encrypted
storage, audit failures, availability, UTC serialization, retries, and ETag
conflicts. Optional calendar tests skip when calendar dependencies are absent.

After deployment, compare an agenda against Apple's Calendar app. In a selected
test calendar, decline a create and verify no event appears. Approve a create,
retry its same request UUID, and verify one event. Update it, then edit it in
Apple's app and verify an old ETag cannot overwrite that edit. Confirm deletion
of the test event. Repeat the credential-read check after a Gateway restart.

Technical references: [OpenClaw MCP](https://docs.openclaw.ai/tools/mcp),
[skills](https://docs.openclaw.ai/tools/skills),
[Apple app-specific passwords](https://support.apple.com/en-us/102654),
[Python CalDAV iCloud compatibility](https://github.com/python-caldav/caldav/releases/tag/v3.7.0),
and [OS keyring setup](https://pypi.org/project/keyring/).
