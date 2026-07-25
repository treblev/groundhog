# Groundhog — TODO

- Add deterministic Telegram sleep-screenshot processing: extract sleep data from incoming screenshots, store it in the existing sleep data flow, and prepare review-pending local eval candidates alongside activity and workout-plan uploads.
- Harden the LangGraph agent in `langgraph_client/client.py`:
  - Add `ToolRetryMiddleware` (from `langchain.agents.middleware`) for malformed Ollama tool calls. `qwen3:32b` can emit a literal JSON tool call as text instead of structured `tool_calls`.
  - Add a deep_agents-style mutable `write_todos` planning tool without adopting the full deepagents framework.
  - Prompt the agent to re-check its plan after every tool result, so contradictory observations trigger replanning.
  - Add a database-grounding verifier middleware: require factual answers about personal data to be supported by results from the current tool/DB calls, and revise or decline claims that cannot be verified instead of relying on model memory or context fragments.
- Extend `activities` for pool swims without adding a separate results table: extract and store swim-specific fields such as pool distance, laps, stroke, and swim pace while keeping activity history unified.
- Add Telegram import confirmations: after each deterministic screenshot import, send a concise result summary (activity, date, distance/duration, average pace, and average HR) so classification errors can be caught immediately.

- `notebooks/vision_prompt_evals.ipynb` currently imports `PROMPT` straight from `ingestion/health.py`, so it always tests whatever prompt is actually live in production — good for catching drift, but it means you can't tweak prompt wording inside the notebook and eyeball results before committing to `health.py` (the old pre-rewrite notebook supported that via an editable in-notebook `PROMPT` variable). If that experimentation workflow turns out to be wanted, add an optional local override variable in the notebook that defaults to the imported `PROMPT` but can be redefined per-cell for trial wording, without changing what `score` actually tests once you're done.
