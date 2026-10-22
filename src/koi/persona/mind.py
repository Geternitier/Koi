"""认知接口。每次醒来分两个阶段：

1. perceive：评估新事件（产出离散评估标签）、识别主人的请求、给出回忆线索；
2. 核心据此更新心情、对请求做出意愿决定、激活相关日记；
3. act：在决定的约束下思考并表达（回复、自主活动、闹钟、调用工具）。
   act 请求了工具时，核心执行后把结果放进 tool_results，再调用一次 act，直到不再调用工具。
   act 认为还有信息没说完（more）时，核心把已说的话放进 said，再调用一次 act，
   把剩下的内容拆成后面几条消息；轮数、精力和心情由核心封顶。

没有新事件时（如自主醒来）跳过 perceive。入睡时由核心收集当天经历，
交给 consolidate 写日记、提炼笔记、更新对打过交道的人的印象；写入与激活规则都由核心负责。
"""

from datetime import date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from koi.kernel.bus import Event
from koi.persona.affect import Appraisal
from koi.persona.journal import JournalEntry
from koi.persona.memory import Diary, Importance, MemoryView
from koi.persona.people import PersonBrief, PersonUpdate
from koi.persona.state import Snapshot, WakeReason
from koi.persona.will import RequestAssessment, WillDecision, WillEntry


class EventAppraisal(Appraisal):
    event: int
    # 这件事让她对发消息的人更亲近还是更疏远。转述坏消息不等于惹她不快，所以和 valence 分开
    regard: Literal["warmer", "neutral", "colder"] | None = None

    @property
    def toward_sender(self) -> Literal["positive", "neutral", "negative"]:
        match self.regard:
            case None:
                return self.valence
            case "warmer":
                return "positive"
            case "colder":
                return "negative"
            case _:
                return "neutral"


class Perception(BaseModel):
    appraisals: list[EventAppraisal] = Field(default_factory=list)
    requests: list[RequestAssessment] = Field(default_factory=list)
    # 用于检索过去日记的关键词
    recall: list[str] = Field(default_factory=list)
    energy_cost: float = 0.0


class Reply(BaseModel):
    # 回复第几个新事件（从 1 开始）；None 表示主动找主人
    event: int | None = None
    text: str


class AlarmRequest(BaseModel):
    at: datetime
    reason: str


class ToolSpec(BaseModel):
    # 插件 ID.能力名
    name: str
    description: str = ""
    parameters: dict[str, str] = Field(default_factory=dict)
    energy_cost: float = 0.0


class ToolCall(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    # 已截断的结果文本
    result: str


class WakeContext(BaseModel):
    reason: WakeReason
    events: list[Event]
    snapshot: Snapshot
    journal: list[JournalEntry] = Field(default_factory=list)
    open_requests: list[WillEntry] = Field(default_factory=list)
    proactive_allowed: bool = False
    memory: MemoryView = Field(default_factory=MemoryView)
    # 只有在处理主人交办的事时才有工具可用
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    # 用工具的过程中已经发出去的话
    said: list[str] = Field(default_factory=list)
    # 每个新事件来自谁（人物档案 ID，与 events 对齐；认不出是谁时为 None）
    event_people: list[str | None] = Field(default_factory=list)
    # 这次涉及的人
    people: list[PersonBrief] = Field(default_factory=list)


class WakeOutcome(BaseModel):
    energy_cost: float = 0.0
    # 对自己所做之事的感受
    appraisals: list[Appraisal] = Field(default_factory=list)
    # 自主醒来时是否找到了想做的事，决定退避
    engaged: bool = False
    alarms: list[AlarmRequest] = Field(default_factory=list)
    replies: list[Reply] = Field(default_factory=list)
    # 这一拍发完后还要接着说，把没说完的信息放到后面几条里
    more: bool = False
    thought: str | None = None
    activity: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ConsolidationInput(BaseModel):
    day: date
    entries: list[JournalEntry]
    # 当天已经写过的日记（比如午睡时写过），需要整合成一篇
    existing: Diary | None = None
    notebook: str = ""
    notes: list[str] = Field(default_factory=list)
    mood_label: str = ""
    can_mark_important: bool = True
    # 这一天打过交道的人，可以更新对他们的印象
    people: list[PersonBrief] = Field(default_factory=list)


class DiaryDraft(BaseModel):
    text: str
    summary: str = ""
    importance: Importance = "normal"
    people: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Consolidation(BaseModel):
    diary: DiaryDraft | None = None
    notes: list[str] = Field(default_factory=list)
    people: list[PersonUpdate] = Field(default_factory=list)


class Mind(Protocol):
    async def perceive(self, ctx: WakeContext) -> Perception: ...

    async def act(self, ctx: WakeContext, decisions: list[WillDecision]) -> WakeOutcome: ...

    async def consolidate(self, inp: ConsolidationInput) -> Consolidation:
        """记忆巩固：写日记、提炼实践经验。"""
        ...

    async def refine_notebook(self, notebook: str, notes: list[str]) -> str:
        """把待整理的笔记合并进笔记本，返回新的笔记本全文。"""
        ...
