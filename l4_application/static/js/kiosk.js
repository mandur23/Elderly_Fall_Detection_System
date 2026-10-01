// 현장 화면: /api/state를 주기적으로 읽어 상태별 화면을 그린다.
"use strict";

const POLL_MS = 500;
const EVENTS_REFRESH_MS = 5000;

const RESULT_LABELS = {
  CLEARED_VOICE: "음성으로 괜찮다고 응답",
  CLEARED_BUTTON: "화면 버튼으로 해제",
  ALERT_HELP: "도움 요청 · 경보",
  ALERT_NO_REPLY: "응답 없음 · 경보",
  ALERT_VOICE_FAIL: "음성 확인 불가 · 경보",
  INTERRUPTED: "처리 중 재시작됨",
};

const NOTIFY_LABELS = {
  pending: "보호자에게 알림을 보내는 중입니다.",
  sent: "보호자에게 알림을 보냈습니다.",
  failed: "알림 전송에 실패했습니다. 자동으로 다시 보냅니다.",
  disabled: "보호자 알림이 설정되지 않았습니다 (시험 모드).",
  cancelled: "알림 전송이 취소되었습니다.",
};

const $ = (id) => document.getElementById(id);
let lastEventsLoad = 0;

function show(el, visible) {
  el.classList.toggle("hidden", !visible);
}

function formatTime(iso) {
  return iso ? iso.slice(0, 19).replace("T", " ") : "--";
}

function render(data) {
  const state = data ? data.state : "OFFLINE";
  const problems = data ? data.problems : ["현장 서버에 연결할 수 없습니다"];
  const hasProblem = problems.length > 0;

  let bodyClass = "state-" + state.toLowerCase();
  if (state === "NORMAL" && hasProblem) bodyClass = "state-error";
  if (state === "OFFLINE") bodyClass = "state-error";
  document.body.className = bodyClass;

  show($("normal-panel"), state === "NORMAL");
  show($("suspect-panel"), state === "SUSPECT");
  show($("alarm-panel"), state === "ALARM");
  show($("clear-button"), state === "SUSPECT" || state === "ALARM");

  if (state === "SUSPECT") {
    $("title").textContent = "낙상이 의심됩니다";
    $("desc").textContent = "상태를 확인하고 있습니다.";
    const ev = data.event;
    $("attempt").textContent = ev && ev.voice_attempt
      ? `음성 확인 ${ev.voice_attempt} / ${ev.voice_attempts}회` : "";
  } else if (state === "ALARM") {
    $("title").textContent = "경보";
    $("desc").textContent = "낙상 의심 상황에서 괜찮다는 응답을 확인하지 못했습니다.";
    const ns = data.event ? data.event.notify_state : null;
    $("notify-status").textContent = NOTIFY_LABELS[ns] || "";
  } else if (state === "NORMAL") {
    $("title").textContent = hasProblem ? "장치 점검 필요" : "정상 감시 중";
    $("desc").textContent = hasProblem
      ? "아래 항목을 확인해 주세요."
      : "카메라로 생활 공간을 살펴보고 있습니다.";
  } else {
    $("title").textContent = "연결 끊김";
    $("desc").textContent = "현장 서버 상태를 확인해 주세요.";
  }

  const list = $("problems");
  list.replaceChildren(...problems.map((p) => {
    const li = document.createElement("li");
    li.textContent = p;
    return li;
  }));
  show(list, hasProblem);
  $("link-status").textContent = data && !data.notifier_enabled ? "알림 미설정" : "";
}

async function loadEvents() {
  try {
    const res = await fetch("/api/events?limit=3");
    if (!res.ok) return;
    const events = await res.json();
    const list = $("events");
    if (events.length === 0) {
      const li = document.createElement("li");
      li.textContent = "기록이 없습니다.";
      list.replaceChildren(li);
      return;
    }
    list.replaceChildren(...events.map((ev) => {
      const li = document.createElement("li");
      li.textContent = `${formatTime(ev.started_at)}  ${RESULT_LABELS[ev.result] || "처리 중"}`;
      return li;
    }));
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
  if (data && data.state === "NORMAL" && Date.now() - lastEventsLoad > EVENTS_REFRESH_MS) {
    lastEventsLoad = Date.now();
    loadEvents();
  }
}

async function clearAlarm() {
  const btn = $("clear-button");
  btn.disabled = true;
  try {
    await fetch("/api/clear", { method: "POST" });
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
