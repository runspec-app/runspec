import anthropic

from ..adapter import ChatResponse, ModelAdapter, ToolCall

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_SYSTEM = (
    "You are a helpful assistant with access to tools running on remote Linux hosts. "
    "Use tools when they help answer the user's request. "
    "When you call a tool, briefly explain what you're doing before the result."
)


class AnthropicAdapter(ModelAdapter):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system: str = DEFAULT_SYSTEM,
        api_key: str | None = None,
    ):
        # api_key=None falls back to ANTHROPIC_API_KEY env var
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.system = system

    async def chat(self, messages: list[dict], tools: list[dict]) -> ChatResponse:
        kwargs: dict = dict(
            model=self.model,
            max_tokens=4096,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools

        response = await self.client.messages.create(**kwargs)

        text = next(
            (block.text for block in response.content if hasattr(block, "text")),
            None,
        )
        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in response.content
            if block.type == "tool_use"
        ]

        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            _raw=response,
        )

    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict]:
        return [
            {"role": "assistant", "content": response._raw.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    }
                    for tc, result in results
                ],
            },
        ]
