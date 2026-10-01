# Optexity take-home notes

## What changed

This solution turns one successful `browser-use` agent run into a deterministic Optexity automation.

Flow:

1. Run the normal `agentic_task` once.
2. Record each action that actually executed in `browser-use`.
3. Store the resolved target element facts beside the Optexity task logs.
4. Compile supported actions into deterministic Optexity nodes with strict locators.
5. Replay cached automation without an LLM call.

The cache is opt-in. Optexity enables it through a task-local trace context around `agent.run(...)`, so concurrent runs do not share a process-global trace path. The recorder still keeps the environment-variable fallback for manual debugging.

## Files

Companion `browser-use` fork:

- `browser_use/agent/service.py` records each executed tool action after it completes.
- `browser_use/agent/optexity_step_cache.py` writes `browser_use_trace.jsonl` into the configured trace directory.

This repo:

- `optexity/inference/core/interaction/handle_agentic_task.py` sets the trace directory for each agentic task step.
- `scripts/run_local_automation.py` runs an automation JSON locally without server uploads by default.
- `scripts/compile_cached_automation.py` converts a trace JSONL file into deterministic Optexity automation nodes.
- `test_automation.json` is the baseline agentic RoboForm task.
- `test_automation_cached.json` is the generated deterministic replay for RoboForm.
- `test_automation_books_cached.json` is a second deterministic multi-page example.

## Why record executed actions, not model plans

A model plan is intent. It can include actions that never run because the agent stops early after an error, navigation, or `done` action.

The cache should represent browser truth: action name, action params, target index, resolved target element, result, and elapsed time. That makes replay based on what actually worked.

## Locator strategy

The compiler prefers stable element facts in this order:

1. `id`
2. `data-testid`
3. `name`
4. `aria-label`
5. `placeholder`
6. XPath fallback

Generated locator commands use XPath predicates with quoted literals for recorded attributes. That avoids CSS selector bugs when an id, name, or test id contains `.`, `#`, `[`, `]`, quotes, or spaces.

For replay, generated nodes use `skip_prompt: true` and `assert_locator_presence: true`. If the cached locator breaks, replay fails loudly instead of silently falling back to an LLM and hiding the cache failure.

## Local commands

Install both forks in one virtualenv, with `browser-use` first so Optexity imports the local companion package:

```bash
cd /Users/ankit/Desktop/Adaline/optexity-takehome
python3.11 -m venv .venv
.venv/bin/pip install -e browser-use
.venv/bin/pip install -e optexity
```

Run baseline agentic automation with a cheap model:

```bash
cd /Users/ankit/Desktop/Adaline/optexity-takehome/optexity
OPENAI_API_KEY="$OPENAI_API_KEY" ../.venv/bin/python scripts/run_local_automation.py \
  test_automation.json \
  --output-dir /tmp/optexity-local-baseline \
  --model openai/gpt-4.1-mini
```

Compile the trace into cached automation:

```bash
../.venv/bin/python scripts/compile_cached_automation.py \
  --baseline test_automation.json \
  --trace /tmp/optexity-local-baseline/<task_id>/logs/step_0/step_cache/browser_use_trace.jsonl \
  --out test_automation_cached.json
```

Replay cached automation without an LLM call:

```bash
../.venv/bin/python scripts/run_local_automation.py \
  test_automation_cached.json \
  --output-dir /tmp/optexity-local-cached
```

## Evidence from local runs

RoboForm baseline agentic run:

- status: `success`
- model: `openai/gpt-4.1-mini`
- elapsed: `23.353s` on the fresh hardened run
- trace rows: 5 actions, including 4 form inputs plus `done`; no redacted fields for this non-sensitive task

RoboForm cached replay:

- status: `success`
- elapsed: `9.316s` on the fresh trace-derived replay after hardening
- no LLM needed for replay

Books cached replay:

- status: `success`
- elapsed: `8.002s`
- exercised click + navigation + assertion

Validation run:

- `compileall` passed for changed Python files in both repos.
- automation JSON schema validation passed for all three test automation files.
- `git diff --check` passed in both repos.
- secret scan found no real OpenAI key in either repo.
- synthetic trace test verified task-local trace nesting and sensitive input redaction.
- synthetic compiler test verified special-character locator generation and secret-block failure.

## Tradeoffs and next steps

This is intentionally a narrow cache, not a broad self-healing system. It supports actions that have enough browser facts to replay safely. Unsupported or ambiguous actions fail during compile or replay. Sensitive input fields are redacted in traces and blocked by the compiler unless they are converted to explicit secure-parameter placeholders.

The next production step would be to persist traces by task/site signature, add cache invalidation rules, and add richer compilers for select, scroll, extraction, uploads, and assertions. I would keep strict replay as the default and make LLM fallback explicit, measured, and visible in logs.
