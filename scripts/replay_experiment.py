"""Bounded deletion experiments on trusted, resettable form workflows.

The supplied oracle is immutable. Corruption probes must fail *at that oracle*
before and after minimization. This measures supplied checks, not equivalence
of hidden effects. Each trial uses the existing local Optexity runner.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

import psutil
from pydantic import BaseModel, ConfigDict, Field

from optexity.schema.automation import Automation


class CorruptionProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    node: dict[str, Any]
    expected_error: str = Field(min_length=1)


class ReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    input_parameters: dict[str, Any]
    oracle: dict[str, Any]
    probes: list[CorruptionProbe] = Field(min_length=1)


class ReplayContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resettable: bool = Field(strict=True)
    cases: list[ReplayCase] = Field(min_length=1)


class ExperimentError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report or {}


def _validate(automation: dict[str, Any], contract: dict[str, Any]) -> None:
    parsed_contract = ReplayContract.model_validate(contract)
    if not parsed_contract.resettable:
        raise ValueError("A resettable workflow and nonempty cases are required")
    model = Automation.model_validate(automation)
    if not model.nodes:
        raise ValueError("Expected nonempty flat interaction workflow")
    for node in model.nodes:
        interaction = getattr(node, "interaction_action", None)
        if not interaction or not interaction.strict_replay:
            raise ValueError("Only flat strict input/click nodes may be minimized")
    names = set()
    for case in contract["cases"]:
        if case["name"] in names:
            raise ValueError("Case names must be nonempty and unique")
        names.add(case["name"])
        if not case["oracle"].get("python_script_action"):
            raise ValueError(
                "Each case requires an independent oracle and corruption probes"
            )
        for probe in case["probes"]:
            mutated = copy.deepcopy(automation)
            mutated["nodes"] = [probe["node"]]
            probe_model = Automation.model_validate(mutated)
            interaction = getattr(probe_model.nodes[0], "interaction_action", None)
            if (
                not interaction
                or not interaction.strict_replay
                or not interaction.input_text
            ):
                raise ValueError(
                    "Corruption probes must be strict native input actions"
                )
        trial = copy.deepcopy(automation)
        trial["nodes"].append(case["oracle"])
        trial["parameters"]["input_parameters"] = case["input_parameters"]
        Automation.model_validate(trial)


async def run_experiment(
    automation: dict[str, Any],
    contract: dict[str, Any],
    evaluate: Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]],
    *,
    repeats: int = 2,
    max_runs: int = 100,
) -> dict[str, Any]:
    """Greedy fixed-point deletion, gated on adversarial outcome checks.

    evaluate receives a fresh automation dictionary and a unique trial label.
    Its result must contain status, error and measured llm.attempts. Callers own
    environment reset: the CLI uses a new runner process/browser for every run.
    """
    _validate(automation, contract)
    if (
        type(repeats) is not int
        or repeats < 2
        or type(max_runs) is not int
        or max_runs < 1
    ):
        raise ValueError("Require repeats >= 2 and a positive run budget")
    report = {
        "promoted": False,
        "repeats": repeats,
        "max_runs": max_runs,
        "trials": [],
        "source_sha256": hashlib.sha256(
            json.dumps(automation, sort_keys=True).encode()
        ).hexdigest(),
        "contract_sha256": hashlib.sha256(
            json.dumps(contract, sort_keys=True).encode()
        ).hexdigest(),
    }
    kept = list(range(len(automation["nodes"])))

    def fail(message: str) -> NoReturn:
        raise ExperimentError(message, report)

    def assemble(indices, case, probe=None):
        candidate = copy.deepcopy(automation)
        candidate["parameters"]["input_parameters"] = copy.deepcopy(
            case["input_parameters"]
        )
        candidate["nodes"] = [copy.deepcopy(automation["nodes"][i]) for i in indices]
        if probe:
            candidate["nodes"].append(copy.deepcopy(probe["node"]))
        candidate["nodes"].append(copy.deepcopy(case["oracle"]))
        return candidate

    async def trial(indices, case, phase, probe=None, removed_index=None):
        if len(report["trials"]) >= max_runs:
            fail("Run budget exhausted; no recipe promoted")
        candidate = assemble(indices, case, probe)
        label = f"trial-{len(report['trials']):03d}"
        try:
            result = await evaluate(candidate, label)
        except ExperimentError as exc:
            fail(str(exc))
        entry = {
            "label": label,
            "phase": phase,
            "case": case["name"],
            "kept_indices": list(indices),
            "removed_index": removed_index,
            "probe": probe["name"] if probe else None,
            "result": result,
            "passed": False,
        }
        report["trials"].append(entry)
        attempts = result.get("llm", {}).get("attempts")
        if type(attempts) is not int or attempts != 0:
            fail("Missing zero-model measurement or attempted model call")
        if result.get("status") == "success" and not result.get("error"):
            entry["passed"] = True
        elif result.get("status") != "failed" or result.get("error") not in {
            p["expected_error"] for p in case["probes"]
        }:
            fail(f"{label}: unexpected failure; not evidence of an oracle rejection")
        if probe and result.get("error") != probe["expected_error"]:
            fail(f"{label}: corruption survived or failed at the wrong check")
        return entry["passed"]

    async def passes(indices, phase, removed_index=None):
        for case in contract["cases"]:
            for _ in range(repeats):
                if not await trial(indices, case, phase, removed_index=removed_index):
                    return False
        return True

    async def audit(indices, phase):
        for case in contract["cases"]:
            for _ in range(repeats):
                if await trial([], case, phase + "-empty"):
                    fail("Empty replay survived: check reset state and outcome oracle")
            for probe in case["probes"]:
                for _ in range(repeats):
                    await trial(indices, case, phase, probe=probe)

    if not await passes(kept, "baseline"):
        fail("Baseline fails its outcome oracle")
    await audit(kept, "audit-before")
    # Restart after a pass: removing another action can change dependencies.
    while True:
        for index in kept:
            candidate = [i for i in kept if i != index]
            if await passes(candidate, "deletion", removed_index=index):
                kept = candidate
                break
        else:
            break
    if not await passes(kept, "final"):
        fail("Final replay did not reproduce")
    await audit(kept, "audit-after")
    # Preserve the oracle with the first verified dataset in the runnable artifact.
    optimized = assemble(kept, contract["cases"][0])
    report.update(
        promoted=True,
        kept_indices=kept,
        removed_indices=[i for i in range(len(automation["nodes"])) if i not in kept],
        automation=optimized,
    )
    return report


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
        self, directory: Path, timeout: float = 120, child_process_id: int = 81
    ):
        self.directory = directory
        self.timeout = timeout
        self.child_process_id = child_process_id

    async def __call__(self, automation: dict[str, Any], label: str) -> dict[str, Any]:
        directory = self.directory / label
        directory.mkdir()
        source = directory / "automation.json"
        source.write_text(json.dumps(automation, indent=2) + "\n")
        runner = Path(__file__).with_name("run_local_automation.py")
        env = os.environ | {"OPTEXITY_API_KEY": "local", "DEPLOYMENT": "dev"}
        env.pop("OPENAI_API_KEY", None)
        with (directory / "runner.log").open("w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(runner),
                str(source),
                "--output-dir",
                str(directory / "run"),
                "--offline",
                "--forbid-llm",
                "--child-process-id",
                str(self.child_process_id),
                stdout=log,
                stderr=log,
                env=env,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=self.timeout)
            except (TimeoutError, asyncio.CancelledError) as exc:
                await _stop_runner(process)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ExperimentError(f"{label}: runner timed out") from exc
        receipts = list((directory / "run").glob("*/run_result.json"))
        if len(receipts) != 1 or process.returncode not in (0, 1):
            raise ExperimentError(f"{label}: runner did not produce one valid receipt")
        result = json.loads(receipts[0].read_text())
        if (result.get("status") == "success") != (process.returncode == 0):
            raise ExperimentError(f"{label}: exit status disagrees with receipt")
        print(
            f"{label}: {result['status']} ({result.get('elapsed_seconds')}s)",
            flush=True,
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("automation", type=Path)
    parser.add_argument("contract", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-runs", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--child-process-id", type=int, default=81)
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    try:
        report = asyncio.run(
            run_experiment(
                json.loads(args.automation.read_text()),
                json.loads(args.contract.read_text()),
                LocalEvaluator(args.output_dir, child_process_id=args.child_process_id),
                repeats=args.repeats,
                max_runs=args.max_runs,
            )
        )
    except (ExperimentError, ValueError, OSError) as exc:
        report = getattr(exc, "report", {}) | {"promoted": False, "error": str(exc)}
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if report["promoted"]:
        (args.output_dir / "optimized.json").write_text(
            json.dumps(report["automation"], indent=2) + "\n"
        )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"automation", "trials"}},
            indent=2,
        )
    )
    raise SystemExit(0 if report["promoted"] else 1)


if __name__ == "__main__":
    main()
