"""Deliver Groundhog outbox items through OpenClaw Telegram.

OpenClaw owns delivery. This bridge uses Groundhog MCP for outbox state and
OpenClaw's configured Telegram channel for transport. It marks an outbox row as
delivered only after OpenClaw reports a successful send.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _payload_dict(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload.strip():
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"message": payload}
    return {}


def _format_message(item: dict[str, Any]) -> str:
    payload = _payload_dict(item)
    message = payload.get("message")
    if message:
        return str(message)

    event_type = item.get("event_type", "groundhog_event")
    details = [
        str(payload[key])
        for key in ("ticker", "alert_type", "date")
        if payload.get(key)
    ]
    suffix = f" ({', '.join(details)})" if details else ""
    return f"Groundhog {event_type}{suffix}"


async def _call_groundhog_tool(name: str, args: dict[str, Any]) -> Any:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(PROJECT_ROOT),
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            text = result.content[0].text if result.content else "null"
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text


def _send_telegram(target: str, message: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"DRY RUN send to {target}: {message}")
        return True

    proc = subprocess.run(
        [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            target,
            "--message",
            message,
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.returncode != 0:
        return False
    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    payload = response.get("payload") if isinstance(response, dict) else None
    return bool((payload or {}).get("ok", response.get("ok", False)))


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deliver pending Groundhog outbox rows via OpenClaw Telegram."
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--target",
        default=os.environ.get("OPENCLAW_TELEGRAM_TARGET"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.target:
        parser.error("set --target or OPENCLAW_TELEGRAM_TARGET")

    pending = await _call_groundhog_tool("get_pending_outbox", {"limit": args.limit})
    if not pending:
        print("Groundhog outbox: no pending items.")
        return 0
    if not isinstance(pending, list):
        print(f"Unexpected get_pending_outbox result: {pending!r}", file=sys.stderr)
        return 2

    delivered = 0
    failed = 0
    for item in pending:
        outbox_id = item.get("id")
        if not outbox_id:
            print(f"Skipping item without id: {item!r}", file=sys.stderr)
            failed += 1
            continue

        if not _send_telegram(args.target, _format_message(item), args.dry_run):
            print(f"Delivery failed; leaving outbox pending: {outbox_id}", file=sys.stderr)
            failed += 1
            continue

        if not args.dry_run:
            await _call_groundhog_tool(
                "mark_outbox_delivered",
                {"outbox_id": outbox_id},
            )
            print(f"Delivered Groundhog outbox item: {outbox_id}")
        else:
            print(f"Would deliver Groundhog outbox item: {outbox_id}")
        delivered += 1

    action = "would_deliver" if args.dry_run else "delivered"
    print(f"Groundhog outbox delivery complete: {action}={delivered} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
