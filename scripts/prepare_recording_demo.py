"""Prepare a strict experiment from the actual Recorder onboarding JSON.

Run from the repository root. Original order, parameters, commands, and input
semantics stay intact. Only explicit replay policy and sleep/retry limits change.
Outcome checks and counterfactual probes are separately authored test fixtures.
"""

import copy
import json
from pathlib import Path

from optexity.schema.automation import Automation

FIELDS = {
    "full_name": "04fullname",
    "address_line_1": "10address1",
    "address_line_2": "11address2",
    "city": "13adr_city",
}


def prepare(source):
    strict = copy.deepcopy(source)
    strict["browser_channel"] = "chrome"
    for node in strict["nodes"]:
        interaction = node["interaction_action"]
        action = interaction.get("input_text") or interaction["click_element"]
        action.update(skip_prompt=True, assert_locator_presence=True)
        interaction.update(
            strict_replay=True, max_tries=1, max_timeout_seconds_per_try=3
        )
        node["end_sleep_time"] = 0
    Automation.model_validate(strict)
    contract = {"resettable": True, "cases": []}
    for name, values in [
        ("captured", ["myname", "xyz", "abc", "SF"]),
        ("different-data", ["Ada Lovelace", "42 Test Road", "Unit 7", "New Delhi"]),
    ]:
        parameters = {key: [value] for key, value in zip(FIELDS, values)}
        expected = dict(zip(FIELDS.values(), values))
        source_code = (
            "async def code_fn(page):\n"
            "    from patchright.async_api import expect\n"
            f"    expected = {expected!r}\n"
            "    for name, value in expected.items():\n"
            "        try:\n"
            "            await expect(page.locator('input[name=\"' + name + '\"]')).to_have_value(value, timeout=500)\n"
            "        except AssertionError:\n"
            "            raise AssertionError('oracle:' + name) from None\n"
        )
        probes = []
        for key, field in FIELDS.items():
            probe = next(
                copy.deepcopy(n)
                for n in strict["nodes"]
                if n["interaction_action"].get("input_text", {}).get("input_text")
                == "{" + key + "[0]}"
            )
            probe["interaction_action"]["input_text"]["input_text"] = "__wrong__"
            probes.append(
                {
                    "name": "corrupt-" + key,
                    "node": probe,
                    "expected_error": "oracle:" + field,
                }
            )
        contract["cases"].append(
            {
                "name": name,
                "input_parameters": parameters,
                "oracle": {
                    "type": "action_node",
                    "end_sleep_time": 0,
                    "python_script_action": {"execution_code": source_code},
                },
                "probes": probes,
            }
        )
    return strict, contract


def main():
    root = Path(__file__).resolve().parents[1]
    source = json.loads((root / "evidence/recorder-source.json").read_text())
    strict, contract = prepare(source)
    weak = copy.deepcopy(contract)
    weak["cases"] = weak["cases"][:1]
    case = weak["cases"][0]
    code = case["oracle"]["python_script_action"]["execution_code"]
    case["oracle"]["python_script_action"]["execution_code"] = code.replace(
        "    for name, value in expected.items():",
        "    del expected['13adr_city']\n    for name, value in expected.items():",
    )
    # First probe demonstrates this deliberately incomplete oracle quickly.
    case["probes"] = list(reversed(case["probes"]))
    for filename, data in [
        ("recorder-strict.json", strict),
        ("recorder-contract.json", contract),
        ("recorder-weak-contract.json", weak),
    ]:
        (root / "evidence" / filename).write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
