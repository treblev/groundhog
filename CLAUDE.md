# Groundhog

Personal data pipeline and agent. Consolidates health, stock, and reminder data into a single local database.

## Stack
- Language: Python
- Database: DuckDB (local, stored in data/db/)
- AI runtime: Ollama (local only, no external API calls)
- LLM: Qwen2.5-Coder 32B

## Project Structure
- data/drop/ — Garmin screenshot images are dropped here for ingestion
- data/db/ — DuckDB database file lives here
- ingestion/ — one file per data source (health, stocks, reminders)
- scheduler/ — polling jobs that trigger ingestion
- agent/ — text-to-SQL query layer using Ollama
- config/settings.py — all paths and model config live here

## Key Conventions
- All config (paths, model names) goes in config/settings.py — no hardcoded paths elsewhere
- data/ is gitignored — never commit personal data or the DuckDB file
- AI must run locally via Ollama — never call external APIs with personal data
- Health data comes from Garmin screenshots parsed by vision AI, not direct API

## Database Tables
- health_metrics — daily grain (steps, avg HR, active minutes)
- stock_watchlist — daily closing price
- reminders — SCD Type 2 (valid_from, valid_to, is_current)
