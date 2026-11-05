"""Calendar MCP lifecycle and local credential setup. Never accepts passwords on argv."""

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calendar_mcp.credentials import SecretStore
from calendar_mcp.provider import open_provider
from calendar_mcp.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "connect", "select", "disconnect", "discover"))
    parser.add_argument("--config", required=True, help="Absolute path to the private calendar service configuration")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # Third-party diagnostics can include event bodies.
    try:
        settings = Settings.load(args.config)
        if args.action == "serve":
            from calendar_mcp.server import create_server
            create_server(settings).run(transport="stdio")
        elif args.action == "connect":
            if not sys.stdin.isatty():
                raise ValueError("Connect interactively in a trusted local terminal.")
            username = input("Apple Account email: ")
            password = getpass.getpass("Dedicated app-specific password: ")
            SecretStore(settings).set(username, password)
            print("Calendar credentials encrypted. Run select to verify authentication and choose calendars.")
        elif args.action == "disconnect":
            SecretStore(settings).disconnect()
            print("Local calendar credentials removed. Revoke the app-specific password at Apple to revoke account access.")
        elif args.action == "select":
            if not sys.stdin.isatty():
                raise ValueError("Select calendars interactively in a trusted local terminal.")
            from calendar_mcp.onboarding import select_calendars
            print(json.dumps(select_calendars(settings, args.config)))
        else:
            with open_provider(settings) as provider:
                # Operator-only discovery includes resource URLs for configuring selection.
                print(json.dumps(provider.calendars(), indent=2))
        return 0
    except Exception:
        print("Calendar operation failed. Check configuration, the unlocked OS keyring, credentials, and connectivity.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
