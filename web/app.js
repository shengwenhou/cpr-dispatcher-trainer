(() => {
  "use strict";

  // ===== 常數與狀態 =====
  const I18N_PATH = "./i18n/zh-TW.json";
  const WS_PATH = "/ws/classroom";
  // 後端送出的 state 值一律為小寫 s0..s7，此陣列與 i18n key 需與其一致
  const STATES = ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7"];
  const PASS_LIMITS = {
    ohca_s: 90,
    compression_s: 180
  };
  const SCENARIOS = [
    { value: "adult", enabled: true },
    { value: "child_over_8", enabled: false },
    { value: "child_1_8", enabled: false },
    { value: "infant_under_1", enabled: false },
    { value: "pregnant", enabled: false },
    { value: "advanced", enabled: false }
  ];
  const LOCALES = [
    { value: "zh-TW", enabled: true },
    { value: "ja-JP", enabled: false },
    { value: "en-US", enabled: false }
  ];

  const app = {
    i18n: {},
    socket: null,
    reconnectTimer: null,
    reconnectDelay: 1000,
    classId: null,
    sessionId: null,
    activeStudent: "",
    activeMode: "voice",
    currentState: "s0",
    sessionStartedAt: null,
    timerId: null,
    partialBubble: null,
    completedStudents: [],
    pendingStart: null,
    lastScenario: "adult",
    lastLocale: "zh-TW"
  };

  const els = {};

  // ===== 啟動 =====
  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    collectElements();
    try {
      app.i18n = await fetchJson(I18N_PATH);
    } catch (error) {
      showToast("messages.i18n_load_failed");
      app.i18n = {};
    }

    applyI18n(document);
    buildScenarioOptions();
    buildLocaleOptions();
    buildStateSteps();
    bindEvents();
    updateStartButton();
    updateConnection("offline");
    connectSocket();
  }

  function collectElements() {
    const ids = [
      "connectionStatus",
      "startScreen",
      "practiceScreen",
      "summaryScreen",
      "classEndedScreen",
      "startForm",
      "scenarioSelect",
      "localeSelect",
      "studentAlias",
      "startButton",
      "activeStudent",
      "callTimer",
      "stateSteps",
      "conversationFlow",
      "speakingIndicator",
      "currentStateLabel",
      "lastEventLabel",
      "abortButton",
      "manualEndButton",
      "textInputBar",
      "studentTextInput",
      "summaryStudent",
      "ohcaCard",
      "compressionCard",
      "ohcaValue",
      "compressionValue",
      "emsValue",
      "ohcaStatus",
      "compressionStatus",
      "dwellChart",
      "defenseStats",
      "nextStudentButton",
      "endClassButton",
      "completedStudents",
      "confirmDialog",
      "confirmTitle",
      "confirmMessage",
      "confirmAccept",
      "toastStack"
    ];
    ids.forEach((id) => {
      els[id] = document.getElementById(id);
    });
  }

  function bindEvents() {
    els.studentAlias.addEventListener("input", updateStartButton);
    els.startForm.addEventListener("submit", handleStart);
    els.textInputBar.addEventListener("submit", handleStudentText);
    els.abortButton.addEventListener("click", confirmAbort);
    els.manualEndButton.addEventListener("click", confirmEndSession);
    els.nextStudentButton.addEventListener("click", prepareNextStudent);
    els.endClassButton.addEventListener("click", confirmEndClass);
    window.addEventListener("beforeunload", () => {
      stopTimer();
      if (app.socket) {
        app.socket.close();
      }
    });
  }

  // ===== i18n =====
  async function fetchJson(path) {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(String(response.status));
    }
    return response.json();
  }

  function t(key, params = {}) {
    const value = key.split(".").reduce((acc, part) => acc && acc[part], app.i18n);
    const template = typeof value === "string" ? value : key;
    return template.replace(/\{(\w+)\}/g, (_, name) => {
      return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : "";
    });
  }

  function applyI18n(root) {
    root.querySelectorAll("[data-i18n]").forEach((node) => {
      node.textContent = t(node.dataset.i18n);
    });
    document.title = t("app.title");
    root.querySelectorAll("[data-i18n-attr]").forEach((node) => {
      node.dataset.i18nAttr.split(",").forEach((pair) => {
        const [attr, key] = pair.split(":");
        if (attr && key) {
          node.setAttribute(attr, t(key));
        }
      });
    });
  }

  function buildScenarioOptions() {
    els.scenarioSelect.innerHTML = "";
    SCENARIOS.forEach((scenario) => {
      const option = document.createElement("option");
      option.value = scenario.value;
      option.textContent = t(`scenarios.${scenario.value}`);
      option.disabled = !scenario.enabled;
      if (!scenario.enabled) {
        option.title = t("start.coming_soon");
        option.textContent = `${option.textContent} - ${t("start.coming_soon")}`;
      }
      els.scenarioSelect.append(option);
    });
  }

  function buildLocaleOptions() {
    els.localeSelect.innerHTML = "";
    LOCALES.forEach((locale) => {
      const option = document.createElement("option");
      option.value = locale.value;
      option.textContent = t(`locales.${locale.value}`);
      option.disabled = !locale.enabled;
      if (!locale.enabled) {
        option.title = t("start.coming_soon");
        option.textContent = `${option.textContent} - ${t("start.coming_soon")}`;
      }
      els.localeSelect.append(option);
    });
  }

  // ===== WebSocket =====
  function connectSocket() {
    if (!("WebSocket" in window)) {
      showToast("messages.ws_unavailable");
      return;
    }

    clearTimeout(app.reconnectTimer);
    updateConnection(app.reconnectDelay > 1000 ? "reconnecting" : "connecting");

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    app.socket = new WebSocket(`${protocol}//${window.location.host}${WS_PATH}`);

    app.socket.addEventListener("open", () => {
      app.reconnectDelay = 1000;
      updateConnection("online");
      if (app.sessionId) {
        sendEnvelope("resume", {}, app.sessionId);
      }
    });

    app.socket.addEventListener("message", (event) => {
      handleServerMessage(safeParse(event.data));
    });

    app.socket.addEventListener("close", () => {
      updateConnection("offline");
      scheduleReconnect();
    });

    app.socket.addEventListener("error", () => {
      updateConnection("offline");
    });
  }

  function scheduleReconnect() {
    showToast("messages.socket_closed");
    clearTimeout(app.reconnectTimer);
    app.reconnectTimer = setTimeout(connectSocket, app.reconnectDelay);
    app.reconnectDelay = Math.min(app.reconnectDelay * 2, 10000);
  }

  function sendEnvelope(type, payload = {}, sessionId = app.sessionId) {
    if (!app.socket || app.socket.readyState !== WebSocket.OPEN) {
      showToast("messages.send_failed");
      return false;
    }
    const envelope = { type, payload };
    if (sessionId) {
      envelope.session_id = sessionId;
    }
    app.socket.send(JSON.stringify(envelope));
    return true;
  }

  function handleServerMessage(message) {
    if (!message || !message.type) {
      return;
    }

    const payload = message.payload || {};
    if (message.session_id) {
      app.sessionId = message.session_id;
    }

    const handlers = {
      hello: handleHello,
      class_created: handleClassCreated,
      session_started: handleSessionStarted,
      transcript: handleTranscript,
      state_change: handleStateChange,
      tts_play: handleTtsPlay,
      metric: handleMetric,
      session_ended: handleSessionEnded,
      session_aborted: handleSessionAborted,
      snapshot: handleSnapshot,
      class_ended: handleClassEnded,
      error: handleError,
      degraded: handleDegraded
    };

    if (handlers[message.type]) {
      handlers[message.type](payload, message);
    }
  }

  function handleHello(payload) {
    if (payload.locale) {
      app.lastLocale = payload.locale;
    }
    if (payload.scenario) {
      app.lastScenario = payload.scenario;
    }
    if (payload.active_session) {
      app.sessionId = typeof payload.active_session === "string"
        ? payload.active_session
        : payload.active_session.session_id;
      sendEnvelope("resume", {}, app.sessionId);
    }
  }

  function handleClassCreated(payload) {
    app.classId = payload.class_id || app.classId;
    showToast("messages.class_created");
    if (app.pendingStart) {
      sendEnvelope("start_session", app.pendingStart, null);
      app.pendingStart = null;
    }
  }

  function handleSessionStarted(payload, message) {
    app.sessionId = payload.session_id || message.session_id || app.sessionId;
    app.activeStudent = payload.student_alias || app.activeStudent;
    app.currentState = payload.state || app.currentState || "s0";
    app.sessionStartedAt = Date.now();
    els.activeStudent.textContent = app.activeStudent;
    els.summaryStudent.textContent = app.activeStudent;
    els.conversationFlow.innerHTML = "";
    app.partialBubble = null;
    updateStateUi();
    startTimer();
    showScreen("practice");
    showToast("messages.session_started");
  }

  function handleTranscript(payload) {
    const text = payload.text || "";
    if (!text) {
      return;
    }
    if (payload.kind === "partial") {
      renderPartialTranscript(text);
    } else {
      commitPartialTranscript();
      if (payload.dropped) {
        // 被 echo gate 濾除的定稿：以過濾樣式顯示（非學員泡泡），講師可見系統濾掉了什麼
        addBubble("student", text, { label: t("practice.filtered_label"), filtered: true });
      } else {
        addBubble("student", text, { label: t("practice.student_label") });
      }
    }
  }

  function handleStateChange(payload) {
    app.currentState = payload.to || app.currentState;
    updateStateUi();
    const reason = payload.reason ? ` ${payload.reason}` : "";
    els.lastEventLabel.textContent = `${payload.from || ""} -> ${payload.to || ""}${reason}`;
  }

  function handleTtsPlay(payload) {
    if (payload.event === "start") {
      els.speakingIndicator.hidden = false;
      if (payload.text) {
        const layer = Number(payload.layer || 1);
        addBubble("dispatcher", payload.text, {
          label: layer >= 2 ? t("practice.fallback_layer", { layer }) : t("practice.dispatcher_label"),
          fallback: layer >= 2
        });
      }
    }
    if (payload.event === "end") {
      els.speakingIndicator.hidden = true;
    }
  }

  function handleMetric(payload) {
    if (payload.event) {
      els.lastEventLabel.textContent = payload.event;
    }
  }

  function handleSessionEnded(payload) {
    stopTimer();
    els.speakingIndicator.hidden = true;
    const summary = payload.summary || {};
    if (app.activeStudent && !app.completedStudents.includes(app.activeStudent)) {
      app.completedStudents.push(app.activeStudent);
    }
    renderSummary(summary);
    showScreen("summary");
    showToast("messages.session_ended");
  }

  function handleSessionAborted() {
    // 緊急中止完成：停錶、清說話指示、回開始畫面（中止場次不顯示指標卡）
    stopTimer();
    els.speakingIndicator.hidden = true;
    prepareNextStudent();
    showToast("messages.session_aborted");
  }

  function handleSnapshot(payload) {
    // 重連還原：學員代號一併回填（後端 snapshot 已附帶），標頭與結果卡同步
    if (payload.student_alias) {
      app.activeStudent = payload.student_alias;
      els.activeStudent.textContent = app.activeStudent;
      els.summaryStudent.textContent = app.activeStudent;
    }
    app.currentState = payload.state || app.currentState || "s0";
    updateStateUi();
    els.conversationFlow.innerHTML = "";
    app.partialBubble = null;
    (payload.transcript_tail || []).forEach((item) => {
      const normalized = typeof item === "string" ? { role: "student", text: item } : item;
      const role = normalized.role === "dispatcher" ? "dispatcher" : "student";
      addBubble(role, normalized.text || "", {
        label: role === "dispatcher" ? t("practice.dispatcher_label") : t("practice.student_label")
      });
    });
    startTimer();
    showScreen("practice");
    showToast("messages.session_resumed");
  }

  function handleClassEnded(payload) {
    const students = Array.isArray(payload.students) ? payload.students : app.completedStudents;
    app.completedStudents = students.map((student) => {
      return typeof student === "string" ? student : student.student_alias || student.alias || "";
    }).filter(Boolean);
    renderCompletedStudents();
    showScreen("classEnded");
    showToast("messages.class_ended");
  }

  function handleError(payload) {
    showToast(serverMessage(payload.message_key), true);
  }

  function handleDegraded(payload) {
    showToast(payload.message_key ? serverMessage(payload.message_key) : "messages.degraded", true);
  }

  function safeParse(raw) {
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  // ===== 使用者動作 =====
  function handleStart(event) {
    event.preventDefault();
    const alias = els.studentAlias.value.trim();
    if (!alias) {
      showToast("messages.start_requires_alias");
      return;
    }

    const formData = new FormData(els.startForm);
    app.activeStudent = alias;
    app.activeMode = formData.get("mode") || "voice";
    app.lastScenario = formData.get("scenario") || "adult";
    app.lastLocale = formData.get("locale") || "zh-TW";

    const startPayload = {
      student_alias: app.activeStudent,
      mode: app.activeMode
    };
    app.pendingStart = startPayload;

    if (!app.classId) {
      sendEnvelope("create_class", {
        scenario: app.lastScenario,
        locale: app.lastLocale,
        label: app.activeStudent
      }, null);
    } else if (sendEnvelope("start_session", startPayload, null)) {
      app.pendingStart = null;
    }
  }

  function handleStudentText(event) {
    event.preventDefault();
    const text = els.studentTextInput.value.trim();
    if (!text) {
      return;
    }
    if (sendEnvelope("student_final", { text })) {
      els.studentTextInput.value = "";
      commitPartialTranscript();
      addBubble("student", text, { label: t("practice.student_label") });
    }
  }

  function confirmAbort() {
    openConfirm("dialog.abort_title", "dialog.abort_message", "dialog.abort_accept", () => {
      sendEnvelope("abort_session", { reason: "instructor_abort" });
    });
  }

  function confirmEndSession() {
    openConfirm("dialog.end_session_title", "dialog.end_session_message", "dialog.end_session_accept", () => {
      sendEnvelope("end_session", {});
    });
  }

  function confirmEndClass() {
    openConfirm("dialog.end_class_title", "dialog.end_class_message", "dialog.end_class_accept", () => {
      sendEnvelope("end_class", {}, null);
    });
  }

  function prepareNextStudent() {
    app.sessionId = null;
    app.activeStudent = "";
    app.sessionStartedAt = null;
    app.currentState = "s0";
    els.studentAlias.value = "";
    els.scenarioSelect.value = app.lastScenario;
    els.localeSelect.value = app.lastLocale;
    updateStartButton();
    stopTimer();
    showScreen("start");
  }

  // ===== 畫面渲染 =====
  function showScreen(name) {
    const map = {
      start: els.startScreen,
      practice: els.practiceScreen,
      summary: els.summaryScreen,
      classEnded: els.classEndedScreen
    };
    Object.values(map).forEach((screen) => screen.classList.remove("is-active"));
    map[name].classList.add("is-active");
    els.textInputBar.hidden = !(name === "practice" && app.activeMode === "text");
  }

  function updateConnection(state) {
    const key = state === "reconnecting" ? "connection.reconnecting" : `connection.${state}`;
    els.connectionStatus.dataset.state = state === "reconnecting" ? "connecting" : state;
    const label = els.connectionStatus.querySelector("span:last-child");
    label.textContent = t(key);
  }

  function updateStartButton() {
    els.startButton.disabled = els.studentAlias.value.trim().length === 0;
  }

  function buildStateSteps() {
    els.stateSteps.innerHTML = "";
    STATES.forEach((state) => {
      const item = document.createElement("li");
      item.dataset.state = state;
      item.innerHTML = `<strong>${state.toUpperCase()}</strong><span>${t(`states.${state}`)}</span>`;
      els.stateSteps.append(item);
    });
    updateStateUi();
  }

  function updateStateUi() {
    const activeIndex = STATES.indexOf(app.currentState);
    els.stateSteps.querySelectorAll("li").forEach((item, index) => {
      item.classList.toggle("is-active", item.dataset.state === app.currentState);
      item.classList.toggle("is-complete", activeIndex >= 0 && index < activeIndex);
    });
    els.currentStateLabel.textContent = `${app.currentState.toUpperCase()} ${t(`states.${app.currentState}`)}`;
  }

  function addBubble(role, text, options = {}) {
    if (!text) {
      return null;
    }
    const bubble = document.createElement("article");
    bubble.className = `bubble ${role}`;
    if (options.partial) {
      bubble.classList.add("partial");
    }
    if (options.fallback) {
      bubble.classList.add("fallback");
    }
    if (options.filtered) {
      bubble.classList.add("filtered");
    }
    const label = document.createElement("small");
    label.textContent = options.label || "";
    const body = document.createElement("div");
    body.textContent = text;
    bubble.append(label, body);
    els.conversationFlow.append(bubble);
    scrollConversation();
    return bubble;
  }

  function renderPartialTranscript(text) {
    if (!app.partialBubble) {
      app.partialBubble = addBubble("student", text, {
        label: t("practice.partial_label"),
        partial: true
      });
      return;
    }
    app.partialBubble.querySelector("div").textContent = text;
    scrollConversation();
  }

  function commitPartialTranscript() {
    if (app.partialBubble) {
      app.partialBubble.remove();
      app.partialBubble = null;
    }
  }

  function scrollConversation() {
    requestAnimationFrame(() => {
      els.conversationFlow.scrollTop = els.conversationFlow.scrollHeight;
    });
  }

  function renderSummary(summary) {
    els.summaryStudent.textContent = app.activeStudent;
    // 欄位名對齊後端正本：ohca_recognized_s / compression_start_s / ems_arrived_s
    renderThresholdMetric("ohca", summary.ohca_recognized_s, PASS_LIMITS.ohca_s);
    renderThresholdMetric("compression", summary.compression_start_s, PASS_LIMITS.compression_s);
    els.emsValue.textContent = formatSeconds(summary.ems_arrived_s);
    renderDwell(summary.state_dwell_s || {});
    renderDefense(summary.defense_counts || {});
  }

  function renderThresholdMetric(kind, value, limit) {
    const card = kind === "ohca" ? els.ohcaCard : els.compressionCard;
    const valueEl = kind === "ohca" ? els.ohcaValue : els.compressionValue;
    const statusEl = kind === "ohca" ? els.ohcaStatus : els.compressionStatus;
    card.classList.remove("pass", "fail");
    valueEl.textContent = formatSeconds(value);
    if (typeof value !== "number") {
      statusEl.textContent = t("summary.no_data");
      return;
    }
    const passed = value < limit;
    card.classList.add(passed ? "pass" : "fail");
    statusEl.textContent = t(passed ? "summary.pass" : "summary.fail");
  }

  function renderDwell(dwell) {
    els.dwellChart.innerHTML = "";
    const values = STATES.map((state) => Number(dwell[state] || 0));
    const max = Math.max(...values, 1);
    STATES.forEach((state, index) => {
      const row = document.createElement("div");
      row.className = "bar-row";
      const width = Math.max(2, (values[index] / max) * 100);
      row.innerHTML = `
        <strong>${state.toUpperCase()} ${t(`states.${state}`)}</strong>
        <span class="bar-track"><span class="bar-fill" style="width: ${width}%"></span></span>
        <span>${formatDwellSeconds(values[index])}</span>
      `;
      els.dwellChart.append(row);
    });
  }

  function renderDefense(defense) {
    // defense_counts 的 key 為防禦層編號字串："2"=釐清/拉回、"3"=FAQ、"4"=即時生成、"5"=timeout/安全網
    const rows = [
      ["2", "summary.defense_layer2"],
      ["3", "summary.defense_layer3"],
      ["4", "summary.defense_layer4"],
      ["5", "summary.defense_timeouts"]
    ];
    els.defenseStats.innerHTML = "";
    rows.forEach(([key, labelKey]) => {
      const item = document.createElement("div");
      item.className = "defense-item";
      const count = Number(defense[key] || 0);
      item.innerHTML = `<span>${t(labelKey)}</span><strong>${count}</strong>`;
      els.defenseStats.append(item);
    });
  }

  function renderCompletedStudents() {
    els.completedStudents.innerHTML = "";
    if (app.completedStudents.length === 0) {
      const item = document.createElement("li");
      item.textContent = t("class_ended.empty");
      els.completedStudents.append(item);
      return;
    }
    app.completedStudents.forEach((student) => {
      const item = document.createElement("li");
      item.textContent = student;
      els.completedStudents.append(item);
    });
  }

  // ===== 計時與對話框 =====
  function startTimer() {
    stopTimer();
    if (!app.sessionStartedAt) {
      app.sessionStartedAt = Date.now();
    }
    updateTimer();
    app.timerId = setInterval(updateTimer, 1000);
  }

  function stopTimer() {
    clearInterval(app.timerId);
    app.timerId = null;
  }

  function updateTimer() {
    const elapsed = Math.max(0, Math.floor((Date.now() - app.sessionStartedAt) / 1000));
    const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const seconds = String(elapsed % 60).padStart(2, "0");
    els.callTimer.textContent = `${minutes}:${seconds}`;
  }

  function openConfirm(titleKey, messageKey, acceptKey, onAccept) {
    els.confirmTitle.textContent = t(titleKey);
    els.confirmMessage.textContent = t(messageKey);
    els.confirmAccept.textContent = t(acceptKey);
    els.confirmDialog.returnValue = "";
    els.confirmDialog.showModal();
    els.confirmDialog.addEventListener("close", function handleClose() {
      els.confirmDialog.removeEventListener("close", handleClose);
      if (els.confirmDialog.returnValue === "accept") {
        onAccept();
      }
    });
  }

  function formatSeconds(value) {
    if (typeof value !== "number" || Number.isNaN(value)) {
      return t("summary.no_data");
    }
    return t("summary.seconds", { value: Math.round(value) });
  }

  function formatDwellSeconds(value) {
    // 各階段停留長條圖固定顯示一位小數
    if (typeof value !== "number" || Number.isNaN(value)) {
      return t("summary.no_data");
    }
    return t("summary.seconds", { value: value.toFixed(1) });
  }

  function serverMessage(messageKey) {
    const key = messageKey ? `server_messages.${messageKey}` : "server_messages.default";
    return t(key) === key ? t("server_messages.default") : t(key);
  }

  function showToast(messageKeyOrText, alreadyTranslated = false) {
    if (!els.toastStack) {
      return;
    }
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = alreadyTranslated ? messageKeyOrText : t(messageKeyOrText);
    els.toastStack.append(toast);
    setTimeout(() => {
      toast.remove();
    }, 4200);
  }
})();
