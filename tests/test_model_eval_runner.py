import unittest

from scripts.run_model_eval import _summaries


class ModelEvalRunnerTests(unittest.TestCase):
    def test_summary_includes_latency_timeouts_and_prompt_metrics(self):
        results = [
            {
                "model": "model-a",
                "latency_s": 10.0,
                "error": None,
                "metrics": {"trace_summary": {
                    "prompt_eval_count": 100,
                    "prompt_eval_duration_ms": 200.0,
                    "llm_calls": 3,
                    "tool_calls": 2,
                }},
            },
            {
                "model": "model-a",
                "latency_s": 14.0,
                "error": None,
                "metrics": {"trace_summary": {
                    "prompt_eval_count": 200,
                    "prompt_eval_duration_ms": 400.0,
                    "llm_calls": 3,
                    "tool_calls": 2,
                }},
            },
            {
                "model": "model-a",
                "latency_s": 30.0,
                "error": "TimeoutError: timed out",
                "metrics": {"trace_summary": {
                    "prompt_eval_count": 50,
                    "prompt_eval_duration_ms": 100.0,
                    "llm_calls": 1,
                    "tool_calls": 0,
                }},
            },
        ]

        summary = _summaries(results, ["model-a"])[0]

        self.assertEqual(summary["successful"], 2)
        self.assertEqual(summary["timeouts"], 1)
        self.assertEqual(summary["latency_s"]["average"], 12.0)
        self.assertEqual(summary["latency_s"]["median"], 12.0)
        self.assertEqual(summary["latency_s"]["population_sd"], 2.0)
        self.assertEqual(summary["prompt_eval_tokens"]["total"], 350)
        self.assertEqual(summary["llm_calls"]["average"], 2.33)


if __name__ == "__main__":
    unittest.main()
