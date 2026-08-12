"""Remove prematurely recorded future-dated weekly Supertrend artifacts."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json

import duckdb

from config.settings import DB_PATH

PHOENIX = ZoneInfo("America/Phoenix")
WEEKLY_ALERT_TYPES = (
    "supertrend_weekly_bullish",
    "supertrend_weekly_bearish",
)


def repair(
    db_path: Path | str = DB_PATH,
    *,
    as_of_date=None,
    dry_run: bool = False,
) -> dict:
    """Remove only weekly Supertrend facts whose stored date is still in the future."""
    cutoff = as_of_date or datetime.now(PHOENIX).date()
    con = duckdb.connect(str(db_path))
    transaction_started = False
    try:
        alert_ids = [
            row[0]
            for row in con.execute(
                """
                SELECT id FROM stock_alerts
                WHERE alert_type IN (?, ?) AND date > ?
                """,
                [*WEEKLY_ALERT_TYPES, cutoff],
            ).fetchall()
        ]
        event_ids = [
            row[0]
            for row in con.execute(
                """
                SELECT id FROM events
                WHERE (subject_type = 'stock_alert' AND subject_id IN (SELECT UNNEST(?)))
                   OR (event_type = 'stock_signal_flipped'
                       AND json_extract_string(payload, '$.signal_type') = 'supertrend'
                       AND json_extract_string(payload, '$.timeframe') = 'weekly'
                       AND CAST(json_extract_string(payload, '$.date') AS DATE) > ?)
                """,
                [alert_ids or [""], cutoff],
            ).fetchall()
        ]
        signal_count = con.execute(
            """
            SELECT COUNT(*) FROM stock_signals
            WHERE signal_type = 'supertrend' AND timeframe = 'weekly' AND date > ?
            """,
            [cutoff],
        ).fetchone()[0]
        result = {
            "as_of_date": cutoff.isoformat(),
            "dry_run": dry_run,
            "stock_alerts": len(alert_ids),
            "events": len(event_ids),
            "outbox": len(event_ids),
            "semantic_chunks": len(alert_ids),
            "stock_signals": signal_count,
        }
        if dry_run:
            return result

        con.execute("BEGIN")
        transaction_started = True
        if alert_ids:
            con.execute(
                "DELETE FROM semantic_chunks WHERE domain = 'stock_alert' AND source_id IN (SELECT UNNEST(?))",
                [alert_ids],
            )
        if event_ids:
            con.execute("DELETE FROM outbox WHERE event_id IN (SELECT UNNEST(?))", [event_ids])
            con.execute("DELETE FROM events WHERE id IN (SELECT UNNEST(?))", [event_ids])
        if alert_ids:
            con.execute("DELETE FROM stock_alerts WHERE id IN (SELECT UNNEST(?))", [alert_ids])
        con.execute(
            """
            DELETE FROM stock_signals
            WHERE signal_type = 'supertrend' AND timeframe = 'weekly' AND date > ?
            """,
            [cutoff],
        )
        con.execute("COMMIT")
        transaction_started = False
        return result
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair(dry_run=args.dry_run), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
