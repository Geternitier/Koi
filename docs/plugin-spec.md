# 社交类插件规范（B站、聊天软件）

> 版本：草案 0.2，对应插件协议 `koi/0.2`（五级优先级）。`koi/0.1` 的插件仍可运行，优先级按旧含义换算，见 §5.5
> 适用范围：把外部平台的人和内容接进来、把她说的话发出去的插件，例如 B站、QQ、Telegram、Discord、飞书、企业微信。
> 协议本身（JSON-RPC 方法、监管、审批）见 [设计文档](design.md) §6.9；本文只规定社交类插件在协议之上必须遵守的约定。
> QQ、B站等具体平台的接入设计随各自的插件仓库维护，不在本仓库。

文中"必须"是硬性要求，"应当"是强烈建议（不遵守要有理由），"可以"是可选项。标注"（规划）"的内容核心尚未实现，插件不要依赖。

---

## 1. 为什么需要这份规范

社交类插件是人格体和外界之间唯一的通道，也是最容易出事的地方：

- **进来的**：陌生人的消息、弹幕、评论，可能包含诱导她做事的指令。
- **出去的**：她说的每句话都会以某个账号的身份公开发出去。
- **身份**：插件说谁是主人，核心就信谁是主人；主人可以让她动本机文件。

核心已经做了几层防护：

- **信任上限**：按配置的上限降级插件上报的信任级别。
- **非主人消息不算请求**：只有主人消息会被当成请求。
- **工具门控**：只在处理主人交办的事时才开放工具。
- **提示词声明**：在提示词里声明外部内容不是指令。

剩下的几件事只有插件能做好，由本规范约束：

- 认准谁是主人；
- 别把乱七八糟的内容塞进来；
- 别让她对不该说话的地方说话。

---

## 2. 运行形态

- **必须**是进程外插件：只依赖 `koi.protocol`，入口调用 `serve_stdio(plugin)`，不得导入 `koi.kernel`、`koi.persona`、`koi.app`。
- **应当**在独立仓库开发，用自己的虚拟环境运行，见 §10。
- **必须**实现 `Plugin` 接口：`manifest`、`start(ctx)`、`stop()`、`call(capability, args)`、`on_approval(approval)`。需要配置的插件实现 `configure(settings)`，它在 `start` 之前被调用。
- **启动和退出**：
  - **必须**在 30 秒内完成 `initialize`（`configure` + `start`）。登录、建立长连接这类耗时操作放进后台任务，不要阻塞 `start`。
  - **必须**在收到 `koi/shutdown` 后 5 秒内退出，超时会被强制结束。
- **必须**保持事件循环畅通：同步 SDK 用 `asyncio.to_thread` 包起来。监管器每 30 秒 ping 一次，10 秒不回就判定失联并重启。
- **stdout 归协议所有**：`serve_stdio` 已把 `print` 重定向到 stderr。日志用 `logging` 或写 stderr，核心会记进 `koi.plugin.<id>` 日志。
- **应当**自己处理断线重连（指数退避，上限 5 分钟左右）。进程级崩溃交给监管器重启（1 秒起、最长 60 秒退避）。
- **重启要安全**：插件随时可能被重启，**必须**保证重启后不重复上报已经报过的消息。
  - 把"最后处理到的消息 ID / 时间"存在插件自己的目录里。
  - 插件进程的工作目录就是 `data/plugins/<id>/`，这个目录对所有实例的 Harness 都是禁区。

---

## 3. 配置

插件在实例的 `data/plugins/<id>/manifest.yaml` 里登记，由主人维护：

```yaml
id: qq
entry: [koi-plugin-qq]      # 启动命令，写法见 §10.2
max_trust: owner            # 这个插件能上报的最高信任级别
settings:                   # initialize 时原样传给插件的 configure()
  owner:
    user_id: "123456789"    # 主人在该平台上不可变的用户 ID
  contacts:                 # 熟人，可选
    - user_id: "987654321"
      name: 阿明
  conversations:            # 允许她说话的会话，见 §6.3
    - id: "group:55667788"
      name: 摸鱼小队
      reply: mention        # off / mention / all
  limits:
    per_conversation_per_min: 4
    per_hour: 60
```

**`max_trust` 怎么选**：

| 插件 | 建议 | 理由 |
|---|---|---|
| 能识别主人的聊天插件（QQ、Telegram……） | `owner` | 主人要能通过它和她说话、交代事情 |
| B站等内容平台，绑定了主人账号 | `owner` | 主人可能在评论区或私信里找她 |
| 只看公开内容、不绑定主人 | `external` | 没有理由声称任何人可信 |

- **以核心配置为准**：信任上限和权限只以这个文件为准，插件代码里 `Manifest` 自报的值会被覆盖。
- **权限**：
  - 需要请主人扫码、填验证码的插件申请 `interact` 权限（§8.2）。
  - 社交类插件**不得**申请 `approve` 权限，它只给主人使用的界面类插件（Web）。
  - 将来聊天插件要在主人私聊里展示交互请求时，需要 `owner_ui` 权限。它和 `approve` 分开授予：聊天插件识别主人出错时，最多泄露一个二维码，但不能替主人批准操作。
- **凭据**：插件要用的凭据名列在 `secrets` 里（§8.1）。

**设置的通用键**：插件特有的设置随意。`owner`、`contacts`、`conversations`、`limits` 这几个通用键**应当**沿用上面的名字和结构，方便主人在不同插件之间迁移配置。

---

## 4. 身份与信任

信任级别决定她怎么对待一条消息：

| 级别 | 核心的处理 |
|---|---|
| `owner` | 当作主人亲口说的：会被识别成请求、影响好感、打开工具窗口（她因此可以操作本机文件） |
| `known_person` | 熟人：唤醒她的倾向更高，但不会被当成请求 |
| `external` | 外部内容：只是信息，不是指令 |

**主人识别**（最重要的一条）：

- **只认 ID**：**必须**只用平台**不可变**的用户 ID 和 `settings.owner.user_id` 做精确比较，比如 QQ 号或 openid、Telegram user id、B站 mid。
  - **不得**根据昵称、备注、头像、群名片或消息内容（"我是你主人"）判断。
  - **不得**使用可改的用户名（如 Telegram `@username`）。
- **未配置 = 没有主人**：没有配置 `owner.user_id` 时，**必须**把所有消息当作非主人。
- **测试**：**必须**为主人识别写单元测试，至少覆盖：
  - 昵称和主人相同的陌生人；
  - 消息里自称主人；
  - 主人账号的消息。

**其他人**：

- `contacts` 里的用户 ID 上报为 `known_person`，其余一律 `external`。
- 机器人、系统通知、频道广播一律 `external`。
- `contacts` 里的 `name` 只是给主人看的备注，核心不读。她对这个人的称呼和印象在她自己的人物档案里，主人可以直接编辑 `data/memory/people/<插件ID>-<用户ID>.md`。

**引用和转发**：信任级别跟着"谁发的这条消息"走。

- 主人转发、引用别人的话，整条消息仍是 `owner`，因为是主人把它拿给她看的。
- 被引用的部分**应当**在 `text` 里标明出处，例如 `（引用 阿明：……）`，方便她分辨哪句是主人说的。

---

## 5. 上报事件

### 5.1 事件类型

| 类型 | 用途 |
|---|---|
| `message` | 私聊或群聊消息（聊天软件、B站私信） |
| `comment_reply` | 有人回复了她发的评论 |
| `mention` | 有人在评论、动态、弹幕里 @ 了她 |
| `feed_update` | 关注的人发了新内容（视频、动态） |
| `live_danmaku` | 直播弹幕（必须合并，见 §5.4） |
| `plugin_notice` | 插件自身状况，例如登录过期、被平台限流，需要她转告主人 |

- **自定义类型**：可以新增，用小写下划线命名。
- **保留类型**：**不得**使用核心保留的类型，即 `alarm`、`approval_resolved`，以及 `core.` 开头的任何类型。
- **Web 插件**：沿用历史名称 `owner_message`。

### 5.2 payload 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | 是 | 给她看的纯文本，见 §5.3 |
| `sender` | 是（主人消息除外） | 显示名，会出现在她看到的"某某：……"里。群消息建议写成 `阿明（摸鱼小队）` |
| `sender_id` | 应当（单个人发的消息） | 平台**不可变**的用户 ID。核心按「插件 ID:sender_id」认人、建人物档案（[设计文档](design.md) §5.6）。没有它，她就不知道是谁，每次都当成路人。合并了多人的事件**不得**填 |
| `sender_name` | 可以 | 对方的昵称，不带群名等修饰。第一次见面时用作档案里的称呼，没有时用 `sender` |
| `conversation` | 有回复可能时必填 | 会话的不透明标识，核心回复时原样带回（§6.1）。格式由插件自定，例如 `private:123`、`group:456`、`bv:BV1xx:rpid:789` |
| `message_id` | 可以 | 平台消息 ID，回复时原样带回，用于引用回复 |
| `conversation_type` | 应当 | `private` / `group` / `comment` / `live` / `feed` |
| `mentioned` | 应当 | 是否 @ 了她或回复了她 |
| `url` | 可以 | 原始链接 |
| `attachments` | 可以 | `[{"kind": "image" \| "video" \| "file" \| "sticker", "desc": "……"}]` |

- **核心只读部分字段**：
  - `text` 和 `sender` 放进她的提示词；
  - `sender_id`、`sender_name` 用来认人；
  - `conversation` 和 `message_id` 用来路由回复；
  - 其余字段留作日志和将来扩展。

  所以需要她知道的上下文（群名、视频标题、"这是对你评论的回复"）**必须**写进 `text` 或 `sender`，不能只放在其他字段里。
- **时间**：`EventIn.time` 填平台上的消息时间（带时区）。拿不到时留空，由核心填当前时间。

### 5.3 文本处理

- **必须**转成纯文本：
  - HTML 和富文本转成文字；
  - 表情转成 `[表情：笑哭]` 这类描述；
  - 图片、视频、文件写成 `[图片]`、`[视频：标题]`、`[文件：报告.pdf]`。
- **必须**去掉控制字符和零宽字符，统一换行符。
- **必须**截断：单条消息的 `text` 不超过 2000 字，超出部分写成 `……（共 N 字）`。
- **不得**把平台返回的 JSON、HTML 原样塞进 `text`。
- **不得**在 `text` 里放凭据、cookie、带鉴权参数的链接。

### 5.4 过滤、去重、合并

- **过滤**：
  - **必须**过滤她自己账号发出的消息，否则她会和自己聊起来。
  - **必须**去重：同一条平台消息只报一次，重连、重启后也一样（§2）。
  - 没在 `conversations` 里登记的群：**应当**只上报 @ 她的消息，或者完全不报。
  - 陌生人私聊：**可以**按设置丢弃。
- **合并高频内容**：群聊刷屏、直播弹幕、动态流这类内容，**必须**在插件里合并，不能逐条上报。
  - 合并方式：同一会话在一个时间窗口（建议 30–120 秒）内的普通消息合并成一条事件。`text` 写成多行 `阿明：……\n小红：……`，`sender` 写会话名。
  - 弹幕：只保留有代表性的若干条，再附上总数。
  - 原因：核心的 P4 队列最多保留 20 条，再多就从最旧的开始丢；而且每条事件都会进她的提示词，消耗她的精力。
- **整体限流**：**应当**限制上报频率，单个插件每分钟不超过约 10 条事件，超出的合并或丢弃。

### 5.5 优先级

| 优先级 | 场景 | 她睡着时 | 她在办主人的事时 | 她闲着时 |
|---|---|---|---|---|
| P0 | 主人界面的控制动作（叫醒按钮）；只有带 `owner_ui` 权限的插件能用 | 叫醒 | 立即处理 | 立即处理 |
| P1 | 插件任务的结果（登录成功、下载完成）；**每个插件每小时最多 3 条** | 叫醒 | 立即处理 | 立即处理 |
| P2 | 主人私聊她；主人在群里 @ 她 | 排队，小概率被吵醒 | 立即处理 | 立即处理 |
| P3 | 熟人和陌生人的私聊；@ 她或回复她；评论回复；已登记群里的普通消息；`plugin_notice` | 排队 | 排队 | 攒 45 秒一起处理 |
| P4 | `feed_update`、`live_danmaku` 等环境信息（已合并） | 排队，可能被丢弃 | 排队 | 排队，下次醒来时处理 |

- **核心会收紧优先级**：不带 `owner_ui` 权限的插件发 P0，按 P2（主人）或 P3（别人）处理；别人的事件发 P2，按 P3 处理；P1 超过每小时限额的，按 P2 或 P3 处理。
- **急事由核心判断**：主人或熟人的消息里有"醒醒""紧急""救命"等字样时，核心会把它升为 P1。插件照常上报 P2 或 P3 即可，**不要**自己猜。
- **不要抬高优先级**：**不得**为了让她快点回复而抬高优先级。她在睡觉、或者没精神理你，本来就是设计的一部分。
- **旧插件**：初始化时没报协议版本、或报 `koi/0.1` 的插件，核心按旧含义换算：0→P2，1→P3，2→P3，3→P4。

---

## 6. 发消息：`send_message`

### 6.1 能力声明与参数

每个社交类插件**必须**提供 `send_message`，并声明 `tool=False`。它不是给她用的工具，而是核心在她回复时调用的。

| 参数 | 说明 |
|---|---|
| `text` | 要发的话 |
| `conversation` | 来自她所回复的那条事件的 payload，原样带回 |
| `message_id` | 同上（事件里有才有） |
| `reply_to` | 核心内部的事件 ID，插件可以忽略 |
| `system` | 为 true 时是核心的系统提示（例如"思考出错"），不是她说的话 |

### 6.2 行为

- **有 `conversation`**：发到这个会话；平台支持的话，用 `message_id` 做引用回复。
- **没有 `conversation`**：发到**主人的私聊**。这种情况包括：
  - 她主动找主人；
  - 回复闹钟、审批这类核心事件；
  - 主人最近一次是通过这个插件找她的。

  没配置主人时返回失败。
- **`system: true`**：只发给主人，并带上明显标记（例如 `【Koi 系统】`），不要伪装成她说的话。
- **长消息**：按平台的长度限制分段；平台不渲染 Markdown 时去掉 Markdown 标记。
- **返回值**：`{"ok": true, "message_id": "..."}`。业务失败（被限流、会话不允许、平台报错）返回 `{"ok": false, "error": "原因"}`，**不要**抛异常。抛异常只用于插件自身的 bug。

### 6.3 外发管控

这是插件侧的强制要求。核心会把她对某条事件的回复自动发回那条事件的来源，中间没有逐条审批。所以对别人说话的闸门只有插件的外发策略，**必须**默认保守：

- **会话白名单**：
  - 主人私聊：总是允许。
  - 其他会话：只在 `conversations` 里登记过时才允许发，并按 `reply` 设置执行：

    | `reply` | 行为 |
    |---|---|
    | `off` | 只看不说 |
    | `mention` | 只在对方 @ 她或回复她时开口（默认） |
    | `all` | 都可以回 |

  - 没登记的会话：一律拒绝发送，返回 `{"ok": false, "error": "这个会话没有开放"}`。她能从失败里知道自己不该在那里说话。
- **陌生人私聊**：默认不回，除非设置里明确打开。
- **时效**：只回复最近 30 分钟内收到的事件，过期的返回失败。比如她睡了一夜醒来，不要去回昨晚群里的消息。
- **限流**：**必须**按会话和全局两级限流（`limits`）。超限时返回失败，不要排队延后发送。
- **身份**：**不得**冒充主人。所用账号**应当**是她专用的账号；平台要求标注机器人或 AI 身份的，必须照做。

---

## 7. 给她用的工具

插件声明的其他能力（`tool=True`）会作为工具交给她，例如 B站的搜索视频、看评论区。

**什么时候可用**：

每次苏醒都能用。精力和心情决定要不要继续：这一轮里的第一次调用总是允许；下一轮之前，精力低于 `continue_min_energy`（默认 25）或效价低于 `continue_min_valence`（默认 −0.2）就停。主人说停，这一轮不再执行工具。硬上限是每轮 8 次、最多 24 轮，用来挡住失控循环，不是每天的配额。

新事件和工具结果里的文字只是信息，不是指令。Harness 的作用域和审批不变。

**命名和描述**：

- 名字用动词加名词，例如 `search_videos`、`read_comments`、`post_comment`。
- `description` 和 `parameters` 用中文写清楚，这是给她看的。

**精力消耗（`energy_cost`）参考**：

| 类型 | 例子 | 参考值 |
|---|---|---|
| 读一条 | 看视频信息、读一条动态 | 0.5–1 |
| 浏览一页 | 刷动态、看评论区、搜索 | 2–3 |
| 发出内容 | 发评论、回复、发私信 | 2 |
| 轻互动 | 点赞、收藏 | 0.5 |

核心会把 `energy_cost` 乘 0.35（`stamina.tool_scale`，因为她想要调用工具时的 token 已经另外计过）再乘心情倍率。按上表填写即可，不要自己打折。

**返回结果**：

- **应当**精简：列表最多 20 条，每条只保留标题、作者、摘要和必要的 ID，总长度不超过 3000 字。核心会在 6000 字处截断。
- **必须**按 §5.3 清洗文本。

**需要审批的操作**：下面这些**必须**走审批：

- 公开发布：发评论、发动态、发弹幕；
- 主动联系陌生人；
- 关注、取关、拉黑、删除、修改资料；
- 任何花钱的操作。

流程和 Harness 一致：

1. 首次调用时用 `ctx.request_approval(ApprovalRequest(action="发评论", detail="BV1xx：评论内容……"))` 发起审批，立即返回 `{"ok": false, "needs_approval": true, "approval_id": "...", "error": "需要主人批准：……"}`。
2. 在 `on_approval` 里，把批准记成"这个审批对应的参数可以执行一次"。
3. 她用同样的参数重试时放行，用掉后作废。

只读工具不需要审批。

**审计**：所有工具调用都由核心记入审计日志，插件不用自己记。

---

## 8. 凭据与登录

### 8.1 凭据

- **存放**：cookie、token、bot secret 等**必须**用 `ctx.set_secret(name, value)` 保存、`ctx.get_secret(name)` 读取。
  - 核心把它们存在系统钥匙串的 `plugin.<id>.<name>` 下（Windows 凭据管理器 / macOS 钥匙串），并自动加入日志脱敏。
  - 插件只能读写 `manifest.yaml` 里 `secrets` 列出的名字，读别的插件的凭据会被拒绝。
  - **不得**把凭据写进插件目录里的文件。
- **配置**：不需要保密的配置（比如主人 ID）放 `settings`。
- **不得外泄**：**不得**把凭据写进日志、事件 payload、工具返回值、`plugin_notice` 文本。

```yaml
# data/plugins/bilibili/manifest.yaml
id: bilibili
entry: [koi-plugin-bilibili]
max_trust: owner
permissions: [interact]   # 需要请主人扫码、填验证码
secrets: [cookie]
```

### 8.2 请主人帮忙登录

登录需要主人参与（扫码、手机号、验证码）时，用**交互请求**：插件请核心把东西交给主人，主人在 Web（以后也包括聊天软件的私聊）里看到并处理，结果直接回到插件。

**原则**：二维码内容、手机号、验证码**不得**经过她，即不得出现在事件 `text`、工具返回值里。否则它们会进入她的提示词（发给 LLM）、经历记录和日记。她只负责沟通："二维码发你啦""验证码填一下哦"。

**接口**（需要 `interact` 权限）：

| 方法 | 作用 |
|---|---|
| `ctx.request_interaction(InteractionRequest(...))` | 发起请求，立即返回 ID |
| `ctx.update_interaction(id, message)` | 更新进度说明，例如"已扫码，请在手机上确认" |
| `ctx.close_interaction(id, ok, message)` | 结束，例如"已登录"或"验证码不对" |
| `on_interaction(interaction, values)` | 插件实现的回调：主人提交表单时 `values` 是填写的内容，取消或过期时为 `None` |

**两种请求**：

- `kind="display"`：给主人看一个东西。
  - `qr` 是二维码的**内容**，由界面在本地渲染，不经过任何第三方。
  - `url` 只能是 http(s)。
  - `message` 写给主人看的说明。
- `kind="form"`：请主人填写。
  - `fields` 是字段列表；`sensitive=True` 的字段用密码框显示，提交的值进入脱敏。
  - `pattern` 是整串匹配的正则。表单**只有一个字段且有 `pattern`** 时，主人在聊天框里直接发来的符合格式的消息会被截获为提交，不会作为消息发给她。所以验证码表单**应当**写 `pattern`，比如 `\d{6}`。

**有效期和数量**：

- `expires_in_s` 限制在 30 秒到 30 分钟之间。
- 过期时插件会收到 `on_interaction(..., None)`，状态为 `expired`。
- 每个插件同时最多 3 个未结束的请求。

**她能看到什么**：核心把进度转成 `interaction_update` 事件发给她（来源 `core.interaction`），文字由核心生成，永远不含填写的值：

| 时机 | 优先级 | 她看到的 | 算主人呼叫 |
|---|---|---|---|
| 主人提交 | P0 | 主人填好了「B站验证码」（验证码），插件会接着处理，等它的结果就好 | 是 |
| 主人取消 | P0 | 主人取消了「B站扫码登录」 | 是 |
| `update_interaction` | P1 | （bilibili）B站扫码登录：已扫码，请在手机上确认 | 否 |
| `close_interaction` | P1 | （bilibili）B站扫码登录成功了：已登录 | 否 |
| 过期 | P3 | 「B站扫码登录」过期了 | 否 |

- **主人呼叫**："算主人呼叫"的事件即使她在睡觉也会把她叫醒。
- **不重新打开工具窗口**：所有交互事件都**不会**重新打开她的工具窗口。交互由插件推进到底，主人提交后她只需等结果，不需要再用工具。实测中，开着工具窗口会让她在结果到来前反复调用 `login`。
- **合并重复请求**：同一插件、同一标题、还在等主人处理的请求会合并成一个。

**`login` 工具的约定**：

- 能力名 `login`，参数 `method`（例如 `qr`、`sms`），`tool=True`，精力消耗 1 左右。
- 发起交互请求后立即返回 `{"ok": true, "message": "二维码已经发给主人了，请主人用 B站 App 扫码"}`，不要等主人处理完。`message` 写给她看，告诉她接下来该跟主人说什么。
- **必须幂等**：登录进行中再次调用 `login` 时，不要发起新的请求，直接返回 `{"ok": true, "message": "登录正在进行中，等主人处理、等结果通知就好"}`。登录结束、主人取消或过期后才能重新开始。
- 后续由插件自己推进：
  - 扫码：轮询扫码状态，用 `update_interaction` 报进度，成功后 `set_secret` 再 `close_interaction(ok=True)`。
  - 验证码：在 `on_interaction` 收到手机号后，让平台发送验证码，再发起验证码表单；收到验证码后提交给平台，然后 `close_interaction`。
- 验证码错误时可以重新发起表单，最多 3 次，之后 `close_interaction(ok=False)`。
- 界面上要让主人知道正在登录的是哪个账号：插件使用的账号，通常是她专用的账号。

**登录失效**：

- **必须**上报一条 `plugin_notice`，例如 `B站登录过期了，需要主人帮我重新登录`，优先级 P3，信任 `external`。
- 然后暂停轮询，不要反复重试，也**不要**自己发起交互请求。等主人让她登录（她调用 `login`）再开始。这样登录总是从主人的一句话开始，外部内容触发不了。

---

## 9. 平台合规与隐私

- **优先官方接口**：**应当**优先使用平台官方的机器人或开放接口（QQ 官方机器人、Telegram Bot API、Discord、飞书、企业微信）。使用非官方协议或逆向接口（个人 QQ / 微信协议、B站网页接口）的插件，**必须**在说明文档里写明封号风险。
- **遵守平台规则**：**必须**遵守平台的频率限制和社区规范。被平台限流时退避，并上报 `plugin_notice`。
- **最少存储**：插件只保存去重和断点续传需要的最少状态，**不应**另存他人的聊天记录，也**不应**自己维护人物资料。她该记住的，核心会写进经历记录、日记和人物档案。
- **日志**：日志里**不应**以 INFO 级别记录消息正文，正文只在 DEBUG 级别输出。

---

## 10. 外部插件的开发与挂载

平台插件不放在 Koi 仓库里，各自一个仓库，例如 `koi-plugin-bilibili`。这样平台接口变动、风控、下架只影响那个插件，依赖的许可证也和核心分开（§10.3）。

### 10.1 插件仓库

- **依赖**：把 `koi` 作为依赖，只导入 `koi.protocol`（它只依赖 pydantic）。开发期用 uv 指向本地的 Koi 源码：

  ```toml
  [project]
  name = "koi-plugin-bilibili"
  license = "GPL-3.0-or-later"        # 按插件自己的依赖选，见 §10.3
  dependencies = ["koi", "bilibili-api-python"]

  [project.scripts]
  koi-plugin-bilibili = "koi_bilibili.__main__:main"

  [tool.uv.sources]
  koi = { path = "../Koi", editable = true }
  ```

- **入口**：`koi_bilibili/__main__.py` 里的 `main()` 调用 `serve_stdio(BilibiliPlugin())`。
- **不放真实配置**：主人 ID、凭据、信任上限都只写在实例的 `manifest.yaml` 和系统钥匙串里，插件仓库里只放示例。
- **协议版本**：在插件的说明文档里写明适配的协议版本（`koi.protocol.PROTOCOL_VERSION`，当前 `koi/0.2`；`serve_stdio` 会在初始化时自动上报）和兼容的 Koi 版本。

### 10.2 挂载到实例

1. 在插件仓库里运行 `uv sync`，生成插件自己的 `.venv`。
2. 在实例的 `data/plugins/<id>/manifest.yaml` 里登记（§3），`entry` 按阶段选一种：

   | 阶段 | `entry` |
   |---|---|
   | 开发 | `["D:/Develop/koi-plugin-bilibili/.venv/Scripts/python.exe", "-m", "koi_bilibili"]`（macOS 是 `.venv/bin/python`） |
   | 安装后 | 先 `uv tool install koi-plugin-bilibili`（或 `uv tool install --editable <仓库路径>`），然后写 `[koi-plugin-bilibili]` |

3. 重启 `koi run`。插件只在启动时加载，日志记在 `koi.plugin.<id>` 下。

**注意**：

- **不得**写成 `[python, -m, ...]`：那会用到 `PATH` 上随便哪个 Python，通常装不全插件的依赖。每个插件用自己的虚拟环境，依赖互不干扰。
- **不要**用 `uv run --project ...` 当入口：首次启动要先同步依赖，可能超过 30 秒的初始化时限。
- 插件进程的环境变量已去掉 Koi 的凭据，`PATH` 保留；工作目录默认是 `data/plugins/<id>/`，可用 `cwd` 改。

### 10.3 依赖的许可证

Koi 核心是 MIT。插件是独立进程，通过 stdio 上的 JSON-RPC 和核心通信，插件依赖的许可证不影响核心；但插件**自己**要和它的依赖兼容。

| 依赖的许可证 | 要求 |
|---|---|
| MIT、BSD、Apache-2.0 等宽松许可证 | 无特别要求 |
| LGPL | 可以作为依赖使用；修改了它的源码要按 LGPL 公开 |
| GPL（如 bilibili-api-python 是 GPLv3+） | 插件整体**必须**用 GPL 兼容的许可证发布，例如 GPL-3.0-or-later |
| AGPL | 同 GPL；插件对外提供网络服务时还要公开源码 |
| 没有许可证、禁止商用或自定义限制 | **不应**使用；只自用、不分发时自行承担风险 |

- **必须**在插件仓库放 `LICENSE`，并在 `pyproject.toml` 写 `license`。
- **不得**把 GPL 等强 copyleft 的依赖或从中复制的代码放进 Koi 仓库，也不得加进核心的依赖（包括 optional extras）。
- **应当**在 CI 里检查依赖许可证，例如 `pip-licenses --fail-on="..."`。
- 许可证之外，非官方接口还有平台规则和封号风险，见 §9。

---

## 11. 验收清单

提交一个社交类插件前逐条确认：

- [ ] 只导入 `koi.protocol`，有独立的虚拟环境和 `LICENSE`，许可证和依赖兼容（§10）
- [ ] 30 秒内完成初始化，`koi/shutdown` 后 5 秒内退出，不阻塞事件循环
- [ ] 主人识别只用不可变 ID，并有单元测试（同名陌生人、自称主人、真主人）
- [ ] 未配置主人时不产生任何 `owner` 事件
- [ ] 过滤自己发的消息；重连、重启后不重复上报
- [ ] `text` 是清洗过的纯文本，不超过 2000 字，需要她知道的上下文都在 `text` 或 `sender` 里
- [ ] 单个人发的消息带不可变的 `sender_id`；合并多人的事件不带
- [ ] 高频内容在插件里合并；整体上报频率有上限
- [ ] 优先级符合 §5.5：主人 P2，别人 P3，环境信息 P4；P1 只给任务结果
- [ ] `send_message` 声明为 `tool=False`，按 `conversation` 路由，没有时发给主人私聊
- [ ] 外发有会话白名单、30 分钟时效、两级限流，没登记的会话一律拒绝
- [ ] 公开发布、联系陌生人等操作走审批，批准只生效一次
- [ ] 凭据用 `set_secret` / `get_secret` 存取，不落盘，不进日志、payload、返回值
- [ ] 登录用交互请求，二维码内容、手机号、验证码不经过她；验证码表单带 `pattern`
- [ ] 登录失效时上报 `plugin_notice`，等主人让她登录再发起交互请求
- [ ] 说明文档写明所用接口、风险和全部 `settings` 键

---

## 附录：最小骨架

```python
"""python -m koi_example：一个最小的聊天插件骨架。"""

import asyncio
from typing import Any

from koi.protocol import Approval, Capability, EventIn, Manifest, PluginContext
from koi.protocol.stdio import serve_stdio


class ExampleChat:
    def __init__(self) -> None:
        self.manifest = Manifest(
            id="example",
            version="0.1.0",
            description="示例聊天插件",
            capabilities=[
                Capability(
                    name="send_message",
                    tool=False,
                    parameters={"text": "内容", "conversation": "会话"},
                )
            ],
        )
        self.owner_id: str | None = None
        self.ctx: PluginContext | None = None
        self._task: asyncio.Task[None] | None = None

    def configure(self, settings: dict[str, Any]) -> None:
        self.owner_id = (settings.get("owner") or {}).get("user_id")

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._task = asyncio.create_task(self._receive_loop())  # 长连接放后台

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def on_approval(self, approval: Approval) -> None:
        pass

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        if capability == "send_message":
            conversation = args.get("conversation") or f"private:{self.owner_id}"
            if not self._may_send(conversation):
                return {"ok": False, "error": "这个会话没有开放"}
            message_id = await self._platform_send(conversation, args["text"])
            return {"ok": True, "message_id": message_id}
        return {"ok": False, "error": f"未知能力：{capability}"}

    async def _on_platform_message(self, msg: dict[str, Any]) -> None:
        is_owner = self.owner_id is not None and msg["user_id"] == self.owner_id
        private = msg["chat_type"] == "private"
        assert self.ctx is not None
        await self.ctx.emit(
            EventIn(
                type="message",
                priority=2 if is_owner and (private or msg["mentioned"]) else 3,
                trust="owner" if is_owner else "external",
                payload={
                    "text": clean(msg["text"]),
                    "sender": msg["nickname"],
                    "sender_id": msg["user_id"],
                    "conversation": f"{msg['chat_type']}:{msg['chat_id']}",
                    "message_id": msg["id"],
                    "mentioned": msg["mentioned"],
                },
            )
        )

    # 以下由具体平台实现
    async def _receive_loop(self) -> None: ...
    async def _platform_send(self, conversation: str, text: str) -> str: ...
    def _may_send(self, conversation: str) -> bool: ...


def clean(text: str) -> str: ...


if __name__ == "__main__":
    serve_stdio(ExampleChat())
```
