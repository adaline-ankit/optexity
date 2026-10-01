# Audit evidence

Synthetic RoboForm and public Books examples only. No credentials, full conversations, or private browser logs are committed.

- `roboform.trace.jsonl`: actual LLM-driven tool execution, schema v2.
- `books.trace.jsonl`: actual browser-use tool execution selected by the scripted live-DOM driver; not a paid autonomous Books run.
- Baseline/replay JSON receipts: engine outcome, elapsed time, current LiteLLM boundary counters. Local `/tmp` paths identify original logs and may expire.
- `broken-selector.json`: expected failed replay with zero model attempts.
- `platform-status.json`: separately verified hosted Tasks receipt and outstanding setup distinctions.
- `final-review.json`: 2 October submission hardening, 34 focused tests, and fresh scripted Books capture/native replay. Required Recorder onboarding remains incomplete.

Recompile using commands in TAKEHOME_NOTES.md. Generated `.provenance.json` records hashes and retains/drops. One timing pair does not establish general performance.
