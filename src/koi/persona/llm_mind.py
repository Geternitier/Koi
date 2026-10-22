"""LLM 驱动的 Mind。"""

import base64
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field, field_validator

from koi.kernel.llm import Usage
from koi.persona.affect import Appraisal
from koi.persona.mind import (
    AlarmLevel,
    AlarmRequest,
    Consolidation,
    ConsolidationInput,
    EventAppraisal,
    Perception,
    Reply,
    SeenImage,
    ToolCall,
    WakeContext,
    WakeOutcome,
)
from koi.persona.params import MindParams
from koi.persona.profile import PersonaProfile
from koi.persona.prompt import (
    act_prompt,
    consolidate_prompt,
    perceive_prompt,
    refine_prompt,
    system_prompt,
)
from koi.persona.will import RequestAssessment, WillDecision

T = TypeVar("T", bound=BaseModel)
# 这些状态码通常意味着模型不接受图片，而不是网络或鉴权问题
IMAGE_REJECTED_STATUS = frozenset({400, 413, 415, 422})

log = logging.getLogger(__name__)


def _image_rejected(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    return isinstance(status, int) and status in IMAGE_REJECTED_STATUS


class JsonLLM(Protocol):
    async def chat_json(
        self,
        messages: Sequence[Any],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[T, Usage]: ...


class PerceiveOutput(BaseModel):
    appraisals: list[EventAppraisal] = Field(default_factory=list)
    requests: list[RequestAssessment] = Field(default_factory=list)
    recall: list[str] = Field(default_factory=list)


class NotebookOutput(BaseModel):
    notebook: str


class AlarmOutput(BaseModel):
    at: datetime
    reason: str = ""
    level: AlarmLevel = 1

    @field_validator("level", mode="before")
    @classmethod
    def _coerce_level(cls, v: object) -> object:
        """模型偶尔写成字符串或越界，按最接近的档位收。"""
        try:
            return min(3, max(1, int(v)))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return 1


class ActOutput(BaseModel):
    thought: str = ""
    replies: list[Reply] = Field(default_factory=list)
    more: bool = False
    activity: str | None = None
    engaged: bool = False
    feeling: Appraisal | None = None
    alarm: AlarmOutput | None = None
    tools: list[ToolCall] = Field(default_factory=list)


class LLMMind:
    def __init__(
        self,
        llm: JsonLLM,
        profile: PersonaProfile,
        params: MindParams,
        *,
        calm_threshold: float = 0.12,
        redact: Callable[[str], str] | None = None,
        self_images: list[SeenImage] | None = None,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.params = params
        self.calm_threshold = calm_threshold
        self._redact = redact or (lambda s: s)
        self._self_images = list(self_images or [])
        self._system = system_prompt(profile)
        # 模型明确拒绝过图片后，这个进程不再附图，省得每次多一次失败的调用
        self._vision_off = False

    @staticmethod
    def _append_image(parts: list[dict[str, Any]], image: SeenImage) -> None:
        encoded = base64.b64encode(image.data).decode("ascii")
        parts.append({"type": "text", "text": image.caption})
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.media_type};base64,{encoded}"},
            }
        )

    def _messages(self, user: str, images: list[SeenImage] | None = None) -> list[dict[str, Any]]:
        text = self._redact(user)
        content: str | list[dict[str, Any]] = text
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
            for image in images:
                self._append_image(parts, image)
            # 自己的登记图只在她看图时附上，用来对照照片里的人是不是自己
            if self._self_images:
                parts.append(
                    {
                        "type": "text",
                        "text": "下面是你自己的照片，用来对照前面照片里的人是不是你：",
                    }
                )
                for image in self._self_images:
                    self._append_image(parts, image)
            content = parts
        return [
            {"role": "system", "content": self._system},
            {"role": "user", "content": content},
        ]

    def _cost(self, usage: Usage) -> float:
        p = self.params
        return (
            p.energy_per_call
            + usage.completion_tokens / 1000 * p.energy_per_1k_output
            + usage.cache_miss_tokens / 1000 * p.energy_per_1k_uncached_prompt
        )

    async def _act(
        self, ctx: WakeContext, decisions: list[WillDecision]
    ) -> tuple[ActOutput, Usage]:
        # 有图时用视觉模型；没配就和主模型相同
        vision = bool(ctx.images and self.params.vision_model)
        model = self.params.vision_model if vision else self.params.model
        return await self.llm.chat_json(
            self._messages(
                act_prompt(
                    ctx,
                    decisions,
                    self.profile,
                    self.calm_threshold,
                    max_tool_calls=self.params.max_tool_calls_per_round,
                ),
                ctx.images,
            ),
            ActOutput,
            model=model,
            temperature=self.params.act_temperature,
        )

    async def perceive(self, ctx: WakeContext) -> Perception:
        if not ctx.events:
            return Perception()
        out, usage = await self.llm.chat_json(
            self._messages(perceive_prompt(ctx, self.profile, self.calm_threshold)),
            PerceiveOutput,
            model=self.params.model,
            temperature=self.params.perceive_temperature,
        )
        return Perception(
            appraisals=out.appraisals,
            requests=out.requests,
            recall=out.recall,
            energy_cost=self._cost(usage),
        )

    async def act(self, ctx: WakeContext, decisions: list[WillDecision]) -> WakeOutcome:
        effective = ctx
        if self._vision_off and ctx.images:
            effective = ctx.model_copy(update={"images": [], "images_unavailable": True})
        try:
            out, usage = await self._act(effective, decisions)
        except Exception as e:
            if not effective.images or not _image_rejected(e):
                raise
            log.warning("模型不接受图片，这次改成纯文本：%s", e)
            self._vision_off = True
            effective = ctx.model_copy(update={"images": [], "images_unavailable": True})
            out, usage = await self._act(effective, decisions)
        alarms: list[AlarmRequest] = []
        if out.alarm is not None:
            at = out.alarm.at
            if at.tzinfo is None:
                at = at.replace(tzinfo=ctx.snapshot.time.tzinfo)
            if at > ctx.snapshot.time:
                alarms.append(AlarmRequest(at=at, reason=out.alarm.reason, level=out.alarm.level))
        return WakeOutcome(
            energy_cost=self._cost(usage),
            appraisals=[out.feeling] if out.feeling else [],
            engaged=out.engaged,
            alarms=alarms,
            replies=out.replies,
            more=out.more,
            thought=out.thought or None,
            activity=out.activity or None,
            tool_calls=out.tools if ctx.tools else [],
        )

    async def consolidate(self, inp: ConsolidationInput) -> Consolidation:
        out, _ = await self.llm.chat_json(
            self._messages(consolidate_prompt(inp, self.profile)),
            Consolidation,
            model=self.params.model,
            temperature=self.params.consolidate_temperature,
        )
        return out

    async def refine_notebook(self, notebook: str, notes: list[str]) -> str:
        out, _ = await self.llm.chat_json(
            self._messages(refine_prompt(notebook, notes)),
            NotebookOutput,
            model=self.params.model,
            temperature=self.params.perceive_temperature,
        )
        return out.notebook
