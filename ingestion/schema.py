
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from config.settings import DB_PATH

def init_db(db_path: Path | str | None = None):
    con = duckdb.connect(str(db_path or DB_PATH))
    
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
            open DECIMAL(10, 2),
            high DECIMAL(10, 2),
            low DECIMAL(10, 2),
            closing_price DECIMAL(10, 2),
            volume BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, ticker)
        )
    """)

    con.execute("ALTER TABLE stock_watchlist ADD COLUMN IF NOT EXISTS open DECIMAL(10, 2)")
    con.execute("ALTER TABLE stock_watchlist ADD COLUMN IF NOT EXISTS high DECIMAL(10, 2)")
    con.execute("ALTER TABLE stock_watchlist ADD COLUMN IF NOT EXISTS low DECIMAL(10, 2)")
    con.execute("ALTER TABLE stock_watchlist ADD COLUMN IF NOT EXISTS volume BIGINT")

    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_signals (
            id VARCHAR PRIMARY KEY,
            date DATE,
            ticker VARCHAR,
            signal_type VARCHAR,
            timeframe VARCHAR,
            value DECIMAL(10, 4),
            direction VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_alerts (
            id VARCHAR PRIMARY KEY,
            date DATE,
            ticker VARCHAR,
            alert_type VARCHAR,
            message VARCHAR,
            notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            max_hr INTEGER,
            calories INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        ALTER TABLE activities ADD COLUMN IF NOT EXISTS max_hr INTEGER
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS sleep_metrics (
            date DATE PRIMARY KEY,
            resting_hr INTEGER,
            hrv INTEGER,
            breath_rate DECIMAL(4,1),
            time_to_fall_asleep_minutes INTEGER,
            deep_sleep_minutes INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("ALTER TABLE sleep_metrics ADD COLUMN IF NOT EXISTS time_to_fall_asleep_minutes INTEGER")
    con.execute("ALTER TABLE sleep_metrics ADD COLUMN IF NOT EXISTS deep_sleep_minutes INTEGER")

    con.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            id VARCHAR PRIMARY KEY,
            date DATE,
            day_of_week VARCHAR,
            name VARCHAR,
            category VARCHAR,
            structure_type VARCHAR,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            id VARCHAR PRIMARY KEY,
            fact TEXT,
            embedding FLOAT[],
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id VARCHAR PRIMARY KEY,
            job_name VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            error_text TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id VARCHAR PRIMARY KEY,
            event_type VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            subject_type VARCHAR NOT NULL,
            subject_id VARCHAR NOT NULL,
            dedupe_key VARCHAR UNIQUE NOT NULL,
            occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            payload JSON NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.close()

if __name__ == "__main__":
    init_db()
    print("✅ Database initialized.")
