// 현장 화면: /api/state를 자주 읽어 상태별 화면을 그린다.
"use strict";

const POLL_MS = 500;
const EVENTS_REFRESH_MS = 5000;

const RESULT_LABELS = {
  CLEARED_VOICE: "음성으로 괜찮다고 응답",
  CLEARED_BUTTON: "화면 버튼으로 해제",
  ALERT_HELP: "도움 요청 · 경보",
  ALERT_NO_REPLY: "응답 없음 · 경보",
  ALERT_VOICE_FAIL: "음성 확인 불가 · 경보",
  ALERT_DISCONNECTED: "확인 중 연결 끊김 · 경보",
  INTERRUPTED: "처리 중 서버 재시작",
};

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
};

const $ = (id) => document.getElementById(id);
let lastEventsLoad = 0;

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

function render(data) {
  const view = data ? data.view : "offline";
  const problems = data ? data.problems : [];
  document.body.className = "view-" + view;
  const [title, desc] = VIEWS[view] || VIEWS.offline;
  $("title").textContent = title;
  $("desc").textContent = desc;

  show($("suspect-panel"), view === "suspect");
  show($("alarm-panel"), view === "alarm");
  show($("history-panel"), view === "normal" || view === "check" || view === "disconnected");
  show($("clear-button"), view === "suspect" || view === "alarm");

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
  show($("problems"), lines.length > 0 && view !== "suspect");

  $("location").textContent = data ? data.location || "" : "";
  $("notify-badge").textContent = data && !data.notifier_enabled ? "알림 미설정" : "";
}

async function loadEvents() {
  try {
    const res = await fetch("/api/events?limit=3", { cache: "no-store" });
    if (!res.ok) return;
    const events = await res.json();
    setList($("events"), events.length === 0 ? ["기록이 없습니다."]
      : events.map((ev) => `${formatTime(ev.started_at)}  ${RESULT_LABELS[ev.result] || "처리 중"}`
        + (ev.cleared_by === "BUTTON" ? " → 화면에서 해제" : "")));
  } catch (err) {
    console.warn("기록 조회 실패", err);
  }
}

async function poll() {
  let data = null;
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (res.ok) data = await res.json();
  } catch (err) {
    console.warn("상태 조회 실패", err);
  }
  render(data);
  if (data && Date.now() - lastEventsLoad > EVENTS_REFRESH_MS) {
    lastEventsLoad = Date.now();
    loadEvents();
  }
}

async function clearAlarm() {
  const btn = $("clear-button");
  btn.disabled = true;
  try {
    await fetch("/api/clear", { method: "POST", headers: { "X-Kiosk-Clear": "1" } });
    lastEventsLoad = 0;
  } catch (err) {
    console.warn("해제 요청 실패", err);
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
setInterval(tickClock, 1000);
setInterval(poll, POLL_MS);
tickClock();
poll();
