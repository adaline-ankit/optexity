import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from scripts.generate_cached_automation import ReplayProposal, validate_proposal
from scripts.learn_replay import _baseline, improve_replay

FIXTURES = Path(__file__).parent / "fixtures/replay"


class ReplayLoopTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, directory, *, fail_round=None, repeat_trace=False):
        contexts = []

        async def generate(context, model, feedback):
            contexts.append(context)
            return validate_proposal(
                context, ReplayProposal(nodes=context["allowed_nodes"])
            ), {
                "llm": {"attempts": 1},
                "model": model,
            }

        async def evaluate(automation, label):
            number = len(contexts)
            path = directory / f"{label}.jsonl"
            rows = [
                json.loads(line)
                for line in (FIXTURES / "form_trace.jsonl").read_text().splitlines()
            ]
            for row in rows:
                row["run_id"] = "repeated" if repeat_trace else label
            path.write_text("\n".join(json.dumps(row) for row in rows))
            return {
                "status": "failed" if number == fail_round else "success",
                "llm": {"attempts": 0},
                "trace_path": str(path),
            }

        report = await improve_replay(
            FIXTURES / "form.json",
            FIXTURES / "form_trace.jsonl",
            directory,
            evaluate,
            generate=generate,
            rounds=3,
        )
        return report, contexts

    async def test_second_round_consumes_new_evidence_and_converges(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, contexts = await self.exercise(Path(tmp))
            self.assertEqual(report["stop_reason"], "converged")
            self.assertTrue(report["promoted"])
            self.assertEqual(len(contexts), 2)
            self.assertEqual(contexts[1]["request"]["trace"][0]["run_id"], "round-1")
            result = json.loads((Path(tmp) / "optimized.json").read_text())
            self.assertIn(
                "to_have_value",
                result["nodes"][-1]["python_script_action"]["execution_code"],
            )

    async def test_failed_candidate_keeps_last_verified_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = await self.exercise(Path(tmp), fail_round=2)
            self.assertEqual(report["stop_reason"], "candidate_failed")
            self.assertTrue(report["promoted"])
            self.assertEqual(report["best_round"], 1)

    async def test_first_failure_does_not_promote(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = await self.exercise(Path(tmp), fail_round=1)
            self.assertFalse(report["promoted"])
            self.assertFalse((Path(tmp) / "optimized.json").exists())

    async def test_stale_recapture_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = await self.exercise(Path(tmp), repeat_trace=True)
            self.assertEqual(report["stop_reason"], "error")
            self.assertIn("fresh", report["error"])
            self.assertEqual(report["best_round"], 1)

    async def test_invalid_round_budget_rejected_before_generation(self):
        generator = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            await improve_replay(
                FIXTURES / "form.json",
                FIXTURES / "form_trace.jsonl",
                Path(tmp),
                AsyncMock(),
                generate=generator,
                rounds=0,
            )
        generator.assert_not_awaited()

    async def test_success_without_zero_call_measurement_is_not_promoted(self):
        async def generate(context, model, feedback):
            return (
                validate_proposal(
                    context, ReplayProposal(nodes=context["allowed_nodes"])
                ),
                {},
            )

        for metrics in ({}, {"attempts": 1}):
            with tempfile.TemporaryDirectory() as tmp:
                report = await improve_replay(
                    FIXTURES / "form.json",
                    FIXTURES / "form_trace.jsonl",
                    Path(tmp),
                    AsyncMock(return_value={"status": "success", "llm": metrics}),
                    generate=generate,
                )
                self.assertFalse(report["promoted"])
                self.assertEqual(report["stop_reason"], "candidate_failed")

    async def test_baseline_without_independent_outcomes_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = json.loads((FIXTURES / "form.json").read_text())
            data["nodes"] = data["nodes"][:1]
            path = Path(tmp) / "baseline.json"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "independent"):
                _baseline(path)
