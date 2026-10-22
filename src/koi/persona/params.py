"""人格参数（设定的数值部分）。只能由配置服务修改，运行时只读。"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Params(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SleepParams(_Params):
    # 睡眠双过程模型
    tau_wake_h: float = 18.2
    tau_sleep_h: float = 4.2
    upper_base: float = 0.60
    lower_base: float = 0.17
    circadian_amplitude: float = 0.12
    circadian_peak_hour: float = 17.0
    # 超过入睡阈值后还能硬撑的余量，耗尽即精力归零
    stay_up_margin: float = 0.15
    # 活动疲劳的恢复时间常数
    fatigue_tau_awake_h: float = 4.0
    fatigue_tau_sleep_h: float = 1.5
    # 睡眠中自醒：基础速率 + 临近自然醒时的附加速率
    self_wake_rate_per_h: float = 0.015
    near_wake_rate_per_h: float = 0.6
    near_wake_scale: float = 0.03
    # 睡着时排队的 P2 事件（主人的普通消息）把她吵醒的概率
    disturb_wake_prob: float = 0.05
    # 提前醒来后，睡眠压力仍高于下阈值这么多就能睡回笼觉
    resume_margin: float = 0.05
    resume_delay_min: float = 15.0
    # 被叫醒时的起床气 [0, 1]，按入睡时长的睡眠惯性放大
    grumpiness: float = 0.5
    sleep_inertia_min: float = 20.0
    # 作息随实际行为漂移
    phase_adapt_rate: float = 0.1
    phase_max_shift_h: float = 1.0
    phase_anchor_rate: float = 0.1


class AffectParams(_Params):
    baseline_valence: float = 0.15
    baseline_arousal: float = 0.0
    theta_emotion_per_h: float = 0.8
    theta_mood_per_h: float = 0.05
    mood_coupling_per_h: float = 0.15
    noise: float = 0.05
    sensitivity_positive: float = 1.0
    sensitivity_negative: float = 1.0
    sensitivity_arousal: float = 0.8
    energy_valence_coupling: float = 0.15
    energy_arousal_coupling: float = 0.3
    sleep_arousal_drop: float = 0.3
    calm_threshold: float = 0.12


class WakeParams(_Params):
    base_interval_min: float = 60.0
    min_interval_min: float = 5.0
    max_interval_min: float = 240.0
    # 心情好时醒得更勤：g = 0.5 + E/100 + valence_gain * max(0, v)
    valence_gain: float = 0.3
    heat_alpha: float = 2.0
    heat_half_life_min: float = 20.0
    heat_owner: float = 1.0
    heat_known: float = 0.5
    heat_max: float = 3.0
    # 距上次交互超过这么久才视为"没有活跃交互"，可以入睡
    sleep_quiet_min: float = 15.0
    max_backoff: int = 3
    jitter_low: float = 0.6
    jitter_high: float = 1.4


class WillParams(_Params):
    # 意愿度 W = base - 厌恶 + 心情 + 精力 - 烦躁 + 好感
    base: float = 0.35
    aversion_like: float = -0.2
    aversion_neutral: float = 0.0
    aversion_dislike: float = 0.3
    aversion_hate: float = 0.6
    coef_valence: float = 0.4
    coef_energy: float = 0.3
    coef_irritation: float = 0.4
    coef_affinity: float = 0.3
    # 分档：W >= high 爽快；low <= W < high 讨价还价；W < low 拒绝
    tau_high: float = 0.3
    tau_low: float = 0.0
    # 坚持压力
    pressure_normal: float = 1.0
    pressure_strong: float = 2.0
    pressure_half_life_h: float = 2.0
    # 连续追问（间隔不超过此值）时压力不衰减
    pressure_grace_min: float = 15.0
    stubbornness: float = 3.0
    # 心情越差越固执：P_max = stubbornness * (1 + mood_stubbornness * max(0, -v))
    mood_stubbornness: float = 0.5
    # 强烈要求达到这么多次必须照做（硬保证）
    strong_insist_limit: int = Field(default=2, ge=1, le=3)
    ledger_ttl_h: float = 24.0
    # 不情愿照做的代价
    reluctant_valence_impulse: float = -0.15
    reluctant_affinity_drop: float = 0.05
    # 对主人的近期好感 [-1, 1]，回归长期感情；长期感情从 affinity_baseline 起步
    affinity_baseline: float = 0.5
    affinity_half_life_h: float = 72.0
    affinity_gain: float = 0.02
    affinity_loss: float = 0.04


class MindParams(_Params):
    model: str | None = None
    # 看图片时用的模型。空表示和 model 相同；模型不支持图片时会自动降级为纯文本
    vision_model: str | None = None
    perceive_temperature: float = 0.3
    act_temperature: float = 1.0
    journal_entries: int = 80
    show_thoughts: bool = True
    # 是否把日记与笔记本正文交给主人界面（只读）。默认关闭，只暴露数量
    share_memory: bool = False
    proactive_min_interval_h: float = 2.0
    consolidate_temperature: float = 0.7
    # 旧字段。工具不再按这个窗口开关，保留是为了旧存档还能读。
    owner_task_window_min: float = 30.0
    # 一轮思考里工具循环的天花板。平时由精力和心情提前停，不是日常配额。
    max_tool_rounds: int = 24
    # 自己闲着找事做时，一次最多用几轮工具
    max_autonomous_tool_rounds: int = 6
    max_tool_calls_per_round: int = 8
    tool_result_chars: int = 6000
    # 第一拍之后最多再开口几次，用来把一段信息拆成多条消息
    max_speech_rounds: int = 3
    # 精力或效价低于此值就不再续说（累了、心情低落时只说完这一拍）
    continue_min_energy: float = 25.0
    continue_min_valence: float = -0.2
    # LLM 调用折算为活动疲劳（报价；实际扣多少还要乘心情倍率，见 StaminaParams）
    energy_per_call: float = 0.12
    energy_per_1k_output: float = 0.35
    energy_per_1k_uncached_prompt: float = 0.06


class MemoryParams(_Params):
    # 近期日记全部激活
    recent_days: int = 30
    # 更早的日记只注入摘要（重要日记除外）
    summary_only_after_days: int = 90
    # 远期日记每天抽样篇数，权重 age^-d * (1+recall)^beta * mood_peak^gamma
    sample_k: int = 3
    forget_exponent: float = 1.0
    recall_exponent: float = 1.0
    mood_exponent: float = 1.0
    mood_floor: float = 0.1
    important_per_month: int = 3
    # 一天里有效经历少于这么多条就不写日记
    min_entries: int = 4
    notebook_refine_days: float = 7.0
    # 对话激活每次最多想起几篇
    recall_limit: int = 2
    # 记忆巩固后，prompt 里仍保留的最近经历条数
    journal_min_entries: int = 12


class PeopleParams(_Params):
    # 刚认识时的好感 [-1, 1]：陌生人 / 主人登记的熟人
    stranger_affinity: float = 0.0
    known_affinity: float = 0.3
    # 主人登记的熟人，熟悉度不低于这个值
    known_familiarity: float = 0.3
    # 别人带来的好事 / 坏事改变好感：乘以强度（0.1 / 0.25 / 0.5）
    affinity_gain: float = 0.1
    affinity_loss: float = 0.2
    # 近期好感以这个半衰期回到长期感情
    affinity_half_life_h: float = 72.0
    # 每天睡前，长期感情向近期好感靠拢这个比例（主人也一样）
    bond_rate: float = 0.1
    # 对主人的长期感情不低于这个值：关系本身由人设决定，不会被磨没
    owner_bond_floor: float = 0.2
    # 每次交往，熟悉度向 1 靠拢这个比例；间隔不到 contact_gap_min 的算同一次
    familiarity_gain: float = 0.08
    contact_gap_min: float = 30.0
    # 不来往时熟悉度的半衰期（天）
    familiarity_half_life_days: float = 90.0
    # 陌生人带来的情绪冲击打这个折扣，越熟越接近原样
    stranger_impact: float = Field(default=0.6, ge=0.0, le=1.0)
    # 一次思考最多带几个人的档案
    prompt_people: int = 4
    # 印象最多多少字
    impression_chars: int = 200
    # 还没留下印象、不熟、这么久没来往的人会被忘掉
    forget_after_days: float = 30.0
    forget_familiarity: float = 0.2
    max_people: int = 300


class StaminaParams(_Params):
    # 插件声明的 energy_cost（工具和画面）折算成疲劳的比例
    tool_scale: float = 0.35
    # 心情倍率 = 1 - relief * max(0, v) + strain * max(0, -v)，夹在 [factor_min, factor_max]
    mood_relief: float = 0.6
    mood_strain: float = 0.8
    factor_min: float = 0.7
    factor_max: float = 1.4
    # 别人带来的好事回充、坏事额外消耗：乘以强度（0.1 / 0.25 / 0.5）
    recharge_gain: float = 3.0
    drain_gain: float = 2.4
    # 回充额度：每小时补这么多，最多攒这么多
    recharge_per_h: float = 4.0
    recharge_max: float = 6.0
    # 疲劳最低到这里，相当于兴头上能比平时多出的精力
    fatigue_floor: float = -10.0
    # 工具成败的效价冲击；连续失败按次数放大，最多放大到 fail_streak_max 倍
    tool_success_v: float = 0.04
    tool_failure_v: float = -0.06
    fail_streak_max: int = 3


class InterruptParams(_Params):
    # 主人或熟人的消息里出现这些字样，就升为重要事件（P1），睡着时也会被叫醒
    urgent_words: tuple[str, ...] = (
        "醒醒",
        "快醒",
        "起床",
        "紧急",
        "急事",
        "救命",
        "快来",
        "马上",
    )
    # 每次思考最多处理几条事件、正文合计多少字；剩下的稍后再处理
    batch_events: int = 8
    batch_chars: int = 3000
    # 剩下的事件够格时，隔多久再处理一批
    followup_s: float = 120.0
    # 同类事件每多等这么多分钟，相当于简单一级
    aging_min: float = 10.0
    # 在队里等超过这么久的事件升一级，最多升到 P2
    promote_after_min: float = 30.0
    # 日常状态下 P3 先攒一会儿再处理，两次由 P3 触发的思考至少隔这么久
    p3_gather_s: float = 45.0
    p3_min_gap_min: float = 5.0
    p3_tired_gap_min: float = 15.0


class PersonaParams(_Params):
    sleep: SleepParams = Field(default_factory=SleepParams)
    affect: AffectParams = Field(default_factory=AffectParams)
    wake: WakeParams = Field(default_factory=WakeParams)
    will: WillParams = Field(default_factory=WillParams)
    mind: MindParams = Field(default_factory=MindParams)
    memory: MemoryParams = Field(default_factory=MemoryParams)
    people: PeopleParams = Field(default_factory=PeopleParams)
    stamina: StaminaParams = Field(default_factory=StaminaParams)
    interrupts: InterruptParams = Field(default_factory=InterruptParams)


def load_params(path: Path) -> PersonaParams:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PersonaParams.model_validate(data)
