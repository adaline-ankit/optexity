import argparse
import json
from pathlib import Path
from typing import Any

from optexity.schema.automation import Automation


def _q(value: str) -> str:
    return repr(value)


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _attr(attrs: dict[str, Any], name: str) -> str | None:
    value = attrs.get(name)
    return value if isinstance(value, str) and value else None


def _command_from_target(target: dict[str, Any]) -> str:
    attrs = target.get("attributes") or {}
    tag = target.get("tag_name") or "*"

    if element_id := _attr(attrs, "id"):
        css = f"#{_css_escape(element_id)}"
        return f"locator({_q(css)})"
    if test_id := _attr(attrs, "data-testid"):
        css = f'[data-testid="{_css_escape(test_id)}"]'
        return f"locator({_q(css)})"
    if name := _attr(attrs, "name"):
        css = f'{tag}[name="{_css_escape(name)}"]'
        return f"locator({_q(css)})"
    if aria_label := _attr(attrs, "aria-label"):
        return f"get_by_label({_q(aria_label)})"
    if placeholder := _attr(attrs, "placeholder"):
        return f"get_by_placeholder({_q(placeholder)})"
    if xpath := target.get("xpath"):
        return f"locator({_q(f'xpath={xpath}')})"

    raise ValueError(f"Cannot derive locator command from target: {target}")


def _action_params(row: dict[str, Any]) -> dict[str, Any]:
    action = row.get("action") or {}
    params = action.get(row.get("action_name")) or {}
    if not isinstance(params, dict):
        raise ValueError(f"Invalid action params: {row}")
    return params


def _compile_row(row: dict[str, Any]) -> dict[str, Any] | None:
    name = row.get("action_name")
    if name in {"done", "wait"}:
        return None

    target = row.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"Action {name!r} has no cached target")

    command = _command_from_target(target)
    params = _action_params(row)

    if name == "input":
        text = params.get("text")
        if not isinstance(text, str):
            raise ValueError(f"Input action missing text: {row}")
        return {
            "type": "action_node",
            "interaction_action": {
                "input_text": {
                    "command": command,
                    "input_text": text,
                    "fill_or_type": "fill",
                    "skip_prompt": True,
                    "assert_locator_presence": True,
                }
            },
            "end_sleep_time": 0,
        }

    if name == "click":
        return {
            "type": "action_node",
            "interaction_action": {
                "click_element": {
                    "command": command,
                    "skip_prompt": True,
                    "assert_locator_presence": True,
                }
            },
            "end_sleep_time": 0,
        }

    raise ValueError(f"Unsupported cached action: {name}")


def _read_trace(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            rows.append(row)
        except Exception as exc:
            raise ValueError(f"Invalid trace row {line_number}: {exc}") from exc
    return rows


def compile_cached_automation(baseline_path: Path, trace_path: Path) -> Automation:
    baseline = Automation.model_validate(json.loads(baseline_path.read_text()))
    nodes = []
    for row in _read_trace(trace_path):
        node = _compile_row(row)
        if node is not None:
            nodes.append(node)

    if not nodes:
        raise ValueError("Trace produced zero deterministic nodes")

    data = baseline.model_dump(mode="json")
    data["nodes"] = nodes
    return Automation.model_validate(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    automation = compile_cached_automation(args.baseline, args.trace)
    args.out.write_text(json.dumps(automation.model_dump(mode="json"), indent=2) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
