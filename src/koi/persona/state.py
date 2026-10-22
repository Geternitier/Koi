"""人格体运行状态。只能由核心自身写入。"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from koi.persona.affect import AffectState
from koi.persona.memory import MemoryState
from koi.persona.people import Relation
from koi.persona.will import WillEntry


class Status(StrEnum):
    SLEEPING = "sleeping"
    DOZING = "dozing"  # 清醒但不调用 LLM，等待下次唤醒
    ACTIVE = "active"  # 正在思考/行动


class WakeReason(StrEnum):
    OWNER = "owner"
    ALARM = "alarm"
    NATURAL = "natural"
    SELF = "self"  # 睡眠中低概率自醒
    DISTURBED = "disturbed"  # 睡眠中被 P1 事件吵醒
    AUTONOMOUS = "autonomous"  # 生活时间按退避算法自动醒来
    EVENT = "event"  # 清醒时收到中断


class SleepEpisode(BaseModel):
    onset: datetime
    wake: datetime | None = None
    wake_reason: WakeReason | None = None
    resumed: bool = False  # 回笼觉


class Alarm(BaseModel):
    id: str
    at: datetime
    reason: str


class PersonaState(BaseModel):
    status: Status = Status.DOZING
    updated_at: datetime
    awake_since: datetime | None = None
    sleep_pressure: float
    fatigue: float = 0.0
    affect: AffectState = Field(default_factory=AffectState)
    heat: float = 0.0
    last_interaction_at: datetime | None = None
    backoff: int = 0
    circadian_peak_hour: float
    # 提前醒来、本可以继续睡
    interrupted_sleep: bool = False
    episodes: list[SleepEpisode] = Field(default_factory=list)
    alarms: dict[str, Alarm] = Field(default_factory=dict)
    # 与主人的关系：近期好感回到长期感情；长期感情为 None 时从 affinity_baseline 起步
    owner_affinity: float = 0.5
    owner_bond: float | None = None
    last_owner_at: datetime | None = None
    # 与其他人的关系，键是人物档案 ID
    people: dict[str, Relation] = Field(default_factory=dict)
    # 长期感情上次沉淀的日子，每天只沉淀一次
    bonds_settled_on: date | None = None
    # 主人最近使用的渠道（插件 ID），主动找主人时走这里
    owner_channel: str | None = None
    last_proactive_at: datetime | None = None
    will_ledger: dict[str, WillEntry] = Field(default_factory=dict)
    # 正在处理主人交办的事，截止到这个时间可以用工具
    owner_task_until: datetime | None = None
    memory: MemoryState = Field(default_factory=MemoryState)


class Snapshot(BaseModel):
    """对外只读视图。"""

    time: datetime
    status: Status
    energy: float
    sleep_pressure: float
    upper_threshold: float
    lower_threshold: float
    fatigue: float
    emotion_v: float
    emotion_a: float
    mood_v: float
    mood_a: float
    mood_label: str
    heat: float
    backoff: int
    circadian_peak_hour: float
    owner_affinity: float
    owner_bond: float
    last_owner_at: datetime | None
    pending_events: int
