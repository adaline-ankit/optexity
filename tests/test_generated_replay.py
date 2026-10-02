import copy
import json
import unittest
from pathlib import Path

from scripts.generate_cached_automation import (
    ReplayProposal,
    prepare_generation,
    validate_proposal,
)

FIXTURES = Path(__file__).parent / "fixtures/replay"


class GeneratedReplayTests(unittest.TestCase):
    def setUp(self):
        self.context = prepare_generation(
            FIXTURES / "form.json", FIXTURES / "form_trace.jsonl"
        )

    def proposal(self, nodes=None):
        return ReplayProposal(
            nodes=copy.deepcopy(
                self.context["allowed_nodes"] if nodes is None else nodes
            ),
            explanation="Use observed input targets.",
        )

    def test_generated_json_preserves_original_assertions(self):
        result = validate_proposal(self.context, self.proposal())
        self.assertEqual(len(result.nodes), 5)
        self.assertEqual(
            result.nodes[-1].model_dump(),
            self.context["baseline"].nodes[-1].model_dump(),
        )

    def test_changed_target_and_input_are_rejected(self):
        for key, value in [
            ("command", "locator('#invented')"),
            ("input_text", "wrong"),
        ]:
            proposal = self.proposal()
            proposal.nodes[0]["interaction_action"]["input_text"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "evidence"):
                validate_proposal(self.context, proposal)

    def test_reordered_or_duplicated_actions_are_rejected(self):
        nodes = self.context["allowed_nodes"]
        for altered in ([nodes[1], nodes[0]], [nodes[0], nodes[0]]):
            with (
                self.subTest(nodes=altered),
                self.assertRaisesRegex(ValueError, "evidence"),
            ):
                validate_proposal(self.context, self.proposal(altered))

    def test_unsupported_fields_cannot_be_silently_ignored(self):
        proposal = self.proposal()
        proposal.nodes[0]["interaction_action"]["input_text"]["made_up_option"] = True
        with self.assertRaisesRegex(ValueError, "Unknown"):
            validate_proposal(self.context, proposal)

    def test_fallback_and_injected_script_are_rejected(self):
        proposal = self.proposal()
        proposal.nodes[0]["interaction_action"]["input_text"]["skip_prompt"] = False
        with self.assertRaises(ValueError):
            validate_proposal(self.context, proposal)
        with self.assertRaises(ValueError):
            validate_proposal(
                self.context,
                self.proposal(
                    [
                        {
                            "type": "action_node",
                            "python_script_action": {
                                "execution_code": 'print("unsafe")'
                            },
                        }
                    ]
                ),
            )

    def test_deletion_is_only_a_candidate_until_replay(self):
        result = validate_proposal(
            self.context, self.proposal(self.context["allowed_nodes"][1:])
        )
        self.assertEqual(len(result.nodes), 4)
        self.assertIsNotNone(result.nodes[-1].python_script_action)

    def test_prompt_contains_docs_trace_and_output_schema(self):
        request = self.context["request"]
        self.assertIn("Optexity", request["documentation"])
        self.assertEqual(len(request["trace"]), 5)
        self.assertIn("nodes", request["response_schema"]["properties"])
        self.assertIn("04fullname", json.dumps(request))

    def test_empty_or_extra_proposal_fields_rejected(self):
        for proposal in (
            {"nodes": []},
            {"nodes": self.context["allowed_nodes"], "url": "elsewhere"},
        ):
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                ReplayProposal.model_validate(proposal)


class GenerationBudgetTests(unittest.TestCase):
    def test_call_budget_blocks_before_provider_and_caps_output(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        import litellm

        from scripts.llm_guard import measure_llm_calls

        response = SimpleNamespace(usage=None)
        with patch.object(litellm, "completion", return_value=response) as provider:
            with measure_llm_calls(
                False, max_calls=1, max_output_tokens=200
            ) as metrics:
                litellm.completion(
                    model="openai/gpt-4.1-mini", messages=[], max_tokens=900
                )
                with self.assertRaisesRegex(RuntimeError, "budget"):
                    litellm.completion(model="openai/gpt-4.1-mini", messages=[])
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(provider.call_args.kwargs["max_tokens"], 200)
        self.assertEqual(metrics["attempts"], 2)

    def test_invalid_budgets_are_rejected(self):
        from scripts.llm_guard import measure_llm_calls

        for options in ({"max_calls": 0}, {"max_output_tokens": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                with measure_llm_calls(False, **options):
                    self.fail("Invalid budget was accepted")
