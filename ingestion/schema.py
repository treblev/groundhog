
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
        CREATE TABLE IF NOT EXISTS stock_notes (
            id VARCHAR PRIMARY KEY,
            ticker VARCHAR NOT NULL,
            note TEXT NOT NULL,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_note_revisions (
            note_id VARCHAR NOT NULL,
            revision INTEGER NOT NULL,
            note TEXT NOT NULL,
            action VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (note_id, revision)
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
            pool_distance DECIMAL(7,2),
            pool_distance_unit VARCHAR,
            laps INTEGER,
            stroke VARCHAR,
            swim_pace_seconds_per_100 INTEGER,
            swim_pace_unit VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        ALTER TABLE activities ADD COLUMN IF NOT EXISTS max_hr INTEGER
    """)
    con.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS pool_distance DECIMAL(7,2)")
    con.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS pool_distance_unit VARCHAR")
    con.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS laps INTEGER")
    con.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS stroke VARCHAR")
    con.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS swim_pace_seconds_per_100 INTEGER")
    con.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS swim_pace_unit VARCHAR")

    # Garmin provides a much richer activity record than the screenshot
    # importer can see.  Keep the existing, human-friendly summary columns
    # above for compatibility, and add the scalar Garmin fields here.  Values
    # that repeat (laps, splits, and timestamped sensor readings) live in the
    # child tables below rather than being packed into JSON in one row.
    for column, data_type in (
        ("activity_source", "VARCHAR"),
        ("garmin_activity_id", "BIGINT"),
        ("garmin_user_id", "BIGINT"),
        ("activity_name", "VARCHAR"),
        ("activity_type_key", "VARCHAR"),
        ("event_type_key", "VARCHAR"),
        ("start_at", "TIMESTAMP"),
        ("end_at", "TIMESTAMP"),
        ("timezone_offset_hours", "DOUBLE"),
        ("elapsed_duration_seconds", "DOUBLE"),
        ("moving_duration_seconds", "DOUBLE"),
        ("distance_meters", "DOUBLE"),
        ("lap_count", "INTEGER"),
        ("average_speed_mps", "DOUBLE"),
        ("max_speed_mps", "DOUBLE"),
        ("start_latitude", "DOUBLE"),
        ("start_longitude", "DOUBLE"),
        ("end_latitude", "DOUBLE"),
        ("end_longitude", "DOUBLE"),
        ("location_name", "VARCHAR"),
        ("aerobic_training_effect", "DOUBLE"),
        ("aerobic_training_effect_message", "VARCHAR"),
        ("anaerobic_training_effect", "DOUBLE"),
        ("anaerobic_training_effect_message", "VARCHAR"),
        ("training_effect_label", "VARCHAR"),
        ("activity_training_load", "DOUBLE"),
        ("difference_body_battery", "INTEGER"),
        ("moderate_intensity_minutes", "INTEGER"),
        ("vigorous_intensity_minutes", "INTEGER"),
        ("bmr_calories", "DOUBLE"),
        ("water_estimated", "DOUBLE"),
        ("hr_time_in_zone_1", "DOUBLE"),
        ("hr_time_in_zone_2", "DOUBLE"),
        ("hr_time_in_zone_3", "DOUBLE"),
        ("hr_time_in_zone_4", "DOUBLE"),
        ("hr_time_in_zone_5", "DOUBLE"),
        ("device_id", "BIGINT"),
        ("device_manufacturer", "VARCHAR"),
        ("garmin_time_zone_id", "INTEGER"),
        ("has_polyline", "BOOLEAN"),
        ("has_images", "BOOLEAN"),
        ("has_video", "BOOLEAN"),
        ("has_splits", "BOOLEAN"),
        ("has_heat_map", "BOOLEAN"),
        ("is_parent_activity", "BOOLEAN"),
        ("purposeful", "BOOLEAN"),
        ("favorite", "BOOLEAN"),
        ("elevation_corrected", "BOOLEAN"),
        ("atp_activity", "BOOLEAN"),
        ("manual_activity", "BOOLEAN"),
        ("personal_record", "BOOLEAN"),
        ("auto_calc_calories", "BOOLEAN"),
        ("ts_data_available", "BOOLEAN"),
        ("parent_garmin_activity_id", "BIGINT"),
    ):
        con.execute(f"ALTER TABLE activities ADD COLUMN IF NOT EXISTS {column} {data_type}")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS activities_garmin_activity_id_uidx "
        "ON activities(garmin_activity_id)"
    )

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_activity_metrics (
            activity_id VARCHAR NOT NULL,
            metric_group VARCHAR NOT NULL,
            metric_name VARCHAR NOT NULL,
            value DOUBLE,
            text_value VARCHAR,
            units VARCHAR,
            PRIMARY KEY (activity_id, metric_group, metric_name)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_activity_lap_metrics (
            activity_id VARCHAR NOT NULL,
            lap_index INTEGER NOT NULL,
            metric_name VARCHAR NOT NULL,
            value DOUBLE,
            units VARCHAR,
            PRIMARY KEY (activity_id, lap_index, metric_name)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_activity_split_metrics (
            activity_id VARCHAR NOT NULL,
            split_index INTEGER NOT NULL,
            split_type VARCHAR,
            metric_name VARCHAR NOT NULL,
            value DOUBLE,
            units VARCHAR,
            PRIMARY KEY (activity_id, split_index, metric_name)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_activity_samples (
            activity_id VARCHAR NOT NULL,
            sample_timestamp TIMESTAMP NOT NULL,
            metric_name VARCHAR NOT NULL,
            value DOUBLE,
            units VARCHAR,
            PRIMARY KEY (activity_id, sample_timestamp, metric_name)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_activity_paths (
            activity_id VARCHAR PRIMARY KEY,
            path_json JSON NOT NULL,
            point_count INTEGER NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_swimming_aggregates (
            activity_id VARCHAR PRIMARY KEY,
            pool_length_cm DOUBLE,
            active_lengths INTEGER,
            strokes DOUBLE,
            avg_stroke_distance_m DOUBLE,
            avg_strokes DOUBLE,
            avg_swim_cadence DOUBLE,
            avg_swolf DOUBLE
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_strength_exercises (
            activity_id VARCHAR NOT NULL,
            exercise_category VARCHAR,
            exercise_name VARCHAR,
            sets INTEGER,
            reps INTEGER,
            volume DOUBLE,
            duration_ms DOUBLE,
            max_weight DOUBLE,
            PRIMARY KEY (activity_id, exercise_category, exercise_name)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS garmin_strength_sets (
            activity_id VARCHAR NOT NULL,
            set_index INTEGER NOT NULL,
            set_type VARCHAR,
            start_time TIMESTAMP,
            duration_seconds DOUBLE,
            wkt_step_index INTEGER,
            repetition_count INTEGER,
            weight DOUBLE,
            exercise_category VARCHAR,
            exercise_name VARCHAR,
            exercise_probability DOUBLE,
            PRIMARY KEY (activity_id, set_index)
        )
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
            embedding_model VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding_model VARCHAR")

    con.execute("""
        CREATE TABLE IF NOT EXISTS semantic_chunks (
            id VARCHAR PRIMARY KEY,
            domain VARCHAR NOT NULL,
            source_id VARCHAR NOT NULL,
            source_date DATE,
            chunk_kind VARCHAR NOT NULL,
            chunk_index INTEGER NOT NULL,
            section_label VARCHAR,
            title VARCHAR,
            content TEXT NOT NULL,
            metadata JSON NOT NULL,
            content_hash VARCHAR NOT NULL,
            embedding_model VARCHAR NOT NULL,
            embedding FLOAT[] NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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

    con.execute("""
        CREATE TABLE IF NOT EXISTS outbox (
            id VARCHAR PRIMARY KEY,
            event_id VARCHAR UNIQUE NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            delivered_at TIMESTAMP,
            delivery_error TEXT
        )
    """)
    con.execute("ALTER TABLE outbox ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0")
    con.execute("ALTER TABLE outbox ADD COLUMN IF NOT EXISTS priority_reason VARCHAR")

    con.execute("""
        CREATE TABLE IF NOT EXISTS media_ingestion_jobs (
            id VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL,
            source_channel VARCHAR NOT NULL,
            source_message_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL CHECK (kind IN ('activity', 'expense')),
            spool_path VARCHAR NOT NULL,
            caption TEXT,
            status VARCHAR NOT NULL CHECK (
                status IN ('queued', 'processing', 'retry_wait', 'imported', 'needs_review')
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            lease_owner VARCHAR,
            lease_expires_at TIMESTAMP,
            error_code VARCHAR,
            error_text TEXT,
            result_json JSON,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            UNIQUE (source_channel, source_message_id, content_hash, kind)
        )
    """)

    media_kind_checks = con.execute(
        """
        SELECT constraint_text
        FROM duckdb_constraints()
        WHERE table_name = 'media_ingestion_jobs' AND constraint_type = 'CHECK'
          AND constraint_text LIKE '%kind%'
        """
    ).fetchall()
    if media_kind_checks and not any("'expense'" in row[0] for row in media_kind_checks):
        # DuckDB cannot alter or drop a CHECK constraint. Rebuild the table in
        # one transaction so deployed activity jobs are preserved atomically.
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("DROP TABLE IF EXISTS media_ingestion_jobs_kind_migration")
            con.execute("""
                CREATE TABLE media_ingestion_jobs_kind_migration (
                    id VARCHAR PRIMARY KEY,
                    content_hash VARCHAR NOT NULL,
                    source_channel VARCHAR NOT NULL,
                    source_message_id VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL CHECK (kind IN ('activity', 'expense')),
                    spool_path VARCHAR NOT NULL,
                    caption TEXT,
                    status VARCHAR NOT NULL CHECK (
                        status IN ('queued', 'processing', 'retry_wait', 'imported', 'needs_review')
                    ),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    lease_owner VARCHAR,
                    lease_expires_at TIMESTAMP,
                    error_code VARCHAR,
                    error_text TEXT,
                    result_json JSON,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    UNIQUE (source_channel, source_message_id, content_hash, kind)
                )
            """)
            con.execute("""
                INSERT INTO media_ingestion_jobs_kind_migration
                SELECT * FROM media_ingestion_jobs
            """)
            con.execute("DROP TABLE media_ingestion_jobs")
            con.execute("ALTER TABLE media_ingestion_jobs_kind_migration RENAME TO media_ingestion_jobs")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    con.execute("""
        CREATE TABLE IF NOT EXISTS derived_artifacts (
            id VARCHAR PRIMARY KEY,
            artifact_type VARCHAR NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            content TEXT NOT NULL,
            model VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("ALTER TABLE derived_artifacts ADD COLUMN IF NOT EXISTS embedding FLOAT[]")
    con.execute("ALTER TABLE derived_artifacts ADD COLUMN IF NOT EXISTS embedding_model VARCHAR")
    con.execute("ALTER TABLE derived_artifacts ADD COLUMN IF NOT EXISTS embedding_content_hash VARCHAR")

    con.execute("""
        CREATE TABLE IF NOT EXISTS activity_progress_notes (
            id VARCHAR PRIMARY KEY,
            period_kind VARCHAR NOT NULL CHECK (period_kind IN ('weekly', 'monthly')),
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            activity_key VARCHAR NOT NULL,
            note_kind VARCHAR NOT NULL CHECK (note_kind IN ('activity', 'rollup')),
            facts JSON NOT NULL,
            narrative TEXT NOT NULL,
            model VARCHAR NOT NULL,
            facts_hash VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (period_kind, period_start, period_end, activity_key)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS spending (
            id VARCHAR PRIMARY KEY,
            transaction_date DATE NOT NULL,
            visible_date_label VARCHAR,
            merchant VARCHAR NOT NULL,
            amount DECIMAL(12, 2) NOT NULL,
            payment_method VARCHAR,
            category VARCHAR NOT NULL,
            source_image_hash VARCHAR NOT NULL,
            source_row INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source_image_hash, source_row)
        )
    """)

    con.close()

if __name__ == "__main__":
    init_db()
    print("✅ Database initialized.")
