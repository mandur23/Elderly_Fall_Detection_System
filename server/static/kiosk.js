// 현장 화면: /api/state를 자주 읽어 상태별 화면을 그린다.
// 주소의 ?device=<장치 id>로 이 화면이 보여 줄 라즈베리파이를 정한다(서버에 여러 대가 붙을 수 있음).
// 주소의 &key=<화면 열쇠>는 모든 요청에 X-Kiosk-Key 헤더로 붙인다. 열쇠가 틀리면 서버가 상태를 알려 주지 않는다.
"use strict";

const POLL_MS = 500;
const FETCH_TIMEOUT_MS = 3000;   // 이 시간 안에 답이 없으면 요청을 끊고 '연결 끊김'으로 보인다
const CLEAR_TIMEOUT_MS = 5000;
const CLEAR_ERROR_MS = 8000;     // 해제 실패 안내를 보여 주는 시간
const EVENTS_REFRESH_MS = 5000;
const PARAMS = new URLSearchParams(location.search);
const DEVICE = PARAMS.get("device");
const KEY = PARAMS.get("key");

function withDevice(path) {
  if (!DEVICE) return path;
  return `${path}${path.includes("?") ? "&" : "?"}device=${encodeURIComponent(DEVICE)}`;
}

const RESULT_LABELS = {
  CLEARED_VOICE: "음성으로 괜찮다고 응답",
  CLEARED_BUTTON: "화면 버튼으로 해제",
  CLEARED_ADMIN: "관리자가 해제",
  CLEARED_GUARDIAN: "보호자가 해제",
  ALERT_HELP: "도움 요청 · 경보",
  ALERT_NO_REPLY: "응답 없음 · 경보",
  ALERT_VOICE_FAIL: "음성 확인 불가 · 경보",
  ALERT_DISCONNECTED: "확인 중 연결 끊김 · 경보",
  ALERT_INTERRUPTED: "확인 중 서버 재시작 · 경보",
  INTERRUPTED: "처리 중 서버 재시작",  // 예전 기록
};
const ALERTS = new Set(["ALERT_HELP", "ALERT_NO_REPLY", "ALERT_VOICE_FAIL", "ALERT_DISCONNECTED", "ALERT_INTERRUPTED"]);

const NOTIFY_LABELS = {
  pending: "보호자에게 알림을 보내는 중입니다.",
  sent: "보호자에게 알림을 보냈습니다.",
  failed: "알림 전송에 실패했습니다. 자동으로 다시 보냅니다.",
  disabled: "보호자 알림이 설정되지 않았습니다 (알림 미설정).",
  cancelled: "알림 전송이 취소되었습니다.",
};

const VIEWS = {
  normal: ["정상 감시 중", "카메라로 생활 공간을 살펴보고 있습니다."],
  suspect: ["낙상이 의심됩니다", "상태를 확인하고 있습니다."],
  alarm: ["경보", "괜찮다는 응답을 확인하지 못했습니다."],
  check: ["장치 점검 필요", "아래 항목을 확인해 주세요. 낙상을 감지하지 못할 수 있습니다."],
  disconnected: ["연결 끊김", "카메라 장치(라즈베리파이)가 서버에 연결되어 있지 않습니다."],
  offline: ["연결 끊김", "서버에 연결할 수 없습니다. 서버 PC를 확인해 주세요."],
  select: ["장치를 골라 주세요", "서버에 라즈베리파이가 여러 대 연결되어 있습니다. 이 화면에 보일 장치를 고르세요."],
  denied: ["화면 주소 확인 필요",
    "이 화면 주소가 올바르지 않습니다. 라즈베리파이 설치를 다시 확인하거나 관리자에게 알려 주세요."],
};
const DENIED = { view: "denied", problems: [] };  // 서버가 화면 열쇠를 받아 주지 않음 (401/403)

const $ = (id) => document.getElementById(id);
let lastEventsLoad = 0;
let deviceListKey = "";
let clearErrorTimer = null;
let pollTimer = null;
let pollSeq = 0;      // 보낸 상태 요청 번호
let renderedSeq = 0;  // 화면에 그린 응답의 요청 번호. 이보다 먼저 보낸 요청의 응답은 늦게 와도 버린다

// 화면 열쇠를 붙여 요청하고 JSON 본문까지 받아 { ok, status, data }로 돌려준다.
// ms 안에 끝나지 않으면 요청을 끊는다(AbortError).
async function request(path, options = {}, ms = FETCH_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  const headers = { ...(options.headers || {}) };
  if (KEY) headers["X-Kiosk-Key"] = KEY;
  try {
    const res = await fetch(withDevice(path), { cache: "no-store", ...options, headers, signal: ctrl.signal });
    const data = await res.json().catch((err) => {
      if (err.name === "AbortError") throw err;
      return null;  // JSON이 아닌 응답 (오류 문구 등)
    });
    return { ok: res.ok, status: res.status, data };
  } finally {
    clearTimeout(timer);
  }
}

function show(el, visible) {
  el.classList.toggle("hidden", !visible);
}

function formatTime(iso) {
  return iso ? iso.slice(5, 16).replace("T", " ") : "--";
}

function setList(el, lines) {
  el.replaceChildren(...lines.map((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    return li;
  }));
}

// 장치 고르기 목록: 내용이 바뀔 때만 다시 그린다 (누르는 도중에 링크가 바뀌어 클릭이 사라지지 않게).
function renderDeviceList(devices) {
  const key = JSON.stringify(devices.map((d) => [d.name, d.connected, d.kiosk_url]));
  if (key === deviceListKey) return;
  deviceListKey = key;
  $("device-list").replaceChildren(...devices.map((d) => {
    const li = document.createElement("li");
    const label = `${d.name} (${d.connected ? "연결됨" : "연결 끊김"})`;
    if (d.kiosk_url) {
      const a = document.createElement("a");
      a.href = d.kiosk_url;  // 화면 열쇠까지 든 주소는 서버가 만들어 준다
      a.textContent = label;
      li.append(a);
    } else {
      li.textContent = label;
    }
    return li;
  }));
  show($("device-list"), devices.length > 0);
}

function render(data) {
  const view = data ? data.view : "offline";
  const problems = data ? data.problems : [];
  document.body.className = "view-" + view;
  const [title, desc] = VIEWS[view] || VIEWS.offline;
  $("title").textContent = title;
  $("desc").textContent = desc;

  const asking = view === "suspect" || view === "alarm";
  show($("suspect-panel"), view === "suspect");
  show($("alarm-panel"), view === "alarm");
  show($("history-panel"), view === "normal" || view === "check" || view === "disconnected");
  show($("clear-button"), asking);
  if (!asking) hideClearError();

  const ev = data && data.event;
  if (view === "suspect" && ev) {
    const step = ev.phase === "listen" ? "지금 말씀해 주세요." : "안내 음성을 들려 드리고 있습니다.";
    $("attempt").textContent = ev.voice_attempt
      ? `${step} (확인 ${ev.voice_attempt} / ${ev.voice_attempts}회)` : step;
  }
  if (view === "alarm" && ev) {
    $("notify-status").textContent = NOTIFY_LABELS[ev.notify_state] || "";
  }

  const lines = data && data.device_alert_sent && problems.length
    ? [...problems, "보호자에게 장치 이상을 알렸습니다"] : problems;
  setList($("problems"), lines);
  show($("problems"), lines.length > 0 && view !== "suspect" && view !== "select");

  renderDeviceList(view === "select" ? data.devices || [] : []);

  $("location").textContent = data ? data.location || "" : "";
  $("notify-badge").textContent = data && data.notifier_enabled === false ? "알림 미설정" : "";
}

async function loadEvents() {
  try {
    const res = await request("/api/events?limit=3");
    if (!res.ok || !Array.isArray(res.data)) return;
    const events = res.data;
    const by = { BUTTON: " → 화면에서 해제", ADMIN: " → 관리자가 해제" };
    setList($("events"), events.length === 0 ? ["기록이 없습니다."]
      : events.map((ev) => `${formatTime(ev.started_at)}  ${RESULT_LABELS[ev.result] || "처리 중"}`
        + (ALERTS.has(ev.result)
          ? (ev.cleared_by || "").startsWith("GUARDIAN:") ? " → 보호자가 해제" : by[ev.cleared_by] || "" : "")));
  } catch (err) {
    console.warn("기록 조회 실패", err);
  }
}

// 상태 하나를 읽는다. 서버가 답하지 않거나 끊기면 null(연결 끊김), 열쇠가 틀리면 DENIED.
async function loadState() {
  try {
    const res = await request("/api/state");
    if (res.status === 401 || res.status === 403) return DENIED;
    return res.ok ? res.data : null;
  } catch (err) {
    console.warn("상태 조회 실패", err);
    return null;
  }
}

// 요청이 끝난 뒤에 다음 요청을 예약한다(setTimeout 사슬). 서버가 느려도 요청이 겹쳐 쌓이지 않는다.
// 현장 화면은 탭이 가려져 있어도 천천히 읽지 않는다(경보가 늦게 보이면 안 됨).
async function poll() {
  clearTimeout(pollTimer);
  const seq = ++pollSeq;
  const data = await loadState();
  try {
    if (seq > renderedSeq) {
      renderedSeq = seq;
      render(data);
      if (data && data !== DENIED && Date.now() - lastEventsLoad > EVENTS_REFRESH_MS) {
        lastEventsLoad = Date.now();
        loadEvents();
      }
    }
  } finally {
    if (seq === pollSeq) pollTimer = setTimeout(poll, POLL_MS);  // 가장 최근 요청만 다음 요청을 예약
  }
}

function showClearError(text) {
  const box = $("clear-error");
  box.textContent = text;
  show(box, true);
  clearTimeout(clearErrorTimer);
  clearErrorTimer = setTimeout(hideClearError, CLEAR_ERROR_MS);
}

function hideClearError() {
  clearTimeout(clearErrorTimer);
  show($("clear-error"), false);
}

async function clearAlarm() {
  const btn = $("clear-button");
  btn.disabled = true;
  hideClearError();
  try {
    const res = await request("/api/clear", { method: "POST", headers: { "X-Kiosk-Clear": "1" } }, CLEAR_TIMEOUT_MS);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    lastEventsLoad = 0;
  } catch (err) {
    console.warn("해제 요청 실패", err);
    showClearError("해제하지 못했습니다. 버튼을 다시 눌러 주세요.");
  } finally {
    btn.disabled = false;
    poll();
  }
}

function tickClock() {
  const d = new Date();
  $("clock").textContent = [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0")).join(":");
}

$("clear-button").addEventListener("click", clearAlarm);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();  // 화면이 다시 보이면 바로 읽는다
});
setInterval(tickClock, 1000);
tickClock();
poll();
