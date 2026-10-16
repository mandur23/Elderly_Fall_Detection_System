// 관리자 화면: /api/admin/overview를 1초마다 읽어 모든 장치 상태와 기록을 그린다.
// 바뀐 칸만 고쳐 그려서 폴링 중에도 버튼 클릭·포커스·글자 선택이 지워지지 않게 한다.
"use strict";

const POLL_MS = 1000;
const HIDDEN_POLL_MS = 10000;     // 탭이 가려져 있을 때는 천천히 읽는다
const FETCH_TIMEOUT_MS = 3000;    // 상태 조회가 이 시간 안에 끝나지 않으면 끊고 '서버 연결 끊김'으로 보인다
const ACTION_TIMEOUT_MS = 10000;  // 해제·저장·지우기 같은 버튼 요청의 시간 제한

const VIEW_LABELS = {
  alarm: "경보",
  suspect: "낙상 의심",
  check: "장치 점검",
  disconnected: "연결 끊김",
  normal: "정상 감시",
};

const DETECT_LABELS = { NORMAL: "정상", FALLING: "하강 감지", SUSPECT: "낙상 의심" };

const RESULT_LABELS = {
  CLEARED_VOICE: "음성으로 괜찮다고 응답",
  CLEARED_BUTTON: "현장 화면 버튼으로 해제",
  CLEARED_ADMIN: "관리자 화면에서 해제",
  CLEARED_GUARDIAN: "보호자 웹에서 해제",
  ALERT_HELP: "도움 요청 · 경보",
  ALERT_NO_REPLY: "응답 없음 · 경보",
  ALERT_VOICE_FAIL: "음성 확인 불가 · 경보",
  ALERT_DISCONNECTED: "확인 중 연결 끊김 · 경보",
  ALERT_INTERRUPTED: "확인 중 서버 재시작 · 경보",
  INTERRUPTED: "처리 중 서버 재시작",  // 예전 기록
};
const ALERT_RESULTS = new Set(["ALERT_HELP", "ALERT_NO_REPLY", "ALERT_VOICE_FAIL", "ALERT_DISCONNECTED",
  "ALERT_INTERRUPTED"]);

const NOTIFY_LABELS = {
  none: "-",
  pending: "보내는 중",
  sent: "보냄",
  failed: "실패 · 다시 보냄",
  disabled: "알림 미설정",
  cancelled: "취소(해제됨)",
};

const CLEARED_BY = { BUTTON: "현장 버튼", ADMIN: "관리자" };

function clearedByLabel(by) {
  if (!by) return "-";
  if (by.startsWith("GUARDIAN:")) return `보호자 (${by.slice(9)})`;
  return CLEARED_BY[by] || by;
}

const $ = (id) => document.getElementById(id);
let names = {};
const cards = new Map();  // 장치 id → { node: 화면의 카드, data: 마지막으로 그린 장치 정보 }
const noDevices = el("p", "아직 접속한 라즈베리파이가 없습니다.", "muted");
let eventsKey = "";
let pollTimer = null;
let pollSeq = 0;      // 보낸 상태 요청 번호
let renderedSeq = 0;  // 화면에 그린 응답의 요청 번호. 이보다 먼저 보낸 요청의 응답은 늦게 와도 버린다

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

// 관리자 동작 요청: X-Admin-Action 헤더를 붙이고, 로그인이 풀렸으면 로그인 화면으로 보낸다.
async function adminFetch(url, options = {}, ms = ACTION_TIMEOUT_MS) {
  const res = await fetchJson(url, { ...options, headers: { "X-Admin-Action": "1", ...(options.headers || {}) } }, ms);
  if (res.status === 401) location.href = "/admin/login";
  return res;
}

function failText(err) {
  return err.name === "AbortError" ? "서버가 응답하지 않습니다" : String(err);
}

function formatTime(iso) {
  return iso ? iso.slice(5, 19).replace("T", " ") : "-";
}

function formatDuration(seconds) {
  if (seconds == null) return "-";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}시간 ${m}분` : `${m}분`;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

// 글자가 바뀌었을 때만 바꾼다 (같은 글자를 다시 넣으면 그 안의 글자 선택이 풀린다).
function setText(node, text) {
  const s = String(text);
  if (node.textContent !== s) node.textContent = s;
}

// 목록 내용이 바뀌었을 때만 다시 그린다. lines가 비고 emptyText가 있으면 그 문구를 흐리게 보인다.
function setItems(list, lines, emptyText) {
  const key = JSON.stringify([lines, emptyText]);
  if (list.dataset.key === key) return;
  list.dataset.key = key;
  list.replaceChildren(...(lines.length || emptyText == null
    ? lines.map((p) => el("li", p)) : [el("li", emptyText, "muted")]));
}

// dl의 항목 이름이 그대로면 값(dd)만 고친다.
function setRows(dl, rows) {
  const labels = JSON.stringify(rows.map(([k]) => k));
  if (dl.dataset.labels !== labels) {
    dl.dataset.labels = labels;
    dl.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", k), el("dd", v)]));
    return;
  }
  rows.forEach(([, v], i) => setText(dl.children[i * 2 + 1], v));
}

// 순서가 달라진 노드만 옮긴다. 제자리인 노드는 떼었다 붙이지 않아 포커스가 유지된다.
function placeChildren(box, nodes) {
  nodes.forEach((node, i) => {
    if (box.children[i] !== node) box.insertBefore(node, box.children[i] || null);
  });
  while (box.children.length > nodes.length) box.lastElementChild.remove();
}

function setCheck(node, ok, connected) {
  node.classList.toggle("ok", connected && ok === true);
  node.classList.toggle("bad", connected && ok === false);
}

function eventDetail(d) {
  const ev = d.event;
  if (d.view === "alarm" && ev) {
    return `${formatTime(ev.started_at)} 경보 — ${RESULT_LABELS[ev.result] || ev.result} (보호자 알림: ${NOTIFY_LABELS[ev.notify_state] || ev.notify_state})`;
  }
  if (d.view === "suspect" && ev) {
    const step = ev.phase === "listen" ? "대답을 듣는 중" : ev.phase === "prompt" ? "안내 음성 재생 중" : "확인 준비 중";
    return `${formatTime(ev.started_at)} 낙상 의심 — 음성 확인 ${ev.voice_attempt || 1}/${ev.voice_attempts}회, ${step}`;
  }
  if (!d.connected) return `연결 끊김 (${formatTime(d.since)}부터)`;
  if (d.person_detected) return `사람 보임 · 판정 ${DETECT_LABELS[d.detect_state] || d.detect_state || "-"}`;
  return "사람이 보이지 않음";
}

// 장치마다 카드는 한 번만 만들고, 버튼은 늘 그 장치의 최신 정보(entry.data)로 동작한다.
function createCard() {
  const entry = { node: $("device-card").content.firstElementChild.cloneNode(true), data: null };
  const clearBtn = entry.node.querySelector(".clear-btn");
  clearBtn.addEventListener("click", () => clearDevice(entry.data, clearBtn));
  const forgetBtn = entry.node.querySelector(".forget-btn");
  forgetBtn.addEventListener("click", () => forgetDevice(entry.data, forgetBtn));
  return entry;
}

function updateCard(entry, d) {
  entry.data = d;
  const card = entry.node;
  const cls = "card view-" + d.view;
  if (card.className !== cls) card.className = cls;
  setText(card.querySelector(".name"), d.name);
  setText(card.querySelector(".id"), d.device_id);
  setText(card.querySelector(".state-badge"), VIEW_LABELS[d.view] || d.view);
  setText(card.querySelector(".detail"), eventDetail(d));

  setCheck(card.querySelector(".camera"), d.camera_ok, d.connected);
  setCheck(card.querySelector(".mic"), d.mic_ok, d.connected);
  setCheck(card.querySelector(".speaker"), d.speaker_ok, d.connected);

  const rows = [
    ["영상 처리", d.connected ? `${d.processed_fps ?? 0} fps (받음 ${d.fps ?? "-"} fps)` : "-"],
    ["최근 24시간", `낙상 의심 ${d.last_24h.events}건, 경보 ${d.last_24h.alarms}건`],
  ];
  if (d.cpu_temp_c != null) rows.push(["CPU 온도", `${d.cpu_temp_c}°C`]);
  if (d.connected) rows.push(["접속", `${formatTime(d.since)}부터`]);
  setRows(card.querySelector(".stats"), rows);

  const lines = [...d.problems];
  if (d.alert_sent) lines.push("보호자에게 장치 이상을 알렸습니다");
  setItems(card.querySelector(".problems"), lines);

  // 화면 열쇠가 든 현장 화면 주소는 서버가 만들어 준다(kiosk_url). 없으면 링크를 숨긴다.
  const link = card.querySelector(".kiosk-link");
  if (d.kiosk_url && link.getAttribute("href") !== d.kiosk_url) link.href = d.kiosk_url;
  link.classList.toggle("hidden", !d.kiosk_url);

  card.querySelector(".clear-btn").classList.toggle("hidden", d.view !== "alarm" && d.view !== "suspect");
  // 연결이 끊겼고 처리 중인 낙상 의심·경보가 없는 장치만 목록에서 지울 수 있다.
  card.querySelector(".forget-btn").classList.toggle("hidden", d.connected || d.state !== "NORMAL");
}

function renderDevices(devices) {
  const seen = new Set();
  const nodes = devices.map((d) => {
    seen.add(d.device_id);
    let entry = cards.get(d.device_id);
    if (!entry) {
      entry = createCard();
      cards.set(d.device_id, entry);
    }
    // 해제·지우기 요청 중인 카드는 건드리지 않음 (끝나면 바로 다시 읽어 그린다)
    if (!entry.node.querySelector("button:disabled")) updateCard(entry, d);
    return entry.node;
  });
  for (const id of cards.keys()) {
    if (!seen.has(id)) cards.delete(id);
  }
  placeChildren($("devices"), nodes.length ? nodes : [noDevices]);
}

// 기록 표는 보이는 내용이 바뀌었을 때만 다시 그린다.
function renderEvents(events) {
  const rows = events.map((ev) => [
    ALERT_RESULTS.has(ev.result),
    formatTime(ev.started_at),
    names[ev.device_id] || ev.device_id || "-",
    ev.result ? RESULT_LABELS[ev.result] || ev.result : "확인 중",
    NOTIFY_LABELS[ev.notify_state] || ev.notify_state,
    clearedByLabel(ev.cleared_by),
  ]);
  const key = JSON.stringify(rows);
  if (key === eventsKey) return;
  eventsKey = key;

  const tbody = $("events");
  if (rows.length === 0) {
    const td = el("td", "기록이 없습니다.", "muted");
    td.colSpan = 5;
    const tr = el("tr");
    tr.append(td);
    tbody.replaceChildren(tr);
    return;
  }
  tbody.replaceChildren(...rows.map(([alarm, ...cells]) => {
    const tr = el("tr", null, alarm ? "alarm" : "");
    tr.append(...cells.map((text) => el("td", text)));
    return tr;
  }));
}

function render(data) {
  $("offline-banner").classList.toggle("hidden", !!data);
  if (!data) return;
  names = Object.fromEntries(data.devices.map((d) => [d.device_id, d.name]));

  const s = data.server;
  setText($("server-info"), `서버 실행 ${formatDuration(s.uptime_s)} · 안내 음성: ${s.prompt_source || "준비 중"}`);
  $("notify-badge").classList.toggle("hidden", s.notifier_enabled);
  $("logout").classList.toggle("hidden", !s.login);

  const sp = $("server-problems");
  setItems(sp, s.problems.map((p) => "서버: " + p));
  sp.classList.toggle("hidden", s.problems.length === 0);

  const sum = data.summary;
  setText($("sum-connected"), `${sum.connected} / ${sum.known}`);
  setText($("sum-alarm"), sum.alarm);
  setText($("sum-suspect"), sum.suspect);
  setText($("sum-check"), sum.check);
  document.querySelector(".tile.alarm").classList.toggle("on", sum.alarm > 0);

  const alarms = data.devices.filter((d) => d.view === "alarm");
  const banner = $("alarm-banner");
  setText(banner, alarms.length ? `경보: ${alarms.map((d) => d.name).join(", ")} — 현장 상태를 확인해 주세요.` : "");
  banner.classList.toggle("hidden", alarms.length === 0);
  const title = alarms.length ? `(경보 ${alarms.length}) 낙상 감지 관리자` : "낙상 감지 관리자";
  if (document.title !== title) document.title = title;

  renderDevices(data.devices);
  renderEvents(data.events);
  setItems($("system-events"), data.system_events.map((e) => `${formatTime(e.at)}  ${e.message}`), "기록이 없습니다.");
}

// 상태 하나를 읽는다. 서버가 답하지 않거나 끊기면 null (화면에 '서버 연결 끊김' 표시).
async function loadOverview() {
  try {
    const res = await fetchJson("/api/admin/overview");
    if (res.status === 401) location.href = "/admin/login";
    return res.ok ? res.data : null;
  } catch (err) {
    console.warn("상태 조회 실패", err);
    return null;
  }
}

// 요청이 끝난 뒤에 다음 요청을 예약한다(setTimeout 사슬). 서버가 느려도 요청이 겹쳐 쌓이지 않는다.
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
  const what = d.view === "alarm" ? "경보를" : "낙상 의심 확인을";
  if (!confirm(`${d.name}: ${what} 해제할까요?\n현장 상태를 확인한 뒤에 해제해 주세요.`)) return;
  btn.disabled = true;
  try {
    const res = await adminFetch(`/api/admin/clear?device=${encodeURIComponent(d.device_id)}`, { method: "POST" });
    if (!res.ok && res.status !== 401) alert("해제하지 못했습니다: " + res.status);
  } catch (err) {
    alert("해제 요청 실패: " + failText(err));
  } finally {
    btn.disabled = false;
    poll();
  }
}

// 더 쓰지 않는 라즈베리파이를 장치 목록에서 지운다 (서버는 재시작해도 장치를 기억하므로).
async function forgetDevice(d, btn) {
  if (!confirm(`'${d.name}' (${d.device_id}) 장치를 목록에서 지울까요?\n더 이상 쓰지 않는 라즈베리파이일 때만 지워 주세요.`)) return;
  btn.disabled = true;
  try {
    const res = await adminFetch(`/api/admin/devices/${encodeURIComponent(d.device_id)}`, { method: "DELETE" });
    if (!res.ok && res.status !== 401) {
      alert("목록에서 지우지 못했습니다: " + ((res.data && res.data.error) || res.status));
    }
  } catch (err) {
    alert("지우기 요청 실패: " + failText(err));
  } finally {
    btn.disabled = false;
    poll();
  }
}

// --- 보호자 계정 ---
const GUARDIAN_REFRESH_MS = 10000;
let guardianData = { guardians: [], devices: [] };
let guardiansKey = "";
let guardianSeq = 0;  // 늦게 도착한 옛 목록이 새 목록을 덮지 않게

function renderGuardianDevices(selected) {
  const box = $("guardian-devices");
  const known = guardianData.devices;
  const ids = new Set(known.map((d) => d.device_id));
  box.replaceChildren(...(known.length ? known.map((d) => {
    const label = el("label");
    const cb = el("input");
    cb.type = "checkbox";
    cb.name = "device";
    cb.value = d.device_id;
    cb.checked = selected.includes(d.device_id);
    label.append(cb, `${d.name} (${d.device_id})`);
    return label;
  }) : [el("span", "아직 접속한 장치가 없습니다.", "muted")]));
  document.querySelector("#guardian-form [name=extra_devices]").value =
    selected.filter((id) => !ids.has(id)).join(", ");
}

function resetGuardianForm() {
  const form = $("guardian-form");
  form.reset();
  form.elements.id.value = "";
  form.elements.username.readOnly = false;
  form.elements.password.required = true;
  form.elements.password.placeholder = "8자 이상";
  $("guardian-form-title").textContent = "보호자 추가";
  $("guardian-cancel").classList.add("hidden");
  $("guardian-error").classList.add("hidden");
  renderGuardianDevices([]);
}

function editGuardian(g) {
  const form = $("guardian-form");
  form.elements.id.value = g.id;
  form.elements.username.value = g.username;
  form.elements.username.readOnly = true;
  form.elements.name.value = g.name;
  form.elements.password.value = "";
  form.elements.password.required = false;
  form.elements.password.placeholder = "바꿀 때만 입력 (8자 이상)";
  form.elements.can_clear.checked = g.can_clear;
  $("guardian-form-title").textContent = `보호자 수정: ${g.username}`;
  $("guardian-cancel").classList.remove("hidden");
  renderGuardianDevices(g.devices);
  form.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderGuardians() {
  // 보이는 내용이 그대로면 다시 그리지 않는다 (수정·삭제 버튼을 누르는 도중에 바뀌지 않게).
  const key = JSON.stringify([guardianData.guardians, guardianData.guardians.map((g) => g.devices.map((id) => names[id]))]);
  if (key === guardiansKey) return;
  guardiansKey = key;
  const body = $("guardians");
  if (guardianData.guardians.length === 0) {
    const td = el("td", "보호자 계정이 없습니다. 오른쪽에서 추가하세요.", "muted");
    td.colSpan = 6;
    const tr = el("tr");
    tr.append(td);
    body.replaceChildren(tr);
    return;
  }
  body.replaceChildren(...guardianData.guardians.map((g) => {
    const tr = el("tr");
    const actions = el("td", null, "row-actions");
    const edit = el("button", "수정");
    edit.type = "button";
    edit.addEventListener("click", () => editGuardian(g));
    const del = el("button", "삭제");
    del.type = "button";
    del.addEventListener("click", async () => {
      if (!confirm(`보호자 계정 '${g.username}'을(를) 지울까요? 그 보호자는 바로 로그아웃됩니다.`)) return;
      del.disabled = true;
      try {
        const res = await adminFetch(`/api/admin/guardians/${g.id}`, { method: "DELETE" });
        if (!res.ok && res.status !== 401) alert("지우지 못했습니다: " + ((res.data && res.data.error) || res.status));
      } catch (err) {
        alert("지우기 요청 실패: " + failText(err));
      } finally {
        del.disabled = false;
        loadGuardians();
      }
    });
    actions.append(edit, del);
    tr.append(
      el("td", g.username),
      el("td", g.name),
      el("td", g.devices.map((id) => names[id] ? `${names[id]}` : id).join(", ") || "-"),
      el("td", g.can_clear ? "있음" : "없음"),
      el("td", g.last_login_at ? formatTime(g.last_login_at) : "-"),
      actions,
    );
    return tr;
  }));
}

async function loadGuardians() {
  const seq = ++guardianSeq;
  try {
    const res = await adminFetch("/api/admin/guardians", { headers: {} }, FETCH_TIMEOUT_MS);
    if (!res.ok || !res.data || seq !== guardianSeq) return;
    guardianData = res.data;
  } catch (err) {
    console.warn("보호자 목록 조회 실패", err);
    return;
  }
  renderGuardians();
  const form = $("guardian-form");
  if (!form.contains(document.activeElement)) {  // 입력 중이면 장치 목록을 다시 그리지 않음
    const selected = [...form.querySelectorAll("[name=device]:checked")].map((c) => c.value);
    const extra = form.elements.extra_devices.value.split(",").map((s) => s.trim()).filter(Boolean);
    renderGuardianDevices([...selected, ...extra]);
  }
}

// 보호자 목록도 요청이 끝난 뒤에 다음 요청을 예약한다.
async function guardianLoop() {
  await loadGuardians();
  setTimeout(guardianLoop, GUARDIAN_REFRESH_MS);
}

$("guardian-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const devices = [...form.querySelectorAll("[name=device]:checked")].map((c) => c.value);
  devices.push(...form.elements.extra_devices.value.split(",").map((s) => s.trim()).filter(Boolean));
  const body = { name: form.elements.name.value, devices, can_clear: form.elements.can_clear.checked };
  if (form.elements.password.value) body.password = form.elements.password.value;
  const id = form.elements.id.value;
  if (!id) body.username = form.elements.username.value;
  const err = $("guardian-error");
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    const res = await adminFetch(id ? `/api/admin/guardians/${id}` : "/api/admin/guardians", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      err.textContent = (res.data && res.data.error) || `저장하지 못했습니다 (${res.status})`;
      err.classList.remove("hidden");
      return;
    }
  } catch (ex) {
    err.textContent = "저장 요청 실패: " + failText(ex);
    err.classList.remove("hidden");
    return;
  } finally {
    submit.disabled = false;
  }
  resetGuardianForm();
  loadGuardians();
});
$("guardian-cancel").addEventListener("click", resetGuardianForm);

// --- 라즈베리파이 추가 (한 줄 설치 명령) ---
// 서버가 만든 명령(장치 토큰 포함)에 장치 이름·id를 붙여 보인다. 토큰이 늘 화면에 보이지 않게 펼쳤을 때만 불러온다.
const EDGE_ID_RE = /^[A-Za-z0-9._-]{1,64}$/;  // edge/deploy/setup.sh와 같은 규칙
let edgeSetup = null;  // { server_url, version, token, command }
let edgeSeq = 0;       // 늦게 도착한 옛 응답이 새 응답을 덮지 않게

// bash 작은따옴표 문자열로 감싼다 (안의 ' 는 '\'' 로). 이름에 한글·공백·따옴표가 있어도 그대로 전달된다.
function shellQuote(text) {
  return "'" + String(text).replace(/'/g, "'\\''") + "'";
}

function setEdgeError(text) {
  const err = $("edge-setup-error");
  err.textContent = text || "";
  err.classList.toggle("hidden", !text);
}

function renderEdgeCommand() {
  const box = $("edge-command");
  const copy = $("edge-copy");
  setText($("edge-copy-result"), "");
  if (!edgeSetup) {
    copy.disabled = true;
    return;
  }
  const form = $("edge-setup-form");
  const name = form.elements.name.value.trim();
  const id = form.elements.id.value.trim();
  if (id && !EDGE_ID_RE.test(id)) {
    setEdgeError("장치 id는 영문·숫자·점(.)·밑줄(_)·하이픈(-)으로 64자까지 쓸 수 있습니다.");
    box.value = "";
    copy.disabled = true;
    return;
  }
  setEdgeError("");
  let command = edgeSetup.command ||
    `curl -fsSL ${edgeSetup.server_url}/edge/setup.sh | bash -s -- --token ${shellQuote(edgeSetup.token)}`;
  if (name) command += " --name " + shellQuote(name);
  if (id) command += " --id " + shellQuote(id);
  box.value = command;
  copy.disabled = false;
}

async function loadEdgeSetup() {
  const seq = ++edgeSeq;
  let problem = "";
  let data = null;
  try {
    const res = await adminFetch("/api/admin/edge-setup", {}, FETCH_TIMEOUT_MS);
    if (res.status === 401 || seq !== edgeSeq) return;
    const d = res.data;
    if (res.ok && d && (typeof d.command === "string" || (d.server_url && d.token))) {
      data = d;
    } else if (res.status === 404) {
      problem = "이 서버는 라즈베리파이 한 줄 설치를 지원하지 않습니다. 서버를 업데이트해 주세요.";
    } else {
      problem = `설치 명령을 불러오지 못했습니다 (${res.status}).`;
    }
  } catch (err) {
    if (seq !== edgeSeq) return;
    problem = "설치 명령 요청 실패: " + failText(err);
  }
  edgeSetup = data;
  setText($("edge-version"), data ? data.version || "알 수 없음" : "-");
  if (!data) {
    $("edge-command").value = "";
    setEdgeError(problem);
  }
  renderEdgeCommand();
}

async function copyEdgeCommand() {
  const box = $("edge-command");
  let copied = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(box.value);
      copied = true;
    } catch (err) {
      copied = false;
    }
  }
  if (!copied) {
    // 서버를 http(보안 연결 아님)로 열면 clipboard API가 없어, 글자를 골라 두고 브라우저 복사를 쓴다.
    box.focus();
    box.select();
    try {
      copied = document.execCommand("copy");
    } catch (err) {
      copied = false;
    }
  }
  setText($("edge-copy-result"), copied ? "복사했습니다. 라즈베리파이 터미널에 붙여 넣으세요."
    : "명령을 선택해 두었습니다. Ctrl+C로 복사하세요.");
}

$("edge-setup").addEventListener("toggle", () => {
  if ($("edge-setup").open) loadEdgeSetup();  // 펼칠 때마다 새로 읽는다 (서버 주소·버전이 바뀌었을 수 있음)
});
$("edge-setup-form").addEventListener("input", renderEdgeCommand);
$("edge-setup-form").addEventListener("submit", (e) => e.preventDefault());
$("edge-copy").addEventListener("click", copyEdgeCommand);

function tickClock() {
  const d = new Date();
  $("clock").textContent = [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0")).join(":");
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();  // 탭이 다시 보이면 바로 읽는다
});
setInterval(tickClock, 1000);
tickClock();
poll();
resetGuardianForm();
guardianLoop();
