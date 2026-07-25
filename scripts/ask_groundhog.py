"""Answer one Telegram /ask question through the guarded Groundhog agent."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client.client import ask_question


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+", help="Question to ask Groundhog")
    args = parser.parse_args()

    try:
        print(await ask_question(" ".join(args.question)))
    except Exception as error:
        print(f"Groundhog could not answer that question: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
