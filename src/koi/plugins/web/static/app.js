/* Koi · Pondside web interface
 *
 * A dependency-free front end for the local Koi web plugin.
 * Data comes from /api/state, /api/history, /api/stream,
 * /api/approvals, /api/interactions and /api/audit.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const app = $("koiApp");
  if (!app) return;

  const els = {
    log: $("log"), live: $("liveMessage"), jump: $("jumpLatest"),
    nudge: $("nudge"), wake: $("wakeBtn"),
    pinbar: $("pinbar"), pinbarText: $("pinbarText"), pinbarBtn: $("pinbarBtn"),
    form: $("composerForm"), input: $("input"), send: $("sendBtn"), count: $("count"),
    brandName: $("brandName"), crumb: $("crumb"), viewTitle: $("viewTitle"),
    statusPill: $("statusPill"), statusText: $("statusText"),
    connBadge: $("connBadge"), connTextTop: $("connTextTop"),
    banner: $("connBanner"), connText: $("connText"), retryIn: $("retryIn"), retry: $("retryBtn"),
    msDot: $("msDot"), msStatus: $("msStatus"), msEnergy: $("msEnergy"),
    msPending: $("msPending"), openSide: $("openSideBtn"),
    side: $("side"), sideClose: $("sideClose"), sideName: $("sideName"),
    sideStatus: $("sideStatus"), sideConn: $("sideConn"), attention: $("kAttention"),
    energyRing: $("energyRing"), energyNum: $("energyNum"), energyHead: $("energyHead"),
    energySub: $("energySub"), energySpark: $("energySpark"),
    moodValue: $("moodValue"), moodBar: $("moodBar"), moodWord: $("moodWord"),
    sleepValue: $("sleepValue"), sleepMeter: $("sleepMeter"), sleepWord: $("sleepWord"),
    affValue: $("affValue"), affinity: $("affinity"),
    nowValue: $("nowValue"), nowWord: $("nowWord"),
    thoughtCard: $("thoughtCard"), thought: $("sideThought"),
    sideRun: $("sideRun"), sideRunLabel: $("sideRunLabel"),
    statusPage: $("statusPage"), memoryPage: $("memoryPage"), recordsPage: $("recordsPage"),
    bnavDot: $("bnavDot"), backdrop: $("sideBackdrop"), toasts: $("toasts"),
    boot: $("boot"), bootText: $("bootText"), bootErr: $("bootErr"), bootRetry: $("bootRetry"),
    bell: $("bellBtn"), bellBadge: $("bellBadge"), cmd: $("cmdBtn"),
    theme: $("themeBtn"), railTheme: $("railTheme"),
    railAvatar: $("railAvatar"), topAvatar: $("topAvatar"), railDot: $("railDot")
  };

  const STATUS_WORD = { sleeping: "睡着了", dozing: "发呆中", active: "正在想" };
  const VIEW_META = {
    chat: ["客厅", "和她说说话"],
    status: ["此刻", "她的此刻"],
    memory: ["记忆", "她记得的"],
    records: ["记录", "她做过的"]
  };
  const PLUGIN_WORD = {
    running: "运行中", starting: "启动中", restarting: "重启中",
    stopped: "已停止", failed: "异常"
  };
  const MAX_MESSAGE = 4000;
  const MAX_SAMPLES = 120;
  const AWAIT_TIMEOUT = 45000;

  let state = null;
  let es = null;
  let retryCount = 0;
  let retryTimer = null;
  let awaitTimer = null;
  let popEl = null;
  let lastAttentionSig = "";
  let started = false;

  /* ------------------------------------------------------------------ text */

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ESC[c]);
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

  function fmt(value) {
    return esc(value)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  }

  function initialOf(name) {
    const chars = Array.from(String(name || "").trim());
    const cjk = chars.filter((c) => /[\u3400-\u9fff]/.test(c));
    if (cjk.length) return cjk[cjk.length - 1];
    return (chars[0] || "K").toUpperCase();
  }

  function dateOf(value) {
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function timeOf(value) {
    const d = dateOf(value);
    if (!d) return "";
    return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function dayKey(value) {
    const d = dateOf(value);
    if (!d) return "";
    return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  }

  function dayLabel(value) {
    const d = dateOf(value);
    if (!d) return "";
    const now = new Date();
    const same = d.getFullYear() === now.getFullYear()
      && d.getMonth() === now.getMonth()
      && d.getDate() === now.getDate();
    const text = d.toLocaleDateString("zh-CN", { month: "long", day: "numeric" });
    const week = d.toLocaleDateString("zh-CN", { weekday: "short" });
    return (same ? "今天 · " : "") + text + " · " + week;
  }

  function relative(value) {
    const d = dateOf(value);
    if (!d) return "";
    const diff = Math.max(0, Date.now() - d.getTime());
    if (diff < 60000) return "刚刚";
    if (diff < 3600000) return Math.round(diff / 60000) + " 分钟前";
    if (diff < 86400000) return Math.round(diff / 3600000) + " 小时前";
    return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
  }

  function remaining(value) {
    const d = dateOf(value);
    if (!d) return { text: "", expired: false };
    const ms = d.getTime() - Date.now();
    if (ms <= 0) return { text: "已过期", expired: true };
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    if (minutes > 0) return { text: "还剩 " + minutes + " 分 " + (seconds % 60) + " 秒", expired: false };
    return { text: "还剩 " + seconds + " 秒", expired: false };
  }

  function signed(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return (n >= 0 ? "+" : "") + n.toFixed(2);
  }

  function percent(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.round(n * 100) + "%" : "—";
  }

  function pendingCount() {
    if (state.pendingLoaded) return state.approvals.length + state.interactions.length;
    return (state.serverPending.approvals || 0) + (state.serverPending.interactions || 0);
  }

  function statusWord() {
    return STATUS_WORD[(state.snapshot && state.snapshot.status) || "dozing"] || "发呆中";
  }

  function affinityWord(value) {
    if (value >= 0.7) return "很亲近";
    if (value >= 0.4) return "挺好的";
    if (value >= 0.1) return "有点冷淡";
    return "有情绪";
  }

  function moodAWord(value) {
    if (value >= 0.25) return "有点兴奋";
    if (value <= -0.25) return "有点低落";
    return "比较平静";
  }

  function pluginWord(value) {
    return PLUGIN_WORD[value] || String(value || "未知");
  }

  function fingerprint(message) {
    return message.role + "|" + (message.time || "") + "|" + (message.text || "");
  }

  function messageKey(message) {
    return message && message.id ? "id:" + message.id : "fp:" + fingerprint(message);
  }

  function medallionHTML(size, initial) {
    return '<span class="medallion" style="width:' + size + "px;height:" + size + 'px" aria-hidden="true">'
      + '<svg viewBox="0 0 100 100"><use href="#medallion-base"/>'
      + '<text x="50" y="61.5" text-anchor="middle" font-size="31" fill="#1D4A46" stroke="#FFF9EC" stroke-width="1.1" paint-order="stroke">'
      + esc(initial || "K") + "</text></svg></span>";
  }

  function paintMedallions() {
    document.querySelectorAll("[data-medallion]").forEach((el) => {
      const size = parseInt(el.getAttribute("data-medallion"), 10) || 36;
      el.style.width = size + "px";
      el.style.height = size + "px";
      el.innerHTML = '<svg viewBox="0 0 100 100" aria-hidden="true"><use href="#medallion-base"/>'
        + '<text x="50" y="61.5" text-anchor="middle" font-size="31" fill="#1D4A46" stroke="#FFF9EC" stroke-width="1.1" paint-order="stroke">'
        + esc(state.initial) + "</text></svg>";
    });
  }

  /* ------------------------------------------------------------------ API */

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  async function api(path, options) {
    const init = Object.assign({ credentials: "same-origin", cache: "no-store" }, options || {});
    const headers = Object.assign({ Accept: "application/json" }, (options && options.headers) || {});
    if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    init.headers = headers;
    const response = await fetch(path, init);
    if (!response.ok) {
      let detail = response.statusText || ("请求失败（" + response.status + "）");
      try {
        const body = await response.json();
        if (typeof body.detail === "string") detail = body.detail;
        else if (body.detail) detail = JSON.stringify(body.detail);
        else if (typeof body.error === "string") detail = body.error;
      } catch (err) { /* keep the status text */ }
      throw new ApiError(detail, response.status);
    }
    if (response.status === 204) return null;
    const type = response.headers.get("content-type") || "";
    return type.indexOf("application/json") >= 0 ? response.json() : response.text();
  }

  /* ------------------------------------------------------------------ theme */

  function preferredTheme() {
    try {
      const saved = localStorage.getItem("koi.theme");
      if (saved === "day" || saved === "night") return saved;
    } catch (err) { /* private mode */ }
    try {
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "night" : "day";
    } catch (err) {
      return "day";
    }
  }

  function setTheme(theme) {
    const value = theme === "night" ? "night" : "day";
    app.dataset.theme = value;
    try { localStorage.setItem("koi.theme", value); } catch (err) { /* ignore */ }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", value === "night" ? "#0F191A" : "#F7F3E9");
    [els.theme, els.railTheme].forEach((button) => {
      if (!button) return;
      button.setAttribute("aria-pressed", String(value === "night"));
      button.setAttribute("aria-label", value === "night" ? "切换到日间" : "切换到夜间");
      button.setAttribute("title", value === "night" ? "切换到日间" : "切换到夜间");
    });
  }

  function toggleTheme() {
    setTheme(app.dataset.theme === "night" ? "day" : "night");
  }

  /* ------------------------------------------------------------------ render */

  function updateViewMeta() {
    const meta = VIEW_META[state.view] || VIEW_META.chat;
    els.crumb.textContent = meta[0];
    els.viewTitle.textContent = meta[1];
  }

  function updateComposer() {
    const length = els.input.value.length;
    const sleeping = state.snapshot && state.snapshot.status === "sleeping";
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 130) + "px";
    els.count.textContent = length + " / " + MAX_MESSAGE;
    els.count.classList.toggle("is-over", length > MAX_MESSAGE);
    els.send.disabled = length === 0 || length > MAX_MESSAGE || !state.connected;
    els.form.classList.toggle("is-off", !state.connected);
    els.input.placeholder = !state.connected
      ? "还没连上她，正在重试…"
      : sleeping
        ? "她睡着了，发消息会叫醒她…"
        : "说点什么……";
  }

  function renderTop() {
    const snapshot = state.snapshot || {};
    const status = snapshot.status || "dozing";
    const word = STATUS_WORD[status] || "发呆中";
    const pending = pendingCount();

    els.brandName.textContent = state.name;
    els.sideName.textContent = state.name;
    els.statusPill.dataset.status = status;
    els.statusText.textContent = word;
    els.railDot.className = "k-status-dot" + (status === "sleeping" ? " sleeping" : status === "active" ? " active" : "");
    els.msDot.className = "dot" + (status === "sleeping" ? " sleeping" : status === "active" ? " active" : "");
    els.msStatus.textContent = word;
    els.msEnergy.textContent = Number.isFinite(Number(snapshot.energy)) ? Math.round(snapshot.energy) : "—";

    const statusLine = els.sideStatus.querySelector("span");
    if (statusLine) {
      statusLine.textContent = !state.connected
        ? "连接断开 · 正在重试"
        : pending
          ? word + " · 在等你处理"
          : status === "sleeping"
            ? "睡着了 · 先别吵她"
            : status === "active"
              ? "正在想 · 可能还要一会儿"
              : "发呆中 · 在等你";
    }
    els.sideStatus.dataset.status = status;
    els.sideConn.innerHTML = "本机<br>" + (state.connected ? "已连接" : "重连中");

    els.connBadge.classList.toggle("is-off", !state.connected);
    els.connBadge.title = state.connected ? "已连接" : "连接断开，正在重试";
    els.connBadge.setAttribute("aria-label", els.connBadge.title);
    els.connTextTop.textContent = state.connected ? "本机 · 已连接" : "本机 · 重连中";

    els.bellBadge.hidden = pending === 0;
    els.bellBadge.textContent = String(pending);
    els.bnavDot.hidden = pending === 0;
    els.msPending.hidden = pending === 0;
    els.msPending.textContent = pending + " 件待处理";
    els.pinbar.hidden = pending === 0;
    els.pinbarText.textContent = state.approvals.length && state.interactions.length
      ? pending + " 件事等你处理"
      : state.interactions.length
        ? "有 " + pending + " 个请求等你确认"
        : pending + " 件事等你点头";

    els.nudge.hidden = status !== "sleeping";
    updateViewMeta();
    document.title = (pending ? "(" + pending + ") " : "") + state.name + " · Koi";
    updateComposer();
  }

  function energyHeadline(snapshot) {
    if (snapshot.status === "sleeping") return "她正在睡觉";
    const energy = Number(snapshot.energy) || 0;
    if (energy >= 75) return "精神不错";
    if (energy >= 50) return "还能撑一阵";
    if (energy >= 25) return "有点累了";
    return "快没电了";
  }

  function sparkline(values) {
    if (!values || values.length < 2) {
      return '<path class="line" d="M0 22 H150" opacity=".22"/>';
    }
    const rawMin = Math.min.apply(null, values);
    const rawMax = Math.max.apply(null, values);
    const min = rawMin - 2;
    const max = Math.max(min + 4, rawMax + 2);
    const span = Math.max(1, max - min);
    const points = values.map((value, index) => {
      const x = (index / (values.length - 1)) * 150;
      const y = 26 - ((value - min) / span) * 20;
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    const line = points.join(" ");
    return '<path class="area" d="M0,30 L' + line.replace(/ /g, " L") + ' L150,30 Z"/>'
      + '<path class="line" d="M' + line.replace(/ /g, " L") + '"/>';
  }

  function renderSide() {
    const snapshot = state.snapshot;
    if (!snapshot) return;

    const energy = clamp(Number(snapshot.energy) || 0, 0, 100);
    const moodV = Number(snapshot.emotion_v) || 0;
    const moodA = Number(snapshot.emotion_a) || 0;
    const pressure = clamp(Number(snapshot.sleep_pressure) || 0, 0, 1);
    const upper = clamp(Number(snapshot.upper_threshold) || 0.66, 0, 1);
    const lower = clamp(Number(snapshot.lower_threshold) || 0.23, 0, 1);
    const affinity = clamp(Number(snapshot.owner_affinity) || 0, 0, 1);
    const pending = pendingCount();

    els.energyNum.textContent = String(Math.round(energy));
    els.energyRing.style.strokeDashoffset = String(163.36 * (1 - energy / 100));
    els.energyHead.textContent = energyHeadline(snapshot);
    els.energySub.textContent = "困意 " + pressure.toFixed(2) + "，睡眠阈值 " + upper.toFixed(2) + "。";
    els.energySpark.innerHTML = sparkline(state.samples.map((sample) => sample.energy));

    els.moodValue.textContent = signed(moodV);
    els.moodBar.style.width = Math.round(((moodV + 1) / 2) * 100) + "%";
    els.moodWord.textContent = (snapshot.mood_label || "—") + " · " + moodAWord(moodA);

    els.sleepValue.textContent = pressure.toFixed(2);
    els.sleepMeter.innerHTML = '<i class="cool" style="width:' + Math.round(pressure * 100) + '%"></i>'
      + '<span class="mark lower" style="left:' + Math.round(lower * 100) + '%"></span>'
      + '<span class="mark upper" style="left:' + Math.round(upper * 100) + '%"></span>';
    els.sleepWord.textContent = pressure >= upper
      ? "已经越过睡眠阈值"
      : pressure >= lower
        ? "有点困，但还能撑"
        : "离睡着还早";

    els.affValue.textContent = affinityWord(affinity);
    const dots = Math.round(affinity * 5);
    Array.from(els.affinity.children).forEach((dot, index) => {
      dot.classList.toggle("on", index < dots);
    });

    const now = new Date();
    els.nowValue.textContent = now.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    els.nowWord.textContent = pending ? "有你的事等着" : statusWord();

    if (state.thought) {
      els.thoughtCard.hidden = false;
      els.thought.textContent = state.thought;
    } else {
      els.thoughtCard.hidden = true;
    }

    const plugins = Object.entries(state.plugins || {});
    const running = plugins.filter((entry) => entry[1] === "running").length;
    els.sideRunLabel.textContent = plugins.length ? running + " / " + plugins.length : "本机";
    const pluginRows = plugins.length
      ? plugins.map((entry) => {
          const id = entry[0];
          const value = entry[1];
          return '<div class="k-runrow"><i class="' + (value === "running" ? "" : "warn") + '"></i> '
            + esc(id) + '<span class="spacer"></span><code>' + esc(pluginWord(value)) + "</code></div>";
        }).join("")
      : '<div class="k-quiet-row">没有已加载的插件。</div>';
    const calls = Number(state.llm.calls) || 0;
    const cache = state.llm.cache_hit_rate;
    const diaries = Number(state.memory.diaries) || 0;
    const notes = Number(state.memory.pending_notes) || 0;
    els.sideRun.innerHTML = pluginRows
      + '<div class="k-runrow"><i></i> LLM 调用 <span class="spacer"></span><code>' + calls + " 次</code></div>"
      + '<div class="k-runrow"><i class="' + (cache == null ? "warn" : "") + '"></i> 缓存命中 <span class="spacer"></span><code>' + (cache == null ? "—" : percent(cache)) + "</code></div>"
      + '<div class="k-runrow"><i></i> 记忆 <span class="spacer"></span><code>' + diaries + " 篇日记 · " + notes + " 条待整理</code></div>";
  }

  function messageHTML(message) {
    const time = timeOf(message.time);
    const mid = message.id ? ' data-msg-id="' + esc(message.id) + '"' : "";
    if (message.role === "system") {
      const text = String(message.text || "");
      const isError = message.error || /失败|出错|错误|没想成/.test(text);
      return '<div class="k-sys' + (isError ? " error" : "") + '"' + mid + ">"
        + '<svg class="i"><use href="#' + (isError ? "i-alert" : "i-sparkles") + '"/></svg>'
        + "<span>" + fmt(text) + "</span>"
        + (time ? '<span class="time">' + time + "</span>" : "")
        + "</div>";
    }
    if (message.role === "owner") {
      const status = message.pending
        ? '<div class="k-msg-status">发送中…</div>'
        : message.failed
          ? '<div class="k-msg-status">未送达</div>'
          : "";
      return '<div class="k-msg owner' + (message.pending ? " pending" : "") + (message.failed ? " failed" : "") + '"' + mid + ">"
        + '<div class="k-msg-main">'
        + '<div class="k-msg-name"><span>你</span><time>' + esc(time) + "</time></div>"
        + '<div class="k-bubble">' + fmt(message.text) + "</div>"
        + status
        + "</div></div>";
    }
    return '<div class="k-msg koi"' + mid + ">"
      + medallionHTML(29, state.initial)
      + '<div class="k-msg-main">'
      + '<div class="k-msg-name"><b>' + esc(state.name) + "</b><time>" + esc(time) + "</time></div>"
      + '<div class="k-bubble">' + fmt(message.text) + "</div>"
      + "</div></div>";
  }

  function typingHTML() {
    return '<div class="k-msg koi typing">'
      + medallionHTML(29, state.initial)
      + '<div class="k-msg-main"><div class="k-msg-name"><b>' + esc(state.name) + "</b><time>正在想</time></div>"
      + '<div class="k-bubble"><i class="k-dot"></i><i class="k-dot"></i><i class="k-dot"></i><span class="k-typing-label">她在想…</span></div>'
      + "</div></div>";
  }

  function emptyHTML() {
    return '<div class="k-empty">'
      + medallionHTML(92, state.initial)
      + "<h2>她还在打盹。</h2>"
      + "<p>说句话，让她醒过来。或者先看看她今天过得怎么样。</p>"
      + '<div class="k-chips">'
      + '<button class="k-chip" data-prompt="我回来了。">我回来了。</button>'
      + '<button class="k-chip" data-prompt="今天过得怎么样？">今天过得怎么样？</button>'
      + '<button class="k-chip" data-prompt="帮我看看工作区里的笔记。">帮我看看工作区里的笔记。</button>'
      + "</div></div>";
  }

  function computeTyping() {
    if (state.awaiting) return true;
    const snapshot = state.snapshot;
    const last = state.messages[state.messages.length - 1];
    if (!snapshot || snapshot.status !== "active" || !last || last.role !== "owner") return false;
    const at = dateOf(last.time);
    if (!at) return false;
    const age = Date.now() - at.getTime();
    return age >= 0 && age < 120000;
  }

  function isNearBottom() {
    return els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 120;
  }

  function scrollBottom(behavior) {
    els.log.scrollTo({ top: els.log.scrollHeight, behavior: behavior || "auto" });
    els.jump.hidden = true;
  }

  function renderMessages(forceBottom) {
    const stay = forceBottom || isNearBottom();
    state.typing = computeTyping();
    if (!state.messages.length && !state.typing) {
      els.log.innerHTML = emptyHTML();
      els.jump.hidden = true;
      return;
    }
    const rows = [];
    let lastDay = "";
    for (const message of state.messages) {
      const key = dayKey(message.time);
      if (key && key !== lastDay) {
        lastDay = key;
        rows.push('<div class="k-day">' + esc(dayLabel(message.time)) + "</div>");
      }
      rows.push(messageHTML(message));
    }
    if (state.typing) rows.push(typingHTML());
    const more = state.historyHasMore
      ? '<button class="k-history-more" data-more' + (state.historyLoading ? " disabled" : "") + ">"
        + (state.historyLoading ? "正在读取更早的消息…" : "加载更早的消息") + "</button>"
      : "";
    els.log.innerHTML = '<div class="k-log-inner">' + more + rows.join("") + "</div>";
    if (stay) {
      requestAnimationFrame(() => scrollBottom("auto"));
    } else {
      els.jump.hidden = false;
    }
  }

  /* ------------------------------------------------------------------ attention */

  function approvalMeta(approval) {
    const action = String(approval.action || "操作");
    if (action.indexOf("读取") >= 0) {
      return {
        tags: [{ text: "只读" }, { text: "目录级" }, { text: "有有效期" }],
        note: "批准后，这个目录在一段时间内都可以读。"
      };
    }
    if (action.indexOf("写入") >= 0) {
      return {
        tags: [{ text: "写入", risk: true }, { text: "工作区外" }, { text: "这一次" }],
        note: "只对这一次写入有效；覆盖工作区外的文件会先备份。"
      };
    }
    if (action.indexOf("删除") >= 0) {
      return {
        tags: [{ text: "删除", risk: true }, { text: "进回收站" }, { text: "这一次" }],
        note: "删除会移进回收站，不会永久删除。"
      };
    }
    if (action.indexOf("命令") >= 0 || action.indexOf("执行") >= 0) {
      return {
        tags: [{ text: "命令", risk: true }, { text: "需要确认" }, { text: "这一次" }],
        note: "只对这条命令有效；执行结果会写进操作记录。"
      };
    }
    return {
      tags: [{ text: "需要确认" }, { text: "来自插件" }],
      note: "批准后她会继续。"
    };
  }

  function approvalCard(approval) {
    const meta = approvalMeta(approval);
    const action = String(approval.action || "操作");
    const command = action.indexOf("命令") >= 0 || action.indexOf("执行") >= 0;
    const tags = meta.tags.map((tag) =>
      '<span class="k-tag' + (tag.risk ? " risk" : "") + '">' + esc(tag.text) + "</span>"
    ).join("");
    return '<article class="k-attn" data-approval-id="' + esc(approval.id) + '">'
      + '<div class="k-attn-head"><span class="k-attn-icon"><svg class="i"><use href="#i-shield"/></svg></span>'
      + "<div><b>" + esc(action) + "</b><small>" + esc(approval.plugin || "插件") + " · " + esc(relative(approval.created_at)) + "</small></div>"
      + '<span class="from">' + esc(String(approval.plugin || "").toUpperCase()) + "</span></div>"
      + '<div class="k-attn-path' + (command ? " is-command" : "") + '" title="' + esc(approval.detail || "") + '">' + esc(approval.detail || "—") + "</div>"
      + '<div class="k-attn-meta">' + tags + "</div>"
      + '<div class="k-attn-actions">'
      + '<button class="k-btn primary" data-act="approve" data-approval-id="' + esc(approval.id) + '">允许这一次</button>'
      + '<button class="k-btn ghost danger" data-act="deny" data-approval-id="' + esc(approval.id) + '">拒绝</button>'
      + "</div>"
      + '<div class="k-attn-note">' + esc(meta.note) + "</div>"
      + '<div class="k-attn-error" data-error hidden></div>'
      + "</article>";
  }

  function safeUrl(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch (err) {
      return null;
    }
  }

  function fieldInput(interaction, field) {
    const key = interaction.id + "::" + field.name;
    const code = field.pattern || /code|验证码/i.test(field.name + field.label);
    const type = field.sensitive ? "password" : "text";
    const autocomplete = code ? "one-time-code" : "off";
    const mode = code ? ' inputmode="numeric"' : "";
    return '<label class="k-field"><span>' + esc(field.label || field.name) + "</span>"
      + '<input data-draft="' + esc(key) + '" data-field="' + esc(field.name) + '" type="' + type + '"'
      + mode + ' autocomplete="' + autocomplete + '" placeholder="' + esc(field.placeholder || field.label || "") + '">'
      + "</label>";
  }

  function interactionCard(interaction) {
    const fields = Array.isArray(interaction.fields) ? interaction.fields : [];
    const fieldHTML = fields.map((field) => fieldInput(interaction, field)).join("");
    const qr = interaction.has_qr
      ? '<div class="k-qr-wrap"><div class="k-qr"><img src="/api/interactions/'
        + encodeURIComponent(interaction.id) + '/qr.svg" alt="二维码" loading="eager" decoding="async"></div>'
        + '<div class="k-qr-copy"><b>用手机扫一下</b>扫完在手机上确认'
        + '<span class="k-countdown" data-expires="' + esc(interaction.expires_at) + '">' + esc(remaining(interaction.expires_at).text) + "</span></div></div>"
      : "";
    const url = safeUrl(interaction.url);
    const link = url
      ? '<a class="k-btn" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">打开链接 <svg class="i"><use href="#i-external"/></svg></a>'
      : "";
    const submit = interaction.kind === "form" && fields.length
      ? '<button class="k-btn primary" data-act="submit">提交</button>'
      : "";
    const hint = fields.length === 1 && fields[0].pattern
      ? "也可以直接把验证码发在聊天框；她不会看到。"
      : fields.length
        ? "填写的内容只会交给发起请求的插件。"
        : "这个请求只会交给发起请求的插件。";
    return '<article class="k-attn" data-interaction-id="' + esc(interaction.id) + '" data-kind="' + esc(interaction.kind) + '">'
      + '<div class="k-attn-head"><span class="k-attn-icon"><svg class="i"><use href="#'
      + (interaction.has_qr ? "i-qr" : "i-message") + '"/></svg></span>'
      + "<div><b>" + esc(interaction.title || "需要你确认") + "</b><small>"
      + esc(interaction.plugin || "插件") + " · " + esc(relative(interaction.created_at)) + "</small></div>"
      + '<span class="from">' + esc(String(interaction.plugin || "").toUpperCase()) + "</span></div>"
      + (interaction.message ? '<div class="k-attn-message">' + fmt(interaction.message) + "</div>" : "")
      + qr + fieldHTML
      + '<div class="k-attn-actions">' + submit + link
      + '<button class="k-btn ghost danger" data-act="cancel">取消</button>'
      + "</div>"
      + '<div class="k-attn-note">' + hint + "</div>"
      + '<div class="k-attn-error" data-error hidden></div>'
      + "</article>";
  }

  function captureDrafts() {
    const active = document.activeElement;
    const key = active && active.dataset ? active.dataset.draft : null;
    const start = active && typeof active.selectionStart === "number" ? active.selectionStart : null;
    const end = active && typeof active.selectionEnd === "number" ? active.selectionEnd : null;
    const drafts = {};
    els.attention.querySelectorAll("[data-draft]").forEach((input) => {
      drafts[input.dataset.draft] = input.value;
    });
    return { drafts, key, start, end };
  }

  function restoreDrafts(snapshot) {
    if (!snapshot) return;
    const inputs = Array.from(els.attention.querySelectorAll("[data-draft]"));
    inputs.forEach((input) => {
      if (Object.prototype.hasOwnProperty.call(snapshot.drafts, input.dataset.draft)) {
        input.value = snapshot.drafts[input.dataset.draft];
      }
    });
    if (snapshot.key) {
      const target = inputs.find((input) => input.dataset.draft === snapshot.key);
      if (target) {
        target.focus({ preventScroll: true });
        if (snapshot.start != null && target.setSelectionRange) {
          try { target.setSelectionRange(snapshot.start, snapshot.end); } catch (err) { /* ignore */ }
        }
      }
    }
  }

  function renderAttention() {
    const signature = JSON.stringify([state.approvals, state.interactions]);
    if (signature === lastAttentionSig) {
      updateCountdowns();
      return;
    }
    const drafts = captureDrafts();
    lastAttentionSig = signature;
    const total = state.approvals.length + state.interactions.length;
    if (!total) {
      els.attention.innerHTML = "";
      return;
    }
    let html = '<div class="k-side-title"><span>等你</span><span class="badge">' + total + "</span></div>";
    html += state.approvals.map(approvalCard).join("");
    html += state.interactions.map(interactionCard).join("");
    els.attention.innerHTML = html;
    restoreDrafts(drafts);
    updateCountdowns();
  }

  function updateCountdowns() {
    document.querySelectorAll("[data-expires]").forEach((el) => {
      const left = remaining(el.dataset.expires);
      el.textContent = left.text;
      el.classList.toggle("is-expired", left.expired);
    });
  }

  /* ------------------------------------------------------------------ pages */

  function affinityDots(value) {
    const count = Math.round(clamp(value, 0, 1) * 5);
    let html = '<div class="k-affinity" aria-hidden="true">';
    for (let i = 0; i < 5; i += 1) html += '<i class="' + (i < count ? "on" : "") + '"></i>';
    return html + "</div>";
  }

  function sparkChart(samples) {
    if (!samples || samples.length < 2) {
      return '<div class="k-notice">打开页面后，她会在这里留下精力的变化。多待一会儿就能看到曲线。</div>';
    }
    const values = samples.map((sample) => sample.energy);
    const rawMin = Math.min.apply(null, values);
    const rawMax = Math.max.apply(null, values);
    const min = rawMin - 2;
    const max = Math.max(min + 4, rawMax + 2);
    const span = Math.max(1, max - min);
    const points = values.map((value, index) => {
      const x = 6 + (index / (values.length - 1)) * 288;
      const y = 84 - ((value - min) / span) * 66;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    const first = timeOf(new Date(samples[0].t).toISOString());
    return '<svg class="k-chart" viewBox="0 0 300 96" role="img" aria-label="本次打开后的精力曲线">'
      + '<path d="M0 20 H300 M0 52 H300 M0 84 H300" stroke="var(--k-line-soft)" stroke-width="1"/>'
      + '<polyline points="' + points + '" fill="none" stroke="var(--k-water)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
      + '<text x="2" y="94">' + esc(first) + '</text><text x="268" y="94">现在</text>'
      + "</svg>";
  }

  function wakeLabel(value) {
    const date = dateOf(value);
    if (!date) return "";
    const now = new Date();
    const tomorrow = new Date(now);
    tomorrow.setDate(tomorrow.getDate() + 1);
    if (date.toDateString() === now.toDateString()) return timeOf(value);
    if (date.toDateString() === tomorrow.toDateString()) return "明天 " + timeOf(value);
    return (date.getMonth() + 1) + " 月 " + date.getDate() + " 日 " + timeOf(value);
  }

  function sleepTrackHTML(sleep, snapshot) {
    const now = new Date();
    const start = new Date(now);
    start.setHours(0, 0, 0, 0);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    const pct = (value) => {
      const date = value instanceof Date ? value : dateOf(value);
      if (!date) return 0;
      return clamp(((date.getTime() - start.getTime()) / (end.getTime() - start.getTime())) * 100, 0, 100);
    };
    const episodes = Array.isArray(sleep.episodes) ? sleep.episodes : [];
    const blocks = episodes.map((episode) => {
      const onset = dateOf(episode.onset);
      if (!onset) return "";
      const wake = episode.wake ? dateOf(episode.wake) : now;
      if (!wake) return "";
      const leftTime = new Date(Math.max(onset.getTime(), start.getTime()));
      const rightTime = new Date(Math.min(wake.getTime(), end.getTime()));
      if (rightTime.getTime() <= leftTime.getTime()) return "";
      const left = pct(leftTime);
      const width = Math.max(0.4, pct(rightTime) - left);
      return '<span class="k-span sleep' + (episode.wake ? "" : " current") + '" style="left:'
        + left.toFixed(2) + "%;width:" + width.toFixed(2) + '%"></span>';
    }).join("");
    const last = episodes.length ? episodes[episodes.length - 1] : null;
    const awake = sleep.awake_since
      ? "醒着 · 从 " + timeOf(sleep.awake_since) + " 起"
      : snapshot && snapshot.status === "sleeping"
        ? "正在睡"
        : "还没有醒着的记录";
    const next = sleep.next_wake
      ? " · 下一次闹钟 " + wakeLabel(sleep.next_wake.at)
      : "";
    const interrupted = sleep.interrupted ? " · 被提前叫醒" : "";
    return '<div class="k-daytrack"><div class="k-track">' + blocks + "</div>"
      + '<span class="k-now" style="left:' + pct(now).toFixed(2) + '%"></span>'
      + '<div class="k-ticks"><span>00</span><span>04</span><span>08</span><span>12</span><span>16</span><span>20</span><span>24</span></div></div>'
      + '<div class="k-legend"><span><i class="water"></i>醒着</span><span><i style="background:var(--k-sleep)"></i>睡着</span><span><i style="background:var(--k-koi)"></i>现在</span></div>'
      + '<p class="k-sleep-summary">' + esc(awake + next + interrupted) + "</p>"
      + (episodes.length ? "" : '<div class="k-notice">还没有睡眠片段；可能是刚启动，或核心尚未记录。</div>');
  }

  function renderStatus() {
    const snapshot = state.snapshot;
    if (!snapshot) {
      els.statusPage.innerHTML = '<div class="k-empty-page">' + medallionHTML(80, state.initial)
        + "<h3>还在读她的状态</h3><p>等她醒过来，或者过一会儿再来看看。</p></div>";
      return;
    }
    const energy = clamp(Number(snapshot.energy) || 0, 0, 100);
    const pressure = clamp(Number(snapshot.sleep_pressure) || 0, 0, 1);
    const upper = clamp(Number(snapshot.upper_threshold) || 0.66, 0, 1);
    const lower = clamp(Number(snapshot.lower_threshold) || 0.23, 0, 1);
    const moodV = Number(snapshot.emotion_v) || 0;
    const moodA = Number(snapshot.emotion_a) || 0;
    const baseV = Number(snapshot.mood_v) || 0;
    const baseA = Number(snapshot.mood_a) || 0;
    const affinity = clamp(Number(snapshot.owner_affinity) || 0, 0, 1);
    const dotX = clamp(50 + moodV * 38, 8, 92);
    const dotY = clamp(50 - moodA * 38, 8, 92);
    const baseX = clamp(50 + baseV * 38, 8, 92);
    const baseY = clamp(50 - baseA * 38, 8, 92);
    const memory = state.memory || {};
    const plugins = Object.entries(state.plugins || {});
    const running = plugins.filter((entry) => entry[1] === "running").length;
    const lastOwner = relative(snapshot.last_owner_at);
    const sleepCard = state.sleep
      ? '<section class="k-card span2"><div class="k-card-head"><h3>今天 · 睡与醒</h3><small>'
        + esc(dayLabel(snapshot.time).replace(/^今天 · /, "")) + "</small></div>" + sleepTrackHTML(state.sleep, snapshot) + "</section>"
      : "";

    els.statusPage.innerHTML =
      '<div class="k-page-head"><div><h2>她的此刻</h2><p>不是仪表盘，是一张可以读懂的生活切片。</p></div>'
      + '<button class="k-btn" data-go="chat"><svg class="i"><use href="#i-arrow-back"/></svg> 回到对话</button></div>'
      + '<div class="k-status-note">当前状态 · ' + esc(statusWord()) + '</div>'
      + '<div class="k-grid2">'
      + sleepCard
      + '<section class="k-card span2"><div class="k-card-head"><h3>精力</h3><small>本次打开以来</small></div>'
      + '<div class="k-stat-row"><div><span class="k-big">' + Math.round(energy) + '<small>/ 100</small></span><p>' + esc(energyHeadline(snapshot)) + "</p></div>"
      + '<div><span class="k-big">' + pressure.toFixed(2) + '<small>/ ' + upper.toFixed(2) + '</small></span><p>困意与睡眠阈值</p></div>'
      + '<div><span class="k-big">' + running + '<small>/ ' + plugins.length + '</small></span><p>插件在运行</p></div></div>'
      + sparkChart(state.samples)
      + '<div class="k-notice">曲线是本次打开页面后的采样，每 5 秒左右记录一次；不会伪造历史。</div></section>'
      + '<section class="k-card"><div class="k-card-head"><h3>心情</h3><small>效价 × 唤醒</small></div>'
      + '<div class="k-moodmap" aria-label="二维心情坐标"><span class="ax a1">兴奋</span><span class="ax a2">低落</span><span class="ax a3">不愉快</span><span class="ax a4">愉快</span>'
      + '<span class="trail" style="left:' + baseX + '%;top:' + baseY + '%"></span>'
      + '<span class="dot" style="left:' + dotX + '%;top:' + dotY + '%"></span></div>'
      + "<p><b style=\"color:var(--k-koi)\">" + esc(snapshot.mood_label || "—") + "</b> · 效价 " + signed(moodV)
      + "，唤醒 " + signed(moodA) + "。浅色点是这段时间的心境。</p></section>"
      + '<section class="k-card"><div class="k-card-head"><h3>困意</h3><small>sleep pressure</small></div>'
      + '<div class="k-big">' + pressure.toFixed(2) + '<small>/ ' + upper.toFixed(2) + '</small></div>'
      + '<div class="k-meter" style="height:6px;margin-top:8px"><i class="cool" style="width:' + Math.round(pressure * 100) + '%"></i>'
      + '<span class="mark lower" style="left:' + Math.round(lower * 100) + '%"></span>'
      + '<span class="mark upper" style="left:' + Math.round(upper * 100) + '%"></span></div>'
      + '<p>越过 ' + upper.toFixed(2) + "，她会去找地方睡；现在" + (pressure >= upper ? "已经越过了。" : "还没到。") + "</p></section>"
      + '<section class="k-card"><div class="k-card-head"><h3>她对你的感觉</h3><small>affinity</small></div>'
      + '<div class="k-big">' + esc(affinityWord(affinity)) + "</div>" + affinityDots(affinity)
      + "<p>好感会随互动慢慢变；它不是一个需要刷的分数。</p></section>"
      + '<section class="k-card"><div class="k-card-head"><h3>她记得的</h3><small>memory</small></div>'
      + '<div class="k-stat-row"><div><span class="k-big">' + (Number(memory.diaries) || 0) + '</span><p>篇日记</p></div>'
      + '<div><span class="k-big">' + (Number(memory.pending_notes) || 0) + '</span><p>条待整理</p></div></div>'
      + "<p>日记内容还在核心手里，网页目前只能读到数量。</p></section>"
      + '<section class="k-card"><div class="k-card-head"><h3>上次和你说过话</h3><small>last owner</small></div>'
      + '<div class="k-big" style="font-size:22px">' + esc(lastOwner || "还没有记录") + "</div>"
      + "<p>她记得你上次来找她的时间，也会在闲下来时想起你。</p></section>"
      + '<section class="k-card span2"><div class="k-card-head"><h3>此刻在心里</h3><small>thought</small></div>'
      + (state.thought
        ? '<p style="font-family:var(--serif);font-size:12.5px;font-style:italic;line-height:1.95;color:var(--k-ink2)">“' + esc(state.thought) + "”</p>"
        : '<p style="font-family:var(--serif);font-size:12.5px;font-style:italic;line-height:1.95;color:var(--k-ink3)">她现在没有把想法说出来。</p>')
      + "</section></div>";
  }

  function memoryMoodWord(v, a) {
    if (v >= 0.25) return a >= 0.2 ? "雀跃" : "开心";
    if (v <= -0.25) return "低落";
    return "平静";
  }

  function diaryCard(entry) {
    const date = new Date(String(entry.date || "") + "T00:00:00");
    const dateText = Number.isNaN(date.getTime())
      ? String(entry.date || "")
      : date.toLocaleDateString("zh-CN", { month: "long", day: "numeric" });
    const tags = [...(entry.tags || []), ...(entry.people || [])]
      .filter(Boolean)
      .map((tag) => '<span class="k-tag">' + esc(tag) + "</span>")
      .join("");
    return '<article class="k-diary-item' + (entry.importance === "important" ? " important" : "") + '">'
      + "<header><time>" + esc(dateText) + "</time><b>" + esc(entry.summary || "这一天") + "</b>"
      + (entry.importance === "important" ? '<span class="k-tag risk">重要</span>' : "")
      + '<span class="mood">' + esc(memoryMoodWord(Number(entry.mood_v) || 0, Number(entry.mood_a) || 0)) + "</span></header>"
      + "<p>" + fmt(entry.body || entry.summary || "") + "</p>"
      + (tags ? '<div class="k-diary-meta">' + tags + "</div>" : "")
      + "</article>";
  }

  function renderMemory() {
    const view = state.memoryView;
    const memory = state.memory || {};
    const shared = view ? Boolean(view.shared) : Boolean(memory.shared);
    const diaries = Number(view ? view.diaries : memory.diaries) || 0;
    const notesCount = Number(view ? view.pending_notes : memory.pending_notes) || 0;
    const head = '<div class="k-page-head"><div><h2>她记得的</h2><p>日记、笔记，以及那些她愿意给你看的部分。</p></div>'
      + '<div style="display:flex;gap:6px;flex-wrap:wrap">'
      + '<button class="k-btn" data-action="refresh-memory"><svg class="i"><use href="#i-refresh"/></svg> 刷新</button>'
      + '<button class="k-btn" data-go="chat"><svg class="i"><use href="#i-arrow-back"/></svg> 回到对话</button></div></div>';

    if (state.memoryLoading && !view) {
      els.memoryPage.innerHTML = head
        + '<div class="k-empty-page">' + medallionHTML(80, state.initial)
        + "<h3>正在翻她的日记</h3><p>只读，不会改动任何内容。</p></div>";
      return;
    }
    if (state.memoryError && !view) {
      els.memoryPage.innerHTML = head
        + '<div class="k-empty-page">' + medallionHTML(80, state.initial)
        + "<h3>日记暂时打不开</h3><p>" + esc(state.memoryError) + "</p>"
        + '<button class="k-btn" data-action="refresh-memory">再试一次</button></div>';
      return;
    }

    if (!shared) {
      els.memoryPage.innerHTML = head
        + '<div class="k-empty-page">' + medallionHTML(80, state.initial)
        + "<h3>日记还锁着</h3>"
        + "<p>她已经写下一些东西，但还没有允许网页查看正文。这里只显示数量，不会用假日记填满页面。</p>"
        + '<div class="k-memory-grid"><div class="k-card"><span class="k-big">' + diaries + '</span><p>篇日记</p></div>'
        + '<div class="k-card"><span class="k-big">' + notesCount + '</span><p>条待整理</p></div></div>'
        + "</div>"
        + '<div class="k-notice"><b>怎么打开：</b>在实例的 <code>data/persona/params.yaml</code> 里把 '
        + "<code>mind.share_memory</code> 设为 <code>true</code>，重启后她会把最近 10 篇日记和笔记本交给网页只读展示。"
        + "默认关闭，是为了让“给你看”仍然是一个选择。</div>";
      return;
    }

    const entries = Array.isArray(view && view.entries) ? view.entries : [];
    const notebook = view && view.notebook ? String(view.notebook) : "";
    const notes = Array.isArray(view && view.notes) ? view.notes : [];
    const shown = entries.length;
    const sharedNote = diaries
      ? "她愿意给你看的 · 共 " + diaries + " 篇" + (shown < diaries ? "，展示最近 " + shown + " 篇" : "")
      : "她还没有写下日记";
    const diaryHTML = entries.length
      ? '<div class="k-diary">' + entries.map(diaryCard).join("") + "</div>"
      : '<div class="k-quiet">她还没有写下可以被你看到的日记。</div>';
    const noteHTML = notes.length
      ? notes.map((note, index) => '<div class="k-note"><span class="no">' + String(index + 1).padStart(2, "0")
        + "</span><span>" + fmt(note) + "</span></div>").join("")
      : '<div class="k-quiet">待整理区是空的。</div>';
    els.memoryPage.innerHTML = head
      + '<div class="k-status-note">' + esc(sharedNote) + "</div>"
      + '<div class="k-grid2">'
      + '<section class="k-card span2"><div class="k-card-head"><h3>最近的日记</h3><small>' + shown + " / " + diaries + " 篇</small></div>"
      + diaryHTML + "</section>"
      + '<section class="k-card"><div class="k-card-head"><h3>笔记本</h3><small>整理过的长期记忆</small></div>'
      + (notebook ? '<div class="k-notebook">' + fmt(notebook) + "</div>" : '<div class="k-quiet">还没有整理好的笔记。</div>')
      + "</section>"
      + '<section class="k-card"><div class="k-card-head"><h3>待整理</h3><small>' + notes.length + " 条</small></div>"
      + '<div class="k-notes">' + noteHTML + "</div></section>"
      + '<section class="k-card span2"><div class="k-card-head"><h3>关于这份记忆</h3><small>read only</small></div>'
      + "<p>这里是只读视图：网页不会修改、删除或触发她的回忆。正文最多展示最近 10 篇，单篇截断到约 1500 字；其余仍然留在她的数据目录里。</p></section>"
      + "</div>";
  }

  function pluginRows() {
    const plugins = Object.entries(state.plugins || {});
    if (!plugins.length) return '<div class="k-quiet">没有已加载的插件。</div>';
    return plugins.map((entry) => {
      const id = entry[0];
      const value = entry[1];
      const stateClass = value === "running" ? "" : value === "starting" || value === "restarting" ? " warn" : " off";
      return '<div class="k-plugin"><span class="icon">' + esc(String(id).slice(0, 2).toUpperCase()) + "</span>"
        + '<span class="name"><b>' + esc(id) + "</b><small>由核心监管 · 进程内或独立进程</small></span>"
        + '<span class="state' + stateClass + '"><i></i>' + esc(pluginWord(value)) + "</span></div>";
    }).join("");
  }

  function auditRows() {
    const filter = state.auditFilter || "all";
    const records = state.audit || [];
    const filtered = records.filter((record) => filter === "all" || (filter === "ok" ? record.ok : !record.ok));
    if (state.auditBusy && !records.length) {
      return '<div class="k-quiet">正在读取她的操作记录…</div>';
    }
    if (state.auditError) {
      return '<div class="k-quiet">操作记录读取失败：' + esc(state.auditError) + "</div>";
    }
    if (!filtered.length) {
      return '<div class="k-quiet">' + (records.length ? "没有符合条件的记录。" : "还没有工具调用记录。") + "</div>";
    }
    return '<div class="k-audit">' + filtered.map((record) =>
      '<div class="k-audit-row"><span class="dot' + (record.ok ? "" : " fail") + '"></span>'
      + '<div class="main"><b>' + esc(record.plugin) + "." + esc(record.capability) + "</b>"
      + '<span class="args">' + esc(record.args || "—") + "</span>"
      + (record.summary ? '<span class="summary">' + esc(record.summary) + "</span>" : "")
      + "</div><time>" + esc(timeOf(record.time)) + "</time></div>"
    ).join("") + "</div>";
  }

  function renderRecords() {
    const records = state.audit || [];
    const all = records.length;
    const ok = records.filter((record) => record.ok).length;
    const fail = all - ok;
    const llm = state.llm || {};
    const snapshot = state.snapshot || {};
    const filter = state.auditFilter || "all";
    const chip = (key, label, count) =>
      '<button class="k-filter' + (filter === key ? " is-active" : "") + '" data-filter="' + key + '">' + label + " <b>" + count + "</b></button>";
    els.recordsPage.innerHTML =
      '<div class="k-page-head"><div><h2>她做过的</h2><p>每一次工具调用都有记录；敏感内容永远不会出现在这里。</p></div>'
      + '<div style="display:flex;gap:6px;flex-wrap:wrap"><button class="k-btn" data-action="refresh-audit"><svg class="i"><use href="#i-refresh"/></svg> 刷新</button>'
      + '<button class="k-btn" data-go="chat"><svg class="i"><use href="#i-arrow-back"/></svg> 回到对话</button></div></div>'
      + '<div class="k-grid2">'
      + '<section class="k-card"><div class="k-card-head"><h3>操作记录</h3><small>最近 ' + (all || 0) + " 条</small></div>"
      + '<div class="k-audit-toolbar">' + chip("all", "全部", all) + chip("ok", "成功", ok) + chip("fail", "失败", fail) + "</div>"
      + auditRows()
      + '<div class="k-notice">工具调用的参数和结果在写入前已经经过脱敏与截断；这里不会出现验证码、密码或 Cookie。</div></section>'
      + '<section class="k-card"><div class="k-card-head"><h3>插件</h3><small>' + (Object.keys(state.plugins || {}).length || 0) + " 个已加载</small></div>"
      + pluginRows()
      + '<div class="k-notice">插件状态来自 <code>/api/state</code>；更细的重启次数和日志需要后续接口。</div></section>'
      + '<section class="k-card span2"><div class="k-card-head"><h3>这一轮她用了多少</h3><small>LLM meter</small></div>'
      + '<div class="k-stat-row"><div><span class="k-big">' + (Number(llm.calls) || 0) + "</span><p>LLM 调用</p></div>"
      + '<div><span class="k-big">' + (llm.cache_hit_rate == null ? "—" : percent(llm.cache_hit_rate)) + "</span><p>缓存命中</p></div>"
      + '<div><span class="k-big">' + (Number(snapshot.fatigue) || 0).toFixed(1) + "</span><p>疲劳值</p></div>"
      + '<div><span class="k-big">' + (Number(snapshot.sleep_pressure) || 0).toFixed(2) + "</span><p>困意</p></div></div>"
      + '<div class="k-notice">这些数字只在这里出现，不进入聊天主界面。它们回答的是“她累不累”，不是“系统健不健康”。</div></section>'
      + "</div>";
  }

  /* ------------------------------------------------------------------ data */

  function addSample(snapshot) {
    if (!snapshot) return;
    const now = Date.now();
    const last = state.samples[state.samples.length - 1];
    if (last && now - last.t < 3000) return;
    state.samples.push({
      t: now,
      energy: clamp(Number(snapshot.energy) || 0, 0, 100)
    });
    if (state.samples.length > MAX_SAMPLES) state.samples.shift();
  }

  function applyState(data) {
    if (!data || typeof data !== "object") throw new Error("状态格式不对");
    const previousInitial = state.initial;
    state.name = data.name || "Koi";
    state.initial = initialOf(state.name);
    state.snapshot = data.snapshot || null;
    state.thought = data.thought || null;
    state.llm = data.llm || {};
    state.memory = data.memory || {};
    state.sleep = data.sleep || null;
    state.plugins = data.plugins || {};
    state.serverPending = {
      approvals: Number(data.pending_approvals) || 0,
      interactions: Number(data.pending_interactions) || 0
    };
    addSample(state.snapshot);
    if (previousInitial !== state.initial) {
      paintMedallions();
      renderMessages();
    }
    const wasTyping = state.typing;
    state.typing = computeTyping();
    renderTop();
    renderSide();
    if (wasTyping !== state.typing) renderMessages();
    if (state.view === "status") renderStatus();
  }

  async function loadState() {
    const data = await api("/api/state");
    applyState(data);
  }

  function normalizeMessage(message) {
    const role = message.role === "owner" || message.role === "koi" ? message.role : "system";
    return {
      id: message.id || "",
      role,
      text: String(message.text || ""),
      time: message.time || new Date().toISOString()
    };
  }

  async function loadHistory() {
    const data = await api("/api/history?limit=60");
    const list = Array.isArray(data) ? data : (data && data.messages) || [];
    if (!Array.isArray(list)) return;
    const server = list.map(normalizeMessage);
    const pending = state.messages.filter((message) => message.pending || message.failed);
    const serverOwner = new Set(server.filter((message) => message.role === "owner").map((message) => message.text));
    const kept = pending.filter((message) => !(message.role === "owner" && serverOwner.has(message.text)));
    state.messages = server.concat(kept);
    state.seen = new Set(server.map(messageKey));
    state.historyHasMore = Boolean(data && data.has_more);
    state.historyNextBefore = (data && data.next_before) || null;
    state.historyLoading = false;
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === "koi") {
      state.awaiting = false;
      if (awaitTimer) {
        window.clearTimeout(awaitTimer);
        awaitTimer = null;
      }
    }
    renderMessages();
  }

  function visibleMessageAnchor() {
    const top = els.log.getBoundingClientRect().top;
    const nodes = els.log.querySelectorAll("[data-msg-id]");
    for (const node of nodes) {
      if (node.getBoundingClientRect().bottom > top + 1) {
        return { id: node.dataset.msgId, top: node.getBoundingClientRect().top, scrollTop: els.log.scrollTop };
      }
    }
    return null;
  }

  async function loadOlder() {
    if (!state.historyHasMore || state.historyLoading) return;
    const oldest = state.messages.find((message) => message.id);
    if (!oldest || !oldest.id) return;
    const anchor = visibleMessageAnchor();
    const oldHeight = els.log.scrollHeight;
    const oldTop = els.log.scrollTop;
    state.historyLoading = true;
    renderMessages();
    try {
      const data = await api("/api/history?limit=60&before=" + encodeURIComponent(oldest.id));
      const list = ((data && data.messages) || []).map(normalizeMessage);
      const existing = new Set(state.messages.map(messageKey));
      const older = list.filter((message) => !existing.has(messageKey(message)));
      older.forEach((message) => state.seen.add(messageKey(message)));
      state.messages = older.concat(state.messages);
      state.historyHasMore = Boolean(data && data.has_more);
      state.historyNextBefore = (data && data.next_before) || null;
      state.historyLoading = false;
      renderMessages();
      const next = anchor
        ? Array.from(els.log.querySelectorAll("[data-msg-id]")).find((node) => node.dataset.msgId === anchor.id)
        : null;
      const previousBehavior = els.log.style.scrollBehavior;
      els.log.style.scrollBehavior = "auto";
      els.log.scrollTop = anchor && next
        ? anchor.scrollTop + (next.getBoundingClientRect().top - anchor.top)
        : els.log.scrollHeight - oldHeight + oldTop;
      requestAnimationFrame(() => {
        els.log.style.scrollBehavior = previousBehavior;
      });
    } catch (err) {
      state.historyLoading = false;
      if (err && err.status === 404) state.historyHasMore = false;
      renderMessages();
      toast("读取更早消息失败：" + err.message, "warn");
    }
  }

  async function refreshPending() {
    const results = await Promise.allSettled([
      api("/api/approvals"),
      api("/api/interactions")
    ]);
    let any = false;
    if (results[0].status === "fulfilled" && Array.isArray(results[0].value)) {
      state.approvals = results[0].value;
      any = true;
    }
    if (results[1].status === "fulfilled" && Array.isArray(results[1].value)) {
      state.interactions = results[1].value;
      any = true;
    }
    if (!any) throw (results[0].reason || results[1].reason || new Error("读取待办失败"));
    state.pendingLoaded = true;
    state.serverPending = {
      approvals: state.approvals.length,
      interactions: state.interactions.length
    };
    renderAttention();
    renderTop();
  }

  async function loadAudit() {
    if (state.auditBusy) return;
    state.auditBusy = true;
    if (state.view === "records") renderRecords();
    try {
      const list = await api("/api/audit?limit=30");
      state.audit = Array.isArray(list) ? list : [];
      state.auditError = "";
    } catch (err) {
      state.auditError = err.message;
      if (state.view === "records") toast("读取操作记录失败：" + err.message, "warn");
    } finally {
      state.auditBusy = false;
      if (state.view === "records") renderRecords();
    }
  }

  async function loadMemory() {
    if (state.memoryLoading) return;
    state.memoryLoading = true;
    state.memoryError = "";
    if (state.view === "memory") renderMemory();
    try {
      state.memoryView = await api("/api/memory");
    } catch (err) {
      state.memoryView = null;
      state.memoryError = err.message;
      toast("读取记忆失败：" + err.message, "warn");
    } finally {
      state.memoryLoading = false;
      if (state.view === "memory") renderMemory();
    }
  }

  /* ------------------------------------------------------------------ stream */

  function setConnected(connected, quiet) {
    state.connected = connected;
    els.connBadge.classList.toggle("is-off", !connected);
    els.connTextTop.textContent = connected ? "本机 · 已连接" : "本机 · 重连中";
    els.banner.hidden = connected || quiet;
    if (!connected) els.connText.textContent = "没连上她。正在重试…";
    updateComposer();
    if (state.snapshot) renderTop();
  }

  function scheduleReconnect() {
    if (retryTimer) window.clearTimeout(retryTimer);
    const delay = Math.min(15000, 800 * Math.pow(2, retryCount));
    retryCount += 1;
    els.retryIn.textContent = Math.ceil(delay / 1000) + " 秒后重试";
    els.connText.textContent = "没连上她。正在重试…";
    retryTimer = window.setTimeout(() => connect(false), delay);
  }

  function connect(manual) {
    if (manual) retryCount = 0;
    if (retryTimer) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (es) {
      try { es.close(); } catch (err) { /* ignore */ }
      es = null;
    }
    if (!("EventSource" in window)) {
      setConnected(false);
      els.connText.textContent = "浏览器不支持实时连接，请换一个现代浏览器。";
      return;
    }
    els.retryIn.textContent = "";
    if (!manual) els.connText.textContent = "正在重新连接…";
    setConnected(false, true);
    try {
      es = new EventSource("/api/stream");
    } catch (err) {
      setConnected(false);
      scheduleReconnect();
      return;
    }
    es.onopen = () => {
      retryCount = 0;
      if (retryTimer) {
        window.clearTimeout(retryTimer);
        retryTimer = null;
      }
      setConnected(true);
      els.retryIn.textContent = "";
      loadState().catch(() => {});
      loadHistory().catch(() => {});
      refreshPending().catch(() => {});
    };
    es.onerror = () => {
      setConnected(false);
      if (es) {
        try { es.close(); } catch (err) { /* ignore */ }
        es = null;
      }
      scheduleReconnect();
    };
    es.addEventListener("state", (event) => {
      try {
        applyState(JSON.parse(event.data));
      } catch (err) {
        /* malformed state: wait for the next poll */
      }
    });
    es.onmessage = (event) => {
      try {
        handleIncoming(JSON.parse(event.data));
      } catch (err) {
        /* malformed event: ignore */
      }
    };
  }

  function findLocalOwner(text) {
    for (let i = state.messages.length - 1; i >= 0; i -= 1) {
      const message = state.messages[i];
      if (message.role === "owner" && message.local && message.text === text) return message;
    }
    return null;
  }

  function handleIncoming(raw) {
    if (!raw || (raw.role !== "owner" && raw.role !== "koi" && raw.role !== "system")) return;
    const message = normalizeMessage(raw);
    if (message.role === "owner") {
      const local = findLocalOwner(message.text);
      if (local) {
        local.id = message.id || local.id;
        local.role = message.role;
        local.text = message.text;
        local.time = message.time;
        local.pending = false;
        local.failed = false;
        local.local = false;
        state.seen.add(messageKey(local));
        renderMessages();
        return;
      }
    }
    if (state.seen.has(messageKey(message))) return;
    state.seen.add(messageKey(message));
    state.messages.push(message);
    if (message.role === "koi") {
      state.awaiting = false;
      if (awaitTimer) {
        window.clearTimeout(awaitTimer);
        awaitTimer = null;
      }
    } else if (message.role === "system" && /失败|出错|错误|没想成/.test(message.text)) {
      state.awaiting = false;
      if (awaitTimer) {
        window.clearTimeout(awaitTimer);
        awaitTimer = null;
      }
    }
    if (message.role !== "owner" && els.live) {
      els.live.textContent = (message.role === "koi" ? state.name : "系统") + "：" + message.text;
    }
    renderMessages();
    if (message.role !== "owner") {
      renderTop();
      loadState().catch(() => {});
    }
  }

  /* ------------------------------------------------------------------ send */

  function scheduleAwaitTimeout() {
    if (awaitTimer) window.clearTimeout(awaitTimer);
    awaitTimer = window.setTimeout(() => {
      if (!state.awaiting) return;
      state.awaiting = false;
      renderMessages();
    }, AWAIT_TIMEOUT);
  }

  async function sendMessage() {
    const text = els.input.value.trim();
    if (!text) return;
    if (!state.connected) {
      toast("还没连上她，消息没有发出去", "warn");
      return;
    }
    if (text.length > MAX_MESSAGE) return;
    els.input.value = "";
    updateComposer();
    state.awaiting = true;
    const optimistic = {
      role: "owner", text: text, time: new Date().toISOString(),
      pending: true, failed: false, local: true
    };
    state.messages.push(optimistic);
    renderMessages(true);
    try {
      await api("/api/messages", {
        method: "POST",
        body: JSON.stringify({ text: text })
      });
      optimistic.pending = false;
      renderMessages();
      scheduleAwaitTimeout();
    } catch (err) {
      optimistic.pending = false;
      optimistic.failed = true;
      state.awaiting = false;
      if (!els.input.value) {
        els.input.value = text;
        updateComposer();
      }
      renderMessages();
      toast("没发出去：" + err.message, "warn");
    }
  }

  /* ------------------------------------------------------------------ toast */

  function toast(text, kind) {
    const el = document.createElement("div");
    el.className = "k-toast" + (kind === "warn" ? " warn" : "");
    el.innerHTML = '<svg class="i"><use href="#' + (kind === "warn" ? "i-alert" : "i-check") + '"/></svg><span>'
      + esc(text) + "</span>";
    els.toasts.appendChild(el);
    while (els.toasts.children.length > 3) els.toasts.firstElementChild.remove();
    window.setTimeout(() => {
      el.style.transition = "opacity .2s";
      el.style.opacity = "0";
      window.setTimeout(() => el.remove(), 220);
    }, 3000);
  }

  /* ------------------------------------------------------------------ attention actions */

  function cardBusy(card, busy) {
    if (!card) return;
    card.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  }

  function cardError(card, message) {
    if (!card) return;
    const box = card.querySelector("[data-error]");
    if (!box) return;
    box.textContent = message;
    box.hidden = false;
  }

  async function resolveApproval(id, approved, card) {
    if (!id) return;
    cardBusy(card, true);
    try {
      await api("/api/approvals/" + encodeURIComponent(id), {
        method: "POST",
        body: JSON.stringify({ approved: approved })
      });
    } catch (err) {
      if (err.status === 409) {
        lastAttentionSig = "";
        refreshPending().catch(() => {});
        toast("这个请求已经处理过了", "warn");
      } else {
        cardBusy(card, false);
        cardError(card, err.message);
        toast("处理失败：" + err.message, "warn");
      }
      return;
    }
    toast(approved ? "已批准，她会自己继续" : "已拒绝，她不会继续", approved ? "" : "warn");
    lastAttentionSig = "";
    refreshPending().catch(() => {});
    loadState().catch(() => {});
  }

  async function submitInteraction(interaction, card) {
    if (!interaction || !card) return;
    const values = {};
    let missing = false;
    card.querySelectorAll("[data-field]").forEach((input) => {
      const value = input.value.trim();
      if (!value) missing = true;
      values[input.dataset.field] = input.value;
    });
    if (missing) {
      cardError(card, "请先把需要填写的内容补全。");
      return;
    }
    cardBusy(card, true);
    try {
      await api("/api/interactions/" + encodeURIComponent(interaction.id), {
        method: "POST",
        body: JSON.stringify({ values: values })
      });
    } catch (err) {
      cardBusy(card, false);
      cardError(card, err.message);
      toast("提交失败：" + err.message, "warn");
      return;
    }
    toast("已交给插件，她没有看到具体内容");
    lastAttentionSig = "";
    refreshPending().catch(() => {});
    loadState().catch(() => {});
  }

  async function cancelInteraction(id, card) {
    if (!id) return;
    cardBusy(card, true);
    try {
      await api("/api/interactions/" + encodeURIComponent(id) + "/cancel", { method: "POST" });
    } catch (err) {
      cardBusy(card, false);
      cardError(card, err.message);
      toast("取消失败：" + err.message, "warn");
      return;
    }
    toast("已取消", "warn");
    lastAttentionSig = "";
    refreshPending().catch(() => {});
    loadState().catch(() => {});
  }

  function handleAttentionClick(event) {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    const card = button.closest(".k-attn[data-approval-id]");
    if (card) {
      resolveApproval(card.dataset.approvalId, button.dataset.act === "approve", card);
      return;
    }
    const interactionCardEl = button.closest(".k-attn[data-interaction-id]");
    if (!interactionCardEl) return;
    const id = interactionCardEl.dataset.interactionId;
    const interaction = state.interactions.find((item) => String(item.id) === String(id));
    if (button.dataset.act === "submit") submitInteraction(interaction, interactionCardEl);
    else if (button.dataset.act === "cancel") cancelInteraction(id, interactionCardEl);
  }

  /* ------------------------------------------------------------------ popovers */

  function closePop() {
    if (popEl) {
      popEl.remove();
      popEl = null;
    }
  }

  function openPop(html) {
    closePop();
    popEl = document.createElement("div");
    popEl.className = "k-pop";
    popEl.innerHTML = html;
    app.appendChild(popEl);
    popEl.addEventListener("click", handlePopClick);
    const first = popEl.querySelector("button");
    if (first) first.focus({ preventScroll: true });
  }

  function handlePopClick(event) {
    const item = event.target.closest("[data-cmd]");
    if (!item) return;
    const command = item.dataset.cmd;
    closePop();
    if (command === "chat" || command === "status" || command === "memory" || command === "records") {
      setView(command);
    } else if (command === "theme") {
      toggleTheme();
    } else if (command === "reconnect") {
      retryCount = 0;
      connect(true);
      toast("正在重新连接…");
    } else if (command === "latest") {
      setView("chat");
      scrollBottom("smooth");
    } else if (command === "attention") {
      openSide();
    }
  }

  function openCmd() {
    const item = (cmd, icon, label, key) =>
      '<button class="k-pop-item" data-cmd="' + cmd + '"><svg class="i"><use href="#' + icon + '"/></svg>'
      + label + '<span class="right">' + key + "</span></button>";
    openPop(
      '<div class="k-pop-title">快捷操作</div>'
      + item("chat", "i-chat", "回到对话", "1")
      + item("status", "i-pulse", "她的此刻", "2")
      + item("memory", "i-book", "她记得的", "3")
      + item("records", "i-list", "她做过的", "4")
      + '<div class="k-pop-sep"></div>'
      + item("theme", "i-moon", "切换昼 / 夜", "T")
      + item("reconnect", "i-refresh", "重新连接", "R")
      + item("latest", "i-chevron", "回到最新", "End")
    );
  }

  function openBell() {
    const pending = pendingCount();
    if (pending) {
      const items = state.approvals.slice(0, 3).map((approval) =>
        "<div class=\"k-pop-note\">" + esc(approval.action) + " · " + esc(relative(approval.created_at)) + "</div>"
      ).join("");
      const interactions = state.interactions.slice(0, 3).map((interaction) =>
        "<div class=\"k-pop-note\">" + esc(interaction.title) + "</div>"
      ).join("");
      openPop('<div class="k-pop-title">等你 · ' + pending + "</div>" + items + interactions
        + '<button class="k-btn primary k-pop-cta" data-cmd="attention">去处理</button>');
    } else {
      openPop('<div class="k-pop-title">通知</div>'
        + '<div class="k-pop-note">她这会儿没有找你。你可以先看看她今天过得怎么样。</div>'
        + '<button class="k-btn k-pop-cta" data-cmd="status">看看她的此刻</button>');
    }
  }

  /* ------------------------------------------------------------------ view */

  function setView(view) {
    if (!VIEW_META[view]) view = "chat";
    state.view = view;
    app.dataset.view = view;
    app.querySelectorAll(".k-navbtn").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.go === view);
    });
    app.querySelectorAll(".k-bnav").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.go === view);
    });
    updateViewMeta();
    closeSide();
    closePop();
    if (view === "status") renderStatus();
    if (view === "memory") {
      renderMemory();
      loadMemory();
    }
    if (view === "records") {
      renderRecords();
      loadAudit().catch(() => {});
    }
    if (view === "chat") {
      requestAnimationFrame(() => scrollBottom("auto"));
    }
  }

  function openSide() {
    if (state.view !== "chat") setView("chat");
    app.classList.add("side-open");
    els.side.classList.add("is-open");
    els.backdrop.hidden = false;
    updateCountdowns();
    if (els.sideClose.offsetWidth) {
      els.sideClose.focus({ preventScroll: true });
    }
  }

  function closeSide() {
    app.classList.remove("side-open");
    els.side.classList.remove("is-open");
    els.backdrop.hidden = true;
  }

  function manualReconnect() {
    retryCount = 0;
    connect(true);
    toast("正在重新连接…");
  }

  /* ------------------------------------------------------------------ boot */

  async function boot() {
    els.boot.hidden = false;
    els.bootText.textContent = "正在连接她…";
    els.bootErr.hidden = true;
    els.bootRetry.hidden = true;
    try {
      await loadState();
    } catch (err) {
      els.bootText.textContent = "没能连上她。";
      els.bootErr.textContent = err.message || "检查 koi 是否还在运行。";
      els.bootErr.hidden = false;
      els.bootRetry.hidden = false;
      return;
    }
    const results = await Promise.allSettled([loadHistory(), refreshPending()]);
    paintMedallions();
    renderTop();
    renderSide();
    renderMessages(true);
    renderAttention();
    renderStatus();
    renderMemory();
    renderRecords();
    els.boot.hidden = true;
    setConnected(false, true);
    connect(false);
    startPolling();
    updateComposer();
    if (results.some((result) => result.status === "rejected")) {
      toast("部分历史消息没有读出来，稍后会自动重试", "warn");
    }
  }

  function startPolling() {
    if (started) return;
    started = true;

    const stateLoop = async () => {
      if (!document.hidden) {
        try { await loadState(); } catch (err) { /* banner covers it */ }
      }
      const delay = state.connected ? (state.awaiting ? 6000 : 15000) : (state.awaiting ? 1800 : 5000);
      window.setTimeout(stateLoop, delay);
    };
    const pendingLoop = async () => {
      if (!document.hidden) {
        try { await refreshPending(); } catch (err) { /* banner covers it */ }
      }
      window.setTimeout(pendingLoop, 2000);
    };
    const auditLoop = async () => {
      if (!document.hidden && state.view === "records") {
        const focused = document.activeElement && document.activeElement.closest
          ? document.activeElement.closest("#recordsPage")
          : null;
        if (!focused) await loadAudit();
      }
      window.setTimeout(auditLoop, 10000);
    };
    window.setTimeout(stateLoop, 5000);
    window.setTimeout(pendingLoop, 2000);
    window.setTimeout(auditLoop, 10000);
    window.setInterval(updateCountdowns, 1000);
  }

  /* ------------------------------------------------------------------ events */

  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });

  els.input.addEventListener("input", updateComposer);
  els.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });

  els.wake.addEventListener("click", () => {
    els.input.focus();
    toast("发一句话就会叫醒她");
  });

  els.jump.addEventListener("click", () => scrollBottom("smooth"));

  els.log.addEventListener("scroll", () => {
    els.jump.hidden = isNearBottom();
  });

  els.attention.addEventListener("click", handleAttentionClick);
  els.attention.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    const input = event.target.closest("[data-field]");
    if (!input) return;
    const card = input.closest("[data-interaction-id]");
    if (!card) return;
    event.preventDefault();
    const button = card.querySelector('[data-act="submit"]');
    if (button) button.click();
  });

  app.addEventListener("click", (event) => {
    const go = event.target.closest("[data-go]");
    if (go) {
      event.preventDefault();
      setView(go.dataset.go);
      return;
    }
    const prompt = event.target.closest("[data-prompt]");
    if (prompt) {
      els.input.value = prompt.dataset.prompt;
      els.input.focus();
      updateComposer();
      return;
    }
    if (event.target.closest("[data-more]")) {
      loadOlder();
      return;
    }
    const filter = event.target.closest("[data-filter]");
    if (filter) {
      state.auditFilter = filter.dataset.filter;
      renderRecords();
      requestAnimationFrame(() => {
        const next = els.recordsPage.querySelector('[data-filter="' + state.auditFilter + '"]');
        if (next) next.focus({ preventScroll: true });
      });
      return;
    }
    const action = event.target.closest("[data-action]");
    if (action && action.dataset.action === "refresh-audit") {
      loadAudit();
      return;
    }
    if (action && action.dataset.action === "refresh-memory") {
      loadMemory();
    }
  });

  els.sideClose.addEventListener("click", closeSide);
  els.backdrop.addEventListener("click", closeSide);
  els.openSide.addEventListener("click", openSide);
  els.msPending.addEventListener("click", openSide);
  els.pinbarBtn.addEventListener("click", openSide);
  els.railAvatar.addEventListener("click", openSide);
  els.topAvatar.addEventListener("click", openSide);

  els.theme.addEventListener("click", toggleTheme);
  els.railTheme.addEventListener("click", toggleTheme);
  els.cmd.addEventListener("click", () => {
    if (popEl) closePop();
    else openCmd();
  });
  els.bell.addEventListener("click", () => {
    if (popEl) closePop();
    else openBell();
  });
  els.retry.addEventListener("click", manualReconnect);
  els.bootRetry.addEventListener("click", boot);

  document.addEventListener("click", (event) => {
    if (!popEl) return;
    if (event.target.closest(".k-pop")) return;
    if (event.target.closest("#cmdBtn") || event.target.closest("#bellBtn")) return;
    closePop();
  });

  document.addEventListener("keydown", (event) => {
    const typing = event.target && event.target.closest && event.target.closest("input, textarea, select");
    if (event.key === "Escape") {
      closePop();
      closeSide();
      if (typing) event.target.blur();
      return;
    }
    if (typing) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (popEl) closePop();
      else openCmd();
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const views = { "1": "chat", "2": "status", "3": "memory", "4": "records" };
    if (views[event.key]) {
      event.preventDefault();
      setView(views[event.key]);
    } else if (event.key.toLowerCase() === "t") {
      event.preventDefault();
      toggleTheme();
    } else if (event.key.toLowerCase() === "r") {
      event.preventDefault();
      manualReconnect();
    } else if (event.key === "End") {
      event.preventDefault();
      setView("chat");
      scrollBottom("smooth");
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    loadState().catch(() => {});
    refreshPending().catch(() => {});
    if (!state.connected) connect(false);
  });

  window.addEventListener("resize", () => {
    closePop();
    if (app.clientWidth > 1020) closeSide();
  });

  window.addEventListener("beforeunload", () => {
    if (es) {
      try { es.close(); } catch (err) { /* ignore */ }
    }
  });

  /* ------------------------------------------------------------------ start */

  state = {
    name: "Koi",
    initial: "K",
    view: "chat",
    snapshot: null,
    thought: null,
    llm: {},
    memory: {},
    plugins: {},
    messages: [],
    approvals: [],
    interactions: [],
    audit: [],
    auditFilter: "all",
    auditBusy: false,
    auditError: "",
    historyHasMore: false,
    historyNextBefore: null,
    historyLoading: false,
    memoryView: null,
    memoryLoading: false,
    memoryError: "",
    sleep: null,
    pendingLoaded: false,
    serverPending: { approvals: 0, interactions: 0 },
    samples: [],
    seen: new Set(),
    typing: false,
    awaiting: false,
    connected: false
  };
  paintMedallions();
  setTheme(preferredTheme());
  updateComposer();
  boot();
})();
