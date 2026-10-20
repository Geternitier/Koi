"""组装根：把内核、人格体与插件接在一起并运行。

uv run koi run [--port 8765] [--data-dir PATH]
"""

import argparse
import asyncio
import contextlib
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from koi.kernel import EventBus, RealClock, Scheduler, Store
from koi.kernel.approvals import ApprovalService
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
    Persona,
    PersonaState,
    ToolSpec,
    load_params,
    load_profile,
)
from koi.persona.core import STATE_KEY
from koi.persona.llm_mind import LLMMind
from koi.plugins.web import WebPlugin

log = logging.getLogger("koi")

DEFAULTS = Path(__file__).parent / "persona" / "defaults"

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
"""


def ensure_persona_files(paths: KoiPaths) -> None:
    for name in ("profile.md", "params.yaml"):
        target = paths.persona / name
        if not target.exists():
            shutil.copyfile(DEFAULTS / name, target)


def harness_spec(paths: KoiPaths) -> PluginSpec:
    settings_path = paths.plugins / "harness" / "settings.yaml"
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(HARNESS_SETTINGS, encoding="utf-8")
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    settings.update(workspace=str(paths.workspace), blacklist=[str(paths.data)])
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


async def run(args: argparse.Namespace) -> None:
    paths = KoiPaths.default()
    if args.data_dir:
        paths = KoiPaths(data=Path(args.data_dir), workspace=paths.workspace)
    paths.ensure()
    ensure_persona_files(paths)

    secrets = SecretStore()
    redactor = Redactor.from_store(secrets)
    setup_logging(paths, redactor, args.verbose)

    params = load_params(paths.persona / "params.yaml")
    profile = load_profile(paths.persona / "profile.md")
    overrides: dict[str, Any] = {"model": args.model} if args.model else {}
    gateway = LLMGateway(LLMConfig.from_secrets(secrets, **overrides))

    store = Store(paths.db)
    clock = RealClock()
    scheduler = Scheduler(clock, on_error=lambda e: log.error("调度回调失败", exc_info=e))
    bus = EventBus()
    approvals = ApprovalService(bus, clock)
    host = PluginHost(bus, clock, approvals=approvals, store=store, redact=redactor)
    host.add(WebPlugin(port=args.port))
    env = scrubbed_env(secrets)
    specs = load_specs(paths.plugins)
    if not args.no_harness:
        specs.insert(0, harness_spec(paths))
    for spec in specs:
        host.add(ProcessPlugin(spec, env=env))

    def toolbox() -> list[ToolSpec]:
        return [
            ToolSpec(
                name=f"{pid}.{c.name}",
                description=c.description,
                parameters=c.parameters,
                energy_cost=c.energy_cost,
            )
            for pid, c in host.tools()
        ]

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
    )

    def state() -> dict[str, Any]:
        return {
            "name": profile.name,
            "snapshot": persona.snapshot().model_dump(mode="json"),
            "thought": persona.last_thought if params.mind.show_thoughts else None,
            "llm": {"calls": gateway.meter.calls, "cache_hit_rate": gateway.meter.cache_hit_rate},
            "memory": {
                "diaries": len(persona.memory.diaries),
                "pending_notes": len(persona.memory.inbox),
            },
            "plugins": {
                pid: getattr(host.get(pid), "status", "running") for pid in host.plugin_ids
            },
            "pending_approvals": len(approvals.pending()),
        }

    host.set_state_provider(state)
    await persona.start()
    await host.start()
    print(f"{profile.name} 已启动：http://127.0.0.1:{args.port}  （数据目录：{paths.data}）")
    try:
        await scheduler.run_forever()
    finally:
        await host.stop()
        store.put_model(STATE_KEY, persona.state)
        store.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="koi")
    sub = ap.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="启动人格体与插件")
    run_p.add_argument("--port", type=int, default=8765)
    run_p.add_argument("--data-dir", help="数据目录，默认按平台惯例")
    run_p.add_argument("--model", help="覆盖 LLM 模型名")
    run_p.add_argument("--no-harness", action="store_true", help="不启动 Harness（文件与 Shell）")
    run_p.add_argument("-v", "--verbose", action="store_true", help="在控制台输出 INFO 日志")
    args = ap.parse_args()

    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(args))
    except MissingSecretError as e:
        print(e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
