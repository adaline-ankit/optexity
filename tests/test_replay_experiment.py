"""Behavioral tests for bounded minimization and adversarial oracle checks."""

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.replay_experiment import ExperimentError, run_experiment
from scripts.replay_runner import LocalEvaluator, ReplayRunError


def input_node(value):
    return {
        "type": "action_node",
        "interaction_action": {
            "strict_replay": True,
            "input_text": {
                "command": 'locator("input")',
                "skip_prompt": True,
                "assert_locator_presence": True,
                "input_text": value,
            },
        },
    }


def fixture():
    automation = {
        "url": "https://example.com/resettable-form",
        "parameters": {"input_parameters": {}, "generated_parameters": {}},
        "nodes": [input_node("correct"), input_node("correct")],
    }
    contract = {
        "resettable": True,
        "cases": [
            {
                "name": "one",
                "input_parameters": {},
                "oracle": {
                    "type": "action_node",
                    "python_script_action": {
                        "execution_code": "async def code_fn(page): pass"
                    },
                },
                "probes": [
                    {
                        "name": "wrong value",
                        "node": input_node("wrong"),
                        "expected_error": "oracle:field",
                    }
                ],
            }
        ],
    }
    return automation, contract


async def evaluate(automation, label):
    # A resettable in-memory form. Oracle is independent of action count.
    value = ""
    for node in automation["nodes"][:-1]:
        value = node["interaction_action"]["input_text"]["input_text"]
    return {
        "status": "success" if value == "correct" else "failed",
        "error": None if value == "correct" else "oracle:field",
        "llm": {"attempts": 0},
    }


class ExperimentTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_cleanup_terminates_detached_children(self):
        import asyncio
        import signal
        import sys

        import psutil

        from scripts.replay_runner import _stop_runner

        child_code = "import time; time.sleep(60)"
        parent_code = (
            "import signal, subprocess, sys, time; "
            "signal.signal(signal.SIGINT, signal.SIG_IGN); "
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True); "
            "print(child.pid, flush=True); time.sleep(60)"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent_code,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        child = psutil.Process(int(await process.stdout.readline()))
        try:
            await _stop_runner(process, grace_seconds=0.02)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertFalse(
                child.is_running() and child.status() != psutil.STATUS_ZOMBIE
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if child.is_running():
                child.kill()

    async def test_malformed_contract_fails_before_evaluation(self):
        for changes in (
            {"cases": "invalid"},
            {"cases": [None]},
            {"cases": [{"name": []}]},
        ):
            automation, contract = fixture()
            contract.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await run_experiment(automation, contract, evaluate)

    async def test_cancellation_propagates_after_runner_cleanup(self):
        import asyncio

        automation, _ = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            evaluator = LocalEvaluator(Path(tmp), child_process_id=83)
            task = asyncio.create_task(evaluator(automation, "cancelled"))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_runner_deadline_aborts_without_a_success_receipt(self):
        automation, _ = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            evaluator = LocalEvaluator(Path(tmp), timeout=0.001, child_process_id=83)
            with self.assertRaisesRegex(ReplayRunError, "timed out"):
                await evaluator(automation, "deadline")
            self.assertFalse(list(Path(tmp).glob("**/run_result.json")))

    async def test_removes_duplicate_but_keeps_required_action(self):
        automation, contract = fixture()
        original = copy.deepcopy(automation)
        report = await run_experiment(automation, contract, evaluate, repeats=2)
        self.assertTrue(report["promoted"])
        self.assertEqual(report["kept_indices"], [1])
        self.assertEqual(report["removed_indices"], [0])
        self.assertEqual(automation, original)
        self.assertEqual(len(report["automation"]["nodes"]), 2)
        self.assertEqual(
            report["automation"]["nodes"][-1], contract["cases"][0]["oracle"]
        )

    async def test_weak_oracle_blocks_optimization(self):
        automation, contract = fixture()

        async def always_passes(automation, label):
            return {"status": "success", "error": None, "llm": {"attempts": 0}}

        with self.assertRaisesRegex(ExperimentError, "survived"):
            await run_experiment(automation, contract, always_passes)

    async def test_infrastructure_failure_is_not_a_killed_mutation(self):
        automation, contract = fixture()

        async def broken_probe(automation, label):
            result = await evaluate(automation, label)
            if result["status"] == "failed":
                result["error"] = "browser crashed"
            return result

        with self.assertRaisesRegex(ExperimentError, "unexpected failure"):
            await run_experiment(automation, contract, broken_probe)

    async def test_model_attempt_blocks_promotion_even_when_task_succeeds(self):
        automation, contract = fixture()

        async def paid(automation, label):
            return {"status": "success", "llm": {"attempts": 1}}

        with self.assertRaisesRegex(ExperimentError, "model"):
            await run_experiment(automation, contract, paid)

    async def test_missing_measurement_is_not_zero(self):
        automation, contract = fixture()

        async def missing(automation, label):
            return {"status": "success"}

        with self.assertRaisesRegex(ExperimentError, "model"):
            await run_experiment(automation, contract, missing)

    async def test_budget_exhaustion_does_not_promote(self):
        automation, contract = fixture()
        with self.assertRaisesRegex(ExperimentError, "budget"):
            await run_experiment(automation, contract, evaluate, max_runs=1)

    async def test_each_case_must_pass_before_removal(self):
        automation, contract = fixture()
        second = copy.deepcopy(contract["cases"][0])
        second["name"] = "needs both"
        second["input_parameters"] = {"needs_both": [True]}
        contract["cases"].append(second)

        async def different_case(automation, label):
            result = await evaluate(automation, label)
            if (
                automation["parameters"]["input_parameters"].get("needs_both")
                and len(automation["nodes"]) < 3
            ):
                result.update(status="failed", error="oracle:field")
            return result

        report = await run_experiment(automation, contract, different_case)
        self.assertEqual(report["removed_indices"], [])

    async def test_unsafe_or_empty_contract_rejected_before_execution(self):
        for key, value in [("resettable", False), ("cases", [])]:
            automation, contract = fixture()
            contract[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                await run_experiment(automation, contract, evaluate)

    async def test_non_strict_action_rejected(self):
        automation, contract = fixture()
        automation["nodes"][0]["interaction_action"]["strict_replay"] = False
        with self.assertRaises(ValueError):
            await run_experiment(automation, contract, evaluate)

    async def test_second_repeat_failure_prevents_deletion(self):
        automation, contract = fixture()
        short_runs = 0

        async def flaky(automation, label):
            nonlocal short_runs
            result = await evaluate(automation, label)
            if len(automation["nodes"]) == 2 and result["status"] == "success":
                short_runs += 1
                if short_runs % 2 == 0:
                    result.update(status="failed", error="oracle:field")
            return result

        report = await run_experiment(automation, contract, flaky)
        self.assertEqual(report["removed_indices"], [])

    async def test_failure_at_wrong_assertion_blocks_audit(self):
        automation, contract = fixture()
        probe = copy.deepcopy(contract["cases"][0]["probes"][0])
        probe.update(name="other field", expected_error="oracle:other")
        contract["cases"][0]["probes"].append(probe)
        with self.assertRaisesRegex(ExperimentError, "wrong check"):
            await run_experiment(automation, contract, evaluate)

    async def test_final_failure_discards_previously_successful_deletion(self):
        automation, contract = fixture()
        short_runs = 0

        async def stops_reproducing(automation, label):
            nonlocal short_runs
            result = await evaluate(automation, label)
            if len(automation["nodes"]) == 2 and result["status"] == "success":
                short_runs += 1
                if short_runs > 2:
                    result.update(status="failed", error="oracle:field")
            return result

        with self.assertRaisesRegex(ExperimentError, "Final replay") as failure:
            await run_experiment(automation, contract, stops_reproducing)
        self.assertFalse(failure.exception.report["promoted"])
        self.assertNotIn("automation", failure.exception.report)

    async def test_contract_remains_unchanged_after_evaluator_mutation(self):
        automation, contract = fixture()
        original = copy.deepcopy(contract)

        async def mutating_evaluator(automation, label):
            result = await evaluate(automation, label)
            automation["nodes"][-1]["python_script_action"]["execution_code"] = "broken"
            return result

        report = await run_experiment(automation, contract, mutating_evaluator)
        self.assertEqual(contract, original)
        self.assertEqual(
            report["automation"]["nodes"][-1], original["cases"][0]["oracle"]
        )

    async def test_report_retains_checks_and_original_step_identity(self):
        automation, contract = fixture()
        report = await run_experiment(automation, contract, evaluate)
        self.assertTrue(any(t["phase"] == "audit-before" for t in report["trials"]))
        self.assertTrue(any(t["phase"] == "audit-after" for t in report["trials"]))
        self.assertTrue(
            any(
                t.get("removed_index") == 1 and not t["passed"]
                for t in report["trials"]
            )
        )
