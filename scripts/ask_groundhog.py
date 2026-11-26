"""Answer one Telegram /ask question through the guarded Groundhog agent."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client.client import ask_question
from langgraph_client.calendar import terminal_calendar_approval


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+", help="Question to ask Groundhog")
    parser.add_argument("--calendar-config", help="Private configuration path for the separate calendar MCP server")
    args = parser.parse_args()

    try:
        options = {"calendar_config": args.calendar_config} if args.calendar_config else {}
        if sys.stdin.isatty():
            options["calendar_elicitation_callback"] = terminal_calendar_approval
        print(await ask_question(" ".join(args.question), **options))
    except Exception as error:
        print(f"Groundhog could not answer that question: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
