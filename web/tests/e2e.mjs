// 모바일 웹 종단 간(e2e) 테스트.
//
// 실행: node web/tests/e2e.mjs   (자세한 방법은 web/tests/README.md)
//
// 이 스크립트가 하는 일
// 1. 중앙 서버(python -m server)를 직접 띄운다. 가짜 자세 추정·가짜 음성 인식, 임시 데이터 폴더, 빈 포트를 쓴다.
//    웹은 서버가 함께 내보내므로(별도 웹 서버 없음) 서버 하나만 띄우면 된다.
// 2. 실제 Chromium(Playwright)으로 화면을 누르며 시나리오를 차례로 돌린다.
//    Pi 기기는 Node 22에 들어 있는 WebSocket으로 흉내 낸다(가짜 자세 JSON 프레임을 보낸다).
// 3. 시나리오마다 PASS/FAIL을 찍고, 하나라도 실패하면 종료 코드 1로 끝낸다. 서버는 항상 끈다.
//
// 시나리오는 앞 시나리오가 만든 계정·기기를 이어서 쓴다(실제 사용 순서와 같게).
// 앞 단계가 실패하면 뒤 단계는 '앞 단계 실패'로 함께 실패한다.

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Playwright는 저장소에 설치하지 않고 전역 설치본을 쓴다.
// ES 모듈의 import는 NODE_PATH(전역 경로)를 보지 않으므로 require로 불러온다.
const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  console.error('playwright를 찾을 수 없습니다. `npm install -g playwright && npx playwright install chromium` 후 다시 실행하세요.');
  process.exit(2);
}

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const PYTHON = process.env.PYTHON || findPython();
const HEADED = process.env.HEADED === '1';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findPython() {
  // 저장소 안 가상환경이 있으면 그것을, 없으면 PATH의 python3를 쓴다.
  for (const p of ['.venv/bin/python', 'venv/bin/python', '.venv/Scripts/python.exe']) {
    if (fs.existsSync(path.join(REPO, p))) return path.join(REPO, p);
  }
  return 'python3';
}

// ---------------------------------------------------------------- 서버 실행

const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'fall-e2e-'));
const SERVER_LOG = path.join(DATA_DIR, 'server.log');
let PORT = 0;
let BASE = '';
let server = null;

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

async function startServer() {
  PORT = await freePort();
  BASE = `http://localhost:${PORT}`;
  const env = {
    ...process.env,
    FALL_POSE_BACKEND: 'fake',     // 프레임 바이트를 관절 좌표 JSON으로 읽는다
    FALL_STT_BACKEND: 'fake',      // 녹음 바이트를 인식 문장(UTF-8)으로 읽는다
    FALL_COOKIE_SECURE: '0',       // http://localhost 에서도 로그인 쿠키가 붙게
    FALL_DATA_DIR: DATA_DIR,
    FALL_HOST: '127.0.0.1',
    FALL_PORT: String(PORT),
    FALL_T1_SECONDS: '0.3',        // 누운 뒤 0.3초면 '확인 중'으로 (기본 2초)
    // 카메라 화면은 안내(약 1.5초) + 녹음 5초 + 업로드를 거치므로 그보다 넉넉해야 한다.
    FALL_VOICE_TIMEOUT: '12',
    PYTHONUNBUFFERED: '1',
  };
  // 웹 푸시 키가 환경에 있으면 테스트가 실제 푸시를 시도하므로 지운다.
  delete env.FALL_VAPID_PRIVATE_KEY;
  delete env.FALL_VAPID_PUBLIC_KEY;
  delete env.FALL_SSL_CERT;
  delete env.FALL_SSL_KEY;
  const log = fs.openSync(SERVER_LOG, 'a');
  server = spawn(PYTHON, ['-m', 'server'], { cwd: REPO, env, stdio: ['ignore', log, log] });
  let exitInfo = null;
  server.exitPromise = new Promise((r) => server.once('exit', (code, signal) => { exitInfo = { code, signal }; r(); }));
  for (let i = 0; i < 150; i++) {
    if (exitInfo) throw new Error(`서버가 바로 종료됨 (코드 ${exitInfo.code}). 로그: ${SERVER_LOG}`);
    try {
      const r = await fetch(`${BASE}/api/push/vapid-public-key`);
      if (r.ok) return;
    } catch { /* 아직 안 뜸 */ }
    await sleep(200);
  }
  throw new Error(`서버가 30초 안에 뜨지 않음. 로그: ${SERVER_LOG}`);
}

async function stopServer() {
  if (!server) return;
  const s = server;
  server = null;
  if (s.exitCode !== null || s.signalCode !== null) return;
  s.kill('SIGTERM');
  const done = await Promise.race([s.exitPromise.then(() => true), sleep(5000).then(() => false)]);
  if (!done) s.kill('SIGKILL');
}

// Ctrl+C 로 멈춰도 서버가 남지 않게
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => { await stopServer(); process.exit(130); });
}

// ---------------------------------------------------------------- 결과 기록

const results = [];   // {name, ok, ms, error}
const consoleErrors = [];

class CheckError extends Error {}
function check(cond, message) {
  if (!cond) throw new CheckError(message);
}
function need(value, what) {
  if (value === undefined || value === null) throw new CheckError(`앞 단계 실패로 ${what} 없음`);
  return value;
}

async function scenario(name, fn) {
  const t0 = Date.now();
  process.stdout.write(`- ${name} ... `);
  try {
    await fn();
    results.push({ name, ok: true, ms: Date.now() - t0 });
    console.log(`PASS (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
  } catch (err) {
    results.push({ name, ok: false, ms: Date.now() - t0, error: err });
    console.log(`FAIL (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
    console.log(`    ${err instanceof CheckError ? err.message : err.stack}`);
  }
}

// 페이지의 JS 오류를 모은다. 다음은 페이지 코드 오류가 아니라 브라우저 네트워크 기록이므로 뺀다.
// - 'Failed to load resource': 로그아웃 상태의 /api/me 401, 틀린 비밀번호 401,
//   저장소에 없는 /static/audio/ask_status.mp3 404 등 예상한 응답
// - 'WebSocket connection to': 서버가 연결을 거절·종료했을 때 브라우저가 찍는 기록
const IGNORED_CONSOLE = [/^Failed to load resource/, /^WebSocket connection to/];

function watchPage(page, tag) {
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const text = m.text();
    if (IGNORED_CONSOLE.some((re) => re.test(text))) return;
    consoleErrors.push(`[${tag}] console.error: ${text}`);
  });
  page.on('pageerror', (e) => consoleErrors.push(`[${tag}] pageerror: ${e.message}`));
  // 확인 창(삭제·해제 확인)은 모두 '확인'을 누른다.
  page.on('dialog', (d) => d.accept().catch(() => {}));
  return page;
}

// ---------------------------------------------------------------- Pi 흉내

// server/detection/pose.py 의 fake_frame 과 같은 모양
function fakeFrame(kind, dy = 0) {
  if (kind === 'standing') {
    return JSON.stringify({
      ls: [305, 200 + dy], rs: [335, 200 + dy], lh: [310, 300 + dy], rh: [330, 300 + dy],
      bbox: [290, 150 + dy, 350, 450 + dy],
    });
  }
  return JSON.stringify({
    ls: [220, 385 + dy], rs: [220, 415 + dy], lh: [320, 390 + dy], rh: [320, 410 + dy],
    bbox: [150, 370 + dy, 470, 430 + dy],
  });
}

// Pi 클라이언트처럼 /ws/camera 에 붙어 10fps로 프레임을 보낸다.
// Node의 WebSocket은 헤더를 못 붙이므로 기기 키를 ?key= 로 보낸다(서버가 둘 다 받는다).
function connectPi(deviceId, key) {
  const ws = new WebSocket(`ws://localhost:${PORT}/ws/camera?device_id=${deviceId}&key=${encodeURIComponent(key)}`);
  ws.binaryType = 'arraybuffer';
  const messages = [];
  const enc = new TextEncoder();
  let pose = 'standing';
  let timer = null;
  let closeCode = null;   // 서버가 닫은 코드 (계정 정지 → 4403)
  ws.addEventListener('message', (e) => {
    if (typeof e.data === 'string') messages.push(JSON.parse(e.data));
  });
  ws.addEventListener('close', (e) => { closeCode = e.code; clearInterval(timer); });
  const opened = new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error('Pi WebSocket 연결 실패')), { once: true });
  });
  const sendPose = () => {
    if (ws.readyState === WebSocket.OPEN) ws.send(enc.encode(pose === 'standing' ? fakeFrame('standing') : fakeFrame('lying', 100)));
  };
  return {
    messages,
    get closeCode() { return closeCode; },
    async waitClosed(timeout = 5000) {
      for (let i = 0; i < timeout / 50 && closeCode === null; i++) await sleep(50);
      if (closeCode === null) throw new CheckError('서버가 Pi 연결을 끊지 않음');
      return closeCode;
    },
    async open() {
      await opened;
      timer = setInterval(sendPose, 100);
    },
    // 서 있다가 빠르게 내려가 눕는다 → FALLING → (T1 뒤) SUSPECT
    async fall() {
      pose = 'standing';
      await sleep(600);
      pose = 'lying';
    },
    async standUp() {
      pose = 'standing';
      await sleep(800);  // 판정기가 '일어선 상태'를 보고 다음 넘어짐을 받을 준비를 하게
    },
    async waitVoiceCheck(count) {
      for (let i = 0; i < 100; i++) {
        const checks = messages.filter((m) => m.type === 'voice_check');
        if (checks.length >= count) return checks[count - 1];
        await sleep(50);
      }
      throw new CheckError(`Pi가 ${count}번째 voice_check를 받지 못함`);
    },
    close() {
      clearInterval(timer);
      ws.close();
    },
  };
}

// Pi처럼 녹음(가짜 STT는 바이트를 문장으로 읽는다)을 multipart로 올린다.
async function uploadPiVoice(eventId, attempt, key, text) {
  const form = new FormData();
  form.append('audio', new Blob([text], { type: 'audio/wav' }), 'reply.wav');
  form.append('attempt', String(attempt));
  const r = await fetch(`${BASE}/api/events/${eventId}/voice`, { method: 'POST', body: form, headers: { 'X-Device-Key': key } });
  return { status: r.status, body: await r.json().catch(() => null) };
}

// ---------------------------------------------------------------- 공용 동작

async function getJson(ctx, url, headers) {
  const r = await ctx.request.get(BASE + url, { headers });
  return { status: r.status(), body: await r.json().catch(() => null) };
}

async function signupUI(page, user) {
  await page.goto(BASE + '/signup');
  await page.fill('#name', user.name);
  await page.fill('#email', user.email);
  await page.fill('#password', user.password);
  await page.fill('#password2', user.password);
  await page.click('#submit');
  await page.waitForURL(BASE + '/mypage');
  await page.waitForFunction((n) => document.querySelector('#my-name').textContent === n, user.name);
}

async function loginUI(page, user, password = user.password) {
  await page.goto(BASE + '/login');
  await page.fill('#email', user.email);
  await page.fill('#password', password);
  await page.click('#submit');
}

async function waitText(page, selector, pattern, timeout = 8000) {
  await page.waitForFunction(([sel, src]) => {
    const node = document.querySelector(sel);
    return node && new RegExp(src).test(node.textContent);
  }, [selector, pattern.source], { timeout });
}

async function eventById(ctx, eventId) {
  const { body } = await getJson(ctx, '/api/events?limit=30');
  return body.events.find((e) => e.id === eventId);
}

async function waitEventEnded(ctx, eventId, timeout = 8000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const ev = await eventById(ctx, eventId);
    if (ev && ev.ended_at) return ev;
    await sleep(150);
  }
  throw new CheckError(`이벤트 ${eventId}가 ${timeout}ms 안에 끝나지 않음`);
}

// 서버에 머리글을 마음대로 정해 요청한다. Node의 fetch는 Host 머리글을 바꿔도 무시하고 실제 주소를 보내므로,
// 'Host: evil.example'(DNS 리바인딩 흉내) 같은 확인은 node:http로 한다.
function rawRequest(method, urlPath, headers = {}, body = undefined) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port: PORT, method, path: urlPath, headers }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try { json = JSON.parse(text); } catch { /* JSON이 아님 */ }
        resolve({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

// 서버 PC 브라우저가 보내는 것과 같은 관리자 요청 (localhost, 같은 출처 Origin, X-Admin-Request)
function adminRequest(method, urlPath, json = undefined) {
  const headers = { Host: `localhost:${PORT}`, 'X-Admin-Request': '1' };
  if (method !== 'GET') headers.Origin = `http://localhost:${PORT}`;
  if (json !== undefined) headers['Content-Type'] = 'application/json';
  return rawRequest(method, urlPath, headers, json === undefined ? undefined : JSON.stringify(json));
}

async function adminUserByEmail(email) {
  const { json } = await adminRequest('GET', '/admin/api/users?query=' + encodeURIComponent(email));
  return json.users.find((u) => u.email === email);
}

// ---------------------------------------------------------------- 시나리오

const stamp = Date.now();
const WARD = { name: '김할머니', email: `ward${stamp}@example.com`, password: 'password1' };
const GUARD = { name: '김보호', email: `guard${stamp}@example.com`, password: 'password2' };
const OTHER = { name: '이웃', email: `other${stamp}@example.com`, password: 'password3' };

const state = {};  // 시나리오 사이에 넘기는 값 (기기 id, 키, 이벤트 id …)

async function main() {
  console.log(`서버 시작: ${PYTHON} -m server (데이터 ${DATA_DIR})`);
  await startServer();
  console.log(`서버 준비됨: ${BASE}\n`);

  const browser = await chromium.launch({
    headless: !HEADED,
    // 가짜 카메라·마이크를 쓰고 권한 창은 자동으로 허용
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required'],
  });
  const newContext = async (opts = {}) => {
    const ctx = await browser.newContext({ viewport: { width: 360, height: 780 }, locale: 'ko-KR', ...opts });
    ctx.setDefaultTimeout(10000);  // 한 단계가 막혀도 전체가 2분을 넘지 않게
    return ctx;
  };

  const ctxWard = await newContext({ permissions: ['camera', 'microphone'] });
  const ctxGuard = await newContext();
  const ctxOther = await newContext();
  const wardPage = watchPage(await ctxWard.newPage(), 'ward');
  const guardPage = watchPage(await ctxGuard.newPage(), 'guardian');
  let pi = null;

  try {
    await scenario('회원가입·로그아웃·틀린 비밀번호·로그인', async () => {
      await signupUI(wardPage, WARD);
      await wardPage.click('.site-nav >> text=로그아웃');
      await wardPage.waitForURL(BASE + '/');
      await wardPage.waitForSelector('#logged-out:not(.hidden)');
      check(await wardPage.isVisible('#logged-out >> text=응급 신고를 대신하지 않습니다'), '첫 화면에 보조 수단 안내가 없음');
      check((await getJson(ctxWard, '/api/me')).status === 401, '로그아웃 뒤에도 /api/me 가 401이 아님');

      // 로그인이 필요한 화면 → 로그인 화면으로 (next 유지)
      await wardPage.goto(BASE + '/mypage');
      await wardPage.waitForURL(/\/login\?next=/);

      await loginUI(wardPage, WARD, 'wrong-password');
      await wardPage.waitForSelector('#error:not(.hidden)');
      check((await wardPage.textContent('#error')).includes('올바르지 않습니다'), '틀린 비밀번호 오류 문구가 다름');
      check(wardPage.url().includes('/login'), '틀린 비밀번호인데 로그인 화면을 벗어남');

      await wardPage.goto(BASE + '/login?next=/mypage');
      await wardPage.fill('#email', WARD.email);
      await wardPage.fill('#password', WARD.password);
      await wardPage.click('#submit');
      await wardPage.waitForURL(BASE + '/mypage');
      await wardPage.waitForFunction((n) => document.querySelector('#my-name').textContent === n, WARD.name);

      // 로그인 뒤 돌아갈 주소(next)로 다른 사이트에 보낼 수 없다(open redirect).
      // 브라우저는 주소의 탭·줄바꿈을 지우고 '\'를 '/'로 읽으므로 '/<탭>/evil'도 '//evil'(다른 사이트)이 된다.
      // 이미 로그인되어 있으면 로그인 화면이 바로 next로 넘기므로, 그 자리에서 확인한다.
      const evil = (u) => u.hostname === 'evil.example';   // 주소 안의 next=...가 아니라 실제 이동한 곳만
      await wardPage.route(evil, (route) => route.abort());
      for (const bad of ['//evil.example/x', '/%09/evil.example/x', '/%0A/evil.example/x', '/%0D/evil.example/x',
        '/.//evil.example/x', '/%5C/evil.example/x', 'https://evil.example/x']) {
        await wardPage.goto(`${BASE}/login?next=${bad}`);
        await wardPage.waitForURL((u) => u.pathname === '/' || !u.href.startsWith(BASE), { timeout: 5000 });
        check(wardPage.url() === BASE + '/', `next=${bad} 로 다른 곳에 감: ${wardPage.url()}`);
      }
      await wardPage.unroute(evil);
      await wardPage.goto(`${BASE}/login?next=${encodeURIComponent('/mypage?from=login#top')}`);
      await wardPage.waitForURL(BASE + '/mypage?from=login#top');

      await signupUI(guardPage, GUARD);
    });

    await scenario('보호자 요청 → 다른 브라우저에서 수락 → 보호 대상 표시', async () => {
      await wardPage.goto(BASE + '/mypage');
      await wardPage.waitForSelector('#guardians li');
      await wardPage.fill('#guardian-email', GUARD.email);
      await wardPage.click('#guardian-form button[type=submit]');
      await wardPage.waitForSelector('#guardians .badge-pending');

      await guardPage.goto(BASE + '/mypage');
      await guardPage.waitForSelector('#requests-card:not(.hidden)');
      check((await guardPage.textContent('#requests')).includes(WARD.name), '보호자 쪽에 받은 요청이 없음');
      await guardPage.click('#requests >> text=수락');
      await waitText(guardPage, '#wards', new RegExp(WARD.name));

      await wardPage.reload();
      await wardPage.waitForSelector('#guardians .badge-accepted');
      check((await wardPage.textContent('#guardians')).includes(GUARD.name), '수락 뒤 보호자 이름이 보이지 않음');
    });

    await scenario('Pi 기기 추가 → 키는 한 번만 표시', async () => {
      await wardPage.fill('#device-name', '거실 카메라');
      await wardPage.click('#device-form button[type=submit]');
      await wardPage.waitForSelector('#new-device:not(.hidden)');
      const env = await wardPage.textContent('#new-device-env');
      const kiosk = await wardPage.textContent('#new-device-kiosk');
      const id = /PI_DEVICE_ID=(\d+)/.exec(env)?.[1];
      const key = /PI_DEVICE_KEY=(\S+)/.exec(env)?.[1];
      check(id && key, `설정 내용에 PI_DEVICE_ID/PI_DEVICE_KEY가 없음: ${env}`);
      check(kiosk.includes(`/kiosk?device_id=${id}&key=`), `현장 화면 주소가 다름: ${kiosk}`);
      check((await getJson(ctxWard, `/api/devices/${id}/state`, { 'X-Device-Key': key })).status === 200, '발급된 키로 기기 상태를 볼 수 없음');

      await wardPage.click('#close-new-device');  // '적어 두셨나요?' 확인 창 → 확인
      await wardPage.waitForSelector('#new-device', { state: 'hidden' });
      check(!(await wardPage.content()).includes(key), '닫은 뒤에도 키가 화면에 남아 있음');
      await wardPage.reload();
      await wardPage.waitForSelector(`#devices li[data-device-id="${id}"]`);
      check(!(await wardPage.content()).includes(key), '새로 고침 뒤 키가 다시 보임');
      check(!(await wardPage.evaluate(() => JSON.stringify(localStorage))).includes(key), '키가 localStorage에 저장됨');
      state.piId = Number(id);
      state.piKey = key;
    });

    await scenario('보호자 화면: 스냅샷에 보호 대상의 Pi 기기 표시', async () => {
      const id = need(state.piId, 'Pi 기기');
      await guardPage.goto(BASE + '/guardian');
      await guardPage.waitForSelector('#conn-banner.hidden', { state: 'attached', timeout: 8000 });
      const card = `.device-card[data-device-id="${id}"]`;
      await guardPage.waitForSelector(card);
      check((await guardPage.textContent(`${card} .name`)) === '거실 카메라', '기기 이름이 다름');
      check((await guardPage.getAttribute(card, 'data-online')) === '0', '접속 전인데 온라인으로 보임');
      check(!(await guardPage.isVisible('#empty-help')), '보호 대상이 있는데 연결 안내가 보임');
      // 경보 소리를 미리 켜 둔다 (실제 보호자도 처음에 한 번 누른다)
      await guardPage.click('#sound-unlock');
    });

    await scenario('Pi 넘어짐 → 보호자 화면 SUSPECT → "도와주세요" → ALARM → 경보 해제', async () => {
      const id = need(state.piId, 'Pi 기기');
      const card = `.device-card[data-device-id="${id}"]`;
      pi = connectPi(id, state.piKey);
      await pi.open();
      await guardPage.waitForSelector(`${card}[data-look="normal"]`, { timeout: 8000 });

      await pi.fall();
      await guardPage.waitForSelector('#active-events .event-card[data-state="SUSPECT"]', { timeout: 8000 });
      check((await guardPage.textContent(`${card} .badge`)) === '상태 확인 중', '기기 배지가 상태 확인 중이 아님');
      check(!(await guardPage.isVisible('#alarm-bar')), 'SUSPECT인데 경보 띠가 보임');

      const vc = await pi.waitVoiceCheck(1);
      const up = await uploadPiVoice(vc.event_id, vc.attempt, state.piKey, '도와주세요');
      check(up.status === 200 && up.body?.accepted === true, `음성 업로드가 받아들여지지 않음: ${up.status} ${JSON.stringify(up.body)}`);

      const alarmCard = `#active-events .event-card[data-event-id="${vc.event_id}"][data-state="ALARM"]`;
      await guardPage.waitForSelector(alarmCard, { timeout: 8000 });
      check((await guardPage.textContent(alarmCard)).includes('도움 요청'), '경보 카드에 "도움 요청"이 없음');
      check(await guardPage.isVisible('#alarm-bar'), '경보 띠가 보이지 않음');

      await guardPage.click(`${alarmCard} button[data-action="clear"]`);  // 확인 창 → 확인
      await guardPage.waitForSelector(alarmCard, { state: 'detached', timeout: 8000 });
      check(!(await guardPage.isVisible('#alarm-bar')), '해제 뒤에도 경보 띠가 보임');
      const ev = await waitEventEnded(ctxGuard, vc.event_id);
      check(ev.result === 'ALERT_HELP' && ev.cleared_by, `서버 기록이 다름: ${JSON.stringify(ev)}`);
      await waitText(guardPage, '#history', /경보 해제됨/);
    });

    await scenario('현장 화면(kiosk): 상태 표시 → 괜찮아요로 SUSPECT 해제', async () => {
      const id = need(state.piId, 'Pi 기기');
      need(pi, 'Pi 접속');
      const ctxKiosk = await newContext({ viewport: { width: 800, height: 480 } });
      const kiosk = watchPage(await ctxKiosk.newPage(), 'kiosk');
      try {
        await pi.standUp();
        await kiosk.goto(`${BASE}/kiosk?device_id=${id}&key=${encodeURIComponent(state.piKey)}`);
        check(!kiosk.url().includes('key='), `주소창에 기기 키가 남아 있음: ${kiosk.url()}`);
        await kiosk.waitForSelector('body.state-normal', { timeout: 8000 });
        check((await kiosk.textContent('#k-title')).includes('정상'), '정상 화면 문구가 다름');
        check(!(await kiosk.isVisible('#k-ok')), '정상인데 괜찮아요 버튼이 보임');
        // 화면이 새로 고쳐져도(주소에는 이미 키가 없다) '설정 필요'에 멈추지 않고 계속 동작한다
        await kiosk.reload();
        await kiosk.waitForSelector('body.state-normal', { timeout: 8000 });
        check(!kiosk.url().includes('key='), `새로 고친 뒤 주소창에 기기 키가 있음: ${kiosk.url()}`);
        check(!(await kiosk.evaluate(() => JSON.stringify(localStorage))).includes(state.piKey), '기기 키가 localStorage에 저장됨');

        const before = pi.messages.filter((m) => m.type === 'voice_check').length;
        await pi.fall();
        await kiosk.waitForSelector('body.state-suspect', { timeout: 8000 });
        check(await kiosk.isVisible('#k-ok'), 'SUSPECT인데 괜찮아요 버튼이 없음');
        const vc = await pi.waitVoiceCheck(before + 1);
        await guardPage.waitForSelector(`.event-card[data-event-id="${vc.event_id}"]`, { timeout: 8000 });

        await kiosk.click('#k-ok');
        await kiosk.waitForSelector('body.state-normal', { timeout: 8000 });
        const ev = await waitEventEnded(ctxGuard, vc.event_id);
        check(ev.result === 'CLEARED_BUTTON', `서버 결과가 CLEARED_BUTTON이 아님: ${ev.result}`);
        check(ev.cleared_by === `device:${id}`, `해제한 쪽이 기기가 아님: ${ev.cleared_by}`);
        // 보호자 화면에서도 카드가 사라진다
        await guardPage.waitForSelector(`.event-card[data-event-id="${vc.event_id}"]`, { state: 'detached', timeout: 8000 });
        const sw = await kiosk.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]);
        check(sw[0] <= 800 && sw[1] <= 480, `현장 화면에 스크롤이 생김: ${sw}`);
      } finally {
        await ctxKiosk.close();
      }
    });

    await scenario('카메라 화면: 가짜 카메라로 접속·전송 → voice_check → 녹음 업로드', async () => {
      // 실제 카메라 프레임(JPEG)은 가짜 자세 추정기가 읽지 못하므로, 브라우저와 서버 사이에서
      // 프레임을 가짜 자세 JSON으로 바꿔 서버가 '넘어짐'을 보게 한다. 그 밖의 메시지는 그대로 전달한다.
      let mode = 'jpeg';
      let jpegFrames = 0;
      await wardPage.routeWebSocket(/\/ws\/camera/, (ws) => {
        const upstream = ws.connectToServer();
        ws.onMessage((m) => {
          if (typeof m === 'string') return upstream.send(m);
          if (m[0] === 0xff && m[1] === 0xd8) jpegFrames++;
          if (mode === 'jpeg') upstream.send(m);
          else upstream.send(Buffer.from(mode === 'standing' ? fakeFrame('standing') : fakeFrame('lying', 100)));
        });
        upstream.onMessage((m) => ws.send(m));
        ws.onClose((code, reason) => upstream.close({ code, reason }));
        upstream.onClose((code, reason) => ws.close({ code, reason }));
      });
      // 업로드한 multipart 내용을 확인하려고 페이지의 fetch를 감싼다 (Playwright는 Blob 본문을 보여 주지 않는다).
      await wardPage.addInitScript(() => {
        window.__voiceForms = [];
        const original = window.fetch;
        window.fetch = function (url, init) {
          if (/\/voice$/.test(String(url)) && init && init.body instanceof FormData) {
            const fields = {};
            for (const [k, v] of init.body.entries()) fields[k] = v instanceof Blob ? { filename: v.name, size: v.size } : v;
            window.__voiceForms.push(fields);
          }
          return original.apply(this, arguments);
        };
      });
      const uploads = [];
      wardPage.on('response', async (res) => {
        if (/\/api\/events\/\d+\/voice$/.test(res.url())) uploads.push({ status: res.status(), body: await res.json().catch(() => null) });
      });

      await wardPage.goto(BASE + '/camera');
      await wardPage.click('#start-btn');
      await waitText(wardPage, '#conn-badge', /^연결됨$/, 10000);
      const { body: list } = await getJson(ctxWard, '/api/devices');
      const phone = list.devices.find((d) => d.kind === 'phone');
      check(phone, '휴대폰 기기가 등록되지 않음');
      state.phoneId = phone.id;

      await sleep(1200);
      check(jpegFrames >= 5, `JPEG 프레임이 충분히 가지 않음 (${jpegFrames}장)`);
      const st = await getJson(ctxWard, `/api/devices/${phone.id}/state`);
      check(st.body.device.online && st.body.device.camera_ok, `서버가 카메라를 온라인으로 보지 않음: ${JSON.stringify(st.body.device)}`);
      // 보호자 화면에도 휴대폰 카메라가 접속 중으로 보인다
      await guardPage.waitForSelector(`.device-card[data-device-id="${phone.id}"][data-online="1"]`, { timeout: 8000 });

      mode = 'standing'; await sleep(700);
      mode = 'lying'; await sleep(1200);
      mode = 'jpeg';
      await wardPage.waitForSelector('#alert-overlay:not(.hidden)', { timeout: 8000 });
      check((await wardPage.textContent('#alert-text')).includes('괜찮으신가요'), '음성 확인 안내 문구가 없음');
      await waitText(wardPage, '#alert-step', /말씀해 주세요/, 12000);

      for (let i = 0; i < 150 && uploads.length === 0; i++) await sleep(100);
      check(uploads.length >= 1, '녹음 업로드가 없음');
      const up = uploads[0];
      check(up.status === 200 && up.body?.accepted === true, `서버가 업로드를 받지 않음: ${up.status} ${JSON.stringify(up.body)}`);
      const [form] = await wardPage.evaluate(() => window.__voiceForms);
      check(form && /^reply\.\w+$/.test(form.audio?.filename || '') && form.audio.size > 0 && form.attempt === '1',
        `업로드 multipart 모양이 다름: ${JSON.stringify(form)}`);

      // 가짜 STT는 webm을 문장으로 못 읽으므로 '음성 확인 불가' 경보가 된다 → 카메라 화면이 빨간색으로
      await wardPage.waitForSelector('#alert-overlay.is-alarm', { timeout: 10000 });
      const { body: evs } = await getJson(ctxWard, '/api/events?limit=5');
      const ev = evs.events.find((e) => e.device_id === phone.id);
      check(ev && ev.state === 'ALARM', `카메라 이벤트가 ALARM이 아님: ${JSON.stringify(ev)}`);

      await wardPage.click('#ok-btn');
      await wardPage.waitForSelector('#alert-overlay', { state: 'hidden', timeout: 8000 });
      const ended = await waitEventEnded(ctxWard, ev.id);
      check(ended.cleared_by, '괜찮아요로 해제되지 않음');

      await wardPage.click('#stop-btn');
      await waitText(wardPage, '#conn-badge', /연결 안 됨/);
    });

    await scenario('카메라 화면(가짜 연결): 못 전한 괜찮아요는 실패로 안내 · 끊긴 업로드는 다시 보냄', async () => {
      // 서버 대신 이 테스트가 /ws/camera 를 맡아 원하는 메시지를 보낸다(프레임은 버린다).
      const page = watchPage(await ctxWard.newPage(), 'camera-mock');
      let fakeServer = null;
      await page.routeWebSocket(/\/ws\/camera/, (ws) => {
        fakeServer = ws;
        ws.onMessage(() => {});
      });
      let stateMode = 'abort';           // 'abort' = 서버에 닿지 못함, 'event' = 진행 중 이벤트 알려 줌
      await page.route(/\/api\/devices\/\d+\/state$/, (route) => (stateMode === 'abort'
        ? route.abort()
        : route.fulfill({ json: { device: {}, active_event: { id: 90001, state: 'ALARM' } } })));
      const clears = [];
      await page.route(/\/api\/events\/\d+\/clear$/, (route) => {
        clears.push(route.request().url());
        return route.fulfill({ json: { ok: true } });
      });
      let voiceCalls = 0;
      await page.route(/\/api\/events\/\d+\/voice$/, (route) => {
        voiceCalls += 1;
        return voiceCalls === 1 ? route.abort() : route.fulfill({ json: { accepted: true } });
      });
      try {
        await page.goto(BASE + '/camera');
        await page.click('#start-btn');
        await waitText(page, '#conn-badge', /^연결됨$/, 10000);
        const send = (m) => need(fakeServer, '가짜 서버 연결').send(JSON.stringify(m));

        // 1) 다시 연결한 뒤의 첫 state에는 이벤트 번호가 없다. 번호를 묻지도 못하면 '전했다'고 하면 안 된다.
        send({ type: 'state', state: 'ALARM' });
        await page.waitForSelector('#alert-overlay:not(.hidden)');
        await page.click('#ok-btn');
        await page.waitForSelector('.toast-error', { timeout: 5000 });
        check(await page.isVisible('#alert-overlay'), '전하지 못했는데 경보 화면이 닫힘');
        check(!(await page.isVisible('.toast-success')), '전하지 못했는데 "괜찮다고 전했습니다"가 뜸');
        check(clears.length === 0, `해제 요청이 갔음: ${clears}`);
        check(await page.isEnabled('#ok-btn'), '실패 뒤 괜찮아요 버튼을 다시 누를 수 없음');
        // 서버에 다시 닿으면 같은 버튼으로 전해진다
        stateMode = 'event';
        await page.click('#ok-btn');
        await page.waitForSelector('#alert-overlay', { state: 'hidden', timeout: 5000 });
        check(clears.length === 1 && clears[0].endsWith('/api/events/90001/clear'), `해제 요청이 다름: ${clears}`);
        send({ type: 'state', state: 'NORMAL' });

        // 2) 녹음을 올리다 끊기면, 서버가 같은 voice_check를 다시 보낼 때 녹음해 둔 대답을 다시 올린다
        const vc = { type: 'voice_check', event_id: 90002, attempt: 1 };
        send(vc);
        await page.waitForSelector('#alert-overlay:not(.hidden)');
        for (let i = 0; i < 150 && voiceCalls < 1; i++) await sleep(100);
        check(voiceCalls === 1, '녹음 업로드를 시도하지 않음');
        await waitText(page, '.toast-error', /대답을 보내지 못했습니다/, 5000);
        const t0 = Date.now();
        send(vc);   // 다시 연결했을 때 서버가 보내는 것과 같은 요청
        for (let i = 0; i < 50 && voiceCalls < 2; i++) await sleep(100);
        check(voiceCalls === 2, '같은 요청을 다시 받았는데 다시 올리지 않음');
        // 다시 녹음(5초)하지 않고 남겨 둔 녹음을 바로 올렸는가
        check(Date.now() - t0 < 3000, `다시 올리는 데 너무 오래 걸림(다시 녹음함?): ${Date.now() - t0}ms`);
        await waitText(page, '#alert-step', /대답을 확인하고 있습니다/, 5000);
        send(vc);   // 이번에는 올렸으므로 또 받아도 무시한다
        await sleep(600);
        check(voiceCalls === 2, `이미 올린 회차를 또 올림 (${voiceCalls}번)`);

        send({ type: 'state', state: 'NORMAL' });
        await page.waitForSelector('#alert-overlay', { state: 'hidden' });
        await page.click('#stop-btn');
        await waitText(page, '#conn-badge', /연결 안 됨/);
      } finally {
        await page.close();
      }
    });

    await scenario('권한: 관계없는 사용자는 기기·이벤트를 볼 수 없음', async () => {
      const id = need(state.piId, 'Pi 기기');
      const page = watchPage(await ctxOther.newPage(), 'other');
      await signupUI(page, OTHER);
      await page.goto(BASE + '/guardian');
      await page.waitForSelector('#conn-banner.hidden', { state: 'attached', timeout: 8000 });
      await sleep(500);
      check((await page.$$('.device-card')).length === 0, '남의 기기가 보호자 화면에 보임');
      check((await page.$$('#active-events .event-card')).length === 0, '남의 이벤트가 보임');
      check(await page.isVisible('#empty-help'), '연결 안내가 보이지 않음');
      check((await page.textContent('#history')).includes('아직 기록이 없습니다'), '최근 기록에 남의 기록이 보임');

      const events = await getJson(ctxOther, '/api/events?limit=30');
      check(events.status === 200 && events.body.events.length === 0, '남의 이벤트가 API로 보임');
      const devices = await getJson(ctxOther, '/api/devices');
      check(devices.body.devices.length === 0, '남의 기기가 API로 보임');
      const st = await getJson(ctxOther, `/api/devices/${id}/state`);
      check(st.status === 403 || st.status === 404, `남의 기기 상태가 보임 (${st.status})`);
      const { body: wardEvents } = await getJson(ctxWard, '/api/events?limit=1');
      const r = await ctxOther.request.post(`${BASE}/api/events/${wardEvents.events[0].id}/clear`);
      check(r.status() === 403 || r.status() === 404, `남의 이벤트를 해제할 수 있음 (${r.status()})`);
    });

    await scenario('카메라 화면: 감지 중 마이페이지에서 기기를 지우면 멈추고 새 기기를 만들지 않음', async () => {
      const phoneId = need(state.phoneId, '휴대폰 기기');
      const page = watchPage(await ctxWard.newPage(), 'camera-delete');
      try {
        await page.goto(BASE + '/camera');
        await page.click('#start-btn');
        await waitText(page, '#conn-badge', /^연결됨$/, 10000);
        const before = (await getJson(ctxWard, '/api/devices')).body.devices.filter((d) => d.kind === 'phone');
        check(before.length === 1 && before[0].id === phoneId, `휴대폰 기기 목록이 다름: ${JSON.stringify(before)}`);

        await wardPage.goto(BASE + '/mypage');
        await wardPage.click(`#devices li[data-device-id="${phoneId}"] >> text=삭제`);  // 확인 창 → 확인
        await wardPage.waitForSelector(`#devices li[data-device-id="${phoneId}"]`, { state: 'detached', timeout: 8000 });

        await waitText(page, '#conn-badge', /연결 안 됨/, 8000);
        await waitText(page, '#camera-error', /삭제/, 5000);
        check(await page.isVisible('#start-btn'), '감지가 멈추지 않음(감지 시작 버튼이 없음)');
        await sleep(800);
        const after = (await getJson(ctxWard, '/api/devices')).body.devices.filter((d) => d.kind === 'phone');
        check(after.length === 0, `지운 뒤 새 휴대폰 기기가 저절로 생김: ${JSON.stringify(after)}`);
      } finally {
        await page.close();
      }
    });

    // ------------------------------------------------------------ 관리자 페이지 (/admin)
    // 서버 PC 화면(넓은 창)에서 연다. 관리자 페이지는 로그인과 상관없으므로 로그인하지 않은 브라우저를 쓴다.
    const ctxAdmin = await newContext({ viewport: { width: 1280, height: 900 } });
    const admin = watchPage(await ctxAdmin.newPage(), 'admin');

    await scenario('관리자 현황: localhost·127.0.0.1에서 열림, 기기·진행 중 이벤트 표시(2초 자동 갱신)', async () => {
      const id = need(state.piId, 'Pi 기기');
      need(pi, 'Pi 접속');
      await admin.goto(BASE + '/admin');
      check((await admin.title()).startsWith('관리자 (서버 PC 전용)'), `제목이 다름: ${await admin.title()}`);
      check(!(await admin.$('.site-nav')), '관리자 페이지에 사용자용 메뉴가 붙음');
      const row = `#device-table tr[data-device-id="${id}"]`;
      await admin.waitForSelector(`${row}[data-online="1"]`);
      const rowText = await admin.textContent(row);
      check(rowText.includes('거실 카메라') && rowText.includes(WARD.name), `기기 줄에 이름·주인이 없음: ${rowText}`);
      check((await admin.textContent(`${row} .badge`)) === '정상 감시 중', '기기 상태 배지가 다름');
      check((await admin.textContent('#stat-users .value')) === '3', '사용자 수가 3이 아님');
      check((await admin.textContent('#server-info')).includes('fake'), '서버 정보에 자세 추정 방식이 없음');

      // 넘어짐 → 다시 불러오지 않아도(자동 갱신) 진행 중 이벤트에 나타난다
      await pi.standUp();
      const before = pi.messages.filter((m) => m.type === 'voice_check').length;
      await pi.fall();
      const vc = await pi.waitVoiceCheck(before + 1);
      const item = `#active-events li[data-event-id="${vc.event_id}"]`;
      await admin.waitForSelector(item, { timeout: 5000 });
      check((await admin.textContent(item)).includes('거실 카메라'), '진행 중 이벤트에 기기 이름이 없음');
      check((await admin.textContent('#stat-active .value')) === '1', '진행 중 이벤트 수가 1이 아님');

      // Pi가 '괜찮아요'로 답하면 목록에서 사라진다
      const up = await uploadPiVoice(vc.event_id, vc.attempt, state.piKey, '괜찮아요');
      check(up.status === 200 && up.body?.accepted === true, `음성 업로드가 받아들여지지 않음: ${up.status}`);
      await admin.waitForSelector(item, { state: 'detached', timeout: 6000 });
      const ev = await waitEventEnded(ctxGuard, vc.event_id);
      check(ev.result === 'CLEARED_VOICE', `서버 결과가 CLEARED_VOICE가 아님: ${ev.result}`);
      state.voiceClearedEventId = vc.event_id;

      // 폭 1024px에서도 화면 전체가 가로로 넘치지 않는다(넓은 표는 표 안에서만 스크롤)
      await admin.setViewportSize({ width: 1024, height: 768 });
      for (const tab of ['overview', 'users', 'events']) {
        await admin.click(`#tab-${tab}`);
        await sleep(300);
        const w = await admin.evaluate(() => document.documentElement.scrollWidth);
        check(w <= 1024, `${tab} 탭이 1024px에서 가로로 넘침 (${w}px)`);
      }
      await admin.setViewportSize({ width: 1280, height: 900 });
      await admin.click('#tab-overview');

      // 127.0.0.1로 열어도 된다
      const page = watchPage(await ctxAdmin.newPage(), 'admin-127');
      try {
        await page.goto(`http://127.0.0.1:${PORT}/admin`);
        await page.waitForSelector(row);
      } finally {
        await page.close();
      }
    });

    await scenario('관리자 접근 제한: 다른 Host·X-Admin-Request 없음·다른 출처의 변경 요청은 404', async () => {
      const other = need(await adminUserByEmail(OTHER.email), '이웃 사용자');
      const ok = await adminRequest('GET', '/admin/api/overview');
      check(ok.status === 200 && ok.headers['x-frame-options'] === 'DENY', `서버 PC 요청이 거부됨 (${ok.status})`);

      // DNS 리바인딩 흉내: 접속은 127.0.0.1이지만 Host가 다른 사이트 이름
      for (const p of ['/admin', '/admin/api/overview', '/admin/api/users']) {
        const r = await rawRequest('GET', p, { Host: `evil.example:${PORT}` });
        check(r.status === 404 && !r.text.includes(WARD.email), `Host가 evil.example인데 ${p}가 ${r.status}`);
      }
      // 관리자 화면의 스크립트·스타일은 /static(누구나 받음)에 없고, /admin 아래에서 서버 PC에게만 나간다
      for (const p of ['/static/js/admin.js', '/static/css/admin.css']) {
        const r = await rawRequest('GET', p, { Host: `localhost:${PORT}` });
        check(r.status === 404, `${p}가 공개되어 있음 (${r.status})`);
      }
      for (const p of ['/admin/admin.js', '/admin/admin.css']) {
        const local = await rawRequest('GET', p, { Host: `localhost:${PORT}` });
        check(local.status === 200 && local.headers['cache-control'] === 'no-store', `서버 PC에서 ${p}를 받지 못함 (${local.status})`);
        const r = await rawRequest('GET', p, { Host: `evil.example:${PORT}` });
        check(r.status === 404, `Host가 evil.example인데 ${p}가 ${r.status}`);
      }
      // 변경 요청: X-Admin-Request 없음 / 다른 출처 Origin / Origin 없음
      const disable = `/admin/api/users/${other.id}/disable`;
      const cases = [
        { Host: `localhost:${PORT}`, Origin: `http://localhost:${PORT}` },
        { Host: `localhost:${PORT}`, Origin: 'http://evil.example', 'X-Admin-Request': '1' },
        { Host: `localhost:${PORT}`, Origin: `http://127.0.0.1:${PORT}`, 'X-Admin-Request': '1' },
        { Host: `localhost:${PORT}`, 'X-Admin-Request': '1' },
      ];
      for (const headers of cases) {
        const r = await rawRequest('POST', disable, headers);
        check(r.status === 404, `막아야 할 변경 요청이 ${r.status}: ${JSON.stringify(headers)}`);
      }

      // 실제 브라우저: 다른 출처(여기서는 127.0.0.1)의 페이지가 localhost 관리자 API를 부르면
      // X-Admin-Request 머리글 때문에 사전 요청(CORS)에서 막히고, 머리글 없는 단순 요청은 서버가 404로 거부한다.
      // CORS 차단은 콘솔 오류를 찍으므로 watchPage를 붙이지 않는다.
      const page = await ctxAdmin.newPage();
      try {
        await page.goto(`http://127.0.0.1:${PORT}/`);
        const outcome = await page.evaluate(async (url) => {
          const out = {};
          try {
            await fetch(url, { method: 'POST', headers: { 'X-Admin-Request': '1' } });
            out.withHeader = 'sent';
          } catch { out.withHeader = 'blocked'; }
          try {
            await fetch(url, { method: 'POST', mode: 'no-cors' });
            out.simple = 'sent';
          } catch { out.simple = 'blocked'; }
          return out;
        }, `${BASE}${disable}`);
        check(outcome.withHeader === 'blocked', `다른 출처의 사용자 정의 머리글 요청이 막히지 않음: ${JSON.stringify(outcome)}`);
      } finally {
        await page.close();
      }
      const after = await adminUserByEmail(OTHER.email);
      check(after && !after.disabled_at, '다른 출처의 요청으로 계정이 정지됨');
    });

    await scenario('관리자 사용자: 정지 → 로그인 거부·세션 삭제·Pi 연결 끊김(4403) → 해제 → 다시 로그인', async () => {
      need(pi, 'Pi 접속');
      const ward = need(await adminUserByEmail(WARD.email), '보호 대상 사용자');
      await admin.click('#tab-users');
      await admin.fill('#user-search', WARD.email);
      await admin.waitForFunction((id) => {
        const rows = document.querySelectorAll('#user-table tbody tr');
        return rows.length === 1 && rows[0].dataset.userId === String(id);
      }, ward.id);
      await admin.click(`#user-table tr[data-user-id="${ward.id}"]`);
      await waitText(admin, '#user-detail', new RegExp(WARD.email.replace(/[.]/g, '\\.')));
      const detail = await admin.textContent('#user-detail');
      check(detail.includes('거실 카메라') && detail.includes(GUARD.name), `사용자 정보에 기기·보호자가 없음: ${detail}`);

      // 휴대폰 카메라 화면도 감지 중이다. 정지되면 멈추되 기억해 둔 기기 번호는 지우지 않아야 한다
      // (지우면 정지를 푼 뒤 '감지 시작'에서 같은 휴대폰이 새 기기로 또 등록된다).
      const cam = watchPage(await ctxWard.newPage(), 'camera-suspend');
      const phoneKey = `fall.camera.deviceId.${ward.id}`;
      const storedPhone = () => cam.evaluate((k) => localStorage.getItem(k), phoneKey);
      await cam.goto(BASE + '/camera');
      await cam.click('#start-btn');
      await waitText(cam, '#conn-badge', /^연결됨$/, 10000);
      const phones = (await getJson(ctxWard, '/api/devices')).body.devices.filter((d) => d.kind === 'phone');
      check(phones.length === 1 && (await storedPhone()) === String(phones[0].id), `휴대폰 기기가 하나가 아님: ${JSON.stringify(phones)}`);

      let confirmText = '';
      admin.once('dialog', (d) => { confirmText = d.message(); });   // 확인은 watchPage가 누른다
      await admin.click('#user-detail [data-action="disable"]');   // 확인 창 → 확인
      await admin.waitForSelector('#user-detail [data-action="enable"]');
      await admin.waitForSelector(`#user-table tr[data-user-id="${ward.id}"][data-disabled="1"]`);
      // Pi 클라이언트는 4403이면 다시 접속하지 않으므로 해제 뒤 다시 시작해야 한다고 알려 준다
      check(confirmText.includes('sudo systemctl restart pi-client'), `정지 확인 창에 Pi 재시작 안내가 없음: ${confirmText}`);

      await waitText(cam, '#camera-error', /계정이 정지되었습니다/, 8000);
      check(await cam.isVisible('#start-btn'), '정지 뒤 휴대폰 감지가 멈추지 않음');
      check((await storedPhone()) === String(phones[0].id), `정지로 끊기자 기억한 휴대폰 기기 번호를 지움: ${await storedPhone()}`);

      check((await pi.waitClosed()) === 4403, `Pi 연결이 4403으로 닫히지 않음 (${pi.closeCode})`);
      const retry = connectPi(state.piId, state.piKey);
      await retry.open().catch(() => {});
      check((await retry.waitClosed()) === 4403, `정지 중 Pi 재접속이 4403으로 거부되지 않음 (${retry.closeCode})`);
      retry.close();
      check((await getJson(ctxWard, '/api/me')).status === 401, '정지 뒤에도 기존 로그인 세션이 살아 있음');

      const ctxLogin = await newContext();
      try {
        const page = watchPage(await ctxLogin.newPage(), 'suspended-login');
        await loginUI(page, WARD);
        await page.waitForSelector('#error:not(.hidden)');
        check((await page.textContent('#error')).includes('정지된 계정'), `정지 안내 문구가 다름: ${await page.textContent('#error')}`);

        await admin.click('#user-detail [data-action="enable"]');
        await admin.waitForSelector('#user-detail [data-action="disable"]');
        await admin.waitForSelector(`#user-table tr[data-user-id="${ward.id}"][data-disabled="0"]`);
        await waitText(admin, '.toast-success', /sudo systemctl restart pi-client/, 5000);
        await loginUI(page, WARD);
        await page.waitForURL((u) => !u.pathname.startsWith('/login'));
      } finally {
        await ctxLogin.close();
      }
      // 해제 뒤 같은 휴대폰에서 다시 감지를 시작하면 원래 기기로 붙는다(새 기기가 생기지 않는다)
      try {
        await loginUI(cam, WARD);
        await cam.waitForURL((u) => !u.pathname.startsWith('/login'));
        await cam.goto(BASE + '/camera');
        await cam.click('#start-btn');
        await waitText(cam, '#conn-badge', /^연결됨$/, 10000);
        const again = (await getJson(ctxWard, '/api/devices')).body.devices.filter((d) => d.kind === 'phone');
        check(again.length === 1 && again[0].id === phones[0].id, `정지 해제 뒤 휴대폰 기기가 새로 생김: ${JSON.stringify(again)}`);
      } finally {
        await cam.close();
      }
      // 해제 뒤에는 Pi가 다시 접속된다 (뒤 단계와 finally가 쓰도록 pi를 바꿔 둔다)
      pi = connectPi(state.piId, state.piKey);
      await pi.open();
      await sleep(300);
      check(pi.closeCode === null && pi.messages.some((m) => m.type === 'hello'), '정지 해제 뒤 Pi가 다시 접속하지 못함');
    });

    await scenario('관리자 이벤트·통계: 라벨 저장 → 오경보율, 라벨 필터, CSV 다운로드(BOM·머리글)', async () => {
      const falseAlarmId = need(state.voiceClearedEventId, '음성으로 해제한 이벤트');
      await admin.click('#tab-events');
      const row = (eventId) => `#event-table tr[data-event-id="${eventId}"]`;
      await admin.waitForSelector(row(falseAlarmId));
      check((await admin.$$('#day-chart svg rect[data-series]')).length > 0, '하루별 막대 그래프에 막대가 없음');

      // 오경보 하나, 연출 낙상 하나 → 오경보율 50%
      await admin.selectOption(`${row(falseAlarmId)} .label-select`, 'false_alarm');
      await admin.fill(`${row(falseAlarmId)} .note-input`, '소파에 털썩 앉음');
      await admin.click(`${row(falseAlarmId)} .save-label`);
      await admin.waitForSelector(`${row(falseAlarmId)} .label-edit[data-saved="false_alarm"]`);
      const helpRow = '#event-table tr[data-result="ALERT_HELP"]';
      const helpId = need(await admin.getAttribute(helpRow, 'data-event-id'), '도움 요청 이벤트');
      await admin.selectOption(`${row(helpId)} .label-select`, 'staged_fall');
      await admin.click(`${row(helpId)} .save-label`);
      await admin.waitForSelector(`${row(helpId)} .label-edit[data-saved="staged_fall"]`);
      await waitText(admin, '#stat-false-alarm .value', /^50%$/);
      const stats = (await adminRequest('GET', '/admin/api/stats?days=14')).json;
      check(stats.labels.false_alarm_rate === 0.5 && stats.labels.labeled === 2, `통계 API의 라벨 값이 다름: ${JSON.stringify(stats.labels)}`);

      // 라벨 필터 → 오경보 하나만
      await admin.selectOption('#f-label', 'false_alarm');
      await admin.waitForFunction((id) => {
        const rows = document.querySelectorAll('#event-table tbody tr');
        return rows.length === 1 && rows[0].dataset.eventId === String(id);
      }, falseAlarmId);

      // CSV는 지금 필터 그대로 내려받는다
      const [download] = await Promise.all([admin.waitForEvent('download'), admin.click('#csv-link')]);
      check(/^events-\d{8}-\d{4}\.csv$/.test(download.suggestedFilename()), `CSV 파일 이름이 다름: ${download.suggestedFilename()}`);
      const bytes = fs.readFileSync(await download.path());
      check(bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf, 'CSV가 UTF-8 BOM으로 시작하지 않음');
      const lines = bytes.toString('utf8').replace(/^﻿/, '').trim().split(/\r?\n/);
      check(lines[0].startsWith('이벤트 번호,시작 시각') && lines[0].includes('검토 라벨'), `CSV 머리글이 다름: ${lines[0]}`);
      check(lines.length === 2 && lines[1].startsWith(`${falseAlarmId},`) && lines[1].includes('false_alarm') && lines[1].includes('소파에 털썩 앉음'),
        `CSV 내용이 다름: ${lines.slice(1).join(' / ')}`);

      // 새로 고쳐도 저장한 라벨·메모가 남아 있다
      await admin.reload();
      await admin.waitForSelector(row(falseAlarmId));
      check(await admin.inputValue(`${row(falseAlarmId)} .label-select`) === 'false_alarm', '새로 고친 뒤 라벨이 사라짐');
      check(await admin.inputValue(`${row(falseAlarmId)} .note-input`) === '소파에 털썩 앉음', '새로 고친 뒤 메모가 사라짐');
    });

    await scenario('관리자 CSV: 서버 PC에서 연 다른 사이트의 링크로는 받지 못함(404), 관리자 화면의 버튼은 그대로', async () => {
      // 다른 사이트 흉내: 127.0.0.1의 다른 포트에서 연 페이지. localhost:PORT와는 다른 사이트라
      // 브라우저가 Sec-Fetch-Site: cross-site를 붙인다.
      const evilPort = await freePort();
      const evil = http.createServer((req, res) => {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(`<!doctype html><title>다른 사이트</title><a id="steal" href="${BASE}/admin/api/events.csv">받기</a>`);
      });
      await new Promise((resolve) => evil.listen(evilPort, '127.0.0.1', resolve));
      const page = await ctxAdmin.newPage();   // 404 응답을 콘솔에 찍으므로 watchPage를 붙이지 않는다
      try {
        await page.goto(`http://127.0.0.1:${evilPort}/`);
        let downloaded = null;
        page.on('download', (d) => { downloaded = d.suggestedFilename(); });
        const [res] = await Promise.all([
          page.waitForResponse((r) => r.url().startsWith(`${BASE}/admin/api/events.csv`)),
          page.click('#steal'),
        ]);
        check(res.status() === 404, `다른 사이트의 링크로 CSV 요청이 ${res.status()}`);
        await sleep(500);
        check(downloaded === null, `다른 사이트의 링크로 CSV가 내려받아짐: ${downloaded}`);
      } finally {
        await page.close();
        evil.close();
      }

      // 관리자 화면 자신의 CSV 버튼(같은 출처)은 그대로 내려받는다
      await admin.click('#tab-events');
      const [download] = await Promise.all([admin.waitForEvent('download'), admin.click('#csv-link')]);
      const text = fs.readFileSync(await download.path(), 'utf8');
      check(text.replace(/^\uFEFF/, '').startsWith('이벤트 번호,'), `관리자 화면에서 받은 CSV가 다름: ${text.slice(0, 40)}`);
    });

    await scenario('관리자 사용자 삭제: 이메일을 정확히 적어야 삭제 → 그 계정으로 로그인 불가', async () => {
      const other = need(await adminUserByEmail(OTHER.email), '이웃 사용자');
      await admin.click('#tab-users');
      await admin.fill('#user-search', OTHER.email);
      await admin.waitForSelector(`#user-table tr[data-user-id="${other.id}"]`);
      await admin.click(`#user-table tr[data-user-id="${other.id}"]`);
      await admin.waitForSelector('#user-detail [data-action="delete"]');
      await admin.click('#user-detail [data-action="delete"]');
      await admin.waitForSelector('#delete-dialog[open]');
      await admin.fill('#delete-email', WARD.email);   // 다른 사람 이메일
      check(await admin.isDisabled('#delete-confirm'), '이메일이 다른데 삭제 버튼이 켜짐');
      await admin.fill('#delete-email', `  ${OTHER.email.toUpperCase()} `);   // 대소문자·공백은 상관없다
      check(await admin.isEnabled('#delete-confirm'), '이메일을 맞게 적었는데 삭제 버튼이 꺼져 있음');
      await admin.click('#delete-confirm');
      await admin.waitForSelector('#delete-dialog', { state: 'hidden' });
      await admin.waitForSelector(`#user-table tr[data-user-id="${other.id}"]`, { state: 'detached' });
      check(!(await adminUserByEmail(OTHER.email)), '삭제한 사용자가 목록에 남아 있음');

      const page = watchPage(await ctxOther.newPage(), 'deleted-login');
      await loginUI(page, OTHER);
      await page.waitForSelector('#error:not(.hidden)');
      check(page.url().includes('/login'), '삭제한 계정으로 로그인됨');
      await page.close();
    });

    await ctxAdmin.close();

    await scenario('페이지 JS 오류 없음', async () => {
      check(consoleErrors.length === 0, `콘솔 오류 ${consoleErrors.length}건:\n    ${consoleErrors.join('\n    ')}`);
    });
  } finally {
    if (pi) pi.close();
    await browser.close().catch(() => {});
  }
}

let fatal = null;
const t0 = Date.now();
try {
  await main();
} catch (err) {
  fatal = err;
}
await stopServer();

const failed = results.filter((r) => !r.ok);
console.log('\n================ 결과 ================');
for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}`);
if (fatal) console.log(`FAIL  실행 중단: ${fatal.stack || fatal}`);
const total = ((Date.now() - t0) / 1000).toFixed(1);
if (failed.length || fatal) {
  console.log(`\nFAIL: ${failed.length + (fatal ? 1 : 0)}개 실패 / ${results.length}개 시나리오 (${total}s)`);
  console.log(`서버 로그: ${SERVER_LOG}`);
  process.exit(1);
}
console.log(`\nPASS: ${results.length}개 시나리오 모두 통과 (${total}s)`);
fs.rmSync(DATA_DIR, { recursive: true, force: true });
process.exit(0);
