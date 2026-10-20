"""LLM 网关：统一调用、结构化输出校验、token 与缓存命中计量。"""

from collections.abc import Sequence
from typing import Any, TypeVar

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, SecretStr, ValidationError

from koi.kernel.secrets import DEEPSEEK_API_KEY, SecretStore

T = TypeVar("T", bound=BaseModel)


class LLMConfig(BaseModel):
    base_url: str = "https://api.deepseek.com"
    api_key: SecretStr
    model: str = "deepseek-chat"
    timeout_s: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_secrets(
        cls, store: SecretStore, *, secret: str = DEEPSEEK_API_KEY, **overrides: Any
    ) -> "LLMConfig":
        return cls(api_key=store.require(secret), **overrides)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
        )


class UsageMeter:
    def __init__(self) -> None:
        self.total = Usage()
        self.calls = 0

    def record(self, usage: Usage) -> None:
        self.total = self.total + usage
        self.calls += 1

    @property
    def cache_hit_rate(self) -> float | None:
        prompt = self.total.cache_hit_tokens + self.total.cache_miss_tokens
        return self.total.cache_hit_tokens / prompt if prompt else None


class ChatResult(BaseModel):
    text: str
    usage: Usage
    model: str


class LLMError(RuntimeError):
    pass


def parse_usage(raw: Any) -> Usage:
    """兼容 DeepSeek（prompt_cache_hit_tokens）与 OpenAI（prompt_tokens_details）两种格式。"""
    if raw is None:
        return Usage()
    prompt = getattr(raw, "prompt_tokens", 0) or 0
    hit = getattr(raw, "prompt_cache_hit_tokens", None)
    miss = getattr(raw, "prompt_cache_miss_tokens", None)
    if hit is None:
        details = getattr(raw, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", 0) or 0
        miss = prompt - hit
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=getattr(raw, "completion_tokens", 0) or 0,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss if miss is not None else prompt - hit,
    )


class LLMGateway:
    def __init__(self, config: LLMConfig, *, client: Any = None) -> None:
        self.config = config
        self.client = client or AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key.get_secret_value(),
            timeout=config.timeout_s,
            max_retries=config.max_retries,
        )
        self.meter = UsageMeter()

    async def chat(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await self.client.chat.completions.create(
            model=model or self.config.model, messages=list(messages), **kwargs
        )
        usage = parse_usage(resp.usage)
        self.meter.record(usage)
        return ChatResult(text=resp.choices[0].message.content or "", usage=usage, model=resp.model)

    async def chat_json(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[T, Usage]:
        """JSON 模式调用并按 schema 校验；校验失败时带着错误信息重试一次。

        DeepSeek 的 JSON 模式要求 prompt 中出现 "json" 字样，由调用方保证。
        """
        msgs = list(messages)
        total = Usage()
        for attempt in range(2):
            result = await self.chat(
                msgs, model=model, temperature=temperature, max_tokens=max_tokens, json_mode=True
            )
            total = total + result.usage
            try:
                return schema.model_validate_json(result.text), total
            except ValidationError as e:
                if attempt == 1:
                    raise LLMError(f"结构化输出校验失败：{e}") from e
                msgs.append({"role": "assistant", "content": result.text})
                msgs.append(
                    {
                        "role": "user",
                        "content": f"上面的 json 不符合要求：{e}\n请只输出修正后的 json。",
                    }
                )
        raise AssertionError("unreachable")
