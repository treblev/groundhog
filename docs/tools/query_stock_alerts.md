# `query_stock_alerts`

List recorded stock alert events by type, direction, tickers, and relative or explicit dates. For “stocks that entered death cross in the last two weeks”, use alert_type="death_cross", period="2w", latest_per_ticker=true. For buy/sell alerts use direction, which includes SMA crosses and Supertrend. Returns exact event rows, resolved window, source freshness, total_count and pagination. An empty result means no recorded matching events, not current bullish/bearish state. Default: all recorded history through today.

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

### `timeframe`

Optional daily or weekly filter. SMA golden/death crosses are daily; Supertrend supports daily and weekly.

### `direction`

Optional bullish (buy) or bearish (sell) signal classification. Includes golden/death crosses and matching Supertrend alerts; these are recorded indicator labels, not trading instructions.

### `alert_type`

Optional exact event: golden_cross, death_cross, supertrend_daily_bullish, supertrend_daily_bearish, supertrend_weekly_bullish, or supertrend_weekly_bearish.

### `latest_per_ticker`

When true, return the newest matching alert for each ticker AFTER applying event/date filters. Use for lists of distinct stocks that triggered an event, not current indicator state.

### `order`

asc or desc. Defaults to desc; date ordering for history, sort_by ordering for performance.

### `limit`

Rows per page, 1–500, default 100. count is this page; total_count is all matching rows. For all-results requests, follow next_cursor until truncated=false.

### `cursor`

Non-negative row offset. Use next_cursor from the previous response with identical filters and as_of.
