// 보호자 웹: /api/g/overview를 2초마다 읽어 맡은 장치 상태를 그린다. 새 경보는 소리·진동으로 알린다.
// 소리를 켜 두면 경보가 끝날 때까지 10초마다 다시 울린다. 바뀐 칸만 고쳐 그려 버튼 누르기가 폴링에 끊기지 않게 한다.
"use strict";

const POLL_MS = 2000;
const HIDDEN_POLL_MS = 10000;     // 화면이 가려져 있을 때는 천천히 읽는다
const FETCH_TIMEOUT_MS = 3000;    // 상태 조회가 이 시간 안에 끝나지 않으면 끊고 '서버 연결 끊김'으로 보인다
const ACTION_TIMEOUT_MS = 10000;  // 경보 끄기 요청의 시간 제한
const ALARM_REPEAT_MS = 10000;    // 경보가 이어지는 동안 소리·진동을 다시 내는 간격

const HEADLINES = {
  normal: "정상 감시 중이에요",
  suspect: "넘어진 것 같아 상태를 확인하고 있어요",
  alarm: "경보: 괜찮다는 대답을 듣지 못했어요",
  check: "장치 점검이 필요해요",
  disconnected: "장치가 연결되어 있지 않아요",
};

const RESULT_LABELS = {
  CLEARED_VOICE: "넘어짐 의심 → 음성으로 괜찮다고 대답",
  CLEARED_BUTTON: "넘어짐 의심 → 현장 화면에서 괜찮다고 누름",
  CLEARED_ADMIN: "넘어짐 의심 → 관리자가 해제",
  CLEARED_GUARDIAN: "넘어짐 의심 → 보호자가 해제",
  ALERT_HELP: "경보: 도움을 요청함",
  ALERT_NO_REPLY: "경보: 대답이 없음",
  ALERT_VOICE_FAIL: "경보: 장치 문제로 음성 확인을 못 함",
  ALERT_DISCONNECTED: "경보: 확인 중 장치 연결이 끊김",
  ALERT_INTERRUPTED: "경보: 확인 중 서버가 다시 시작됨",
  INTERRUPTED: "확인 중 서버가 다시 시작됨",  // 예전 기록
};
const ALERTS = new Set(["ALERT_HELP", "ALERT_NO_REPLY", "ALERT_VOICE_FAIL", "ALERT_DISCONNECTED", "ALERT_INTERRUPTED"]);

const $ = (id) => document.getElementById(id);
let names = {};
let alarmed = new Set();     // 지금 울리고 있는 경보 (장치 id:이벤트 id)
let audio = null;            // 사용자가 소리를 켜야 브라우저가 소리를 낼 수 있다
let soundOn = false;
let repeatTimer = null;
const cards = new Map();     // 장치 id → { node: 화면의 카드, data: 마지막으로 그린 장치 정보 }
const noDevices = el("p", "맡은 장치가 없어요. 관리자에게 장치 연결을 요청해 주세요.", "muted");
let eventsKey = "";
let pollTimer = null;
let pollSeq = 0;      // 보낸 상태 요청 번호
let renderedSeq = 0;  // 화면에 그린 응답의 요청 번호. 이보다 먼저 보낸 요청의 응답은 늦게 와도 버린다

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

// JSON 본문까지 받아 { ok, status, data }로 돌려준다. ms 안에 끝나지 않으면 요청을 끊는다(AbortError).
async function fetchJson(url, options = {}, ms = FETCH_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    const res = await fetch(url, { cache: "no-store", ...options, signal: ctrl.signal });
    const data = await res.json().catch((err) => {
      if (err.name === "AbortError") throw err;
      return null;  // JSON이 아닌 응답 (오류 문구 등)
    });
    return { ok: res.ok, status: res.status, data };
  } finally {
    clearTimeout(timer);
  }
}

// 글자가 바뀌었을 때만 바꾼다 (같은 글자를 다시 넣으면 그 안의 글자 선택이 풀린다).
function setText(node, text) {
  const s = String(text);
  if (node.textContent !== s) node.textContent = s;
}

// 목록 내용이 바뀌었을 때만 다시 그린다.
function setItems(list, lines) {
  const key = JSON.stringify(lines);
  if (list.dataset.key === key) return;
  list.dataset.key = key;
  list.replaceChildren(...lines.map((p) => el("li", p)));
}

// 순서가 달라진 노드만 옮긴다. 제자리인 노드는 떼었다 붙이지 않아 포커스가 유지된다.
function placeChildren(box, nodes) {
  nodes.forEach((node, i) => {
    if (box.children[i] !== node) box.insertBefore(node, box.children[i] || null);
  });
  while (box.children.length > nodes.length) box.lastElementChild.remove();
}

function formatTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  const today = new Date();
  const time = iso.slice(11, 16);
  return d.toDateString() === today.toDateString() ? `오늘 ${time}` : `${iso.slice(5, 10).replace("-", "/")} ${time}`;
}

function detail(d) {
  const ev = d.event;
  if (d.view === "suspect" && ev) {
    const step = ev.phase === "listen" ? "대답을 듣는 중" : "안내 음성을 들려 드리는 중";
    return `${formatTime(ev.started_at)}부터 · 음성 확인 ${ev.voice_attempt || 1}/${ev.voice_attempts}회 (${step})`;
  }
  if (d.view === "alarm" && ev) {
    return `${formatTime(ev.started_at)} · ${RESULT_LABELS[ev.result] || ev.result}. 전화하거나 현장을 확인해 주세요.`;
  }
  if (!d.connected && d.since) return `${formatTime(d.since)}부터 연결이 끊겨 있어요. 낙상을 감지하지 못할 수 있어요.`;
  return "";
}

// 장치마다 카드는 한 번만 만들고, 버튼은 늘 그 장치의 최신 정보(entry.data)로 동작한다.
function createCard() {
  const entry = { node: $("device-card").content.firstElementChild.cloneNode(true), data: null };
  const btn = entry.node.querySelector(".clear-btn");
  btn.addEventListener("click", () => clearDevice(entry.data, btn));
  return entry;
}

function updateCard(entry, d, canClear) {
  entry.data = d;
  const card = entry.node;
  const cls = "card view-" + d.view;
  if (card.className !== cls) card.className = cls;
  setText(card.querySelector(".name"), d.name);
  setText(card.querySelector(".headline"), HEADLINES[d.view] || d.view);
  setText(card.querySelector(".detail"), detail(d));
  setItems(card.querySelector(".problems"), d.view === "suspect" || d.view === "alarm" ? [] : d.problems);
  card.querySelector(".call-btn").classList.toggle("hidden", d.view !== "alarm");
  card.querySelector(".clear-btn").classList.toggle("hidden",
    !(canClear && (d.view === "alarm" || d.view === "suspect")));
  setText(card.querySelector(".counts"),
    `최근 24시간: 넘어짐 의심 ${d.last_24h.events}번, 경보 ${d.last_24h.alarms}번`);
}

function renderDevices(devices, canClear) {
  const seen = new Set();
  const nodes = devices.map((d) => {
    seen.add(d.device_id);
    let entry = cards.get(d.device_id);
    if (!entry) {
      entry = createCard();
      cards.set(d.device_id, entry);
    }
    // 경보 끄기 요청 중인 카드는 건드리지 않음 (끝나면 바로 다시 읽어 그린다)
    if (!entry.node.querySelector(".clear-btn:disabled")) updateCard(entry, d, canClear);
    return entry.node;
  });
  for (const id of cards.keys()) {
    if (!seen.has(id)) cards.delete(id);
  }
  placeChildren($("devices"), nodes.length ? nodes : [noDevices]);
}

function notifyNewAlarms(devices) {
  const now = new Set();
  let fresh = false;
  for (const d of devices) {
    if (d.view === "alarm" && d.event) {
      const key = `${d.device_id}:${d.event.id}`;
      now.add(key);
      if (!alarmed.has(key)) fresh = true;
    }
  }
  alarmed = now;
  const title = now.size ? `(경보) 가족 안심 확인` : "가족 안심 확인";
  if (document.title !== title) document.title = title;
  if (now.size === 0) stopRepeat();  // 경보가 모두 끝나면 다시 울리기를 멈춘다
  else if (fresh) ring();
}

// 진동과 소리로 알리고, 소리가 켜져 있으면 경보가 이어지는 동안 ALARM_REPEAT_MS마다 다시 울린다.
function ring() {
  if (navigator.vibrate) navigator.vibrate([400, 200, 400, 200, 400]);
  beep();
  clearTimeout(repeatTimer);
  repeatTimer = soundOn ? setTimeout(repeatRing, ALARM_REPEAT_MS) : null;
}

function repeatRing() {
  repeatTimer = null;
  if (soundOn && alarmed.size) ring();
}

function stopRepeat() {
  clearTimeout(repeatTimer);
  repeatTimer = null;
}

function beep() {
  if (!audio || !soundOn) return;
  // 휴대폰이 잠들었다 깨는 등으로 오디오가 멈춰 있으면 다시 켠 뒤에 울린다.
  if (audio.state !== "running") {
    audio.resume().then(playTones, (err) => console.warn("경보 소리를 낼 수 없음", err));
  } else {
    playTones();
  }
}

function playTones() {
  const t = audio.currentTime;
  for (let i = 0; i < 3; i++) {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.25, t + i * 0.5);
    gain.gain.setValueAtTime(0, t + i * 0.5 + 0.3);
    osc.connect(gain).connect(audio.destination);
    osc.start(t + i * 0.5);
    osc.stop(t + i * 0.5 + 0.3);
  }
}

function renderEvents(events) {
  const rows = events.map((ev) => [
    `${formatTime(ev.started_at)} · ${names[ev.device_id] || ev.device_id}`,
    ev.result ? RESULT_LABELS[ev.result] || ev.result : "확인 중",
    ALERTS.has(ev.result),
  ]);
  const key = JSON.stringify(rows);
  if (key === eventsKey) return;  // 보이는 내용이 그대로면 다시 그리지 않는다
  eventsKey = key;
  $("events").replaceChildren(...(rows.length ? rows.map(([when, what, alarm]) => {
    const li = el("li");
    li.append(el("span", when, "when"), el("span", what, alarm ? "alarm" : ""));
    return li;
  }) : [el("li", "기록이 없습니다.", "muted")]));
}

function render(data) {
  $("offline").classList.toggle("hidden", !!data);
  if (!data) return;
  setText($("who"), `${data.guardian.name}님`);
  names = Object.fromEntries(data.devices.map((d) => [d.device_id, d.name]));

  const sp = $("server-problems");
  setItems(sp, data.server_problems.map((p) => "서버 점검 필요: " + p));
  sp.classList.toggle("hidden", data.server_problems.length === 0);

  renderDevices(data.devices, data.guardian.can_clear);
  notifyNewAlarms(data.devices);
  renderEvents(data.events);
}

// 상태 하나를 읽는다. 서버가 답하지 않거나 끊기면 null (화면에 '서버 연결 끊김' 표시).
async function loadOverview() {
  try {
    const res = await fetchJson("/api/g/overview");
    if (res.status === 401) location.href = "/g/login";
    return res.ok ? res.data : null;
  } catch (err) {
    console.warn("상태 조회 실패", err);
    return null;
  }
}

// 요청이 끝난 뒤에 다음 요청을 예약한다(setTimeout 사슬). 서버가 느려도 요청이 겹쳐 쌓이지 않고,
// 늦게 도착한 옛 응답은 버리므로 이미 끝난 경보가 다시 울리지 않는다.
async function poll() {
  clearTimeout(pollTimer);
  const seq = ++pollSeq;
  const data = await loadOverview();
  try {
    if (seq > renderedSeq) {
      renderedSeq = seq;
      render(data);
    }
  } finally {
    // 가장 최근 요청만 다음 요청을 예약
    if (seq === pollSeq) pollTimer = setTimeout(poll, document.hidden ? HIDDEN_POLL_MS : POLL_MS);
  }
}

async function clearDevice(d, btn) {
  const what = d.view === "alarm" ? "경보를 끌까요?" : "상태 확인을 끝낼까요?";
  if (!confirm(`${d.name}: ${what}\n현장에 계신 분과 통화했거나 직접 확인한 경우에만 눌러 주세요.`)) return;
  btn.disabled = true;
  try {
    const res = await fetchJson(`/api/g/clear?device=${encodeURIComponent(d.device_id)}`,
      { method: "POST", headers: { "X-Guardian-Action": "1" } }, ACTION_TIMEOUT_MS);
    if (!res.ok) alert("해제하지 못했어요 (" + ((res.data && res.data.error) || res.status) + ")");
  } catch (err) {
    alert("해제 요청에 실패했어요: " + (err.name === "AbortError" ? "서버가 응답하지 않아요" : err));
  } finally {
    btn.disabled = false;
    poll();
  }
}

// 경보 소리 켜기·끄기. 켜면 시험 소리를 한 번 내고, 이미 경보 중이면 바로 울리기 시작한다.
$("sound-btn").addEventListener("click", () => {
  const btn = $("sound-btn");
  soundOn = !soundOn;
  if (soundOn) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!audio && Ctx) audio = new Ctx();
    if (alarmed.size) ring();
    else beep();
  } else {
    stopRepeat();
  }
  btn.textContent = soundOn ? "🔔 경보 소리 켜짐 (누르면 꺼짐)" : "🔔 경보 소리 켜기";
  btn.classList.toggle("on", soundOn);
  btn.setAttribute("aria-pressed", String(soundOn));
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();  // 화면이 다시 보이면 바로 읽는다
});
poll();
