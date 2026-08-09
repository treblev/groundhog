# Fitness Workout Semantic Retrieval Design

## Goal

Use local embeddings to find stored workout plans from natural-language requests,
including HYROX-oriented training needs. This design is implemented. The feature
is read-only: it ranks evidence from stored plans and never schedules, saves, or
modifies a workout.

## Data and retrieval

- Embed the existing `workouts` records with the configured local Ollama model
  (`qwen3-embedding:0.6b`). Do not send workout data or queries to an external
  service.
- Create one whole-plan chunk per non-empty workout plus section chunks for
  recognized Fitness, Performance, HYROX, Tread, Row, and Floor blocks. The
  embedding input includes relevant title, category, structure, and section
  metadata; familiar workout abbreviations are expanded to improve matching.
- Store vectors in DuckDB's `semantic_chunks` table together with a stable
  derived chunk ID, source workout ID/date, source metadata, content hash, and
  embedding-model identifier. `workouts` is authoritative; chunks are a
  disposable/rebuildable index.
- Return a short ranked list with a stable workout ID, date, name, matching
  chunk/section, concise matching text, full plan text, and similarity score.
- Support semantic requests such as "leg-heavy workout plans," including plans
  that describe squats, lunges, step-ups, sleds, deadlifts, or wall balls
  without using the exact phrase "leg-heavy."

## Interaction

1. The user asks for a workout type or training need.
2. The LangGraph agent calls MCP `search_documents` for every non-date workout
   lookup based on meaning, movements, equipment, format, similarity, or focus.
3. Search synchronizes changed chunks by default, embeds the query locally,
   applies optional date/section/structure filters, and ranks chunks with
   DuckDB cosine similarity.
4. Groundhog returns only the best matching chunk for each workout, avoiding
   duplicate whole-plan and section hits in the result list.
5. The agent presents grounded plans. For an exact date it instead calls
   `get_workout_for_date`; for counts or aggregates it uses `run_sql`.

HYROX is one retrieval topic among others, not a separate workflow. Questions
such as "find a HYROX-style legs session" should use the same index.

## Guardrails

- Keep exact dates, plan text, and workout metadata grounded in DuckDB results.
- Embeddings rank candidates; they do not invent workout content.
- Do not persist the user's selection or alter any training schedule.
- Keep comparisons model-consistent. A chunk is reused only when its content
  hash and embedding model match current configuration; a model switch triggers
  re-embedding rather than mixing vector spaces.
- Synchronization batches embedding calls and makes them outside the DuckDB
  write transaction. It upserts changed chunks and removes stale ones, making
  repeated indexing safe.

## Operations and verification

`search_documents` refreshes the workout index automatically. Use the index
script after a bulk import or when explicitly checking index state:

```bash
python scripts/index_semantic_documents.py
python scripts/index_semantic_documents.py --domain all
python scripts/index_semantic_documents.py --dry-run
```

The script reports source/chunk counts and embeddings written (or that would be
written). Offline coverage lives in `tests/test_semantic_search.py` and
`tests/test_mcp_service_tools.py`; run the normal regression suite to verify it.
