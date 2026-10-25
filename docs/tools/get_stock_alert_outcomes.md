# `get_stock_alert_outcomes`

Measure observed close-to-close price changes after recorded alerts. Use for “what happened five trading sessions after death crosses?” Supports alert filters and periods. Distinguishes completed horizons from alerts without enough future bars. Returns price movement, not simulated trade profit or advice; bearish alerts do not invert the sign. Prices stop at as_of even when later data exists.

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

### `horizon_sessions`

Number of stored price sessions AFTER the baseline bar, 1–252, default 5. Baseline is the first close on/after the alert. Missing future sessions produce an incomplete result with no full-horizon return. Evaluation never uses prices after as_of.

### `order`

asc or desc. Defaults to desc; date ordering for history, sort_by ordering for performance.

### `limit`

Rows per page, 1–500, default 100. count is this page; total_count is all matching rows. For all-results requests, follow next_cursor until truncated=false.

### `cursor`

Non-negative row offset. Use next_cursor from the previous response with identical filters and as_of.
