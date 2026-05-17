# Groundhog — Milestone Map

## M1 — Local Data Pipeline (done)
Garmin screenshots → vision LLM → DuckDB. Stock prices via yfinance. All data local, no external APIs.

## M2 — Text-to-SQL Agent (done)
Natural language → SQL → answer. Two-call pipeline: SQL generation + natural language response. Schema injected as context.

## M3 — Agentic Tool Calling (done)
Replaced SQL-only approach with a tool-calling loop. Model picks the right tool, executes it, reasons over the result. Up to 5 rounds before final answer.

## M4 — Persistent Memory + Embeddings (done)
Facts and context stored beyond the session. Embeddings + vector search alongside SQL. First real RAG component — structured data (DuckDB) + unstructured memory (vector store) in the same agent.

## M5 — Multi-step Reasoning (done)
Agent chains multiple tool calls to answer complex questions. Plan → Execute → Synthesize. Upgraded to qwen3:32b after qwen2.5-coder:32b hit its prompt-engineering ceiling on multi-step questions.

## M6 — Production Hardening (planned)
Eval framework for vision extraction accuracy. Observability — log every tool call, result, and latency. Prompt versioning. Guardrails for bad SQL or hallucinated answers.
