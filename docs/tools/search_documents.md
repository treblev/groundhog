# `search_documents`

Search stored workout plans, historical weekly Supertrend alerts, user-authored ticker notes, or Garmin activity progress notes by meaning or similarity. Use domain='activity_progress' for generated weekly and monthly activity notes. Use structured tools for exact dates, counts, and metrics.

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
