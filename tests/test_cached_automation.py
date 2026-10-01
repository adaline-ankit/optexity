"""Regression tests for promoting execution traces into native automation."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.compile_cached_automation import (
    compile_cached_automation,
    _compile_row,
    _command_from_target,
)
from optexity.schema.actions.interaction_action import InteractionAction


def row(name="input", **overrides):
    result = {
        "schema_version": 2,
        "run_id": "unit-run",
        "task_sha256": hashlib.sha256(b"fill form").hexdigest(),
        "step_number": 1,
        "action_number": 1,
        "action_name": name,
        "action": {name: {"index": 1, "text": "hello", "clear": True}},
        "target": {
            "tag_name": "input",
            "attributes": {"name": "field"},
            "scope": "document",
        },
        "result": {},
        "executed": True,
        "redacted_fields": [],
    }
    result.update(overrides)
    return result


def done():
    return row(
        "done",
        action={"done": {"success": True}},
        target=None,
        result={"is_done": True, "success": True},
        step_number=2,
    )


class CompilerTests(unittest.TestCase):
    def compile(self, rows, extra_nodes=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = {
                "url": "https://example.com",
                "parameters": {"input_parameters": {}, "generated_parameters": {}},
                "nodes": [
                    {
                        "type": "action_node",
                        "interaction_action": {
                            "agentic_task": {
                                "task": "fill form",
                                "backend": "browser_use",
                                "max_steps": 5,
                            }
                        },
                    }
                ]
                + (extra_nodes or []),
            }
            (root / "baseline.json").write_text(json.dumps(baseline))
            (root / "trace.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            return compile_cached_automation(
                root / "baseline.json", root / "trace.jsonl"
            )

    def test_mixed_runs_rejected(self):
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.compile([row(), done() | {"run_id": "another"}])

    def test_duplicate_positions_rejected(self):
        with self.assertRaisesRegex(ValueError, "order"):
            self.compile([row(), row(), done()])

    def test_reversed_positions_rejected(self):
        with self.assertRaisesRegex(ValueError, "order"):
            self.compile([row(step_number=3), done()])

    def test_unsupported_action_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported cached action"):
            self.compile([row("scroll"), done()])

    def test_reject_failed_action(self):
        with self.assertRaisesRegex(ValueError, "failed"):
            self.compile([row(result={"error": "not available"}), done()])

    def test_reject_incomplete_trace(self):
        with self.assertRaisesRegex(ValueError, "successful done"):
            self.compile([row()])

    def test_reject_unsuccessful_done(self):
        with self.assertRaises(ValueError):
            self.compile(
                [row(), done() | {"result": {"is_done": True, "success": False}}]
            )

    def test_reject_unexecuted_action(self):
        with self.assertRaisesRegex(ValueError, "executed"):
            self.compile([row(executed=False), done()])

    def test_reject_old_schema(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            self.compile([row(schema_version=1), done()])

    def test_reject_wrong_task(self):
        with self.assertRaisesRegex(ValueError, "task"):
            self.compile([row(task_sha256="different"), done()])

    def test_preserve_other_nodes(self):
        tail = {
            "type": "assert_locator_node",
            "locator": "locator('h1')",
            "assertion": "to_be_visible",
        }
        compiled = self.compile([row(), done()], [tail])
        self.assertEqual(len(compiled.nodes), 2)
        self.assertEqual(compiled.nodes[-1].type, "assert_locator_node")

    def test_strict_replay_is_explicit(self):
        result = self.compile([row(), done()])
        self.assertTrue(result.nodes[0].interaction_action.strict_replay)

    def test_wait_is_preserved(self):
        node = _compile_row(row("wait", action={"wait": {"seconds": 2}}))
        self.assertIsNotNone(node)

    def test_redaction_blocks_compile(self):
        with self.assertRaisesRegex(ValueError, "redacted"):
            _compile_row(row(redacted_fields=["input.text"]))

    def test_scope_blocks_compile(self):
        with self.assertRaisesRegex(ValueError, "scope"):
            _compile_row(row(target={"scope": "shadow", "attributes": {"id": "x"}}))

    def test_append_preserved(self):
        node = _compile_row(
            row(action={"input": {"index": 1, "text": "!", "clear": False}})
        )
        self.assertEqual(
            node["interaction_action"]["input_text"]["fill_or_type"], "type"
        )

    def test_quote_locator_is_single_expression(self):
        import ast

        value = "x'\" [.] #"
        command = _command_from_target(
            {"tag_name": "input", "attributes": {"name": value}, "scope": "document"}
        )
        tree = ast.parse(command, mode="eval")
        self.assertIsInstance(tree.body, ast.Call)
        self.assertEqual(len(tree.body.args), 1)

    def test_strict_flag_rejects_agentic_task(self):
        with self.assertRaises(ValueError):
            InteractionAction.model_validate(
                {"strict_replay": True, "agentic_task": {"task": "x", "max_steps": 5}}
            )


class ReplayPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_locator_failure_never_calls_classifier(self):
        import os

        os.environ.setdefault("DEPLOYMENT", "dev")
        os.environ.setdefault("OPTEXITY_API_KEY", "local")
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from optexity.exceptions import AssertLocatorPresenceException
        from optexity.inference.core.run_interaction import run_interaction_action

        action = InteractionAction.model_validate(
            _compile_row(row())["interaction_action"]
        )
        memory = SimpleNamespace(automation_state=SimpleNamespace())
        error = AssertLocatorPresenceException(
            message="missing", original_error="missing", command='locator("#gone")'
        )
        with (
            patch(
                "optexity.inference.core.run_interaction.handle_input_text",
                new=AsyncMock(side_effect=error),
            ),
            patch(
                "optexity.inference.core.run_interaction.handle_assert_locator_presence_error",
                new=AsyncMock(),
            ) as fallback,
        ):
            with self.assertRaises(AssertLocatorPresenceException):
                await run_interaction_action(
                    action, SimpleNamespace(), memory, SimpleNamespace(), 2
                )
            fallback.assert_not_awaited()

    async def test_llm_guard_blocks_and_counts_attempt(self):
        from scripts.run_local_automation import measure_llm_calls
        import litellm

        with measure_llm_calls(True) as metrics:
            with self.assertRaisesRegex(RuntimeError, "forbidden"):
                await litellm.acompletion(model="openai/gpt-4.1-mini", messages=[])
        self.assertEqual(metrics["attempts"], 1)
        self.assertEqual(metrics["prompt_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
