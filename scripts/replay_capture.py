"""Recapture native replay executions through the existing trace writer."""

import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from browser_use.agent.optexity_step_cache import (
    execution_metadata,
    record_action_trace,
    trace_actions_to,
)


class NativeReplayCapture:
    def __init__(self, directory: Path, objective: str):
        self.directory = directory
        self.path = directory / "browser_use_trace.jsonl"
        self.objective = objective
        self.position = 0
        self.stack = ExitStack()

    async def target(self, locator: Any) -> dict[str, Any]:
        if await locator.count() != 1:
            raise ValueError("Replay capture requires exactly one resolved target")
        return await locator.evaluate("""el => ({
            tag_name: el.tagName.toLowerCase(),
            attributes: Object.fromEntries(
                ['id','data-testid','name','aria-label','placeholder','type','title','href']
                .filter(key => el.hasAttribute(key)).map(key => [key, el.getAttribute(key)])
            ),
            scope: el.getRootNode() !== el.ownerDocument ? 'shadow' :
                el.ownerDocument.defaultView !== window.top ? 'frame' : 'document'
        })""")

    def record(
        self,
        name: str,
        params: dict[str, Any],
        target: dict[str, Any] | None = None,
        elapsed: float = 0,
    ) -> None:
        self.position += 1
        result: dict[str, Any] = {"metadata": execution_metadata(None, target)}
        if name == "done":
            result.update(is_done=True, success=True)
        record_action_trace(
            task=self.objective,
            step_number=self.position,
            action_number=1,
            total_actions=1,
            action_name=name,
            action_data={name: params},
            result=result,
            elapsed_seconds=elapsed,
        )

    def finish(self, success: bool) -> None:
        if success:
            self.record("done", {"success": True})

    def __enter__(self):
        from optexity.inference.core import run_automation as engine

        self.directory.mkdir(parents=True, exist_ok=False)
        self.stack.enter_context(trace_actions_to(self.directory))
        original_interaction = engine.run_interaction_action
        original_sleep = engine.run_sleep_action

        async def interaction(action, task, memory, browser, retries_left):
            if not action.strict_replay:
                raise ValueError("Native recapture requires strict replay actions")
            native = action.input_text or action.click_element
            if native is None:
                raise ValueError("Unsupported native action")
            fields = {"command", "skip_prompt", "assert_locator_presence"}
            if action.input_text:
                fields |= {"input_text", "fill_or_type"}
                if action.input_text.fill_or_type == "key_press":
                    raise ValueError("Key presses cannot be recaptured as input")
            if (
                set(native.model_dump(exclude_defaults=True, exclude_none=True))
                - fields
            ):
                raise ValueError("Unsupported native action options")
            original_locator = browser.get_locator_from_command
            target = None

            async def resolve(command):
                nonlocal target
                locator = await original_locator(command)
                if command == native.command:
                    target = await self.target(locator)
                return locator

            started = time.perf_counter()
            with patch.object(browser, "get_locator_from_command", resolve):
                await original_interaction(action, task, memory, browser, retries_left)
            if target is None:
                raise ValueError("Native action did not resolve a target")
            if action.input_text:
                params = {
                    "text": action.input_text.input_text,
                    "clear": action.input_text.fill_or_type == "fill",
                }
                self.record("input", params, target, time.perf_counter() - started)
            else:
                self.record("click", {}, target, time.perf_counter() - started)

        async def sleep(action):
            started = time.perf_counter()
            await original_sleep(action)
            self.record(
                "wait",
                {"seconds": action.sleep_time},
                elapsed=time.perf_counter() - started,
            )

        self.stack.enter_context(
            patch.object(engine, "run_interaction_action", interaction)
        )
        self.stack.enter_context(patch.object(engine, "run_sleep_action", sleep))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)
