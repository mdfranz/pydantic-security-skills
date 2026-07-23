"""Provider retry and oversized tool-output protections for runner agents."""

import math
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import anyio
from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai_harness.overflowing_tool_output import (
    Band,
    LocalFileStore,
    OverflowingToolOutput,
    Spill,
    Truncate,
)

TOOL_OUTPUT_OVERFLOW_CHARS = 10_000
TOOL_OUTPUT_PREVIEW_CHARS = 1_000
TOOL_OUTPUT_FALLBACK_CHARS = 4_000

# query_events/aggregate_events/describe_events/query_sql already bound their own return size
# (MAX_PAGE_ROWS/MAX_GROUP_ROWS/MAX_SCHEMA_COLUMNS/MAX_SQL_ROWS, has_more/truncated pagination)
# -- they are dispatched as *nested* tool calls from inside run_code, and this capability's
# after_tool_execute hook fires on nested calls too (the inner ToolManager shares the outer
# one's root_capability). Left unexempted, a wide page (e.g. columns=["tls"] rows full of
# cert/SNI/JA4 strings, or a `query_sql` projection over the same struct) can cross the char
# threshold well under the row cap, and the spill silently replaces the tool's documented dict
# return with a preview *string* -- breaking `page["rows"]`/`page["has_more"]` for the
# sandboxed code with no signal beyond an AttributeError, forcing the model to rediscover a
# safe `limit` by trial and error instead of trusting the tool's own contract.
DATA_TOOL_NAMES = frozenset({"query_events", "aggregate_events", "describe_events", "query_sql"})

RateLimitCallback = Callable[[ModelHTTPError, int, float], None]


def _rate_limit_delay_seconds(exc: ModelHTTPError) -> float:
    """Read OpenRouter's optional retry delay, with a safe bounded fallback."""
    candidate: object = 1
    if isinstance(exc.body, Mapping):
        metadata = exc.body.get("metadata")
        if isinstance(metadata, Mapping):
            candidate = metadata.get("retry_after_seconds", 1)

    try:
        delay = float(candidate) if isinstance(candidate, (str, int, float)) else 1.0
    except (TypeError, ValueError):
        delay = 1.0
    if not math.isfinite(delay):
        delay = 1.0
    return min(max(delay, 0.1), 30.0)


async def retry_rate_limited_request(
    request_context: Any,
    handler: Callable[[Any], Awaitable[Any]],
    *,
    on_retry: RateLimitCallback | None = None,
) -> Any:
    """Retry one model request once when the provider returns HTTP 429."""
    for attempt in range(2):
        try:
            return await handler(request_context)
        except ModelHTTPError as exc:
            if exc.status_code != 429 or attempt == 1:
                raise

            delay = _rate_limit_delay_seconds(exc)
            if on_retry is not None:
                on_retry(exc, attempt + 1, delay)
            await anyio.sleep(delay)

    raise AssertionError("unreachable")


def build_provider_hooks(*, on_retry: RateLimitCallback | None = None) -> Hooks:
    """Build per-agent hooks that retry a rate-limited provider request once."""
    provider_hooks = Hooks()

    @provider_hooks.on.model_request
    async def retry_provider_429(ctx, *, request_context, handler):
        return await retry_rate_limited_request(request_context, handler, on_retry=on_retry)

    return provider_hooks


def build_overflow_capability(logs_dir: Path, task_id: str) -> OverflowingToolOutput:
    """Spill large tool returns outside model history in an owner-only task store.

    Excludes `DATA_TOOL_NAMES`: those tools already bound their own return size and would
    otherwise have their documented dict contract silently replaced by a spill-preview
    string when a wide page crosses this capability's char threshold. See `DATA_TOOL_NAMES`.
    """
    return OverflowingToolOutput(
        bands=[
            Band(
                over=TOOL_OUTPUT_OVERFLOW_CHARS,
                action=Spill(
                    preview_chars=TOOL_OUTPUT_PREVIEW_CHARS,
                    then=Truncate(max_chars=TOOL_OUTPUT_FALLBACK_CHARS),
                ),
            )
        ],
        store=LocalFileStore(base_dir=logs_dir / "overflow" / task_id),
        tool_filter=lambda ctx, tool_def: tool_def.name not in DATA_TOOL_NAMES,
    )
