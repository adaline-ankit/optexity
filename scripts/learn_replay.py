"""Learn a resettable workflow, generate strict replay, and improve from fresh traces."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from optexity.schema.automation import ActionNode, Automation
from scripts.generate_cached_automation import generate_candidate, prepare_generation
from scripts.replay_runner import LocalEvaluator


def _write(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def _baseline(path: Path) -> Automation:
    baseline = Automation.model_validate_json(path.read_text())
    first = baseline.nodes[0] if baseline.nodes else None
    interaction = getattr(first, "interaction_action", None)
    if not interaction or not interaction.agentic_task:
        raise ValueError("Loop requires an initial agentic task")
    if len(baseline.nodes) < 2 or any(
        not getattr(node, "python_script_action", None) for node in baseline.nodes[1:]
    ):
        raise ValueError(
            "Loop requires independent Python outcome assertions after the task"
        )
    return baseline


async def improve_replay(
    baseline_path: Path,
    trace_path: Path,
    directory: Path,
    evaluate,
    *,
    generate=generate_candidate,
    model: str = "openai/gpt-4.1-mini",
    rounds: int = 2,
) -> dict[str, Any]:
    """Checkpoint only successful, zero-call replays with compilable fresh evidence."""
    if not 1 <= rounds <= 5:
        raise ValueError("rounds must be between 1 and 5")
    _baseline(baseline_path)
    context = prepare_generation(baseline_path, trace_path)
    seen_runs = {context["request"]["trace"][0]["run_id"]}
    report: dict[str, Any] = {
        "promoted": False,
        "rounds": [],
        "stop_reason": "round_budget",
    }
    previous = None
    feedback = None
    for number in range(1, rounds + 1):
        trial: dict[str, Any] = {
            "round": number,
            "source_trace": str(trace_path),
            "source_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        }
        report["rounds"].append(trial)
        try:
            candidate, generation = await generate(context, model, feedback)
            data = candidate.model_dump(
                mode="json", exclude_none=True, exclude_defaults=True
            )
            trial["generation"] = generation
            trial["actions"] = len(context["allowed_nodes"])
            trial["candidate_actions"] = (
                len(candidate.nodes) - len(context["baseline"].nodes) + 1
            )
            receipt = await evaluate(data, f"round-{number}")
            trial["execution"] = receipt
            attempts = receipt.get("llm", {}).get("attempts")
            if (
                receipt.get("status") != "success"
                or receipt.get("error")
                or type(attempts) is not int
                or attempts != 0
            ):
                report["stop_reason"] = "candidate_failed"
                break
            fresh = Path(receipt["trace_path"])
            next_context = prepare_generation(baseline_path, fresh)
            run_id = next_context["request"]["trace"][0]["run_id"]
            if run_id in seen_runs:
                raise ValueError("Replay did not produce fresh execution evidence")
            seen_runs.add(run_id)
            # Reject incomplete recapture even if the outcome assertions passed.
            if len(next_context["allowed_nodes"]) != trial["candidate_actions"]:
                raise ValueError(
                    "Recapture action count differs from executed candidate"
                )
            _write(directory / "optimized.json", data)
            report.update(promoted=True, best_round=number)
            trial["verified"] = True
            _write(directory / "report.json", report)
            if data == previous:
                report["stop_reason"] = "converged"
                break
            previous = data
            feedback = {
                "previous_actions": trial["candidate_actions"],
                "outcome": "passed independent assertions",
                "llm_attempts": 0,
            }
            trace_path, context = fresh, next_context
        except Exception as exc:
            report.update(stop_reason="error", error=f"{type(exc).__name__}: {exc}")
            break
    _write(directory / "report.json", report)
    return report


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    baseline = _baseline(args.baseline)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    learner = LocalEvaluator(
        args.output_dir,
        timeout=240,
        child_process_id=args.child_process_id,
        model=args.model,
        max_model_calls=args.learning_calls,
    )
    learning = await learner(baseline.model_dump(mode="json"), "learning")
    _write(args.output_dir / "learning.json", learning)
    if learning.get("status") != "success":
        raise ValueError("Learning failed; no replay was generated")
    traces = list(
        Path(learning["task_directory"]).glob(
            "logs/step_*/step_cache/browser_use_trace.jsonl"
        )
    )
    if len(traces) != 1:
        raise ValueError("Learning must produce exactly one successful action trace")
    first = baseline.nodes[0]
    assert isinstance(first, ActionNode)
    interaction = first.interaction_action
    assert interaction is not None and interaction.agentic_task is not None
    evaluator = LocalEvaluator(
        args.output_dir,
        child_process_id=args.child_process_id,
        trace_objective=interaction.agentic_task.task,
    )
    report = await improve_replay(
        args.baseline,
        traces[0],
        args.output_dir,
        evaluator,
        model=args.model,
        rounds=args.rounds,
    )
    report["learning"] = learning
    report["model_call_limit"] = args.learning_calls + args.rounds
    _write(args.output_dir / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="openai/gpt-4.1-mini")
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--learning-calls", type=int, choices=range(1, 11), default=4)
    parser.add_argument("--child-process-id", type=int, default=83)
    parser.add_argument(
        "--resettable",
        action="store_true",
        required=True,
        help="Confirm repeated execution is safe and starts from a reset state",
    )
    args = parser.parse_args()
    args.baseline = args.baseline.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.env_file:
        load_dotenv(args.env_file)
    report = asyncio.run(_run(args))
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"rounds", "learning"}
            },
            indent=2,
        )
    )
    raise SystemExit(0 if report["promoted"] else 1)


if __name__ == "__main__":
    main()
