"""组装根：把内核、人格体与插件接在一起并运行。

koi init [DIR] [--template NAME | --from PATH] [--name NAME]   在目录里创建人格体
koi run [DIR]                                                 启动（默认从当前目录往上找实例）
koi list                                                      列出本机的人格体
koi templates                                                 列出内置人设模板
"""

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from koi.kernel import EventBus, RealClock, Scheduler, Store
from koi.kernel.approvals import ApprovalService
from koi.kernel.instance import (
    CONFIG_NAME,
    DEFAULT_PORT,
    Instance,
    InstanceError,
    InstanceLock,
    Registry,
    default_instance,
    find_instance,
    load_instance,
    pick_port,
)
from koi.kernel.interactions import InteractionService
from koi.kernel.llm import LLMConfig, LLMGateway
from koi.kernel.paths import KoiPaths
from koi.kernel.plugins import PluginHost
from koi.kernel.process import PluginSpec, ProcessPlugin, load_specs
from koi.kernel.secrets import (
    MissingSecretError,
    RedactingFilter,
    Redactor,
    SecretStore,
    scrubbed_env,
)
from koi.persona import (
    Journal,
    Memory,
    People,
    Persona,
    PersonaState,
    ToolSpec,
    load_params,
    load_profile,
    templates,
)
from koi.persona.appearance import Image, load_appearance
from koi.persona.core import STATE_KEY
from koi.persona.llm_mind import LLMMind
from koi.persona.mind import SeenImage
from koi.persona.templates import TemplateError
from koi.plugins.web import WebPlugin
from koi.protocol import AppearanceView, ImageData, ImageInfo

log = logging.getLogger("koi")

INSTANCE_CONFIG = """\
# 人格体实例配置（由主人维护，修改后重启 koi 生效）。
# 人设在 data/persona/profile.md 和 params.yaml 里。

# Web 界面端口
port: {port}
# Harness 工作区，相对路径从这个目录算起
workspace: {workspace}
# 是否启动 Harness（本机文件与 Shell）
harness: true
# 覆盖 LLM 模型名，留空用默认
model: null
"""

HARNESS_SETTINGS = """\
# Harness 设置（由主人维护，修改后重启 koi 生效）。
# 工作区可以随便读写；核心数据目录和系统目录永远禁止访问；其他位置需要在网页上批准。

# 长期授权的目录。mode 为 ro（只读）或 rw（读写）
grants: []
#  - path: D:/Projects
#    mode: ro

# Shell 策略：
#   approve_all  每条命令都要批准
#   whitelist    只读的简单命令（参数也在可读范围内）自动执行，其余要批准（默认）
#   trust        全部自动执行，只记审计
shell_policy: whitelist
shell_timeout_s: 60
# 批准的有效期（分钟）
approval_ttl_min: 30
# 网络搜索。true 时提供 web_search，直接搜索，不必批准
web_search: false
# 工作区内实践。true 时，参数都落在工作区里的命令直接执行，不必批准
workspace_practice: false
"""


def harness_spec(instance: Instance, registry: Registry) -> PluginSpec:
    paths = instance.paths
    settings_path = paths.plugins / "harness" / "settings.yaml"
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(HARNESS_SETTINGS, encoding="utf-8")
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    # 这几项由核心决定，配置文件里写了也会被覆盖
    blacklist = [str(p) for p in instance.protected]
    blacklist.append(str(KoiPaths.default().data))  # 默认实例和登记表所在
    settings.update(
        workspace=str(paths.workspace), blacklist=blacklist, registry=str(registry.path)
    )
    return PluginSpec(
        id="harness",
        entry=[sys.executable, "-m", "koi.plugins.harness"],
        settings=settings,
        cwd=str(paths.workspace),
    )


def setup_logging(paths: KoiPaths, redactor: Redactor, verbose: bool) -> None:
    logs = paths.data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(logs / "koi.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    for h in (file_handler, console):
        h.addFilter(RedactingFilter(redactor))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    # 避免 SDK 在日志中记录请求细节
    logging.getLogger("openai").setLevel(logging.WARNING)


def resolve_instance(directory: str | None) -> Instance:
    if directory:
        return load_instance(Path(directory))
    return find_instance(Path.cwd()) or default_instance()


def init(args: argparse.Namespace, registry: Registry) -> Instance:
    root = Path(args.dir or ".").resolve()
    config_path = root / CONFIG_NAME
    if config_path.exists():
        raise InstanceError(f"{root} 已经是人格体实例目录了")
    data = root / "data"
    if data.exists() and any(data.iterdir()):
        raise InstanceError(f"{data} 已存在且不为空")
    source = Path(args.source) if args.source else None
    templates.resolve(args.template, source)  # 先检查人设，避免留下半成品

    # 默认实例固定用 DEFAULT_PORT，即使它还没登记过
    port = args.port or pick_port(registry.ports() | {DEFAULT_PORT})
    workspace = json.dumps(args.workspace or "workspace", ensure_ascii=False)
    root.mkdir(parents=True, exist_ok=True)
    config_path.write_text(INSTANCE_CONFIG.format(port=port, workspace=workspace), "utf-8")
    try:
        instance = load_instance(root)
    except InstanceError:
        config_path.unlink()
        raise
    instance.paths.ensure()
    profile = templates.install(
        instance.paths.persona, template=args.template, source=source, name=args.name
    )
    registry.register(instance, profile.name)
    print(f"已创建人格体「{profile.name}」：{root}")
    print(f"  人设：{instance.paths.persona / 'profile.md'}")
    print(f"  工作区：{instance.paths.workspace}")
    print(f"  端口：{port}")
    print(f'启动：cd "{root}"; koi run')
    return instance


def list_instances(registry: Registry) -> None:
    records = registry.records()
    if not records:
        print("还没有人格体。用 koi init 创建，或直接 koi run 启动默认实例。")
        return
    for r in records:
        info = InstanceLock.probe(Path(r.data))
        if info is None:
            status = "未运行"
        else:
            status = (
                f"运行中 http://127.0.0.1:{info.get('port', '?')}（PID {info.get('pid', '?')}）"
            )
        print(f"{r.name}\t{status}\t{r.root}")


def list_templates() -> None:
    for key, profile in templates.list_templates().items():
        default = "（默认）" if key == templates.DEFAULT_TEMPLATE else ""
        print(f"{key}\t{profile.name}{default}")


async def run(args: argparse.Namespace) -> None:
    instance = resolve_instance(args.dir)
    paths = instance.paths
    port = args.port or instance.config.port
    paths.ensure()
    lock = InstanceLock(paths.data)
    lock.acquire(port=port, root=str(instance.root))
    try:
        await _run(args, instance, port)
    finally:
        lock.release()


async def _run(args: argparse.Namespace, instance: Instance, port: int) -> None:
    paths = instance.paths
    templates.ensure(paths.persona)
    registry = Registry.default()

    secrets = SecretStore()
    redactor = Redactor.from_store(secrets)
    setup_logging(paths, redactor, args.verbose)

    params = load_params(paths.persona / "params.yaml")
    profile = load_profile(paths.persona / "profile.md")
    appearance = load_appearance(paths.persona, profile)
    registry.register(instance, profile.name)
    model = args.model or instance.config.model
    overrides: dict[str, Any] = {"model": model} if model else {}
    gateway = LLMGateway(LLMConfig.from_secrets(secrets, **overrides))

    store = Store(paths.db)
    clock = RealClock()
    scheduler = Scheduler(clock, on_error=lambda e: log.error("调度回调失败", exc_info=e))
    bus = EventBus()
    approvals = ApprovalService(bus, clock)
    interactions = InteractionService(
        bus, clock, on_sensitive=lambda name, value: redactor.add(name, value, min_len=4)
    )
    host = PluginHost(
        bus,
        clock,
        approvals=approvals,
        interactions=interactions,
        store=store,
        secrets=secrets,
        redactor=redactor,
    )
    host.add(
        WebPlugin(
            port=port,
            history_path=paths.data / "web" / "history.jsonl",
            image_dir=paths.data / "web" / "images",
        )
    )
    # 上传的图片放在 data/web/images，事件里只带相对这个目录的路径
    host.set_work_dir("web", paths.data / "web")
    env = scrubbed_env(secrets)
    specs = load_specs(paths.plugins)
    if instance.config.harness and not args.no_harness:
        specs.insert(0, harness_spec(instance, registry))
    for spec in specs:
        host.add(ProcessPlugin(spec, env=env))

    def sendable() -> list[tuple[str, str]]:
        """她能发到网页上的图片。以后工具产出的图也登记在这里，Web 侧不用改。"""
        items = [("avatar", "头像")] if appearance.avatar else []
        return items + [(img.id, img.caption or img.id) for img in appearance.images]

    def toolbox() -> list[ToolSpec]:
        images = sendable()
        specs: list[ToolSpec] = []
        for pid, c in host.tools():
            params = dict(c.parameters)
            if pid == "web" and c.name == "send_image":
                if not images:
                    continue
                params["image"] = "图片 ID：" + "、".join(f"{i}（{cap}）" for i, cap in images)
            specs.append(
                ToolSpec(
                    name=f"{pid}.{c.name}",
                    description=c.description,
                    parameters=params,
                    energy_cost=c.energy_cost,
                )
            )
        return specs

    journal = Journal(paths.memory / "episodes")
    journal.load(clock.now())
    mind = LLMMind(
        gateway,
        profile,
        params.mind,
        calm_threshold=params.affect.calm_threshold,
        redact=redactor,
    )
    persona = Persona(
        params,
        clock=clock,
        scheduler=scheduler,
        bus=bus,
        mind=mind,
        state=store.get_model(STATE_KEY, PersonaState),
        store=store,
        journal=journal,
        memory=Memory(paths.memory, params.memory),
        actuator=host.call,
        tools=toolbox,
        owner_call=profile.owner_call,
        people=People(paths.memory / "people", owner_call=profile.owner_call),
        media_loader=lambda plugin_id, refs: [
            SeenImage(caption=caption, media_type=media_type, data=data)
            for caption, media_type, data in host.load_images(plugin_id, refs)
        ],
    )

    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _sleep_view() -> dict[str, Any]:
        now = clock.now()
        alarms = sorted(
            (alarm for alarm in persona.state.alarms.values() if alarm.at >= now),
            key=lambda alarm: alarm.at,
        )
        next_alarm = alarms[0] if alarms else None
        return {
            "awake_since": _iso(persona.state.awake_since),
            "interrupted": persona.state.interrupted_sleep,
            "episodes": [
                {
                    "onset": _iso(episode.onset),
                    "wake": _iso(episode.wake),
                    "wake_reason": episode.wake_reason.value if episode.wake_reason else None,
                }
                for episode in persona.state.episodes[-8:]
            ],
            "next_wake": (
                {"at": next_alarm.at.isoformat(), "reason": next_alarm.reason}
                if next_alarm is not None
                else None
            ),
        }

    def memory_view() -> dict[str, Any]:
        shared = params.mind.share_memory
        diaries = persona.memory.diaries
        inbox = persona.memory.inbox
        entries: list[dict[str, Any]] = []
        if shared:
            for diary in sorted(diaries, key=lambda item: item.meta.date, reverse=True)[:10]:
                meta = diary.meta
                body = diary.body
                if len(body) > 1500:
                    body = body[:1500] + "……"
                entries.append(
                    {
                        "date": meta.date.isoformat(),
                        "importance": meta.importance,
                        "mood_v": meta.mood_v,
                        "mood_a": meta.mood_a,
                        "mood_peak": meta.mood_peak,
                        "people": meta.people,
                        "tags": meta.tags,
                        "summary": meta.summary,
                        "body": body,
                    }
                )
        notebook = persona.memory.notebook if shared else ""
        if len(notebook) > 4000:
            notebook = notebook[:4000] + "……"
        return {
            "shared": shared,
            "diaries": len(diaries),
            "pending_notes": len(inbox),
            "entries": entries,
            "notebook": notebook,
            "notes": inbox[:50] if shared else [],
        }

    def state() -> dict[str, Any]:
        return {
            "name": profile.name,
            # 头像的版本号，页面拿它拼 /api/avatar?v=…；没有头像时为 None
            "avatar": appearance.avatar.sha256[:16] if appearance.avatar else None,
            "snapshot": persona.snapshot().model_dump(mode="json"),
            "thought": persona.last_thought if params.mind.show_thoughts else None,
            "llm": {"calls": gateway.meter.calls, "cache_hit_rate": gateway.meter.cache_hit_rate},
            "memory": {
                "diaries": len(persona.memory.diaries),
                "pending_notes": len(persona.memory.inbox),
                "shared": params.mind.share_memory,
            },
            "sleep": _sleep_view(),
            "plugins": {
                pid: getattr(host.get(pid), "status", "running") for pid in host.plugin_ids
            },
            "pending_approvals": len(approvals.pending()),
            "pending_interactions": len(interactions.pending()),
        }

    def image_info(img: Image) -> ImageInfo:
        return ImageInfo(
            id=img.id,
            kind=img.kind,
            caption=img.caption,
            media_type=img.media_type,
            sha256=img.sha256,
            size=len(img.data),
        )

    def appearance_view() -> AppearanceView:
        return AppearanceView(
            name=appearance.name,
            description=appearance.description,
            avatar=image_info(appearance.avatar) if appearance.avatar else None,
            images=[image_info(img) for img in appearance.images],
        )

    def image(image_id: str) -> ImageData | None:
        img = appearance.get(image_id)
        return ImageData(info=image_info(img), data=img.data) if img is not None else None

    host.set_state_provider(state)
    host.set_memory_provider(memory_view)
    host.set_appearance_provider(appearance_view, image)
    await persona.start()
    await host.start()
    print(f"{profile.name} 已启动：http://127.0.0.1:{port}  （实例：{instance.root}）")
    try:
        await scheduler.run_forever()
    finally:
        await host.stop()
        store.put_model(STATE_KEY, persona.state)
        store.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="koi")
    sub = ap.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="在目录里创建一个人格体")
    init_p.add_argument("dir", nargs="?", help="实例目录，默认当前目录")
    group = init_p.add_mutually_exclusive_group()
    group.add_argument("--template", help="内置人设模板（koi templates 查看）")
    group.add_argument(
        "--from", dest="source", help="自己的人设：profile.md，或含 profile.md 的目录"
    )
    init_p.add_argument("--name", help="给她起名（覆盖人设里的名字）")
    init_p.add_argument("--port", type=int, help="Web 端口，默认自动挑一个空闲的")
    init_p.add_argument("--workspace", help="Harness 工作区，默认是实例目录下的 workspace")

    run_p = sub.add_parser("run", help="启动人格体与插件")
    run_p.add_argument("dir", nargs="?", help="实例目录，默认从当前目录往上找，找不到用默认实例")
    run_p.add_argument("--port", type=int, help="覆盖 koi.yaml 里的端口")
    run_p.add_argument("--model", help="覆盖 LLM 模型名")
    run_p.add_argument("--no-harness", action="store_true", help="不启动 Harness（文件与 Shell）")
    run_p.add_argument("-v", "--verbose", action="store_true", help="在控制台输出 INFO 日志")

    sub.add_parser("list", help="列出本机的人格体")
    sub.add_parser("templates", help="列出内置人设模板")
    args = ap.parse_args()

    try:
        match args.command:
            case "init":
                init(args, Registry.default())
            case "list":
                list_instances(Registry.default())
            case "templates":
                list_templates()
            case "run":
                with contextlib.suppress(KeyboardInterrupt):
                    asyncio.run(run(args))
    except (MissingSecretError, InstanceError, TemplateError) as e:
        print(e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
