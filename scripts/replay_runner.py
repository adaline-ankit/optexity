"""Isolated local execution shared by replay generation and deletion experiments."""

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil


class ReplayRunError(RuntimeError):
    """The runner failed to produce a consistent execution receipt."""


async def _stop_runner(
    process: asyncio.subprocess.Process, grace_seconds: float = 15
) -> None:
    # Chrome starts its own process group; killing only the runner group leaks it.
    children = []
    with suppress(psutil.NoSuchProcess):
        children = psutil.Process(process.pid).children(recursive=True)
    try:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    finally:
        for child in children:
            with suppress(psutil.NoSuchProcess):
                child.kill()
        await process.wait()
        if children:
            await asyncio.to_thread(psutil.wait_procs, children, timeout=5)


class LocalEvaluator:
    """Run each trial in a separate process using an unused worker ID."""

    def __init__(
        self,
        directory: Path,
        timeout: float = 120,
        child_process_id: int = 81,
        *,
        model: str | None = None,
        max_model_calls: int | None = None,
        trace_objective: str | None = None,
    ):
        self.model = model
        self.max_model_calls = max_model_calls
        self.trace_objective = trace_objective
        if model is not None and (max_model_calls is None or max_model_calls < 1):
            raise ValueError("Learning requires a positive model call budget")
        self.directory = directory.resolve()
        self.timeout = timeout
        self.child_process_id = child_process_id

    async def __call__(self, automation: dict[str, Any], label: str) -> dict[str, Any]:
        directory = self.directory / label
        directory.mkdir()
        source = directory / "automation.json"
        source.write_text(json.dumps(automation, indent=2) + "\n")
        env = os.environ | {"OPTEXITY_API_KEY": "local", "DEPLOYMENT": "dev"}
        options = ["--forbid-llm"]
        if self.model is not None:
            options = [
                "--model",
                self.model,
                "--max-model-calls",
                str(self.max_model_calls),
                "--max-output-tokens",
                "3000",
            ]
        else:
            env.pop("OPENAI_API_KEY", None)
        if self.trace_objective is not None:
            options.extend(["--trace-objective", self.trace_objective])
        with (directory / "runner.log").open("w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "scripts.run_local_automation",
                str(source),
                "--output-dir",
                str(directory / "run"),
                "--offline",
                *options,
                "--child-process-id",
                str(self.child_process_id),
                stdout=log,
                stderr=log,
                env=env,
                start_new_session=True,
                cwd=Path(__file__).resolve().parents[1],
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=self.timeout)
            except (TimeoutError, asyncio.CancelledError) as exc:
                await _stop_runner(process)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ReplayRunError(f"{label}: runner timed out") from exc
        receipts = list((directory / "run").glob("*/run_result.json"))
        if len(receipts) != 1 or process.returncode not in (0, 1):
            raise ReplayRunError(f"{label}: runner did not produce one valid receipt")
        result = json.loads(receipts[0].read_text())
        if (result.get("status") == "success") != (process.returncode == 0):
            raise ReplayRunError(f"{label}: exit status disagrees with receipt")
        print(
            f"{label}: {result['status']} ({result.get('elapsed_seconds')}s)",
            flush=True,
        )
        return result
