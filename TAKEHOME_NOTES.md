# Deterministic action-cache take-home

Run browser-use once, record actual tool execution evidence, compile a narrow native Optexity recipe, then replay with explicit outcome assertions and no model fallback.

## Scope

- Input/click tools attach their resolved DOM target after successful event dispatch. The recorder writes task-local schema-v2 JSONL.
- Compiler checks schema, objective hash, run identity, action order, completion, errors, execution evidence, redaction, and target scope.
- Input/click become command-only native nodes. Waits remain native sleeps; terminal `done` is removed. Other actions are rejected.
- Compilation replaces one top-level agentic node and preserves surrounding nodes, including outcome checks.
- Each trace row must contain one matching action with supported parameters. Unknown semantics are rejected, not silently discarded; for example, an input containing `press_enter` cannot be compiled by this prototype.
- `strict_replay` prevents both normal prompt fallback and the outer LLM error classifier.
- Literal LLM-assisted compilation and iterative optimization bonuses are not implemented.

## Run locally

Install the personal forks in the brief's order, in one Python 3.11+ virtualenv. Use the compatible Optexity branch of browser-use (distribution `optexity-browser-use`):

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e optexity
.venv/bin/pip install -e browser-use
.venv/bin/python -c 'import optexity, browser_use; print(optexity.__file__); print(browser_use.__file__)'
cd optexity
```

With `OPENAI_API_KEY` already in the environment, run the agentic baseline:

```bash
../.venv/bin/python scripts/run_local_automation.py test_automation.json --model openai/gpt-4.1-mini
```

Compile and replay committed synthetic evidence without an API key:

```bash
../.venv/bin/python scripts/compile_cached_automation.py --baseline test_automation.json --trace evidence/roboform.trace.jsonl --out test_automation_cached.json
env -u OPENAI_API_KEY ../.venv/bin/python scripts/run_local_automation.py test_automation_cached.json --forbid-llm
../.venv/bin/python scripts/compile_cached_automation.py --baseline test_automation_books.json --trace evidence/books.trace.jsonl --out test_automation_books_cached.json
env -u OPENAI_API_KEY ../.venv/bin/python scripts/run_local_automation.py test_automation_books_cached.json --forbid-llm
```

`--offline` defaults true and skips Optexity uploads/callbacks; website access and baseline model calls remain enabled. Each local run writes `run_result.json` with timing, outcome and LiteLLM boundary counters. `--forbid-llm` blocks and counts a model attempt even if a downstream caller catches the error.

Books capture can be reproduced without model spend:

```bash
../.venv/bin/python -m scripts.record_books_demo
```

This driver selects indices from the **live DOM**, executes actual browser-use clicks, verifies category/product pages, and records the same tool evidence. It tests capture/compile/replay, not autonomous Books planning. `test_automation_books.json` is available for an additional paid agent-learning run; no such run is claimed in the committed evidence.

## Actual platform worker

Use a real Optexity API credential, distinct from a model-provider credential. The brief's local-JSON override is opt-in:

```bash
OPTEXITY_LOCAL_AUTOMATION="$PWD/test_automation_cached.json" ../.venv/bin/optexity inference --host 127.0.0.1 --port 9000 --child_process_id 0
```

Allocate the existing personal workflow using `POST /inference`; this runs through the actual control plane and uploads task evidence. The audit verified task `bd6f2759-71ce-492d-a187-6728f510b1d3` as **Local / success** in the dashboard. A JSON-authored workflow is not an extension recording. Analytics with Both environments shows one successful task, 100% success, and 24-second median. Local-only filter showed zero; cause unconfirmed. Chrome-extension recording remains unverified because Chrome access was unavailable in this session. See `evidence/platform-status.json`.

The hosted schema currently strips the new `strict_replay` field when saving JSON. Use the local fork with the override or the local runner for strict-policy evidence until the server schema is updated.

## Evidence

| Run | Outcome | Elapsed | LiteLLM boundary attempts | Prompt/completion tokens |
|---|---|---:|---:|---:|
| RoboForm learning | Four values asserted | 20.826 s | 3 | 22,936 / 765 |
| RoboForm replay | Same assertions | 18.440 s | 0 | 0 / 0 |
| Books replay | URL/title/product section asserted | 8.474 s | 0 | 0 / 0 |
| Broken selector | Expected failure | 21.529 s | 0 | 0 / 0 |

One observed RoboForm pair: 11.46% lower elapsed time. Timings include browser setup, assertions, logging, and cleanup. This is not a statistical performance claim; persistent browser profiles, startup, network and ordering can affect it. Boundary usage is not a billing invoice. Books has no claimed LLM-baseline comparison.

Traces and receipts are in `evidence/`. Each generated JSON has a `.provenance.json` containing input hashes and retain/drop decisions. Checksums aid reproduction; they are not signatures.

## Validation

```bash
../.venv/bin/python -m unittest discover -s tests -p test_cached_automation.py -v
../.venv/bin/python -m unittest discover -s tests -p test_replay_browser.py -v
../.venv/bin/python -m unittest discover -s ../browser-use/tests/takehome -v
```

25 compiler/policy tests, 3 real-Chromium selector/oracle tests, and 6 recorder tests passed in the final submission review on 2 October 2026 (34 total). Added checks cover unsupported input parameters, multiple actions in one row, boolean click counts, preservation of click options, invalid waits, and unchanged metadata when tracing is disabled. Live RoboForm learning/replay, Books capture/replay, broken-selector failure, and actual platform task previously passed their stated checks. The review regenerated both cached recipes and confirmed unchanged JSON output. Repository-pinned Black/isort and Ruff checks, Python compilation, schema validation and `git diff --check` passed for the relevant change surfaces. These are focused checks, not a claim that the entire upstream test suites were run. The GitHub Sourcery check is automated review, not test CI.

## Reviewer reading order

1. `test_automation.json` and `test_automation_cached.json`: objective, native output, and preserved oracle.
2. Companion recorder and tool hooks: where target evidence originates.
3. `scripts/compile_cached_automation.py`: validation and translation.
4. `InteractionAction.strict_replay` and `run_interaction.py`: reuse existing input/click handlers and stop model recovery on strict failures.
5. `tests/` and `evidence/`: positive/negative checks and stated limits.

The implementation adds no production dependencies or parallel replay engine. Small functions handle trace capture and compilation; existing Pydantic schemas, node dispatch, browser handlers, and exception types remain the execution path.

## Important boundaries

- Final assertions establish demo outcomes independently of model `done`. Existing `assert_locator_node` only stores a boolean; it does not by itself fail a task.
- Stable attributes are heuristics; Playwright rejects ambiguity, but uniqueness alone does not prove semantic identity.
- Frame/shadow targets, general nested workflow compilation, parameter generalization, secure-parameter substitution, automatic cache lookup/versioning and adaptive repair are outside current support.
- Redaction applies to this JSONL format only; upstream logs/screenshots/conversations have separate policies. Missing/sensitive targets are blocked from plaintext replay.
- ContextVars isolate configuration, not shared files or browser ownership. Use unique per-run directories. Mixed run IDs are rejected.
- Trusted automation files can contain Python/locator expressions executed by the existing engine. The compiler emits quoted fixed templates; the engine is not a sandbox for arbitrary files.
- No semantic action pruning is claimed. Real actions and waits are retained because a single trace cannot prove their side effects irrelevant.
- Ordering checks do not establish trace completeness: an ordered trace with a missing action can still compile. The task hash binds objective text, not website origin, and compilation does not require a baseline oracle-success receipt. Current manual promotion must inspect baseline and replay results; automatic promotion needs a finalized manifest, environment binding, and oracle receipt.
- Compiler click-option tests validate translation of supplied trace parameters. The current browser-use recorder emits the supported click index only; end-to-end right-click/double-click capture is not claimed.
