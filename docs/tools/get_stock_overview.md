# `get_stock_overview`

Get a compact research snapshot for 1–20 tickers in one call: latest stored price, change from previous stored close, SMA50/SMA200 calculated from stored closes (null until enough observations exist), SMA/Supertrend states, five recent alerts, and up to ten current active user notes with explicit counts/truncation. Use for “what is going on with AXON?” and ticker context. Note text is current and historical note revisions are not reconstructed. No live quotes, fundamentals or news are available from this tool.

## Arguments

### `tickers`

Required list of 1–20 canonical ticker symbols. Case-insensitive. Missing symbols return available=false.

### `as_of`

Optional YYYY-MM-DD reference/evaluation date for historical questions. Defaults to today in America/Phoenix; cannot be in the future.
