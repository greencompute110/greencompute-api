"""Tool calls and reasoning must survive the gateway's response model.

ChatCompletionMessage declared only role+content with no extra="allow", so
pydantic silently deleted `tool_calls` on the way through. vLLM was configured
correctly and DID emit them -- clients saw finish_reason="tool_calls" with no
tool_calls array, which stalls every agent loop (Cursor, aider, LangChain).
The sibling ChatCompletionChoice has extra="allow" and a comment claiming
tool_calls passes through; that only ever covered choice-level keys.
"""
from greencompute_protocol import ChatCompletionResponse

TOOL_CALL = {
    "id": "call_abc",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
}


def _resp(message: dict, finish: str = "tool_calls") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model="kimi-k3",
        choices=[{"index": 0, "message": message, "finish_reason": finish}],
    )


def test_tool_calls_survive_the_round_trip():
    r = _resp({"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]})
    dumped = r.model_dump(mode="json")
    assert dumped["choices"][0]["message"]["tool_calls"] == [TOOL_CALL]


def test_pure_tool_call_with_null_content_validates():
    """OpenAI sets content=null when the model only calls a tool. content was
    declared required, so those responses failed validation entirely."""
    r = _resp({"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]})
    assert r.choices[0].message.content is None


def test_reasoning_is_not_dropped():
    """K3 returns chain-of-thought beside the answer and the published API guide
    tells callers to read message.reasoning -- which never arrived."""
    r = _resp({"role": "assistant", "content": "Paris", "reasoning": "thinking..."}, finish="stop")
    assert r.model_dump(mode="json")["choices"][0]["message"]["reasoning"] == "thinking..."
    r2 = _resp({"role": "assistant", "content": "Paris", "reasoning_content": "thinking..."}, finish="stop")
    assert r2.model_dump(mode="json")["choices"][0]["message"]["reasoning_content"] == "thinking..."


def test_unknown_upstream_fields_ride_through():
    """So a new vLLM field is never silently deleted again."""
    r = _resp({"role": "assistant", "content": "hi", "some_future_field": 42}, finish="stop")
    assert r.model_dump(mode="json")["choices"][0]["message"]["some_future_field"] == 42


def test_plain_text_responses_are_unchanged():
    """The common path must keep working exactly as before."""
    r = _resp({"role": "assistant", "content": "hello"}, finish="stop")
    msg = r.model_dump(mode="json")["choices"][0]["message"]
    assert msg["content"] == "hello"
    assert msg["role"] == "assistant"


def test_multimodal_content_blocks_still_validate():
    """Qwen2-VL sends content as a list of blocks; making content optional must
    not break that form."""
    blocks = [{"type": "text", "text": "what is this?"}]
    r = _resp({"role": "assistant", "content": blocks}, finish="stop")
    assert r.choices[0].message.content is not None
