import json
import tempfile
import unittest
from pathlib import Path

from patchright.async_api import async_playwright

from scripts.replay_capture import NativeReplayCapture


class NativeCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content(
            '<input name="field"><input type="password" name="password">'
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_records_actual_target_and_successful_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = NativeReplayCapture(Path(tmp) / "trace", "fill form")
            with capture:
                locator = self.page.locator("[name=field]")
                target = await capture.target(locator)
                await locator.fill("hello")
                capture.record("input", {"text": "hello", "clear": True}, target)
                capture.finish(True)
            rows = [json.loads(line) for line in capture.path.read_text().splitlines()]
            self.assertEqual(rows[0]["target"]["attributes"]["name"], "field")
            self.assertTrue(rows[0]["executed"])
            self.assertEqual(rows[-1]["action_name"], "done")
            self.assertTrue(rows[-1]["result"]["success"])

    async def test_failed_run_has_no_success_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = NativeReplayCapture(Path(tmp) / "trace", "fill form")
            with capture:
                capture.record(
                    "input",
                    {"text": "hello"},
                    await capture.target(self.page.locator("[name=field]")),
                )
                capture.finish(False)
            rows = [json.loads(line) for line in capture.path.read_text().splitlines()]
            self.assertFalse(any(row.get("result", {}).get("success") for row in rows))

    async def test_sensitive_input_is_redacted_using_existing_recorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = NativeReplayCapture(Path(tmp) / "trace", "fill form")
            with capture:
                target = await capture.target(self.page.locator("[name=password]"))
                capture.record("input", {"text": "private-value"}, target)
            text = capture.path.read_text()
            self.assertNotIn("private-value", text)
            self.assertTrue(json.loads(text)["redacted_fields"])

    async def test_engine_wrapper_records_resolved_execution_and_restores_hooks(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from optexity.inference.core import run_automation as engine
        from optexity.schema.actions.interaction_action import InteractionAction

        action = InteractionAction.model_validate(
            {
                "strict_replay": True,
                "input_text": {
                    "command": "field",
                    "input_text": "actual",
                    "skip_prompt": True,
                    "assert_locator_presence": True,
                },
            }
        )

        async def resolve(command):
            return self.page.locator("[name=field]")

        browser = SimpleNamespace(get_locator_from_command=resolve)

        async def execute(action, task, memory, browser, retries_left):
            locator = await browser.get_locator_from_command(action.input_text.command)
            await locator.fill(action.input_text.input_text)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(engine, "run_interaction_action", execute),
        ):
            with NativeReplayCapture(Path(tmp) / "trace", "fill") as capture:
                await engine.run_interaction_action(action, None, None, browser, 1)
                capture.finish(True)
            self.assertIs(engine.run_interaction_action, execute)
            self.assertIs(browser.get_locator_from_command, resolve)
            row = json.loads(capture.path.read_text().splitlines()[0])
            self.assertEqual(row["action"]["input"]["text"], "actual")
            self.assertEqual(
                await self.page.locator("[name=field]").input_value(), "actual"
            )

    async def test_unsupported_native_options_rejected_before_execution(self):
        from unittest.mock import AsyncMock, patch

        from optexity.inference.core import run_automation as engine
        from optexity.schema.actions.interaction_action import InteractionAction

        execute = AsyncMock()
        action = InteractionAction.model_validate(
            {
                "strict_replay": True,
                "click_element": {
                    "command": "field",
                    "button": "right",
                    "skip_prompt": True,
                    "assert_locator_presence": True,
                },
            }
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(engine, "run_interaction_action", execute),
        ):
            with NativeReplayCapture(Path(tmp) / "trace", "click"):
                with self.assertRaisesRegex(ValueError, "options"):
                    await engine.run_interaction_action(action, None, None, None, 1)
        execute.assert_not_awaited()
