# `search_documents`

Search stored workout plans, historical weekly Supertrend alerts, user-authored ticker notes, or Garmin activity progress notes by meaning or similarity. Use domain='activity_progress' for generated weekly and monthly activity notes. Use structured tools for exact dates, counts, and metrics.

Use domain='summary' to find stored daily summaries and weekly reviews by meaning or recurring themes. Summary vectors live alongside their text in derived_artifacts; no semantic_chunks copy is created. Results include source_id, artifact_type, period_start, period_end, content, and score. Summary text is generated interpretation, not authoritative evidence for recalculating metrics. Use structured records for exact facts. Summary search accepts top_k and date filters only; dates filter the period end inclusively.

## Arguments

### `query`

Natural-language semantic search query.

### `start_date`

Optional inclusive YYYY-MM-DD lower bound.

### `end_date`

Optional inclusive YYYY-MM-DD upper bound.

### `section`

Optional track filter, such as Fitness, HYROX, Tread, Row, or Floor.

### `structure_type`

Optional exact workout structure filter.

### `ticker`

Optional exact ticker filter for stock-alert retrieval.

### `direction`

Optional weekly Supertrend direction filter.

### `activity_type`

Optional exact Garmin activity_type_key filter for activity-progress retrieval.

### `period_kind`

Optional weekly or monthly filter for activity-progress retrieval.
