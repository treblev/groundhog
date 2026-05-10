
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from config.settings import DB_PATH

def init_db():
    con = duckdb.connect(DB_PATH)
    
    con.execute("""
        CREATE TABLE IF NOT EXISTS health_metrics (
            date DATE PRIMARY KEY,
            steps INTEGER,
            avg_hr INTEGER,
            active_minutes INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_watchlist (
            date DATE,
            ticker VARCHAR,
            closing_price DECIMAL(10, 2),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, ticker)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id VARCHAR,
            title VARCHAR,
            state VARCHAR,  -- created, snoozed, completed
            due_date DATE,
            valid_from TIMESTAMP,
            valid_to TIMESTAMP,
            is_current BOOLEAN,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id VARCHAR PRIMARY KEY,
            date DATE,
            activity_type VARCHAR,
            distance_miles DECIMAL(5,2),
            duration_seconds INTEGER,
            avg_pace_seconds_per_mile INTEGER,
            avg_hr INTEGER,
            calories INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.close()

if __name__ == "__main__":
    init_db()
    print("✅ Database initialized.")