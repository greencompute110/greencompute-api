"""Checks on an inference request before anything reaches a model.

A reviewer (2026-11-17) took both shared models down with three requests:
`"messages": []`, an absurd `max_tokens`, a tool call the model wasn't
started for. vLLM answered each with a 400, the node turned it into a 502,
and three of those marked the shared deployment FAILED. The node and the
gateway now pass a model's 4xx back without counting it; this answers the
requests that can never be valid at once, with a reason, without a model
ever seeing them.
"""
from __future__ import annotations

from numbers import Real
from typing import Any

from greencompute_protocol import ChatCompletionRequest

#: max_tokens / max_completion_tokens when the model's context length isn't
#: known here (it is set on catalog models): the longest context any model
#: served here has, and the billing ceiling (_BILLED_COMPLETION_TOKENS_CEILING).
#: vLLM still refuses, with a 400 the caller now sees, a value over the
#: model's own context.
DEFAULT_MAX_TOKENS_CEILING = 131_072
#: Completions per request. Each is generated, and billed, separately.
MAX_N = 8
#: The flag vLLM needs for `tool_choice: "auto"` (and `tools` without a
#: tool_choice, which means "auto"); without it vLLM refuses the request.
AUTO_TOOL_CHOICE_FLAG = "--enable-auto-tool-choice"


class InvalidInferenceRequestError(ValueError):
    """A request no model here can serve as sent. 422 (`validation_error`,
    `detail` in FastAPI's shape) for a malformed field; 400
    (`invalid_request`) for something this model doesn't support."""

    def __init__(self, field: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.message = message
        self.status = status

    def detail(self) -> list[dict[str, Any]] | dict[str, Any]:
        if self.status == 422:
            return [{"loc": ["body", *self.field.split(".")], "msg": self.message, "type": "value_error"}]
        return {"message": self.message, "field": self.field}


def _whole_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def check_chat_request(
    request: ChatCompletionRequest,
    *,
    context_limit: int | None = None,
    auto_tool_choice: bool | None = None,
) -> None:
    """Raise InvalidInferenceRequestError for a request that can't succeed.

    `context_limit` is the model's context length when known; `auto_tool_choice`
    whether the model was started able to choose tools itself (None: unknown,
    for example an external server, so `tools` pass through to it)."""
    extra = request.model_extra or {}
    if not request.messages:
        raise InvalidInferenceRequestError("messages", "messages must hold at least one message")
    for i, message in enumerate(request.messages):
        if not isinstance(message.role, str) or not message.role.strip():
            raise InvalidInferenceRequestError(f"messages.{i}.role", "every message needs a role")

    ceiling = context_limit or DEFAULT_MAX_TOKENS_CEILING
    why = "the model's context length" if context_limit else "the most any model here accepts"
    for field, value in (("max_tokens", request.max_tokens), ("max_completion_tokens", extra.get("max_completion_tokens"))):
        if value is None:
            continue
        if not _whole_number(value) or not 1 <= value <= ceiling:
            raise InvalidInferenceRequestError(
                field, f"{field} must be a whole number from 1 to {ceiling} ({why}); it was {value!r}")

    n = extra.get("n")
    if n is not None and (not _whole_number(n) or not 1 <= n <= MAX_N):
        raise InvalidInferenceRequestError("n", f"n must be a whole number from 1 to {MAX_N}; it was {n!r}")
    if request.temperature is not None and not 0 <= request.temperature <= 2:
        raise InvalidInferenceRequestError("temperature", f"temperature must be from 0 to 2; it was {request.temperature}")
    top_p = extra.get("top_p")
    if top_p is not None and (not _number(top_p) or not 0 < top_p <= 1):
        raise InvalidInferenceRequestError("top_p", f"top_p must be above 0 and at most 1; it was {top_p!r}")
    for field in ("presence_penalty", "frequency_penalty"):
        value = extra.get(field)
        if value is not None and (not _number(value) or not -2 <= value <= 2):
            raise InvalidInferenceRequestError(field, f"{field} must be from -2 to 2; it was {value!r}")

    tools = extra.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise InvalidInferenceRequestError("tools", "tools must be a list of tool definitions")
    tool_choice = extra.get("tool_choice")
    if tools and tool_choice in (None, "auto") and auto_tool_choice is False:
        raise InvalidInferenceRequestError(
            "tool_choice",
            "this model wasn't started with automatic tool choice, so it can't decide by itself when to call a "
            'tool (`tool_choice: "auto"`, the default when `tools` are sent). Name the function in `tool_choice` '
            '({"type": "function", "function": {"name": "..."}}), send `tool_choice: "required"`, or leave '
            "`tools` out",
            status=400,
        )


def model_context_limit(workload: Any) -> int | None:
    """The context length a workload's model is served with, when it was set
    (catalog models carry it); None when the node decides it."""
    runtime = getattr(workload, "runtime", None)
    value = getattr(runtime, "max_model_len", None)
    return value if _whole_number(value) and value > 0 else None


def model_auto_tool_choice(workload: Any) -> bool:
    """Whether a node-served model was started able to choose tools itself:
    only when its engine args include vLLM's auto-tool-choice flag (the node
    adds none of its own)."""
    runtime = getattr(workload, "runtime", None)
    args = getattr(runtime, "extra_engine_args", None) or []
    return any(str(arg).split("=", 1)[0] == AUTO_TOOL_CHOICE_FLAG for arg in args)
