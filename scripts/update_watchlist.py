import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from bs4 import BeautifulSoup

from config.settings import WATCHLIST_FILE

DEFAULT_PERIOD = "2y"


def fetch_nasdaq100() -> set[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; groundhog-watchlist/1.0)"}
    response = httpx.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=headers, timeout=30.0)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    table = soup.find("table", {"id": "constituents"})
    if not table:
        raise ValueError("Could not find constituents table on Wikipedia.")

    tickers = set()
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if cells:
            tickers.add(cells[0].get_text(strip=True))
    return tickers


def load_watchlist() -> dict[str, str]:
    existing = {}
    for line in WATCHLIST_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        existing[parts[0]] = parts[1] if len(parts) > 1 else DEFAULT_PERIOD
    return existing


def write_watchlist(watchlist: dict[str, str]) -> None:
    lines = [f"{ticker} {period}" for ticker, period in sorted(watchlist.items())]
    WATCHLIST_FILE.write_text("\n".join(lines) + "\n")


def run() -> None:
    print("Fetching Nasdaq-100 components from Wikipedia...")
    nasdaq100 = fetch_nasdaq100()
    print(f"  Found {len(nasdaq100)} tickers")

    existing = load_watchlist()
    print(f"  Existing watchlist: {len(existing)} tickers")

    added = []
    merged = dict(existing)
    for ticker in nasdaq100:
        if ticker not in merged:
            merged[ticker] = DEFAULT_PERIOD
            added.append(ticker)

    write_watchlist(merged)
    print(f"  Added {len(added)} new tickers: {', '.join(sorted(added))}")
    print(f"  Total: {len(merged)} tickers")
    print("Done. Run ingestion/stocks.py to backfill new tickers.")


if __name__ == "__main__":
    run()
