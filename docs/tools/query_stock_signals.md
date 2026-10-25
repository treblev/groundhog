# `query_stock_signals`

Read CURRENT/latest stored SMA and Supertrend indicator states, or their historical observations. Use this for which stocks ARE bullish/bearish now, as distinct from which stocks TRIGGERED alerts. Latest mode chooses the newest state before applying direction filters, so old bearish observations cannot masquerade as current states. require_all screens for indicator alignment. Values and observation dates are explicit; stored state may be stale. Defaults: latest mode as of today.

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

### `signal_type`

Optional sma_cross (SMA50 minus SMA200) or supertrend (stored trend-line price).

### `timeframe`

Optional daily or weekly filter. SMA golden/death crosses are daily; Supertrend supports daily and weekly.

### `direction`

Optional bullish (buy) or bearish (sell) signal classification. Includes golden/death crosses and matching Supertrend alerts; these are recorded indicator labels, not trading instructions.

### `mode`

latest (default) returns the newest stored row per ticker/indicator/timeframe; direction filters apply after selecting the newest state. history returns observations, not alert events.

### `require_all`

Latest mode only, with direction required. Return only tickers whose latest stored states match every selected supported indicator/timeframe combination. With supertrend and no timeframe, this requires both daily and weekly alignment. Missing states do not qualify.

### `order`

asc or desc. Defaults to desc; date ordering for history, sort_by ordering for performance.

### `limit`

Rows per page, 1–500, default 100. count is this page; total_count is all matching rows. For all-results requests, follow next_cursor until truncated=false.

### `cursor`

Non-negative row offset. Use next_cursor from the previous response with identical filters and as_of.
