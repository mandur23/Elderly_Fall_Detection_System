// 관리자 페이지(/admin). 서버 PC의 브라우저에서만 열린다(server/admin.py의 local_access_problem).
//
// 탭 세 개
// - 현황: 서버 정보, 전체 개수, 기기 상태, 진행 중인 이벤트, 최근 알림 실패. 2초마다 새로 받는다.
// - 사용자: 검색, 목록, 고른 사용자의 보호자·기기·최근 이벤트, 계정 정지·해제, 삭제(이메일을 다시 적어야 함)
// - 이벤트·통계: 필터, 이벤트 표(검토 라벨·메모 저장), CSV 다운로드, 통계(하루별 막대 그래프 등)
//
// 규칙
// - 관리자 API는 모두 X-Admin-Request: 1 머리글을 붙인다. 서버는 상태를 바꾸는 요청(POST·DELETE)에 이것과
//   같은 출처(Origin)를 요구하고, 맞지 않으면 404로 답한다. GET에도 붙여 한 가지 방법으로만 부른다.
// - 서버에서 받은 글(이름, 이메일, 기기 이름, 메모 등)은 모두 ui.el / textContent로 넣는다(innerHTML 금지).
// - 그래프는 라이브러리 없이 SVG 요소를 직접 만든다.

import { api } from './api.js';
import { $, $$, el, show, hide, toggle, toast, errorText, formatTime, resultLabel, stateBadge } from './ui.js';

// ---------------------------------------------------------------- 공통

const ADMIN_HEADERS = { 'X-Admin-Request': '1' };
const OVERVIEW_INTERVAL_MS = 2000;
const EVENTS_PAGE = 100;
const NOTE_MAX = 200;

// 관리자 API 호출. 로그인과 상관없으므로 401이어도 로그인 화면으로 보내지 않는다.
function adminApi(path, opts = {}) {
  return api(path, { ...opts, headers: { ...ADMIN_HEADERS, ...(opts.headers || {}) }, noRedirect: true });
}

// 시험 검토 라벨 (server/db.py의 REVIEW_LABELS)
const REVIEW_LABELS = {
  real_fall: '실제 낙상',
  staged_fall: '연출 낙상',
  daily_activity: '일상 동작',
  false_alarm: '오경보',
};

// 음성 확인 결과 (server/voice.py). NONE은 음성 결과가 없는 이벤트(버튼 해제 등)
const VOICE_LABELS = {
  SAFE: '괜찮음',
  HELP: '도움 요청',
  UNCLEAR: '알아듣지 못함',
  NO_REPLY: '응답 없음',
  DEVICE_FAIL: '장치 오류',
  NONE: '음성 결과 없음',
};

const CHANNEL_LABELS = { websocket: '보호자 화면', push: '웹 푸시' };
const KIND_LABELS = { alert: '경보', clear: '해제' };
const DEVICE_KIND_LABELS = { pi: 'Pi', phone: '휴대폰' };

// 보호자 화면이 닫혀 있어 화면으로 못 보낸 기록(server/notify.py). 그때는 웹 푸시로 보내고,
// 화면을 다시 열면 snapshot으로 받으므로 장애가 아니다. 실패 목록에서 따로 센다.
const CLOSED_SCREEN_DETAIL = '열린 보호자 화면 없음';

// 결과별 묶음: 하루별 막대는 해제·경보·기타 세 계열로만 나눠 그린다(자세한 것은 '판정별'에서 본다).
const RESULT_ORDER = ['CLEARED_VOICE', 'CLEARED_BUTTON', 'ALERT_HELP', 'ALERT_NO_REPLY', 'ALERT_VOICE_FAIL', 'INTERRUPTED', 'SUSPECT'];
const SERIES = [
  { key: 'cleared', name: '해제', color: 'var(--series-cleared)', results: ['CLEARED_VOICE', 'CLEARED_BUTTON'] },
  { key: 'alert', name: '경보', color: 'var(--series-alert)', results: ['ALERT_HELP', 'ALERT_NO_REPLY', 'ALERT_VOICE_FAIL'] },
  { key: 'other', name: '기타(중단·확인 중)', color: 'var(--series-other)', results: ['INTERRUPTED', 'SUSPECT'] },
];
function seriesOf(result) {
  return SERIES.find((s) => s.results.includes(result)) || SERIES[2];
}

function resultText(result) {
  return result ? resultLabel(result) : '음성 확인 중';
}

// 이벤트 상태 배지: 진행 중이면 기기 상태와 같은 배지, 끝났으면 회색 '종료'
function eventStateBadge(state) {
  if (state === 'SUSPECT') return el('span', { class: 'badge badge-suspect', text: '음성 확인 중' });
  if (state === 'ALARM') return stateBadge('ALARM');
  return el('span', { class: 'badge badge-ended', text: '종료' });
}

// 'device:3' -> '기기 3', 'user:5' -> '사용자 5'
function clearedByText(value) {
  if (!value) return '';
  const [kind, id] = String(value).split(':');
  if (kind === 'device') return `기기 ${id}`;
  if (kind === 'user') return `사용자 ${id}`;
  return String(value);
}

function formatSeconds(seconds) {
  if (seconds === null || seconds === undefined) return '-';
  if (seconds < 60) return `${Number(seconds).toFixed(1).replace(/\.0$/, '')}초`;
  const m = Math.floor(seconds / 60);
  return `${m}분 ${Math.round(seconds - m * 60)}초`;
}

function formatUptime(seconds) {
  if (seconds < 60) return `${seconds}초`;
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return [d && `${d}일`, (d || h) && `${h}시간`, `${m}분`].filter(Boolean).join(' ');
}

function formatRate(rate) {
  return rate === null || rate === undefined ? '-' : `${Math.round(rate * 1000) / 10}%`;
}

function clockNow() {
  const t = new Date();
  return [t.getHours(), t.getMinutes(), t.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

// 서버 PC의 날짜(관리자는 서버 PC에서 보므로 브라우저의 현지 날짜와 같다) 'YYYY-MM-DD'
function isoDay(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function daysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d;
}

// 숫자 칸 하나
function tile(key, label, value, sub, { alert = false } = {}) {
  return el('div', { class: 'tile' + (alert ? ' is-alert' : ''), id: `stat-${key}`, 'data-key': key }, [
    el('div', { class: 'label', text: label }),
    el('div', { class: 'value', text: value }),
    sub ? el('div', { class: 'sub', text: sub }) : null,
  ]);
}

// 표의 tbody를 rows로 바꾼다. 비었으면 안내 한 줄
function fillTable(table, rows, emptyText) {
  const tbody = $('tbody', table);
  if (!rows.length) {
    const cols = $$('thead th', table).length;
    tbody.replaceChildren(el('tr', { class: 'empty-row' }, [el('td', { colspan: cols, text: emptyText })]));
    return;
  }
  tbody.replaceChildren(...rows);
}

const td = (text, cls) => el('td', { class: cls, text: text === null || text === undefined || text === '' ? '-' : text });

// ---------------------------------------------------------------- 탭

const TABS = ['overview', 'users', 'events'];
let currentTab = null;

function selectTab(name) {
  if (!TABS.includes(name)) name = 'overview';
  if (name === currentTab) return;
  currentTab = name;
  for (const tab of TABS) {
    const button = $(`#tab-${tab}`);
    button.setAttribute('aria-selected', String(tab === name));
    button.tabIndex = tab === name ? 0 : -1;
    toggle(`#panel-${tab}`, tab === name);
  }
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);

  if (name === 'overview') startOverview(); else stopOverview();
  if (name === 'users') loadUsers().catch(showLoadError('사용자 목록'));
  if (name === 'events') {
    loadUserOptions().catch(showLoadError('사용자 목록'));
    reloadEventsAndStats();
  }
}

function showLoadError(what) {
  return (err) => toast(`${what}을(를) 받지 못했습니다. ${errorText(err)}`, 'error');
}

function setupTabs() {
  for (const button of $$('.tabs [role="tab"]')) {
    button.addEventListener('click', () => selectTab(button.dataset.tab));
    // 탭끼리는 왼쪽·오른쪽 화살표로 옮긴다
    button.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const i = TABS.indexOf(button.dataset.tab);
      const next = TABS[(i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
      selectTab(next);
      $(`#tab-${next}`).focus();
    });
  }
  window.addEventListener('hashchange', () => selectTab(location.hash.slice(1)));
}

// ---------------------------------------------------------------- 현황

let overviewTimer = null;
let overviewBusy = false;

function startOverview() {
  stopOverview();
  refreshOverview();
  overviewTimer = setInterval(() => {
    if (!document.hidden) refreshOverview();
  }, OVERVIEW_INTERVAL_MS);
}

function stopOverview() {
  clearInterval(overviewTimer);
  overviewTimer = null;
}

async function refreshOverview() {
  if (overviewBusy) return;   // 앞 요청이 아직 안 끝났으면 겹쳐 보내지 않는다
  overviewBusy = true;
  try {
    const data = await adminApi('/admin/api/overview');
    renderOverview(data);
    $('#overview-updated').textContent = `마지막 갱신 ${clockNow()} (2초마다 자동 갱신)`;
    hide('#overview-error');
  } catch (err) {
    const box = $('#overview-error');
    box.textContent = '현황을 받지 못했습니다. 화면의 값은 오래된 것일 수 있습니다. ' + errorText(err);
    show(box);
  } finally {
    overviewBusy = false;
  }
}

function renderOverview(data) {
  renderCounts(data.counts);
  renderServerInfo(data.server);
  renderActiveEvents(data.active_events);
  renderDevices(data.devices);
  renderFailures(data.notification_failures);
}

function renderCounts(c) {
  $('#counts').replaceChildren(
    tile('users', '사용자', c.users, c.disabled_users ? `정지 ${c.disabled_users}명` : '정지 없음'),
    tile('devices', '기기', c.devices, `접속 중 ${c.online_devices}대`),
    tile('links', '보호자 연결', c.guardian_links_accepted, `수락 대기 ${c.guardian_links_pending}건`),
    tile('today', '오늘 이벤트', c.events_today, null),
    tile('active', '진행 중 이벤트', c.active_events, null, { alert: c.active_events > 0 }),
  );
}

function renderServerInfo(s) {
  const t = s.thresholds;
  const rows = [
    ['자세 추정', s.pose_backend],
    ['음성 인식', s.stt_backend],
    ['웹 푸시', s.push_enabled ? '켜짐' : '꺼짐 (VAPID 키 없음)'],
    ['연결', s.https ? 'HTTPS' : 'HTTP (시험용, 휴대폰 카메라·푸시 안 됨)'],
    ['시작 시각', formatTime(s.started_at, { withSeconds: true })],
    ['가동 시간', formatUptime(s.uptime_seconds)],
    ['하강 속도 기준', `${t.descent_velocity_peak} 몸통 길이/초`],
    ['누운 자세 유지(T1)', `${t.t1_lying_seconds}초`],
    ['음성 응답 대기', `${t.voice_reply_timeout_seconds}초`],
    ['음성 확인 횟수', `최대 ${t.voice_max_attempts}회`],
  ];
  $('#server-info').replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
}

function renderActiveEvents(events) {
  const box = $('#active-events');
  if (!events.length) {
    box.replaceChildren(el('p', { class: 'empty', text: '진행 중인 이벤트가 없습니다.' }));
    return;
  }
  box.replaceChildren(el('ul', { class: 'active-list' }, events.map((ev) => el('li', {
    'data-event-id': ev.id, 'data-state': ev.state,
  }, [
    eventStateBadge(ev.state),
    el('strong', { text: `#${ev.id}` }),
    el('span', { text: `${ev.owner_name || '-'} · ${ev.device_name || '-'}` }),
    el('span', { class: 'muted', text: `시작 ${formatTime(ev.started_at, { withSeconds: true })}` }),
    ev.result ? el('span', { text: resultText(ev.result) }) : null,
  ]))));
}

function renderDevices(devices) {
  const rows = devices.map((d) => el('tr', { 'data-device-id': d.device_id, 'data-online': d.online ? '1' : '0' }, [
    td(d.device_id, 'num'),
    td(d.name),
    td(DEVICE_KIND_LABELS[d.kind] || d.kind),
    td(d.owner_name ? `${d.owner_name} (${d.owner_id})` : d.owner_id),
    el('td', {}, [stateBadge(d.state, { online: d.online })]),
    td(d.online ? '접속 중' : '끊김'),
    td(d.online ? (d.camera_ok ? '정상' : '응답 없음') : '-'),
    td(d.person_detected ? '감지' : '-'),
    td(d.online ? Number(d.fps || 0).toFixed(1) : '-', 'num'),
    td(formatTime(d.last_seen_at), 'nowrap'),
  ]));
  fillTable($('#device-table'), rows, '등록된 기기가 없습니다.');
}

function renderFailures(failures) {
  const closed = failures.filter((f) => f.detail === CLOSED_SCREEN_DETAIL);
  const real = failures.filter((f) => f.detail !== CLOSED_SCREEN_DETAIL);
  $('#failures-closed').textContent = closed.length
    ? `최근 실패 기록 ${failures.length}건 중 ${closed.length}건은 보호자 화면이 닫혀 있던 경우라 아래 표에서 뺐습니다.`
    : '';
  const rows = real.map((f) => el('tr', { 'data-notification-id': f.id }, [
    td(formatTime(f.created_at, { withSeconds: true }), 'nowrap'),
    td(`#${f.event_id}`, 'num'),
    td(`${f.owner_name || '-'} · ${f.device_name || '-'}`),
    td(f.guardian_name ? `${f.guardian_name} (${f.guardian_id})` : f.guardian_id),
    td(CHANNEL_LABELS[f.channel] || f.channel),
    td(KIND_LABELS[f.kind] || f.kind),
    td(f.detail),
  ]));
  fillTable($('#failure-table'), rows, '최근 알림 실패가 없습니다.');
}

// ---------------------------------------------------------------- 사용자

let selectedUserId = null;
let selectedUser = null;   // 삭제 확인 창에서 이메일을 비교하려고 둔다

async function loadUsers() {
  const query = $('#user-search').value.trim();
  const { users } = await adminApi('/admin/api/users?query=' + encodeURIComponent(query));
  $('#users-count').textContent = query ? `'${query}' 검색 결과 ${users.length}명` : `전체 ${users.length}명`;
  const rows = users.map((u) => {
    const pick = el('button', { type: 'button', class: 'link-btn', text: u.name });
    const row = el('tr', {
      'data-user-id': u.id,
      'data-disabled': u.disabled_at ? '1' : '0',
      class: u.id === selectedUserId ? 'is-selected' : null,
    }, [
      td(u.id, 'num'),
      el('td', {}, [pick]),
      td(u.email, 'nowrap'),
      el('td', {}, [u.disabled_at
        ? el('span', { class: 'badge badge-disabled', text: '정지됨' })
        : el('span', { class: 'badge badge-active', text: '정상' })]),
      td(`${u.online_device_count} / ${u.device_count}`, 'num'),
      td(`${u.guardians_accepted} / ${u.guardians_pending}`, 'num'),
      td(u.wards_count, 'num'),
    ]);
    row.addEventListener('click', () => selectUser(u.id));
    return row;
  });
  fillTable($('#user-table'), rows, query ? '찾는 사용자가 없습니다.' : '가입한 사용자가 없습니다.');
}

async function selectUser(userId) {
  selectedUserId = userId;
  for (const row of $$('#user-table tbody tr')) {
    row.classList.toggle('is-selected', Number(row.dataset.userId) === userId);
  }
  try {
    renderUserDetail(await adminApi(`/admin/api/users/${userId}`));
  } catch (err) {
    selectedUser = null;
    $('#user-detail').replaceChildren(
      el('h2', { id: 'h-user-detail', text: '사용자 정보' }),
      el('p', { class: 'form-error', text: '사용자 정보를 받지 못했습니다. ' + errorText(err) }),
    );
  }
}

function clearUserDetail() {
  selectedUserId = null;
  selectedUser = null;
  $('#user-detail').replaceChildren(
    el('h2', { id: 'h-user-detail', text: '사용자 정보' }),
    el('p', { class: 'empty', text: '왼쪽 목록에서 사용자를 고르세요.' }),
  );
}

function linkItem(link) {
  const u = link.user;
  return el('li', {}, [
    el('span', { text: `${u.name} (${u.email})` }),
    link.status === 'accepted'
      ? el('span', { class: 'badge badge-accepted', text: '연결됨' })
      : el('span', { class: 'badge badge-pending', text: '수락 대기' }),
    u.disabled_at ? el('span', { class: 'badge badge-disabled', text: '정지됨' }) : null,
  ]);
}

function miniList(items, emptyText) {
  return items.length ? el('ul', { class: 'mini' }, items) : el('p', { class: 'empty small', text: emptyText });
}

function renderUserDetail(detail) {
  const u = detail.user;
  selectedUser = u;
  const disabled = Boolean(u.disabled_at);

  const actions = el('div', { class: 'btn-row' }, [
    disabled
      ? el('button', { type: 'button', class: 'btn btn-small', 'data-action': 'enable', text: '정지 해제', on: { click: () => setDisabled(u, false) } })
      : el('button', { type: 'button', class: 'btn btn-outline-danger btn-small', 'data-action': 'disable', text: '계정 정지', on: { click: () => setDisabled(u, true) } }),
    el('button', { type: 'button', class: 'btn btn-danger btn-small', 'data-action': 'delete', text: '삭제', on: { click: () => openDeleteDialog(u) } }),
    el('button', { type: 'button', class: 'btn btn-secondary btn-small', 'data-action': 'events', text: '이 사용자의 이벤트 보기', on: { click: () => showEventsOf(u.id) } }),
  ]);

  const devices = detail.devices.map((d) => el('li', { 'data-device-id': d.device_id }, [
    el('strong', { text: d.name }),
    el('span', { class: 'muted', text: `${DEVICE_KIND_LABELS[d.kind] || d.kind} · 번호 ${d.device_id}` }),
    stateBadge(d.state, { online: d.online }),
    d.online ? el('span', { text: d.camera_ok ? `카메라 정상 · ${Number(d.fps || 0).toFixed(1)} fps` : '카메라 응답 없음' }) : null,
    el('span', { class: 'muted', text: d.last_seen_at ? `마지막 접속 ${formatTime(d.last_seen_at)}` : '접속한 적 없음' }),
  ]));

  const events = detail.recent_events.map((ev) => el('li', { 'data-event-id': ev.id }, [
    el('span', { class: 'muted', text: `#${ev.id}` }),
    el('span', { text: formatTime(ev.started_at, { withSeconds: true }) }),
    el('span', { text: ev.device_name }),
    ev.state === 'ENDED' ? null : eventStateBadge(ev.state),
    el('span', { text: resultText(ev.result) }),
    ev.review_label ? el('span', { class: 'muted', text: `[${REVIEW_LABELS[ev.review_label] || ev.review_label}]` }) : null,
  ]));

  $('#user-detail').replaceChildren(
    el('h2', { id: 'h-user-detail', text: '사용자 정보' }),
    el('p', {}, [
      el('span', { class: 'who', text: u.name }), ' ',
      disabled ? el('span', { class: 'badge badge-disabled', text: '정지됨' }) : el('span', { class: 'badge badge-active', text: '정상' }),
    ]),
    el('dl', { class: 'kv' }, [
      el('dt', { text: '번호' }), el('dd', { text: u.id }),
      el('dt', { text: '이메일' }), el('dd', { text: u.email }),
      el('dt', { text: '가입' }), el('dd', { text: formatTime(u.created_at, { withSeconds: true }) }),
      disabled ? el('dt', { text: '정지' }) : null,
      disabled ? el('dd', { text: formatTime(u.disabled_at, { withSeconds: true }) }) : null,
    ]),
    actions,
    el('h3', { text: '등록한 보호자' }),
    miniList(detail.guardians.map(linkItem), '등록한 보호자가 없습니다.'),
    el('h3', { text: '보호하는 사람' }),
    miniList(detail.wards.map(linkItem), '이 사용자를 보호자로 등록한 사람이 없습니다.'),
    el('h3', { text: '기기' }),
    miniList(devices, '등록한 기기가 없습니다.'),
    el('h3', { text: `최근 이벤트 (최대 20건)` }),
    miniList(events, '이벤트 기록이 없습니다.'),
  );
}

async function setDisabled(user, disable) {
  if (disable && !confirm(`${user.name}(${user.email}) 계정을 정지할까요?\n\n`
    + '로그인과 기존 로그인 세션이 막히고, 열린 보호자 화면과 이 사용자 기기의 카메라 연결이 끊깁니다. '
    + '나중에 \'정지 해제\'로 되돌릴 수 있습니다.')) return;
  const action = disable ? 'disable' : 'enable';
  try {
    await adminApi(`/admin/api/users/${user.id}/${action}`, { method: 'POST' });
    toast(disable ? `${user.name} 계정을 정지했습니다.` : `${user.name} 계정의 정지를 풀었습니다.`, 'success');
  } catch (err) {
    toast((disable ? '정지하지 못했습니다. ' : '정지를 풀지 못했습니다. ') + errorText(err), 'error');
  }
  await Promise.all([loadUsers(), selectUser(user.id)]).catch(showLoadError('사용자 정보'));
}

// --- 삭제 확인 창 ---

function setupDeleteDialog() {
  const dialog = $('#delete-dialog');
  const input = $('#delete-email');
  const button = $('#delete-confirm');
  const matches = () => selectedUser && input.value.trim().toLowerCase() === selectedUser.email.toLowerCase();

  input.addEventListener('input', () => {
    button.disabled = !matches();
    $('#delete-hint').textContent = input.value && !matches() ? '이메일이 아직 맞지 않습니다.' : '';
  });
  $('#delete-cancel').addEventListener('click', () => dialog.close());
  $('#delete-form').addEventListener('submit', async (e) => {
    e.preventDefault();   // method="dialog"로 바로 닫히지 않게 하고, 서버 답을 본 뒤 닫는다
    if (!matches()) return;
    const user = selectedUser;
    button.disabled = true;
    hide('#delete-error');
    try {
      const res = await adminApi(`/admin/api/users/${user.id}`, { method: 'DELETE', json: { confirm_email: input.value.trim() } });
      dialog.close();
      toast(`${user.name} 사용자를 삭제했습니다. (기기 ${res.deleted_devices}대 함께 삭제)`, 'success');
      clearUserDetail();
      userOptionsLoaded = false;
      await loadUsers().catch(showLoadError('사용자 목록'));
    } catch (err) {
      $('#delete-error').textContent = '삭제하지 못했습니다. ' + errorText(err);
      show('#delete-error');
      button.disabled = !matches();
    }
  });
}

function openDeleteDialog(user) {
  $('#delete-who').textContent = `${user.name} (${user.email})`;
  $('#delete-email').value = '';
  $('#delete-hint').textContent = '';
  $('#delete-confirm').disabled = true;
  hide('#delete-error');
  $('#delete-dialog').showModal();
  $('#delete-email').focus();
}

function setupUserSearch() {
  let timer = null;
  const run = () => loadUsers().catch(showLoadError('사용자 목록'));
  $('#user-search-form').addEventListener('submit', (e) => {
    e.preventDefault();
    clearTimeout(timer);
    run();
  });
  // 입력을 멈추면 0.3초 뒤 바로 찾는다
  $('#user-search').addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(run, 300);
  });
}

// ---------------------------------------------------------------- 이벤트·통계

let userOptionsLoaded = false;
let eventsOffset = 0;
let eventsRequest = 0;    // 필터를 빨리 바꿔도 마지막 요청의 결과만 그리려고 센다
let statsRequest = 0;

async function loadUserOptions() {
  if (userOptionsLoaded) return;
  const { users } = await adminApi('/admin/api/users');
  const select = $('#f-user');
  const current = select.value;
  select.replaceChildren(
    el('option', { value: '', text: '전체' }),
    ...users.map((u) => el('option', { value: u.id, text: `${u.name} (${u.email})` })),
  );
  select.value = users.some((u) => String(u.id) === current) ? current : '';
  userOptionsLoaded = true;
}

function setupFilters() {
  const labelSelect = $('#f-label');
  for (const [value, text] of Object.entries(REVIEW_LABELS)) labelSelect.append(el('option', { value, text }));

  const period = $('#f-period');
  const applyPeriod = () => {
    if (period.value === 'custom') return;
    const days = period.value === 'all' ? null : Number(period.value);
    $('#f-from').value = days ? isoDay(daysAgo(days - 1)) : '';
    $('#f-to').value = days ? isoDay(new Date()) : '';
  };
  applyPeriod();
  period.addEventListener('change', () => { applyPeriod(); reloadEventsAndStats(); });
  for (const id of ['#f-from', '#f-to']) {
    $(id).addEventListener('change', () => { period.value = 'custom'; reloadEventsAndStats(); });
  }
  for (const id of ['#f-result', '#f-label', '#f-user']) {
    $(id).addEventListener('change', () => loadEvents().catch(showLoadError('이벤트')));
  }
  $('#event-filters').addEventListener('submit', (e) => e.preventDefault());
  $('#events-more').addEventListener('click', () => loadEvents({ append: true }).catch(showLoadError('이벤트')));
}

function filterQuery() {
  const q = new URLSearchParams();
  const add = (name, value) => { if (value) q.set(name, value); };
  add('from', $('#f-from').value);
  add('to', $('#f-to').value);
  add('result', $('#f-result').value);
  add('label', $('#f-label').value);
  add('user_id', $('#f-user').value);
  return q;
}

// 통계는 '최근 N일'만 받으므로, 기간이 전체·직접 고르기면 최근 30일을 보여 준다
function statsDays() {
  const value = $('#f-period').value;
  return /^\d+$/.test(value) ? Number(value) : 30;
}

function reloadEventsAndStats() {
  loadEvents().catch(showLoadError('이벤트'));
  loadStats().catch(showLoadError('통계'));
}

function showEventsOf(userId) {
  $('#f-period').value = 'all';
  $('#f-from').value = '';
  $('#f-to').value = '';
  userOptionsLoaded = false;
  // 목록을 다시 받은 뒤 그 사용자를 고른다(방금 가입한 사용자도 있게)
  loadUserOptions().then(() => {
    $('#f-user').value = String(userId);
    if (currentTab === 'events') reloadEventsAndStats();
    else location.hash = '#events';
  }).catch(showLoadError('사용자 목록'));
}

async function loadEvents({ append = false } = {}) {
  const q = filterQuery();
  $('#csv-link').href = '/admin/api/events.csv' + (q.toString() ? '?' + q : '');
  const offset = append ? eventsOffset : 0;
  q.set('limit', String(EVENTS_PAGE));
  q.set('offset', String(offset));
  const request = ++eventsRequest;
  const data = await adminApi('/admin/api/events?' + q);
  if (request !== eventsRequest) return;   // 그 사이에 필터가 바뀌었다

  const table = $('#event-table');
  const rows = data.events.map(eventRow);
  if (append) $('tbody', table).append(...rows);
  else fillTable(table, rows, '조건에 맞는 이벤트가 없습니다.');
  eventsOffset = offset + data.events.length;
  $('#events-count').textContent = `${data.total}건 중 ${eventsOffset}건 표시`;
  toggle('#events-more', eventsOffset < data.total);
}

// 채널별 '보낸 수/시도한 수'
function notificationLines(n) {
  const part = (c) => `${c.sent}/${c.sent + c.failed}`;
  return [el('div', { text: `화면 ${part(n.websocket)}` }), el('div', { text: `푸시 ${part(n.push)}` })];
}

function eventRow(ev) {
  const resultCell = el('td', { class: 'col-result' }, [
    ev.state === 'ENDED' ? null : eventStateBadge(ev.state), ' ',
    el('span', { text: resultText(ev.result) }),
    ev.voice_result ? el('div', { class: 'muted small', text: `음성: ${VOICE_LABELS[ev.voice_result] || ev.voice_result}` }) : null,
    ev.cleared_by ? el('div', { class: 'muted small', text: `해제: ${clearedByText(ev.cleared_by)}` }) : null,
  ]);
  const durationCell = el('td', { class: 'num' }, [
    formatSeconds(ev.duration_seconds),
    // 경보 시각은 시작과 같은 날이므로 시:분:초만
    ev.alarm_at ? el('div', { class: 'muted small', text: `경보 ${String(ev.alarm_at).slice(11, 19)}` }) : null,
  ]);
  const notifyCell = el('td', {
    class: 'num',
    title: '보낸 수/시도한 수. 보호자 화면이 닫혀 있으면 화면 알림은 실패로 셉니다(그때는 웹 푸시로 보냅니다).',
  }, notificationLines(ev.notifications));
  return el('tr', { 'data-event-id': ev.id, 'data-result': ev.result || 'SUSPECT' }, [
    td(ev.id, 'num'),
    td(formatTime(ev.started_at, { withSeconds: true }), 'nowrap'),
    td(`${ev.owner_name} · ${ev.device_name}`, 'col-who'),
    resultCell,
    durationCell,
    notifyCell,
    el('td', { class: 'col-label' }, [labelEditor(ev)]),
  ]);
}

// 검토 라벨·메모 입력. 바꾸면 '저장' 버튼이 켜지고, 저장하면 통계를 다시 받는다.
function labelEditor(ev) {
  const select = el('select', { class: 'label-select', 'aria-label': `이벤트 ${ev.id} 검토 라벨` }, [
    el('option', { value: '', text: '라벨 없음' }),
    ...Object.entries(REVIEW_LABELS).map(([value, text]) => el('option', { value, text })),
  ]);
  select.value = ev.review_label || '';
  const note = el('input', {
    type: 'text', class: 'note-input', maxlength: NOTE_MAX, placeholder: '메모',
    'aria-label': `이벤트 ${ev.id} 검토 메모`,
  });
  note.value = ev.review_note || '';
  const save = el('button', { type: 'button', class: 'btn btn-small save-label', text: '저장', disabled: true });
  const box = el('div', { class: 'label-edit' }, [select, note, save]);

  let saved = { label: select.value, note: note.value };
  const markDirty = () => {
    const dirty = select.value !== saved.label || note.value !== saved.note;
    box.classList.toggle('is-dirty', dirty);
    save.disabled = !dirty;
  };
  select.addEventListener('change', markDirty);
  note.addEventListener('input', markDirty);
  note.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !save.disabled) save.click(); });
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const res = await adminApi(`/admin/api/events/${ev.id}/label`, {
        method: 'POST',
        json: { label: select.value || null, note: note.value },
      });
      saved = { label: res.event.review_label || '', note: res.event.review_note || '' };
      select.value = saved.label;
      note.value = saved.note;
      markDirty();
      box.dataset.saved = saved.label || 'none';
      toast(`이벤트 ${ev.id} 검토 내용을 저장했습니다.`, 'success');
      loadStats().catch(showLoadError('통계'));
    } catch (err) {
      markDirty();
      toast('저장하지 못했습니다. ' + errorText(err), 'error');
    }
  });
  return box;
}

// --- 통계 ---

async function loadStats() {
  const request = ++statsRequest;
  const stats = await adminApi(`/admin/api/stats?days=${statsDays()}`);
  if (request !== statsRequest) return;
  renderStats(stats);
}

function renderStats(s) {
  const by = s.totals.by_result;
  const sum = (results) => results.reduce((n, r) => n + (by[r] || 0), 0);
  const d = s.durations;
  const n = s.notifications;
  const labels = s.labels;
  $('#stats-range').textContent = `${formatDay(s.from)} ~ ${formatDay(s.to)} (${s.days}일)`;

  $('#stat-tiles').replaceChildren(
    tile('total', '전체 이벤트', s.totals.total, null),
    tile('alert', '경보', sum(SERIES[1].results), '도움 요청·응답 없음·확인 불가'),
    tile('cleared', '해제', sum(SERIES[0].results), '음성·버튼으로 괜찮다고 함'),
    tile('false-alarm', '오경보율', formatRate(labels.false_alarm_rate),
      `라벨 단 ${labels.labeled}건 중 오경보 ${labels.counts.false_alarm}건 (라벨 없음 ${labels.unlabeled}건)`),
    tile('suspect-to-clear', '평균: 의심 → 해제', formatSeconds(d.suspect_to_clear.average_seconds), `${d.suspect_to_clear.count}건`),
    tile('suspect-to-alarm', '평균: 의심 → 경보', formatSeconds(d.suspect_to_alarm.average_seconds), `${d.suspect_to_alarm.count}건 (음성 확인에 걸린 시간)`),
    tile('alarm-to-clear', '평균: 경보 → 해제', formatSeconds(d.alarm_to_clear.average_seconds), `${d.alarm_to_clear.count}건`),
    tile('push-rate', '웹 푸시 성공률', formatRate(n.push.success_rate), `성공 ${n.push.sent} / 실패 ${n.push.failed}`),
    tile('screen-rate', '화면 알림 전달률', formatRate(n.websocket.success_rate),
      `전달 ${n.websocket.sent} / 못 함 ${n.websocket.failed} (화면이 닫혀 있던 경우 포함, 장애율 아님)`),
  );

  renderDayChart(s.per_day);

  const resultName = (r) => (r === 'SUSPECT' ? '음성 확인 중' : resultLabel(r));
  renderHBars($('#result-bars'), RESULT_ORDER.map((r) => ({ name: resultName(r), count: by[r] || 0, color: seriesOf(r).color })));
  const voiceKeys = Object.keys(VOICE_LABELS).filter((k) => k in s.voice_results)
    .concat(Object.keys(s.voice_results).filter((k) => !(k in VOICE_LABELS)));
  renderHBars($('#voice-bars'), voiceKeys.map((k) => ({ name: VOICE_LABELS[k] || k, count: s.voice_results[k] })),
    '이 기간에 이벤트가 없습니다.');
  renderHBars($('#label-bars'), [
    ...Object.entries(REVIEW_LABELS).map(([k, name]) => ({ name, count: labels.counts[k] || 0 })),
    { name: '라벨 없음', count: labels.unlabeled, color: 'var(--series-other)' },
  ]);
}

function formatDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${Number(m[2])}월 ${Number(m[3])}일` : iso;
}

// 가로 막대 목록. 막대 길이는 가장 큰 값 기준
function renderHBars(box, items, emptyText = '') {
  const max = Math.max(0, ...items.map((i) => i.count));
  if (!items.length && emptyText) {
    box.replaceChildren(el('p', { class: 'empty small', text: emptyText }));
    return;
  }
  box.replaceChildren(...items.map((item) => {
    const fill = el('div', { class: 'fill' });
    fill.style.width = max ? `${(item.count / max) * 100}%` : '0';
    if (item.color) fill.style.background = item.color;
    return el('div', { class: 'hbar' }, [
      el('span', { class: 'name', text: item.name }),
      el('div', { class: 'track', 'aria-hidden': 'true' }, [fill]),
      el('span', { class: 'count', text: item.count }),
    ]);
  }));
}

// --- 하루별 쌓은 막대 그래프 (SVG) ---

const SVG_NS = 'http://www.w3.org/2000/svg';

function svg(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = String(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

// 0부터 max까지 보기 좋은 눈금 (정수만)
function niceTicks(max) {
  if (max <= 4) return Array.from({ length: max + 1 }, (_, i) => i);
  const step = Math.ceil(max / 4);
  const ticks = [];
  for (let v = 0; v <= max + step - 1; v += step) ticks.push(v);
  return ticks;
}

function renderDayChart(perDay) {
  $('#day-legend').replaceChildren(...SERIES.map((s) => {
    const swatch = el('span', { class: 'swatch' });
    swatch.style.background = s.color;
    return el('span', {}, [swatch, s.name]);
  }));

  const W = 960;
  const H = 240;
  const pad = { left: 36, right: 8, top: 18, bottom: 26 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;
  const days = perDay.map((day) => ({
    ...day,
    parts: SERIES.map((s) => ({ series: s, count: s.results.reduce((n, r) => n + (day.by_result[r] || 0), 0) })),
  }));
  const ticks = niceTicks(Math.max(1, ...days.map((d) => d.total)));
  const top = ticks[ticks.length - 1];
  const y = (v) => pad.top + plotH - (v / top) * plotH;
  const slot = plotW / days.length;
  const barW = Math.max(4, Math.min(36, slot * 0.6));
  const labelEvery = days.length > 16 ? Math.ceil(days.length / 16) : 1;

  const root = svg('svg', {
    viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `하루별 이벤트 수 막대 그래프, ${days.length}일. 아래 '표로 보기'에 같은 내용이 있습니다.`,
  });
  for (const t of ticks) {
    root.append(
      svg('line', { class: 'gridline', x1: pad.left, x2: W - pad.right, y1: y(t), y2: y(t) }),
      svg('text', { class: 'axis-text', x: pad.left - 6, y: y(t) + 4, 'text-anchor': 'end', text: t }),
    );
  }
  days.forEach((day, i) => {
    const cx = pad.left + slot * i + slot / 2;
    const group = svg('g', { class: 'bar-group', 'data-date': day.date });
    // 마우스를 올리면 그날의 자세한 내용이 보인다(막대보다 넓은 영역)
    const detail = RESULT_ORDER.filter((r) => day.by_result[r])
      .map((r) => `${r === 'SUSPECT' ? '음성 확인 중' : resultLabel(r)} ${day.by_result[r]}`).join(', ');
    group.append(
      svg('title', { text: `${formatDay(day.date)}: ${day.total}건${detail ? ` (${detail})` : ''}` }),
      svg('rect', { class: 'bar-hit', x: cx - slot / 2, y: pad.top, width: slot, height: plotH }),
    );
    let base = 0;
    for (const part of day.parts) {
      if (!part.count) continue;
      const y0 = y(base);
      const y1 = y(base + part.count);
      // 쌓인 칸 사이에 2px 틈을 둔다
      const height = Math.max(1, y0 - y1 - (base > 0 ? 2 : 0));
      group.append(svg('rect', {
        x: cx - barW / 2, y: y1, width: barW, height, rx: 2, fill: part.series.color,
        'data-series': part.series.key, 'data-count': part.count,
      }));
      base += part.count;
    }
    if (day.total) {
      group.append(svg('text', { class: 'value-text', x: cx, y: y(day.total) - 4, 'text-anchor': 'middle', text: day.total }));
    }
    if (i % labelEvery === 0) {
      const [, m, d] = day.date.split('-');
      group.append(svg('text', { class: 'axis-text', x: cx, y: H - 8, 'text-anchor': 'middle', text: `${Number(m)}/${Number(d)}` }));
    }
    root.append(group);
  });

  // 같은 내용의 표 (색을 구분하기 어렵거나 화면 읽기 프로그램을 쓰는 경우)
  const table = el('table', { class: 'data' }, [
    el('thead', {}, [el('tr', {}, [
      el('th', { scope: 'col', text: '날짜' }), el('th', { scope: 'col', text: '전체' }),
      ...SERIES.map((s) => el('th', { scope: 'col', text: s.name })),
    ])]),
    el('tbody', {}, days.slice().reverse().map((day) => el('tr', {}, [
      td(formatDay(day.date)), td(day.total, 'num'), ...day.parts.map((p) => td(p.count, 'num')),
    ]))),
  ]);
  $('#day-chart').replaceChildren(root, el('details', {}, [el('summary', { class: 'small', text: '표로 보기' }), table]));
}

// ---------------------------------------------------------------- 시작

setupTabs();
setupUserSearch();
setupDeleteDialog();
setupFilters();
selectTab(location.hash.slice(1) || 'overview');
