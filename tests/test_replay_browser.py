"""Real Chromium checks for generated selectors and outcome assertions.
Run separately: python -m unittest discover -s tests -p test_replay_browser.py -v
No network or LLM calls: HTML lives in a fresh browser page.
"""

import html
import json
import unittest
from pathlib import Path

from patchright.async_api import Error, async_playwright
from replay_fixtures import prepare

from scripts.compile_cached_automation import _command_from_target


class BrowserReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(500)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_quotes_and_symbols_target_exact_field(self):
        name = "a'\".#[] space"
        await self.page.set_content(
            '<input name="' + html.escape(name, quote=True) + '"><input name="other">'
        )
        command = _command_from_target(
            {"tag_name": "input", "attributes": {"name": name}, "scope": "document"}
        )
        locator = eval("page." + command, {"page": self.page})
        await locator.fill("verified")
        self.assertEqual(await locator.input_value(), "verified")
        self.assertEqual(await self.page.locator("[name=other]").input_value(), "")

    async def test_duplicate_locator_fails_instead_of_picking_first(self):
        await self.page.set_content('<input name="same"><input name="same">')
        command = _command_from_target(
            {"tag_name": "input", "attributes": {"name": "same"}, "scope": "document"}
        )
        with self.assertRaisesRegex(Error, "strict mode violation"):
            await eval("page." + command, {"page": self.page}).fill("wrong")

    async def test_form_oracle_detects_wrong_city(self):
        baseline = json.loads(
            (Path(__file__).parent / "fixtures/replay/form.json").read_text()
        )
        source = baseline["nodes"][-1]["python_script_action"]["execution_code"]
        expected = {
            "04fullname": "myname",
            "10address1": "xyz",
            "11address2": "abc",
            "13adr_city": "wrong",
        }
        await self.page.set_content(
            "".join(f'<input name="{k}" value="{v}">' for k, v in expected.items())
        )
        namespace = {}
        exec(source, {}, namespace)
        with self.assertRaises(AssertionError):
            await namespace["code_fn"](self.page)
        await self.page.locator('[name="13adr_city"]').fill("SF")
        await namespace["code_fn"](self.page)

    async def test_recording_oracle_rejects_each_corrupted_field(self):
        source = json.loads(
            (Path(__file__).parent / "fixtures/replay/recorded_form.json").read_text()
        )
        _, contract = prepare(source)
        expected = {
            "04fullname": "myname",
            "10address1": "xyz",
            "11address2": "abc",
            "13adr_city": "SF",
        }
        await self.page.set_content(
            "".join(f'<input name="{k}" value="{v}">' for k, v in expected.items())
        )
        namespace = {}
        exec(
            contract["cases"][0]["oracle"]["python_script_action"]["execution_code"],
            {},
            namespace,
        )
        oracle = namespace["code_fn"]
        await oracle(self.page)
        for name, correct in expected.items():
            with self.subTest(field=name):
                await self.page.locator(f'[name="{name}"]').fill("__wrong__")
                with self.assertRaisesRegex(AssertionError, "oracle:" + name):
                    await oracle(self.page)
                await self.page.locator(f'[name="{name}"]').fill(correct)
        await oracle(self.page)

    async def test_weak_oracle_accepts_wrong_city_despite_other_checks(self):
        source = json.loads(
            (Path(__file__).parent / "fixtures/replay/recorded_form.json").read_text()
        )
        _, contract = prepare(source)
        code = contract["cases"][0]["oracle"]["python_script_action"]["execution_code"]
        code = code.replace(
            "    for name, value in expected.items():",
            "    del expected['13adr_city']\n    for name, value in expected.items():",
        )
        await self.page.set_content(
            '<input name="04fullname" value="myname"><input name="10address1" value="xyz"><input name="11address2" value="abc"><input name="13adr_city" value="WRONG">'
        )
        namespace = {}
        exec(code, {}, namespace)
        await namespace["code_fn"](self.page)
        self.assertEqual(
            await self.page.locator('[name="13adr_city"]').input_value(), "WRONG"
        )
