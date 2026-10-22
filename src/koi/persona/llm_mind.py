"""LLM 驱动的 Mind。"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field

from koi.kernel.llm import Usage
from koi.persona.affect import Appraisal
from koi.persona.mind import (
    AlarmRequest,
    Consolidation,
    ConsolidationInput,
    EventAppraisal,
    Perception,
    Reply,
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
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.params = params
        self.calm_threshold = calm_threshold
        self._redact = redact or (lambda s: s)
        self._system = system_prompt(profile)

    def _messages(self, user: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._system},
            {"role": "user", "content": self._redact(user)},
        ]

    def _cost(self, usage: Usage) -> float:
        p = self.params
        return (
            p.energy_per_call
            + usage.completion_tokens / 1000 * p.energy_per_1k_output
            + usage.cache_miss_tokens / 1000 * p.energy_per_1k_uncached_prompt
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
        out, usage = await self.llm.chat_json(
            self._messages(act_prompt(ctx, decisions, self.profile, self.calm_threshold)),
            ActOutput,
            model=self.params.model,
            temperature=self.params.act_temperature,
        )
        alarms: list[AlarmRequest] = []
        if out.alarm is not None:
            at = out.alarm.at
            if at.tzinfo is None:
                at = at.replace(tzinfo=ctx.snapshot.time.tzinfo)
            if at > ctx.snapshot.time:
                alarms.append(AlarmRequest(at=at, reason=out.alarm.reason))
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
