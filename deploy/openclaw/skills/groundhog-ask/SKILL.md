---
name: groundhog-ask
description: Answer a Telegram message beginning with /ask by running the guarded local Groundhog data agent.
---

# Groundhog `/ask`

For a direct Telegram text message that begins with `/ask`, use this skill.

1. Extract everything after `/ask` as the question. If it is empty, reply: `Usage: /ask <question about your Groundhog data>`.
2. Run the guarded Groundhog query command from the project directory. Use the exec tool's
   argument-array and environment options; do not interpolate the Telegram text into a shell string.
   Pass the complete question as one final argument:

   ```bash
   venv/bin/python -m scripts.ask_groundhog <question>
   ```

   Working directory: `/home/openclaw/apps/groundhog`.
   Environment: `GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb`.

3. Reply with the command's standard output only. Do not answer the data question using your own model or add implementation details.
4. If the command fails, reply: `Groundhog could not answer that question right now. Please try again.` Do not expose error text, paths, commands, or configuration.

Do not use this skill for image uploads; those continue through the deterministic media watcher.
