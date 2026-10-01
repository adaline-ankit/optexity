"""Exercise real browser-use capture on a multi-page site without an LLM.

The harness chooses the two targets from live DOM facts; browser-use executes
both clicks and its done action. This validates capture/compile/replay, not
LLM planning quality. Use test_automation_books.json with the normal runner for
an additional paid agent-learning run when credentials are available.
"""

import asyncio
import json
from pathlib import Path

from scripts.run_local_automation import (
    _build_task,
    _load_automation,
    measure_llm_calls,
)


async def record_books(output_dir: Path) -> Path:
    from browser_use.agent.optexity_step_cache import trace_actions_to
    from optexity.inference import child_process
    from optexity.inference.infra.browser import Browser
    from optexity.inference.models import normalize_model
    from optexity.schema.memory import Memory
    from patchright.async_api import expect

    automation = _load_automation(Path("test_automation_books.json"))
    task = _build_task(automation, output_dir, "openai/gpt-4.1-mini")
    browser = None
    trace_dir = task.logs_directory / "step_0" / "step_cache"
    try:
        await child_process.setup_browser(task, "local-books-capture", 0)
        browser = Browser(
            memory=Memory(unique_child_arn="local-books-capture"),
            cdp_url=child_process._global_actual_browser.cdp_url,
            llm_model=normalize_model(task.llm_provider, task.llm_model_name),
        )
        await browser.start()
        await browser.go_to_url(automation.url)
        agent = browser.backend_agent
        agent.task = automation.nodes[0].interaction_action.agentic_task.task
        with measure_llm_calls(True) as calls, trace_actions_to(trace_dir):
            for step, href_suffix in enumerate(
                ("travel_2/index.html", "its-only-the-himalayas_981/index.html"), 1
            ):
                state = await browser.get_browser_state_summary()
                matches = [
                    (index, node)
                    for index, node in state.dom_state.selector_map.items()
                    if node.tag_name.lower() == "a"
                    and (node.attributes.get("href") or "").endswith(href_suffix)
                ]
                if len(matches) > 1:
                    matches = [
                        (i, n)
                        for i, n in matches
                        if n.attributes.get("title") == "It's Only the Himalayas"
                    ]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"Expected unique target for step {step}; got {len(matches)}"
                    )
                agent.state.n_steps = step
                result = await agent.multi_act(
                    [agent.ActionModel(click={"index": matches[0][0]})]
                )
                if not result or result[0].error:
                    raise RuntimeError(f"Click failed at step {step}")
                page = await browser.get_current_page()
                expected = "Travel" if step == 1 else "It's Only the Himalayas"
                await expect(page.locator("h1")).to_have_text(expected, timeout=5000)
            namespace = {}
            exec(
                automation.nodes[-1].python_script_action.execution_code, {}, namespace
            )
            await namespace["code_fn"](await browser.get_current_page())
            agent.state.n_steps = 3
            await agent.multi_act(
                [
                    agent.ActionModel(
                        done={"success": True, "text": "Verified book detail"}
                    )
                ]
            )
        receipt = {
            "status": "success",
            "capture_mode": "scripted live-DOM selection; real browser-use tools",
            "llm": calls,
            "trace_path": str(trace_dir / "browser_use_trace.jsonl"),
        }
        (task.task_directory / "capture_result.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        print(json.dumps(receipt, indent=2))
        return trace_dir / "browser_use_trace.jsonl"
    finally:
        if browser:
            await browser.stop()
        await child_process.restart_global_actual_browser("books capture cleanup")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("/tmp/optexity-books-tool-capture")
    )
    asyncio.run(record_books(parser.parse_args().output_dir))
