import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from optexity.schema.automation import Automation

_SENSITIVE_MARKER = "<redacted:sensitive_input>"
_SAFE_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _q(value: str) -> str:
    return repr(value)


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"


def _tag_name(raw: Any) -> str:
    tag = raw if isinstance(raw, str) and _SAFE_TAG.fullmatch(raw) else "*"
    return tag.lower()


def _attr(attrs: dict[str, Any], name: str) -> str | None:
    value = attrs.get(name)
    return value if isinstance(value, str) and value else None


def _xpath_for_attr(tag: str, attr: str, value: str) -> str:
    element = "*" if attr in {"id", "data-testid"} else tag
    return f"//{element}[@{attr}={_xpath_literal(value)}]"


def _command_from_target(target: dict[str, Any]) -> str:
    if target.get("scope") != "document":
        raise ValueError(
            "Unsupported target scope; frame/shadow replay needs explicit scoping"
        )
    attrs = target.get("attributes") or {}
    tag = _tag_name(target.get("tag_name"))

    if element_id := _attr(attrs, "id"):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'id', element_id))})"
    if test_id := _attr(attrs, "data-testid"):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'data-testid', test_id))})"
    if name := _attr(attrs, "name"):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'name', name))})"
    if aria_label := _attr(attrs, "aria-label"):
        return (
            f"locator({_q('xpath=' + _xpath_for_attr(tag, 'aria-label', aria_label))})"
        )
    if placeholder := _attr(attrs, "placeholder"):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'placeholder', placeholder))})"
    if href := _attr(attrs, "href"):
        predicate = f"@href={_xpath_literal(href)}"
        if title := _attr(attrs, "title"):
            predicate += f" and @title={_xpath_literal(title)}"
        return f"locator({_q('xpath=//' + tag + '[' + predicate + ']')})"
    if xpath := target.get("xpath"):
        return f"locator({_q(f'xpath={xpath}')})"

    raise ValueError(f"Cannot derive locator command from target: {target}")


def _action_params(row: dict[str, Any]) -> dict[str, Any]:
    name = row.get("action_name")
    allowed = {
        "input": {"index", "text", "clear"},
        "click": {"index", "button", "click_count"},
        "wait": {"seconds"},
        "done": {"success"},
    }
    if name not in allowed:
        raise ValueError(f"Unsupported cached action: {name}")
    action = row.get("action")
    if not isinstance(action, dict) or set(action) != {name}:
        raise ValueError("Trace row must contain exactly one matching action")
    params = action[name]
    if not isinstance(params, dict):
        raise ValueError(f"Invalid parameters for {name}")
    unsupported = set(params) - allowed[name]
    if unsupported:
        raise ValueError(f"Unsupported {name} parameters: {sorted(unsupported)}")
    return params


def _raise_if_redacted(row: dict[str, Any]) -> None:
    if row.get("redacted_fields"):
        raise ValueError(
            "Trace contains redacted sensitive input. Regenerate cached automation with an explicit secure parameter placeholder instead of plaintext."
        )


def _compile_row(row: dict[str, Any]) -> dict[str, Any] | None:
    name = row.get("action_name")
    if row.get("result", {}).get("error"):
        raise ValueError("Cannot compile failed action")
    params = _action_params(row)
    if name == "done":
        return None
    if name == "wait":
        seconds = params.get("seconds")
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 <= seconds <= 60
        ):
            raise ValueError("Invalid wait seconds")
        return {
            "type": "action_node",
            "sleep_action": {"sleep_time": seconds},
            "end_sleep_time": 0,
        }
    if row.get("executed") is not True:
        raise ValueError("Action was not confirmed executed by its tool")

    target = row.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"Action {name!r} has no cached target")

    command = _command_from_target(target)

    if name == "input":
        _raise_if_redacted(row)
        text = params.get("text")
        if text == _SENSITIVE_MARKER:
            _raise_if_redacted({"redacted_fields": ["input.text"]})
        if not isinstance(text, str):
            raise ValueError(f"Input action missing text: {row}")
        clear = params.get("clear", True)
        if type(clear) is not bool:
            raise ValueError("Input clear must be boolean")
        return {
            "type": "action_node",
            "interaction_action": {
                "strict_replay": True,
                "max_tries": 1,
                "max_timeout_seconds_per_try": 5,
                "input_text": {
                    "command": command,
                    "input_text": text,
                    "fill_or_type": "fill" if clear else "type",
                    "skip_prompt": True,
                    "assert_locator_presence": True,
                },
            },
            "end_sleep_time": 0,
        }

    if name == "click":
        click: dict[str, Any] = {
            "command": command,
            "skip_prompt": True,
            "assert_locator_presence": True,
        }
        if button := params.get("button"):
            if button not in {"left", "right", "middle"}:
                raise ValueError(f"Unsupported click button {button!r}: {row}")
            click["button"] = button
        click_count = params.get("click_count")
        if click_count is not None:
            if type(click_count) is not int:
                raise ValueError("Unsupported click_count: expected integer")
            if click_count == 2:
                click["double_click"] = True
            elif click_count != 1:
                raise ValueError(f"Unsupported click_count {click_count!r}: {row}")
        return {
            "type": "action_node",
            "interaction_action": {
                "click_element": click,
                "strict_replay": True,
                "max_tries": 1,
                "max_timeout_seconds_per_try": 5,
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
            if not isinstance(row, dict):
                raise ValueError("Trace row must be an object")
            rows.append(row)
        except Exception as exc:
            raise ValueError(f"Invalid trace row {line_number}: {exc}") from exc
    return rows


def compile_cached_automation(baseline_path: Path, trace_path: Path) -> Automation:
    baseline = Automation.model_validate(json.loads(baseline_path.read_text()))
    data = baseline.model_dump(mode="json")
    agent_indices = [
        i
        for i, node in enumerate(data["nodes"])
        if (node.get("interaction_action") or {}).get("agentic_task")
    ]
    if len(agent_indices) != 1:
        raise ValueError("Expected exactly one top-level agentic task")
    index = agent_indices[0]
    objective = data["nodes"][index]["interaction_action"]["agentic_task"]["task"]
    rows = _read_trace(trace_path)
    expected_hash = hashlib.sha256(objective.encode()).hexdigest()
    if not rows or any(r.get("schema_version") != 2 for r in rows):
        raise ValueError("Expected nonempty schema version 2 trace")
    if any(r.get("task_sha256") != expected_hash for r in rows):
        raise ValueError("Trace task does not match baseline task")
    run_ids = {r.get("run_id") for r in rows}
    if len(run_ids) != 1 or None in run_ids:
        raise ValueError("Trace must contain exactly one run_id")
    positions = [(r.get("step_number"), r.get("action_number")) for r in rows]
    if any(type(s) is not int or type(a) is not int for s, a in positions):
        raise ValueError("Invalid trace ordering fields")
    if positions != sorted(set(positions)):
        raise ValueError("Duplicate or out-of-order trace actions")
    last = rows[-1]
    if (
        last.get("action_name") != "done"
        or last.get("result", {}).get("is_done") is not True
        or last.get("result", {}).get("success") is not True
        or any(r.get("action_name") == "done" for r in rows[:-1])
    ):
        raise ValueError("Trace must end with one successful done result")
    nodes = [node for row in rows if (node := _compile_row(row)) is not None]
    if not nodes:
        raise ValueError("Trace produced zero deterministic nodes")
    # Splice only the learned step; retain surrounding setup and assertions.
    data["nodes"][index : index + 1] = nodes
    return Automation.model_validate(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    automation = compile_cached_automation(args.baseline, args.trace)
    args.out.write_text(
        json.dumps(
            automation.model_dump(
                mode="json", exclude_none=True, exclude_defaults=True
            ),
            indent=2,
        )
        + "\n"
    )
    report = {
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        "decisions": [
            {
                "step": r["step_number"],
                "action": r["action_number"],
                "name": r["action_name"],
                "decision": (
                    "drop terminal bookkeeping"
                    if r["action_name"] == "done"
                    else "retain; no evidence of redundancy"
                ),
            }
            for r in _read_trace(args.trace)
        ],
    }
    args.out.with_suffix(".provenance.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
