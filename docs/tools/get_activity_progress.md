# `get_activity_progress`

Return exact Garmin-only activity progress facts and stored notes for a completed weekly or monthly period. It excludes legacy activity rows. Use current=true to calculate the current partial period without storing it.

## Arguments

### `period_kind`

Required period grain: weekly or monthly.

### `period_end`

Optional completed-period ending date in YYYY-MM-DD format. Weekly dates must be Saturdays; monthly dates must be the final day of a month. Omit it for the latest completed period.

### `activity_type`

Optional exact Garmin activity_type_key filter, such as treadmill_running, stair_climbing, lap_swimming, walking, or strength_training.

### `current`

When true, calculate the current partial week or month without reading or writing a stored note. Do not supply period_end with current=true.
