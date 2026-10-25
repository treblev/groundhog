# `compare_stock_performance`

Compare or rank stored stocks by price change over a requested period. Use for top gainers/losers, comparing tickers, period highs/lows and volume. Computes first/last observed closes, change and percent change in Python/SQL; show actual observation dates and missing data. One observation cannot establish performance. Default period: 1mo. Does not calculate portfolio holdings, fees, dividends, or total return.

## Arguments

### `tickers`

Optional canonical ticker symbols, case-insensitive. Omit to query all stored tickers. No automatic company-name or portfolio inference.

### `period`

Optional structured period: 2w, 14d, 3mo, 1y, today, yesterday, this_week, last_week, this_month, last_month, ytd, or all. Numeric periods include today (2w = 14 calendar days). Weeks run Sunday–Saturday. The tool calculates dates in America/Phoenix. Do not combine with explicit start_date/end_date.

### `start_date`

Optional inclusive YYYY-MM-DD lower bound. Use explicit dates only when supplied by the user; prefer period for relative requests.

### `end_date`

Optional inclusive YYYY-MM-DD upper bound. Defaults to as_of. Cannot exceed as_of.

### `as_of`

Optional YYYY-MM-DD reference/evaluation date for historical questions. Defaults to today in America/Phoenix; cannot be in the future.

### `sort_by`

Rank by price_change_percent (default), ticker, or total_volume. Returns use first/last observed closes within the period, not portfolio profit or total return.

### `order`

asc or desc. Defaults to desc; date ordering for history, sort_by ordering for performance.

### `limit`

Rows per page, 1–500, default 100. count is this page; total_count is all matching rows. For all-results requests, follow next_cursor until truncated=false.

### `cursor`

Non-negative row offset. Use next_cursor from the previous response with identical filters and as_of.
