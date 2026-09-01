# `search_documents`

Search stored workout plans, historical weekly Supertrend alerts, or user-authored ticker notes by meaning or similarity. Use domain='workout' for non-date workout lookup and domain='stock_alert' for historical weekly flips; use domain='stock_note' for semantic note retrieval. Use structured tools for exact dates, counts, and market facts.

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
