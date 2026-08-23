"""Run comparable Groundhog model evaluations with routing enabled or disabled."""

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import langgraph_client.client as groundhog_client


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stats(values: list[float | int], *, digits: int = 2) -> dict:
    if not values:
        return {"average": None, "median": None, "population_sd": None, "total": None}
    return {
        "average": round(statistics.fmean(values), digits),
        "median": round(statistics.median(values), digits),
        "population_sd": round(statistics.pstdev(values), digits),
        "total": round(sum(values), digits),
    }


def _summaries(results: list[dict], models: list[str]) -> list[dict]:
    summaries = []
    for model in models:
        model_results = [item for item in results if item["model"] == model]
        successful = [item for item in model_results if item["error"] is None]
        latencies = [item["latency_s"] for item in successful]
        trace_summaries = [
            item.get("metrics", {}).get("trace_summary", {})
            for item in model_results
        ]
        prompt_counts = [
            item["prompt_eval_count"]
            for item in trace_summaries
            if item.get("prompt_eval_count") is not None
        ]
        prompt_durations = [
            item["prompt_eval_duration_ms"]
            for item in trace_summaries
            if item.get("prompt_eval_duration_ms") is not None
        ]
        llm_calls = [
            item["llm_calls"] for item in trace_summaries if item.get("llm_calls") is not None
        ]
        tool_calls = [
            item["tool_calls"] for item in trace_summaries if item.get("tool_calls") is not None
        ]
        latency_stats = _stats(latencies)
        latency_stats.update({
            "minimum": round(min(latencies), 2) if latencies else None,
            "maximum": round(max(latencies), 2) if latencies else None,
        })
        summaries.append({
            "model": model,
            "completed": len(model_results),
            "successful": len(successful),
            "errors": len(model_results) - len(successful),
            "timeouts": sum(
                item["error"] is not None and "timeout" in item["error"].lower()
                for item in model_results
            ),
            "latency_s": latency_stats,
            "prompt_eval_tokens": _stats(prompt_counts),
            "prompt_eval_duration_ms": _stats(prompt_durations),
            "llm_calls": _stats(llm_calls),
            "tool_calls": _stats(tool_calls),
        })
    return summaries


def _save(
    output_path: Path,
    spec: dict,
    results: list[dict],
    started_at: str,
    routing_mode: str,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "spec": spec,
        "started_at": started_at,
        "updated_at": _utc_now(),
        "routing_mode": routing_mode,
        "results": results,
        "summary": _summaries(results, spec["models"]),
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n")


async def run(
    spec_path: Path,
    output_path: Path,
    routing_mode: str = "configured",
    question_timeout_s: float | None = 180.0,
) -> None:
    spec = json.loads(spec_path.read_text())
    models = spec["models"]
    questions = spec["questions"]
    results: list[dict] = []
    started_at = _utc_now()
    routing_enabled = {
        "enabled": True,
        "disabled": False,
        "configured": None,
    }[routing_mode]

    for model in models:
        groundhog_client.OLLAMA_SQL_MODEL = model
        for index, item in enumerate(questions, start=1):
            print(f"START {model} {item['id']} ({index}/{len(questions)})", flush=True)
            started = time.monotonic()
            answer = None
            error = None
            metrics: dict = {}
            try:
                ask = groundhog_client.ask_question(
                    item["question"],
                    routing_enabled=routing_enabled,
                    metrics_out=metrics,
                )
                answer = await (
                    asyncio.wait_for(ask, timeout=question_timeout_s)
                    if question_timeout_s is not None
                    else ask
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            latency_s = round(time.monotonic() - started, 2)
            results.append({
                "model": model,
                "id": item["id"],
                "question": item["question"],
                "expected": item["expected"],
                "answer": answer,
                "error": error,
                "latency_s": latency_s,
                "metrics": metrics,
            })
            _save(output_path, spec, results, started_at, routing_mode)
            status = "ERROR" if error else "DONE"
            print(f"{status} {model} {item['id']} {latency_s:.2f}s", flush=True)

    _save(output_path, spec, results, started_at, routing_mode)
    print(f"COMPLETE {output_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--routing",
        choices=["configured", "enabled", "disabled"],
        default="configured",
        help="Force request routing on/off for comparable evaluation runs.",
    )
    parser.add_argument(
        "--question-timeout-s",
        type=float,
        default=180.0,
        help="Maximum seconds per model/question; use 0 to disable.",
    )
    args = parser.parse_args()
    timeout = None if args.question_timeout_s == 0 else args.question_timeout_s
    asyncio.run(run(args.spec, args.output, args.routing, timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
