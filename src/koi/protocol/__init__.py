"""插件协议：插件唯一可以依赖的 koi 包。

不依赖 koi 的任何其他模块，以便将来拆成独立分发的 SDK。
同一个 Plugin 对象既可以由内核在进程内承载，也可以用 `serve_stdio` 作为
独立进程运行（JSON-RPC 2.0，见 `koi.protocol.rpc` 与 `koi.protocol.stdio`）。
"""

import base64
from datetime import date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_serializer, field_validator

PROTOCOL_VERSION = "koi/0.2"
# koi/0.1 只有 P0–P3，含义和现在不同；核心按旧含义换算（见 kernel.plugins）
LEGACY_PROTOCOL = "koi/0.1"

TrustLevel = Literal["owner", "known_person", "external"]
# approve：查看与处理审批、查看审计记录。只授予主人使用的界面类插件
# owner_ui：向主人展示交互请求、提交主人填写的内容。只授予主人使用的界面类插件
# interact：向主人发起交互请求（扫码、填验证码等）
# appearance：读取她的形象（头像、外貌描述、参考图），例如显示头像、用生图模型画她
Permission = Literal["approve", "owner_ui", "interact", "appearance"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired"]
InteractionKind = Literal["display", "form"]
InteractionStatus = Literal["pending", "submitted", "done", "failed", "cancelled", "expired"]


class Capability(BaseModel):
    name: str
    description: str = ""
    # 参数名 -> 说明，给 LLM 看
    parameters: dict[str, str] = Field(default_factory=dict)
    energy_cost: float = 0.0
    # 是否作为工具交给认知循环；False 表示只由核心直接调用（如 send_message）
    tool: bool = True
    timeout_s: float = 120.0


class Manifest(BaseModel):
    id: str
    version: str
    description: str = ""
    capabilities: list[Capability] = Field(default_factory=list)
    # 插件上报事件可声明的最高信任级别，超出的会被核心降级。
    # 进程外插件以核心侧配置为准，插件自报的值无效
    max_trust: TrustLevel = "external"
    permissions: list[Permission] = Field(default_factory=list)
    # 插件可以读写的凭据名，存于系统钥匙串的 plugin.<id>.<name>；进程外插件以核心侧配置为准
    secrets: list[str] = Field(default_factory=list)


class EventIn(BaseModel):
    """插件上报给核心的事件。来源由核心按插件 ID 填写，插件无法伪造。

    优先级：0 主人的控制（只有主人界面类插件能用）；1 重要事件（限流）；
    2 主人的普通消息；3 和别人的交互、通知；4 环境信息，不会打断她。
    """

    type: str
    priority: int = Field(ge=0, le=4)
    trust: TrustLevel = "external"
    payload: dict[str, Any] = Field(default_factory=dict)
    time: datetime | None = None


class ApprovalRequest(BaseModel):
    action: str
    detail: str


class Approval(BaseModel):
    id: str
    plugin: str
    action: str
    detail: str
    created_at: datetime
    status: ApprovalStatus = "pending"
    resolved_at: datetime | None = None


class InteractionField(BaseModel):
    name: str
    label: str
    # 敏感内容：界面用密码框，提交的值只转给发起的插件，不进入任何记录
    sensitive: bool = False
    # 整串匹配的正则。表单只有这一个字段时，主人在聊天框里发来的符合格式的消息会被当作提交
    pattern: str | None = None
    placeholder: str = ""


class InteractionRequest(BaseModel):
    """请主人看一个东西（display）或填一个东西（form）。"""

    kind: InteractionKind
    title: str
    message: str = ""
    # 二维码内容，由界面在本地渲染
    qr: str | None = None
    url: str | None = None
    fields: list[InteractionField] = Field(default_factory=list)
    expires_in_s: float = 300.0


class Interaction(BaseModel):
    id: str
    plugin: str
    kind: InteractionKind
    title: str
    message: str = ""
    qr: str | None = None
    url: str | None = None
    fields: list[InteractionField] = Field(default_factory=list)
    status: InteractionStatus = "pending"
    created_at: datetime
    expires_at: datetime
    updated_at: datetime


class AuditRecord(BaseModel):
    time: datetime
    plugin: str
    capability: str
    args: str
    ok: bool
    summary: str


class SharedDiary(BaseModel):
    """主人界面上可以展示的一篇日记。正文由人格体参数决定是否下发。"""

    date: date
    importance: str = "normal"
    mood_v: float = 0.0
    mood_a: float = 0.0
    mood_peak: float = 0.0
    people: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    body: str = ""


class MemorySnapshot(BaseModel):
    """记忆的只读视图。shared 为 false 时只有数量，不包含正文。"""

    shared: bool = False
    diaries: int = 0
    pending_notes: int = 0
    entries: list[SharedDiary] = Field(default_factory=list)
    notebook: str = ""
    notes: list[str] = Field(default_factory=list)


class ImageInfo(BaseModel):
    # avatar，或参考图 ref-1、ref-2……
    id: str
    kind: Literal["avatar", "reference"]
    caption: str = ""
    media_type: str
    # 内容的 SHA-256，图片换了它就变，可以当缓存版本号
    sha256: str
    size: int


class AppearanceView(BaseModel):
    """她的形象。description 和参考图留给生图模型保持人物一致。"""

    name: str
    description: str = ""
    avatar: ImageInfo | None = None
    images: list[ImageInfo] = Field(default_factory=list)


class MediaFrame(BaseModel):
    """插件工作目录里的一张画面。path 是相对路径。"""

    path: str
    # 在视频里的秒数，核心换成「第 N 秒」给她看
    t: float = 0.0


class ImageData(BaseModel):
    info: ImageInfo
    # JSON 里是 base64
    data: bytes

    @field_validator("data", mode="before")
    @classmethod
    def _decode(cls, value: object) -> object:
        return base64.b64decode(value, validate=True) if isinstance(value, str) else value

    @field_serializer("data", when_used="json")
    def _encode(self, value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")


class PluginContext(Protocol):
    @property
    def plugin_id(self) -> str: ...

    async def emit(self, event: EventIn) -> None:
        """上报事件。立即返回，不等待核心处理完。"""
        ...

    async def state(self) -> dict[str, Any]:
        """核心状态的只读视图。"""
        ...

    async def memory_view(self) -> MemorySnapshot:
        """日记与笔记本的只读视图（需要 owner_ui 权限）。"""
        ...

    async def appearance(self) -> AppearanceView:
        """她的形象登记了什么（需要 appearance 权限）。"""
        ...

    async def image(self, image_id: str) -> ImageData:
        """取一张形象图片，没有这张时抛错（需要 appearance 权限）。"""
        ...

    async def request_approval(self, request: ApprovalRequest) -> str:
        """请主人批准某个操作，返回审批 ID。结果通过 Plugin.on_approval 通知。"""
        ...

    async def approvals(self) -> list[Approval]:
        """待处理的审批（需要 approve 权限）。"""
        ...

    async def resolve_approval(self, approval_id: str, approved: bool) -> None:
        """主人处理审批（需要 approve 权限）。"""
        ...

    async def audit(self, limit: int = 50) -> list[AuditRecord]:
        """最近的工具调用审计记录（需要 approve 权限）。"""
        ...

    async def request_interaction(self, request: InteractionRequest) -> str:
        """请主人扫码、填写等（需要 interact 权限），返回 ID。结果通过 on_interaction 通知。"""
        ...

    async def update_interaction(self, interaction_id: str, message: str) -> None:
        """更新进度说明，例如"已扫码，请在手机上确认"（需要 interact 权限）。"""
        ...

    async def close_interaction(self, interaction_id: str, ok: bool, message: str = "") -> None:
        """结束自己发起的交互请求（需要 interact 权限）。"""
        ...

    async def interactions(self) -> list[Interaction]:
        """等主人处理的交互请求（需要 owner_ui 权限）。"""
        ...

    async def submit_interaction(self, interaction_id: str, values: dict[str, str]) -> None:
        """提交主人填写的内容（需要 owner_ui 权限）。"""
        ...

    async def cancel_interaction(self, interaction_id: str) -> None:
        """主人取消（需要 owner_ui 权限）。"""
        ...

    async def get_secret(self, name: str) -> str | None:
        """读取自己的凭据（名字须在清单的 secrets 里）。"""
        ...

    async def set_secret(self, name: str, value: str) -> None:
        """保存自己的凭据，例如登录后的 cookie。"""
        ...

    async def delete_secret(self, name: str) -> None: ...

    async def offer_media(
        self, text: str, frames: list[MediaFrame], *, energy_cost: float = 0.0
    ) -> None:
        """把她刚看见的画面交给下一次思考。

        图片留在插件工作目录。事件里只记录说明和相对路径，不记录图片字节。
        """
        ...


class Plugin(Protocol):
    """插件接口。

    发起交互请求的插件还应实现可选的
    `async def on_interaction(self, interaction: Interaction, values: dict[str, str] | None)`：
    主人提交表单时 values 是填写的内容，取消或过期时为 None。
    """

    manifest: Manifest

    async def start(self, ctx: PluginContext) -> None: ...

    async def stop(self) -> None: ...

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]: ...

    async def on_approval(self, approval: Approval) -> None:
        """自己发起的审批有了结果。"""
        ...


__all__ = [
    "LEGACY_PROTOCOL",
    "PROTOCOL_VERSION",
    "AppearanceView",
    "Approval",
    "ApprovalRequest",
    "ApprovalStatus",
    "AuditRecord",
    "Capability",
    "EventIn",
    "ImageData",
    "ImageInfo",
    "Interaction",
    "InteractionField",
    "InteractionKind",
    "InteractionRequest",
    "InteractionStatus",
    "Manifest",
    "MediaFrame",
    "MemorySnapshot",
    "Permission",
    "Plugin",
    "PluginContext",
    "SharedDiary",
    "TrustLevel",
]
