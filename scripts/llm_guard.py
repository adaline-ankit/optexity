"""Bounded measurement at the existing LiteLLM execution boundary."""

from contextlib import contextmanager


@contextmanager
def measure_llm_calls(
    forbid: bool,
    *,
    max_calls: int | None = None,
    max_output_tokens: int | None = None,
):
    """Measure the engine's LiteLLM boundary, including failed attempts.

    This is a local harness, not a network firewall. Both current Optexity model
    adapters route calls here. New SDK integrations need their own guard.
    """
    if max_calls is not None and max_calls < 1:
        raise ValueError("max_calls must be positive")
    if max_output_tokens is not None and max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")
    from unittest.mock import patch

    import litellm

    metrics = {"attempts": 0, "prompt_tokens": 0, "completion_tokens": 0}
    original_sync, original_async = litellm.completion, litellm.acompletion

    def begin(kwargs):
        metrics["attempts"] += 1
        if forbid:
            raise RuntimeError("LLM call forbidden during deterministic replay")
        if max_calls is not None and metrics["attempts"] > max_calls:
            raise RuntimeError("Model call budget exhausted")
        if max_output_tokens is not None:
            kwargs["max_tokens"] = min(
                kwargs.get("max_tokens") or max_output_tokens, max_output_tokens
            )
        if max_calls is not None:
            # Internal retries/fallbacks would bypass this call budget.
            kwargs.update(num_retries=0, fallbacks=[], timeout=45)

    def finish(response):
        usage = getattr(response, "usage", None)
        metrics["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        metrics["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        return response

    def sync(*args, **kwargs):
        begin(kwargs)
        return finish(original_sync(*args, **kwargs))

    async def asynchronous(*args, **kwargs):
        begin(kwargs)
        return finish(await original_async(*args, **kwargs))

    with (
        patch.object(litellm, "completion", sync),
        patch.object(litellm, "acompletion", asynchronous),
    ):
        yield metrics
