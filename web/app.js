/* server-agent 控制台：把第 05 章的 SSE 事件流渲染成时间线。
   零依赖、无构建：一个 HTML、一个 CSS、一个 JS。 */

const $ = (id) => document.getElementById(id);
const state = {
  token: localStorage.getItem("sa_token") || "",
  currentRun: null,
  es: null,          // 当前 EventSource
  lastSeq: 0,        // 已收到的事件序号，断线重连时用于续传
  textNode: null,    // 正在流式追加的文本节点
  suppress: false,   // 当前这轮回答看起来是 JSON（结构化报告），不再逐字渲染
  cards: {},         // tool_call id -> 卡片元素
  busy: false,
};

/* ---------- 基础工具 ---------- */
function authHeaders() {
  return state.token ? { Authorization: "Bearer " + state.token } : {};
}

function withToken(url) {
  if (!state.token) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(state.token);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* 非 JSON 响应 */ }
    throw new Error(res.status + " " + detail);
  }
  return res.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollToBottom() {
  const t = $("timeline");
  t.scrollTop = t.scrollHeight;
}

/* ---------- 时间线渲染 ---------- */
function clearTimeline() {
  $("timeline").innerHTML = "";
  state.textNode = null;
  state.cards = {};
}

function addUserBubble(text) {
  const wrap = el("div", "bubble user");
  wrap.append(el("div", "body", text));
  $("timeline").append(wrap);
  scrollToBottom();
}

function addStep(n) {
  state.textNode = null;
  state.suppress = false;   // 新步骤：重置「正在输出 JSON」标记
  $("timeline").append(el("div", "step-line", "第 " + n + " 步"));
  scrollToBottom();
}

/* 流式文本：同一段回答追加到同一个气泡，遇到新步骤则另起一个 */
function appendText(text, final = false) {
  // 结构化报告是一整块 JSON，逐字塞进气泡只是噪音；识别出来就不再渲染（交给报告卡片）
  if (state.suppress) return;
  if (!state.textNode && text.trimStart().startsWith("{")) {
    state.suppress = true;
    const bubble = el("div", "bubble");
    bubble.append(el("div", "body", "正在生成结构化报告…"));
    $("timeline").append(bubble);
    return;
  }
  if (!state.textNode) {
    const bubble = el("div", "bubble" + (final ? " final" : ""));
    const body = el("div", "body");
    bubble.append(body);
    $("timeline").append(bubble);
    state.textNode = body;
  }
  state.textNode.textContent += text;
  scrollToBottom();
}

function formatArgs(args) {
  try { return JSON.stringify(JSON.parse(args), null, 2); } catch (e) { return args || "{}"; }
}

function pretty(text) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return text; }
}

function toolCard(id, name, args) {
  const card = el("details", "card");
  const summary = el("summary");
  summary.append(el("span", null, name));
  summary.append(el("span", "tag warn", "调用中"));
  card.append(summary);
  card.append(el("pre", "payload", formatArgs(args)));
  $("timeline").append(card);
  state.cards[id] = { card, summary, payload: card.querySelector(".payload") };
  scrollToBottom();
  return card;
}

function finishToolCard(id, data) {
  const entry = state.cards[id];
  if (!entry) return;
  entry.summary.replaceChildren(el("span", null, data.name));
  entry.summary.append(el("span", "tag " + (data.ok ? "ok" : "bad"),
    data.ok ? "完成 " + (data.elapsed_ms === undefined ? "?" : data.elapsed_ms) + "ms" : "失败"));
  if (data.truncated) entry.summary.append(el("span", "tag warn", "已截断"));
  if (data.skipped) {
    entry.summary.append(el("span", "tag warn",
      data.skipped === "repeat" ? "重复调用已跳过" : "参数非法"));
  }
  entry.payload.append(el("div", "stats", "回喂模型 " + data.chars + " 字符"));
  entry.payload.append(el("pre", null, pretty(data.content)));
  scrollToBottom();
}

function reportCard(data) {
  const card = el("div", "bubble");
  const body = el("div", "body");
  body.style.borderColor = data.parsed ? "#bfe6d8" : "var(--warn)";
  if (!data.parsed) {
    body.textContent = "结论未能解析为结构化报告：" + (data.error || "未知原因") + "\n" + (data.raw || "");
    card.append(body);
    $("timeline").append(card);
    scrollToBottom();
    return;
  }
  const r = data.report;
  const head = el("div", "stats", "诊断报告 · " + r.severity + " · 置信度 " + r.confidence);
  body.append(head);
  body.append(el("div", null, "现象：" + r.summary));
  if (r.root_cause) body.append(el("div", null, "根因：" + r.root_cause));
  if (r.findings && r.findings.length) {
    body.append(el("div", "stats", "观察与依据"));
    r.findings.forEach((f) => body.append(el("div", null, "· " + f.claim + "（" + f.evidence + "）")));
  }
  if (r.actions && r.actions.length) {
    body.append(el("div", "stats", "建议动作"));
    r.actions.forEach((a) => {
      body.append(el("div", null, "· [" + a.risk + "] " + a.description + (a.command ? "  → " + a.command : "")));
    });
  }
  if (r.data_gaps && r.data_gaps.length) {
    body.append(el("div", "stats", "还缺信息：" + r.data_gaps.join("；")));
  }
  card.append(body);
  $("timeline").append(card);
  scrollToBottom();
}

function addError(message) {
  const wrap = el("div", "bubble");
  const body = el("div", "body");
  body.style.borderColor = "var(--danger)";
  body.textContent = "错误：" + message;
  wrap.append(body);
  $("timeline").append(wrap);
  scrollToBottom();
}

function formatEnd(data) {
  const u = data.usage || {};
  const names = {
    final: "完成", max_steps: "达到最大步数", timeout: "超时",
    length: "输出被截断", error: "出错", cancelled: "已取消",
  };
  return (names[data.stopped] || data.stopped) + " · " + data.steps + " 步 · "
    + data.tool_calls + " 次工具调用 · " + data.elapsed_ms + "ms · token 输入 "
    + (u.prompt_tokens || 0) + " / 输出 " + (u.completion_tokens || 0);
}

function handleEvent(ev) {
  const d = ev.data;
  if (ev.type === "start") {
    state.textNode = null;
  } else if (ev.type === "step") {
    addStep(d.step);
  } else if (ev.type === "text") {
    appendText(d.text);
  } else if (ev.type === "tool_call") {
    toolCard(d.id, d.name, d.arguments);
  } else if (ev.type === "tool_result") {
    finishToolCard(d.id, d);
  } else if (ev.type === "report") {
    reportCard(d);
  } else if (ev.type === "error") {
    addError(d.message);
  } else if (ev.type === "end") {
    if (d.text) appendText("", true);   // 把当前气泡标记为「最终回答」样式
    $("timeline").append(el("div", "stats", formatEnd(d)));
    setBusy(false);
    loadRuns();
  }
  scrollToBottom();
}

/* ---------- SSE 订阅 ---------- */
const EVENT_TYPES = ["start", "step", "reasoning", "text", "tool_call", "tool_result",
  "report", "error", "end"];

function subscribe(runId, afterSeq = 0) {
  unsubscribe();
  state.currentRun = runId;
  state.lastSeq = afterSeq;
  const es = new EventSource(withToken("/api/runs/" + runId + "/events?last_event_id=" + afterSeq));
  state.es = es;
  EVENT_TYPES.forEach((type) => {
    es.addEventListener(type, (e) => {
      state.lastSeq = Number(e.lastId || state.lastSeq);
      handleEvent({ type, data: JSON.parse(e.data) });
      if (type === "end") es.close();
    });
  });
  es.onopen = () => { $("health-text").textContent = "事件流已连接"; };
  es.onerror = () => {
    // 浏览器会自动重连并带上 Last-Event-ID；这里只更新提示，并在彻底关闭时手动补一次
    $("health-text").textContent = "事件流中断，重连中…";
    setTimeout(() => {
      if (state.currentRun === runId && state.es && state.es.readyState === EventSource.CLOSED) {
        subscribe(runId, state.lastSeq);
      }
    }, 1500);
  };
}

function unsubscribe() {
  if (state.es) { state.es.close(); state.es = null; }
}

/* ---------- 交互 ---------- */
function setBusy(busy) {
  state.busy = busy;
  $("btn-send").disabled = busy;
  $("btn-stop").disabled = !busy;
}

async function ask(question) {
  setBusy(true);
  clearTimeline();
  addUserBubble(question);
  try {
    const created = await api("/api/runs", { method: "POST", body: JSON.stringify({ input: question }) });
    subscribe(created.id, 0);
    loadRuns();
  } catch (e) {
    addError(e.message);
    setBusy(false);
  }
}

async function stop() {
  if (!state.currentRun) return;
  try {
    await api("/api/runs/" + state.currentRun + "/cancel", { method: "POST" });
  } catch (e) {
    addError(e.message);
  } finally {
    setBusy(false);
  }
}

function statusText(status) {
  return { running: "运行中", done: "已完成", error: "出错", cancelled: "已取消" }[status] || status;
}

async function loadRuns() {
  try {
    const data = await api("/api/runs");
    const list = $("run-list");
    list.replaceChildren();
    data.runs.forEach((run) => {
      const li = el("li");
      if (run.id === state.currentRun) li.classList.add("active");
      li.append(el("div", "t", run.input));
      li.append(el("div", "s", statusText(run.status) + " · " + run.events + " 事件"));
      li.onclick = () => openRun(run.id);
      list.append(li);
    });
  } catch (e) { /* 列表加载失败不打断主流程 */ }
}

function openRun(runId) {
  clearTimeline();
  setBusy(true);
  loadRuns();
  subscribe(runId, 0);   // after_seq=0：服务端补发全部历史事件
}

async function loadTools() {
  const data = await api("/api/tools");
  const list = $("tool-list");
  list.replaceChildren();
  data.tools.forEach((t) => {
    const li = el("li");
    li.append(el("div", "name", t.name + "  [" + t.risk + "]"));
    li.append(el("div", "desc", t.description));
    list.append(li);
  });
}

async function checkHealth() {
  try {
    const res = await fetch("/health");
    const h = await res.json();
    $("health-dot").className = "dot ok";
    $("health-text").textContent = "v" + h.version + (h.auth ? "（已启用鉴权）" : "");
  } catch (e) {
    $("health-dot").className = "dot bad";
    $("health-text").textContent = "服务不可用";
  }
}

/* ---------- 绑定事件 ---------- */
$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = $("input").value.trim();
  if (!value || state.busy) return;
  $("input").value = "";
  ask(value);
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("ask-form").requestSubmit();
  }
});
$("btn-stop").onclick = stop;
$("btn-refresh").onclick = loadRuns;
$("btn-tools").onclick = async () => {
  $("drawer").classList.remove("hidden");
  try { await loadTools(); } catch (e) { addError(e.message); }
};
$("btn-drawer-close").onclick = () => $("drawer").classList.add("hidden");
$("token").value = state.token;
$("token").addEventListener("change", (e) => {
  state.token = e.target.value.trim();
  localStorage.setItem("sa_token", state.token);
  checkHealth();
});

checkHealth();
loadRuns();
