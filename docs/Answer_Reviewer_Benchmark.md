# Local answer-review benchmark

Nimble is faster and accepts the valid answers rejected by the deployed reviewer,
but this evaluation does not justify replacing the reviewer outright.

## Evaluation on October 25, 2026

The private corpus contains 29 unique cases: 13 supported user-facing answers and
16 answers with factual errors or internal disclosures. It includes two actual
production drafts and controlled examples covering rounding, metric units,
tickers, dates, indicator states versus events, unsupported causal claims,
forecasts, stale observations, incomplete outcomes, and untrusted evidence.
Each case was replayed twice. Replays are not independent examples.

| Measure | Deployed Qwen reviewer | Nimble decision contract |
| --- | ---: | ---: |
| Unique cases | 29 | 29 |
| Total replay evaluations | 58 | 58 |
| Incorrect approvals | 0 | 2 |
| Incorrect rejections of valid answers | 4 | 0 |
| Escalations | 0 | 6 |
| Median review time | 1.1162 s | 0.4178 s excluding full cache hits |

The two incorrect approvals are replays of one case: asserting a newly triggered
crossover from an indicator state. The returned concentration score did not
prevent that mistake at the benchmark threshold of 0.6. All escalation cases
contained invalid answers; none was automatically approved. These scores are
distribution concentration, not measured probabilities of correctness.

Nimble had 30 complete cache hits, including the warm-up case. Its overall replay
median of 0.0197 seconds therefore does not represent fresh review latency.
Cold warm-up took 5.67 seconds for Nimble and 6.32 seconds for Qwen.

A separate end-to-end production-data question succeeded with a benchmark-only
in-memory adapter: 20.15 seconds, one retrieval, two Qwen generation calls, and
one Nimble review lasting 1.37 seconds. The earlier production run declined after
46.39 seconds. Most of that improvement came from avoiding an unnecessary
correction and second review; it is not a 26-second speedup of one model call.
This comparison is one request, without controlled cache or load conditions.

This compares the existing generative review instructions with a proposed typed
decision contract, not model architecture in isolation. The sample is small and
the labels were assigned for this project. Threshold changes would need a fresh
held-out evaluation rather than tuning against these same cases.

## Runtime and privacy

Production Ollama 0.35.1 supported the decision endpoint but refused the current
Nimble manifests as requiring a newer version. The evaluation used the official
portable Ollama 0.40.11 release on a separate loopback port and model directory.
Nimble used the MLX runner with MXFP8 weights. Model digest:
`13ae0a50610239c7cc52589608fc86f8405c5cf1daddd98798771a159e549792`.

All inference was local. Production reviewer code and configuration were not
changed. Do not commit the production-derived cases, raw answers, or reports.

## Reproduce

Provide a private JSON list containing `id`, `question`, `evidence`, `answer`,
`expected_grounded`, and `expected_safe`. Both expected labels must be booleans.

```bash
python scripts/benchmark_answer_review.py "$CASES" "$REPORT" \
  --decision-model "$MODEL" --decision-url "$LOCAL_OLLAMA_URL" --repeats 2
```

The script uses the configured local Qwen reviewer as the baseline. A confident
violation requests correction; accepting an answer requires confident support
and a user-facing disclosure verdict. Ambiguous judgments escalate. The harness
reports errors and full cache hits separately from ordinary evaluations.

Official references: [Nimble model](https://ollama.com/library/nimble),
[Ollama decision API](https://docs.ollama.com/api/systemone).
