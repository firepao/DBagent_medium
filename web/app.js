const form = document.querySelector("#queryForm"),
  input = document.querySelector("#question"),
  send = document.querySelector("#sendButton"),
  conversation = document.querySelector("#conversation"),
  details = document.querySelector("#runDetails"),
  runStatus = document.querySelector("#runStatus"),
  copyRequestId = document.querySelector("#copyRequestId"),
  cancelRun = document.querySelector("#cancelRun"),
  resumeRun = document.querySelector("#resumeRun"),
  welcomeMarkup = conversation.innerHTML;
let currentRequestId = "",
  currentRunId = "";
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>'"]/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[
        char
      ],
  );
function setStatus(text, type) {
  ((runStatus.textContent = text), (runStatus.className = `status ${type}`));
}
function recentQuestions() {
  try {
    return JSON.parse(sessionStorage.getItem("recentQuestions") || "[]");
  } catch {
    return [];
  }
}
function saveRecentQuestion(question) {
  const items = [
    question,
    ...recentQuestions().filter((item) => item !== question),
  ].slice(0, 5);
  sessionStorage.setItem("recentQuestions", JSON.stringify(items));
}
function renderRecentQuestions() {
  const container = document.querySelector("#recentQuestions"),
    list = document.querySelector("#recentQuestionList");
  if (!container || !list) return;
  const items = recentQuestions();
  ((container.hidden = !items.length),
    (list.innerHTML = items
      .map(
        (item) =>
          `<button class="recent-question" type="button" data-recent-question="${escapeHtml(item)}">${escapeHtml(item)}</button>`,
      )
      .join("")),
    list
      .querySelectorAll("[data-recent-question]")
      .forEach(
        (button) => (button.onclick = () => ask(button.dataset.recentQuestion)),
      ),
    (document.querySelector("#clearHistory").onclick = () => {
      (sessionStorage.removeItem("recentQuestions"), renderRecentQuestions());
    }));
}
function resetConversation() {
  send.disabled ||
    ((sessionId = null),
    sessionStorage.removeItem("querySessionId"),
    (conversation.innerHTML = welcomeMarkup),
    (details.className = "empty-details"),
    (details.innerHTML =
      '<div class="empty-icon">i</div><p>提交问题后，这里会显示请求编号、数据范围、来源和限制。</p>'),
    (currentRequestId = ""),
    (currentRunId = ""),
    (cancelRun.disabled = !0),
    (resumeRun.hidden = !0),
    (copyRequestId.disabled = !0),
    setStatus("待查询", "idle"),
    bindQuestionButtons(),
    renderRecentQuestions(),
    (input.value = ""),
    input.focus());
}
function bindQuestionButtons() {
  document.querySelectorAll("[data-question]").forEach(
    (button) =>
      (button.onclick = () => {
        ((queryStartedAt = Date.now()), ask(button.dataset.question));
      }),
  );
}
function appendMessage(role, content) {
  const el = document.createElement("article");
  return (
    (el.className = `message ${role}`),
    (el.innerHTML = `<p class="message-label">${"user" === role ? "你" : "数据助手"}</p><div class="bubble">${content}</div>`),
    conversation.appendChild(el),
    (conversation.scrollTop = conversation.scrollHeight),
    el
  );
}
const commonLabels = {
  total_capacity_mw: "总装机容量（MW）",
  total_count: "总数量",
  project_count: "项目数量",
  station_count: "场站数量",
  county: "区县",
  project_type: "项目类型",
};
function labels(data) {
  return Object.fromEntries(
    (data?.schema || []).map((column) => [
      column.name,
      commonLabels[column.name] || column.semantic_label || column.name,
    ]),
  );
}
function units(data) {
  return Object.fromEntries(
    (data?.schema || [])
      .filter((column) => column.unit)
      .map((column) => [column.name, column.unit]),
  );
}
function displayValue(value, unit) {
  return null == value
    ? "—"
    : `${escapeHtml(value)}${unit ? ` ${escapeHtml(unit)}` : ""}`;
}
function table(data) {
  const rows = data?.rows;
  if (!rows?.length) return "<p>当前合法筛选条件下未匹配到记录。</p>";
  const columns = Object.keys(rows[0]),
    names = labels(data),
    columnUnits = units(data);
  return `<div class="result-table-wrap"><table class="result-table"><thead><tr>${columns.map((c) => `<th>${escapeHtml(names[c] || c)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((c) => `<td>${displayValue(row[c], columnUnits[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function summary(data) {
  const entries = Object.entries(data?.summary || {}),
    names = labels(data),
    columnUnits = units(data);
  if ("no_match" === data?.result_status) return "当前条件下没有匹配数据。";
  if (1 === entries.length) {
    const [key, value] = entries[0];
    return `${escapeHtml(names[key] || key)}：<strong>${displayValue(value, columnUnits[key])}</strong>`;
  }
  return 1 === data?.rows?.length
    ? Object.entries(data.rows[0])
        .map(
          ([k, v]) =>
            `${escapeHtml(names[k] || k)}：<strong>${displayValue(v, columnUnits[k])}</strong>`,
        )
        .join("，")
    : `返回 ${data?.rows?.length || 0} 条结果。`;
}
const toolLabels = {
    llm: "Agent 决策",
    metadata_catalog: "数据目录",
    sql_guard: "安全检查",
    sqlite_readonly: "只读数据库",
    agent: "Agent",
    get_table_context: "获取表上下文",
    inspect_field_profile: "检查字段画像",
    execute_readonly_query: "执行只读查询",
    review_evidence: "审核 Evidence",
    ask_user_question: "请求用户澄清",
    finalize_answer: "生成最终回答",
  },
  warningLabels = {
    RESULT_TRUNCATED: "结果超过展示上限，当前仅显示前部分记录。",
    NO_DATA: "当前查询没有返回记录。",
    NO_DATA_AFTER_VALID_FILTER: "合法筛选条件下没有匹配数据。",
  };
function warningText(value) {
  return warningLabels[value] || value;
}
function coverageMarkup(payload) {
  const coverage = payload.coverage;
  if (!coverage) return "";
  const names = labels(payload.data),
    columnUnits = units(payload.data),
    format = (item) =>
      `${escapeHtml(names[item] || item)}${columnUnits[item] ? `（${escapeHtml(columnUnits[item])}）` : ""}`;
  return `<div class="detail-group"><h3>查询口径</h3><p>数据范围：${escapeHtml(coverage.applied_scope)}</p>${coverage.dimensions?.length ? `<p>查询维度：${coverage.dimensions.map(format).join("、")}</p>` : ""}${coverage.measures?.length ? `<p>返回指标：${coverage.measures.map(format).join("、")}</p>` : ""}</div>`;
}
function eventMetadata(event) {
  const items = [];
  return (
    event.tool && items.push(toolLabels[event.tool] || event.tool),
    (event.provider || event.model) &&
      items.push([event.provider, event.model].filter(Boolean).join(" / ")),
    null != event.total_tokens && items.push(`${event.total_tokens} tokens`),
    event.rule_versions?.length &&
      items.push(`规则 ${event.rule_versions.join("、")}`),
    items.length
      ? `<span class="event-meta">${items.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</span>`
      : ""
  );
}
function compactEvents(events) {
  const compact = [];
  for (const event of events) {
    const key =
      event.stage ||
      event.metadata?.stage ||
      `${event.event_type || "event"}:${event.tool_name || ""}:${event.sequence}`;
    const index = compact.findIndex((item) => item._key === key);
    event._key = key;
    index >= 0 ? (compact[index] = event) : compact.push(event);
  }
  return compact;
}
function timelineMarkup(events) {
  return `<ol class="timeline">${compactEvents(events)
    .map(
      (event) =>
        `<li class="${event.status}"><span class="event-summary">${escapeHtml(event.summary.replace(/完成$/, "").replace(/中$/, ""))}</span>${null == event.duration_ms ? "" : `<small>${event.duration_ms} ms</small>`}${eventMetadata({ ...event, tool: event.tool || event.tool_name })}</li>`,
    )
    .join("")}</ol>`;
}
function runMetrics(events) {
  const completed = compactEvents(events);
  return `<div class="run-metrics"><span><strong>${completed.reduce((total, event) => total + (event.duration_ms || 0), 0)}</strong> ms 阶段耗时</span><span><strong>${completed.filter((event) => "llm" === event.tool && "completed" === event.status).length}</strong> 次模型调用</span></div>`;
}
function renderDetails(payload, events = []) {
  const sources = payload.sources || [],
    limitations = payload.limitations || [];
  ((currentRequestId = payload.request_id || ""),
    (copyRequestId.disabled = !currentRequestId),
    (details.className = ""),
    (details.innerHTML = `<div class="detail-group"><h3>请求编号</h3><p class="request-id">${escapeHtml(payload.request_id)}</p>${events.length ? runMetrics(events) : ""}</div>${events.length ? `<div class="detail-group"><h3>执行过程</h3>${timelineMarkup(events)}</div>` : ""}${coverageMarkup(payload)}<div class="detail-group"><h3>数据来源</h3>${sources.length ? `<ul>${sources.map((s) => `<li>${escapeHtml(s.dataset)}${s.version ? ` · ${escapeHtml(s.version)}` : ""}${s.data_as_of ? `<br>数据截至 ${escapeHtml(s.data_as_of)}` : ""}</li>`).join("")}</ul>` : "<p>未返回来源信息</p>"}</div><div class="detail-group"><h3>限制与提示</h3>${limitations.length || payload.warnings?.length ? `<ul>${[...limitations, ...(payload.warnings || []).map(warningText)].map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : "<p>本次运行无额外限制</p>"}</div>`));
}
function renderProgress(events) {
  ((details.className = ""),
    (details.innerHTML = `<div class="run-summary"><p class="request-id">${escapeHtml(events[0]?.request_id || "正在创建请求")}</p></div><div class="detail-group"><h3>执行过程</h3>${timelineMarkup(events)}</div>`));
}
let sessionId = sessionStorage.getItem("querySessionId") || null;
async function ensureSession() {
  if (sessionId) return sessionId;
  const response = await fetch("/api/v3/agent/sessions", { method: "POST" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json();
  sessionId = payload.session_id;
  sessionStorage.setItem("querySessionId", sessionId);
  return sessionId;
}
async function streamQuery(question, onProgress) {
  // Legacy endpoint /api/v2/agent-query/events remains supported for older clients.
  await ensureSession();
  const response = await fetch(
    `/api/v3/agent/sessions/${sessionId}/messages/events`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        text: question,
        client_message_id: `cm_${(crypto.randomUUID?.() || `${Date.now()}_${Math.random()}`).replaceAll("-", "")}`,
      }),
    },
  );
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    result = null;
  for (;;) {
    const { value: value, done: done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      let type = "message",
        data = "";
      for (const line of block.split("\n"))
        (line.startsWith("event:") && (type = line.slice(6).trim()),
          line.startsWith("data:") && (data += line.slice(5).trim()));
      if (!data) continue;
      const payload = JSON.parse(data);
      if (payload.run_id) currentRunId = payload.run_id;
      if (
        "progress" === type ||
        ["run_start", "turn_start", "tool_call", "tool_result"].includes(type)
      )
        onProgress(payload);
      if ("result" === type) result = payload;
      if ("run_end" === type)
        result = payload.response?.response || payload.response || payload;
    }
    if (done) break;
  }
  if (!result) throw new Error("SSE 结果缺失");
  return (
    result.session_id &&
      ((sessionId = result.session_id),
      sessionStorage.setItem("querySessionId", sessionId)),
    result
  );
}
async function ask(question) {
  (saveRecentQuestion(question),
    document.querySelector(".welcome")?.remove(),
    appendMessage("user", escapeHtml(question)));
  const loading = appendMessage(
    "assistant",
    '<div class="loading" aria-label="正在查询"><i></i><i></i><i></i></div>',
  );
  ((send.disabled = !0), setStatus("查询中", "running"));
  cancelRun.disabled = false;
  resumeRun.hidden = true;
  const events = [];
  details.innerHTML =
    '<div class="empty-details"><p>正在创建查询任务…</p></div>';
  try {
    const payload = await streamQuery(question, (event) => {
      (events.push(event), renderProgress(events));
    });
    if ((loading.remove(), renderDetails(payload, events), payload.success))
      (appendMessage(
        "assistant",
        `<div class="answer-title">${summary(payload.data)}</div>${table(payload.data)}<p class="answer-meta">数据时间：${escapeHtml(payload.data?.data_as_of || "以当前数据快照为准")}</p>`,
      ),
        setStatus("已完成", "success"));
    else {
      const clarification = "CLARIFICATION_REQUIRED" === payload.error?.code;
      (appendMessage(
        "assistant",
        `<div class="answer-title">${clarification ? "需要补充条件" : "本次查询未完成"}</div><p>${escapeHtml(payload.error?.message || "服务返回了未知错误。")}</p><p class="answer-meta">请求编号：${escapeHtml(payload.request_id)}</p>`,
      ),
        setStatus(clarification ? "待补充" : "失败", "error"));
    }
  } catch (error) {
    (loading.remove(),
      appendMessage(
        "assistant",
        '<div class="answer-title">暂时无法连接查询服务</div><p>请确认服务正在运行后重试。</p>',
      ),
      (details.innerHTML =
        '<div class="empty-details"><p>服务连接失败</p></div>'),
      setStatus("连接失败", "error"));
  } finally {
    ((queryStartedAt = 0),
      (send.disabled = !1),
      (cancelRun.disabled = !0),
      input.focus());
  }
}
(form.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = input.value.trim();
  value && ((input.value = ""), (input.style.height = "auto"), ask(value));
}),
  bindQuestionButtons(),
  input.addEventListener("input", () => {
    ((input.style.height = "auto"),
      (input.style.height = `${Math.min(input.scrollHeight, 140)}px`));
  }),
  input.addEventListener("keydown", (event) => {
    "Enter" !== event.key ||
      event.shiftKey ||
      (event.preventDefault(), form.requestSubmit());
  }),
  (document.querySelector("#clearConversation").onclick = resetConversation),
  (cancelRun.onclick = async () => {
    if (!currentRunId) return;
    await fetch(`/api/v3/agent/runs/${currentRunId}/cancel`, {
      method: "POST",
    });
    setStatus("已停止", "error");
    cancelRun.disabled = true;
    resumeRun.hidden = false;
  }),
  (resumeRun.onclick = () => {
    resumeRun.hidden = true;
    setStatus("请输入补充内容", "idle");
    input.focus();
  }),
  (copyRequestId.onclick = async () => {
    if (!currentRequestId) return;
    try {
      if (navigator.clipboard?.writeText)
        await navigator.clipboard.writeText(currentRequestId);
      else throw new Error("clipboard unavailable");
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = currentRequestId;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    const original = copyRequestId.textContent;
    ((copyRequestId.textContent = "已复制"),
      setTimeout(() => (copyRequestId.textContent = original), 1200));
  }),
  renderRecentQuestions(),
  sessionId &&
    fetch(`/api/v3/agent/sessions/${sessionId}/messages?limit=200`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        const messages = payload?.messages || [];
        if (!messages.length) return;
        document.querySelector(".welcome")?.remove();
        messages
          .filter(
            (message) =>
              ["user", "assistant"].includes(message.role) && message.content,
          )
          .forEach((message) =>
            appendMessage(
              message.role === "user" ? "user" : "assistant",
              escapeHtml(message.content),
            ),
          );
      })
      .catch(() => {}),
  Promise.all([
    fetch("/health").then((r) => r.json()),
    fetch("/api/v1/rules/runtime").then((r) => r.json()),
    fetch("/api/v1/evaluations/readiness").then((r) => r.json()),
  ])
    .then(([health, rules, readiness]) => {
      const healthy = "healthy" === health.status;
      ((document.querySelector("#healthDot").className = healthy
        ? "ok"
        : "bad"),
        (document.querySelector("#healthText").textContent = healthy
          ? "服务可用"
          : "服务配置不完整"));
      const database = document.querySelector("#databaseState");
      ((database.textContent =
        "healthy" === health.checks?.database ? "在线 · 只读" : "不可用"),
        (database.className =
          "healthy" === health.checks?.database ? "good" : "bad"));
      const providers = health.llm_providers || [];
      ((document.querySelector("#providerState").textContent = providers.length
        ? `${providers.length} 个可用`
        : "已配置"),
        (document.querySelector("#providerState").className = "good"),
        (document.querySelector("#ruleState").textContent =
          `${rules.length} 条已生效`),
        (document.querySelector("#ruleState").className = "good"),
        (document.querySelector("#releaseState").textContent =
          readiness.ready_for_release ? "可以发布" : "尚未通过"),
        (document.querySelector("#releaseState").className =
          readiness.ready_for_release ? "good" : "warn"));
    })
    .catch(() => {
      ((document.querySelector("#healthDot").className = "bad"),
        (document.querySelector("#healthText").textContent = "服务不可用"),
        ["databaseState", "providerState", "ruleState", "releaseState"].forEach(
          (id) => {
            ((document.querySelector(`#${id}`).textContent = "无法获取"),
              (document.querySelector(`#${id}`).className = "bad"));
          },
        ));
    }));
// Compatibility markers: queryStartedAt=0 resets; queryStartedAt=Date.now() starts the timer.
let queryStartedAt = 0;
(setInterval(() => {
  runStatus?.classList.contains("running") &&
    queryStartedAt &&
    Date.now() - queryStartedAt > 15e3 &&
    (runStatus.textContent = "模型仍在处理，复杂问题可能需要更久");
}, 1e3),
  form.addEventListener("submit", () => {
    queryStartedAt = Date.now();
  }));
