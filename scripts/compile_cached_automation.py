import argparse
import json
import re
from pathlib import Path
from typing import Any

from optexity.schema.automation import Automation

_SENSITIVE_MARKER = '<redacted:sensitive_input>'
_SAFE_TAG = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')


def _q(value: str) -> str:
    return repr(value)


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def _tag_name(raw: Any) -> str:
    tag = raw if isinstance(raw, str) and _SAFE_TAG.match(raw) else '*'
    return tag.lower()


def _attr(attrs: dict[str, Any], name: str) -> str | None:
    value = attrs.get(name)
    return value if isinstance(value, str) and value else None


def _xpath_for_attr(tag: str, attr: str, value: str) -> str:
    element = '*' if attr in {'id', 'data-testid'} else tag
    return f"//{element}[@{attr}={_xpath_literal(value)}]"


def _command_from_target(target: dict[str, Any]) -> str:
    attrs = target.get('attributes') or {}
    tag = _tag_name(target.get('tag_name'))

    if element_id := _attr(attrs, 'id'):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'id', element_id))})"
    if test_id := _attr(attrs, 'data-testid'):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'data-testid', test_id))})"
    if name := _attr(attrs, 'name'):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'name', name))})"
    if aria_label := _attr(attrs, 'aria-label'):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'aria-label', aria_label))})"
    if placeholder := _attr(attrs, 'placeholder'):
        return f"locator({_q('xpath=' + _xpath_for_attr(tag, 'placeholder', placeholder))})"
    if xpath := target.get('xpath'):
        return f"locator({_q(f'xpath={xpath}')})"

    raise ValueError(f"Cannot derive locator command from target: {target}")


def _action_params(row: dict[str, Any]) -> dict[str, Any]:
    action = row.get('action') or {}
    params = action.get(row.get('action_name')) or {}
    if not isinstance(params, dict):
        raise ValueError(f"Invalid action params: {row}")
    return params


def _raise_if_redacted(row: dict[str, Any]) -> None:
    if row.get('redacted_fields'):
        raise ValueError(
            'Trace contains redacted sensitive input. Regenerate cached automation with an explicit secure parameter placeholder instead of plaintext.'
        )


def _compile_row(row: dict[str, Any]) -> dict[str, Any] | None:
    name = row.get('action_name')
    if name in {'done', 'wait'}:
        return None

    target = row.get('target')
    if not isinstance(target, dict):
        raise ValueError(f"Action {name!r} has no cached target")

    command = _command_from_target(target)
    params = _action_params(row)

    if name == 'input':
        _raise_if_redacted(row)
        text = params.get('text')
        if text == _SENSITIVE_MARKER:
            _raise_if_redacted({'redacted_fields': ['input.text']})
        if not isinstance(text, str):
            raise ValueError(f"Input action missing text: {row}")
        clear = params.get('clear', True)
        return {
            'type': 'action_node',
            'interaction_action': {
                'input_text': {
                    'command': command,
                    'input_text': text,
                    'fill_or_type': 'fill' if clear else 'type',
                    'skip_prompt': True,
                    'assert_locator_presence': True,
                }
            },
            'end_sleep_time': 0,
        }

    if name == 'click':
        click: dict[str, Any] = {
            'command': command,
            'skip_prompt': True,
            'assert_locator_presence': True,
        }
        if button := params.get('button'):
            if button not in {'left', 'right', 'middle'}:
                raise ValueError(f"Unsupported click button {button!r}: {row}")
            click['button'] = button
        click_count = params.get('click_count')
        if click_count is not None:
            if click_count == 2:
                click['double_click'] = True
            elif click_count != 1:
                raise ValueError(f"Unsupported click_count {click_count!r}: {row}")
        return {
            'type': 'action_node',
            'interaction_action': {'click_element': click},
            'end_sleep_time': 0,
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
        raise ValueError('Trace produced zero deterministic nodes')

    data = baseline.model_dump(mode='json')
    data['nodes'] = nodes
    return Automation.model_validate(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()

    automation = compile_cached_automation(args.baseline, args.trace)
    args.out.write_text(json.dumps(automation.model_dump(mode='json'), indent=2) + '\n')
    print(f'wrote {args.out}')


if __name__ == '__main__':
    main()
