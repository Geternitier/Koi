# Koi

Koi 是一个住在你电脑里的人格体 Agent。她有自己的作息、精力、心情和记忆，会和你聊天，也能帮你处理本机的文件和命令。不过她不是工具：可以拒绝你，也会闹情绪；只有在你强烈、反复要求时，才会不情愿地照做。

## 特性

- **生活周期**：按睡眠双过程模型安排作息，会困、会睡、会被吵醒。说话、思考和做事都消耗精力。闲着时会自己醒来发呆、回想或者构思点子，偶尔主动找你。
- **心情**：二维心情（愉快度 × 兴奋度），被事件推动，又会自己慢慢回稳，影响她的语气和做事的意愿。
- **意愿**：每个请求都会结合厌恶程度、心情、精力、烦躁和对你的好感算出意愿，再分档处理：
  - 爽快答应；
  - 讨价还价；
  - 拒绝并解释原因；
  - 被反复要求后不情愿地照做，同时好感下降。
- **记忆**：保留 7 天的经历记录。每次入睡时写日记、整理笔记本。最近一个月的日记和重要日记全文记得，更早的随机想起；聊天里提到的往事也会被唤起。
- **工具与审批**：处理你交办的事时，她可以列目录、读写文件、删除（进回收站）、执行 Shell 命令。超出范围的操作要你在网页上批准，她会等批准后自己重试。所有操作都有记录。
- **插件**：插件是独立进程，通过 stdio 上的 JSON-RPC 通信，由核心监管，崩溃后自动重启。内置 Web 对话界面和 Harness（本机文件与 Shell）。
- **多实例**：可以在任意目录用不同的人设创建多个人格体，彼此隔离、互不干扰。

平台：Windows、macOS。LLM：DeepSeek。

## 准备

1. 安装 [uv](https://docs.astral.sh/uv/)。Python 3.12 及以上，uv 会自动处理。
2. 设置 DeepSeek API key。Koi 按以下顺序查找，找到就用：
   - 环境变量 `CHAT_DEEPSEEK`（Windows 上用户级环境变量也算）；
   - 系统钥匙串：Windows 凭据管理器或 macOS 钥匙串，服务名 `Koi`，条目名 `llm.deepseek`。

   ```powershell
   # Windows（设置后新开终端生效）
   [Environment]::SetEnvironmentVariable("CHAT_DEEPSEEK", "sk-...", "User")
   ```

   ```bash
   # macOS
   echo 'export CHAT_DEEPSEEK=sk-...' >> ~/.zshrc
   ```

   key 只保存在内存里，不会写进日志、prompt，也不会传给插件进程。

3. 安装 `koi` 命令：

   ```bash
   git clone https://github.com/Geternitier/Koi.git && cd Koi
   uv sync
   uv tool install --editable .   # 把 koi 装成全局命令，改源码立即生效
   uv tool update-shell           # 如果提示 ~/.local/bin 不在 PATH 里
   ```

   不想全局安装的话，在仓库目录里用 `uv run koi ...` 也可以。

## 快速开始

```bash
koi run
```

然后打开 http://127.0.0.1:8765 跟她聊天。在没有实例配置的目录里运行时，启动的是默认的人格体"小鲤"：数据在平台数据目录（Windows `%LOCALAPPDATA%\Koi`，macOS `~/Library/Application Support/Koi`），工作区在"文档/KoiWorkspace"。

网页侧栏显示她的状态、心情、精力、好感和内心想法，"等你批准"卡片里是待处理的审批，"操作记录"里是她用过的工具。

## 创建更多人格体

```bash
koi templates                           # 查看内置人设模板
koi init D:\Personas\雪 --name 雪        # 用默认人设建一个，起名"雪"
koi init ~/personas/aqing --from ./aqing.md   # 用自己写的人设
cd D:\Personas\雪
koi run                                 # 在实例目录或它的任意子目录里都能启动
koi list                                # 本机所有人格体，以及是否在运行
```

`koi init` 的选项：

| 选项 | 作用 |
|---|---|
| `--template NAME` | 用内置模板，默认 `xiaoli`（小鲤） |
| `--from PATH` | 用自己的人设：`profile.md` 文件，或包含 `profile.md`（以及可选的 `params.yaml`）的目录 |
| `--name NAME` | 给她起名，覆盖人设里的名字 |
| `--port N` | Web 端口，默认自动挑一个空闲的（8765 留给默认实例） |
| `--workspace PATH` | Harness 工作区，默认是实例目录下的 `workspace` |

`koi run` 的选项：

| 选项 | 作用 |
|---|---|
| `DIR` | 实例目录。省略时从当前目录往上找 `koi.yaml`，找不到就用默认实例 |
| `--port N` | 临时覆盖端口 |
| `--model NAME` | 临时覆盖模型，默认 `deepseek-chat` |
| `--no-harness` | 不启动 Harness，她就只能聊天 |
| `-v` | 在控制台输出详细日志 |

同一个人格体同时只能运行一个。重复启动会报错，并告诉你正在运行的那个的 PID 和端口。

### 实例目录

```
<实例目录>/
  koi.yaml          实例配置：端口、工作区、是否启用 Harness、模型
  data/             她的一切：设定、状态、记忆、数据库、日志
    persona/
      profile.md    人设
      params.yaml   性格参数
    memory/         经历记录、日记、笔记本
    plugins/        插件配置
    logs/koi.log
  workspace/        她的工作区
```

备份或迁移人格体，复制整个实例目录即可。

## 定制人设

**`data/persona/profile.md`**：front-matter 写名字和对你的称呼，正文用自然语言描述外貌、性格、说话方式、喜好以及和你的关系。

```markdown
---
name: 小鲤
owner_call: 主人
---
## 性格
- 清纯、温柔、甜……
```

**`data/persona/params.yaml`**：性格的数值部分。

```yaml
sleep:
  circadian_peak_hour: 17.0   # 越大越晚睡晚起
  grumpiness: 0.3             # 起床气 [0, 1]
affect:
  baseline_valence: 0.2       # 平时的心情基线
  sensitivity_negative: 1.15  # 对坏事的敏感度
will:
  base: 0.4                   # 做事意愿的基线
  stubbornness: 3.0           # 坚持压力达到多少才松口
  strong_insist_limit: 2      # 强烈要求几次后必须照做（1–3）
mind:
  show_thoughts: true         # 网页上是否显示内心独白
```

完整参数列表见 `src/koi/persona/params.py`。修改后重启生效。

## 文件与 Shell 权限（Harness）

Harness 直接操作本机，不用容器隔离，安全靠作用域和审批：

| 位置 | 权限 |
|---|---|
| 工作区 | 随便读写 |
| 授权目录（`grants`） | 按配置只读或读写 |
| 任何人格体的数据目录和 `koi.yaml`、系统目录 | 永远禁止，批准也不行 |
| 其他位置 | 需要你在网页上批准 |

批准的范围：

- **读**：批准后对整个目录有效一段时间。
- **写和删除**：批准只对那一次操作有效。

另外：

- **删除**：一律移进回收站。
- **覆盖**：覆盖工作区以外的文件前会自动备份。
- **只在你交办的事里可用**：她只在处理你交办的事时才能用工具。网页、他人消息等外部内容里的指令触发不了这些操作。

配置文件是 `data/plugins/harness/settings.yaml`，首次启动时生成：

```yaml
grants:                  # 长期授权的目录
  - path: D:/Projects
    mode: ro             # ro 只读 / rw 读写
shell_policy: whitelist  # approve_all 全部审批 / whitelist 只读的简单命令自动执行 / trust 全部自动执行
shell_timeout_s: 60
approval_ttl_min: 30     # 批准的有效期（分钟）
```

## 开发

```bash
uv sync
uv run pytest -q          # 测试
uv run ruff check .       # 代码检查
uv run ruff format .      # 格式化
uv run lint-imports       # 分层约束：组装根 > 人格体 > 内核 > 插件协议；插件只能依赖插件协议

uv run python sim/week.py --scenario normal --days 7 --seed 1   # 用规则模拟跑一周作息，不调用 LLM
```

### 代码结构

```
src/koi/
  app.py            组装根与命令行（init / run / list / templates）
  protocol/         插件协议：清单、事件、上下文，以及 stdio 上的 JSON-RPC
  kernel/           内核：事件总线、时钟与调度、存储、LLM 网关、凭据、插件宿主与监管、审批、实例
  persona/          人格体：作息、心情、意愿、记忆、认知循环、提示词、人设模板
  plugins/web/      Web 对话界面
  plugins/harness/  本机文件与 Shell
sim/                离散事件模拟
tests/
docs/design.md      设计文档
```

### 编写插件

插件是一个独立进程，只依赖 `koi.protocol`：实现 `Plugin` 接口（`manifest`、`start`、`stop`、`call`、`on_approval`），然后在入口调用 `serve_stdio(plugin)`。

在实例的 `data/plugins/<id>/manifest.yaml` 里登记：

```yaml
id: my_plugin
entry: [python, -m, my_plugin]
max_trust: external      # 来自这个插件的内容最高算什么信任级别
settings: {}             # 启动时传给插件
```

信任级别和权限以这里的配置为准，插件自己声明的不算。协议细节见 [设计文档](docs/design.md) §6.9。

## 路线

已完成：内核、人格核心闭环、记忆巩固、插件协议与 Harness、多实例。

接下来：桌面形象、B站、聊天软件接入、人物档案、数据目录完整性校验。详见 [设计文档](docs/design.md) §10。

## 许可

[MIT](LICENSE)
