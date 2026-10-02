import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from optexity.schema.automation import Automation
from optexity.schema.task import Task
from optexity.schema.types import CompanyID, RecordingID, TaskID, UserID
from scripts.llm_guard import measure_llm_calls

os.environ.setdefault("DEPLOYMENT", "dev")
os.environ.setdefault("OPTEXITY_API_KEY", "local")


def _load_automation(path: Path) -> Automation:
    with path.open() as f:
        return Automation.model_validate(json.load(f))


def _build_task(
    automation: Automation, save_directory: Path, model: str | None
) -> Task:
    now = datetime.now(timezone.utc)
    task = Task(
        task_id=TaskID(str(uuid.uuid4())),
        user_id=UserID(str(uuid.uuid4())),
        recording_id=RecordingID(str(uuid.uuid4())),
        endpoint_name="local/automation",
        automation=automation,
        input_parameters=automation.parameters.input_parameters,
        secure_parameters=automation.parameters.secure_parameters,
        unique_parameter_names=[],
        created_at=now,
        status="queued",
        save_directory=save_directory,
        api_key="local",
        company_id=CompanyID(str(uuid.uuid4())),
        llm_model_name=model,
        is_browser=True,
    )
    task.retry_count = max(1, automation.max_retries)
    task.max_retries = task.retry_count
    automation.max_retries = task.retry_count
    task.automation = automation
    task.status = "running"
    task.allocated_at = now
    task.started_at = now
    return task


def _patch_offline_server_calls() -> None:
    from optexity.inference.core import run_automation as run_automation_module

    async def _noop(*args, **kwargs):
        return None

    for name in [
        "complete_task_in_server",
        "initiate_callback",
        "save_downloads_in_server",
        "save_latest_memory_state_locally",
        "save_output_data_in_server",
        "save_private_node_state_locally",
        "save_trajectory_in_server",
        "start_task_in_server",
    ]:
        setattr(run_automation_module, name, _noop)


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    automation = _load_automation(args.automation)
    task = _build_task(automation, args.output_dir, args.model)
    child_process_id = args.child_process_id
    unique_child_arn = f"local-{child_process_id}"

    from optexity.inference.child_process import (
        restart_global_actual_browser,
        setup_browser,
    )
    from optexity.inference.core.run_automation import run_automation

    if args.offline:
        _patch_offline_server_calls()

    capture = None
    if args.trace_objective is not None:
        from scripts.replay_capture import NativeReplayCapture

        if not args.forbid_llm:
            raise ValueError("Native capture requires --forbid-llm")
        capture = NativeReplayCapture(
            task.task_directory / "native_trace", args.trace_objective
        )

    start = time.perf_counter()
    llm_metrics = {}
    try:
        await setup_browser(task, unique_child_arn, child_process_id)
        from optexity.inference import child_process

        if child_process._global_actual_browser is None:
            raise RuntimeError("Actual browser did not start")
        cdp_url = child_process._global_actual_browser.cdp_url
        if cdp_url is None:
            raise RuntimeError("Actual browser did not expose CDP URL")

        with (
            measure_llm_calls(
                args.forbid_llm,
                max_calls=args.max_model_calls,
                max_output_tokens=args.max_output_tokens,
            ) as llm_metrics,
            capture if capture is not None else nullcontext(),
        ):
            await run_automation(
                task=task,
                unique_child_arn=unique_child_arn,
                child_process_id=child_process_id,
                cdp_url=cdp_url,
                max_tries=1,
            )
            if capture is not None:
                capture.finish(
                    task.status == "success" and llm_metrics["attempts"] == 0
                )
    finally:
        await restart_global_actual_browser("local runner cleanup")

    elapsed = time.perf_counter() - start
    result = {
        "status": task.status,
        "error": task.error,
        "elapsed_seconds": round(elapsed, 3),
        "task_id": task.task_id,
        "task_directory": str(task.task_directory),
        "log_file": str(task.log_file_path),
        "llm": llm_metrics,
        "trace_path": str(capture.path) if capture is not None else None,
        "timing_scope": "browser setup, execution, outcome assertions, final logging and cleanup",
    }
    if args.forbid_llm and llm_metrics.get("attempts"):
        task.status = result["status"] = "failed"
        result["error"] = "Replay attempted an LLM call"
    (task.task_directory / "run_result.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))
    return 0 if task.status == "success" else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("automation", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/optexity-local"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--child-process-id", type=int, default=0)
    parser.add_argument("--forbid-llm", action="store_true")
    parser.add_argument("--trace-objective")
    parser.add_argument("--max-model-calls", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument(
        "--offline", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
