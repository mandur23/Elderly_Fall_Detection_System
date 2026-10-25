// 서버 API 호출 도우미.
//
// 모든 화면이 fetch를 직접 쓰지 않고 api()를 거치게 해서, 오류 처리 방식을 한곳에 모은다.
// - 성공하면 JSON을 풀어서 돌려준다.
// - 실패하면 ApiError(status, detail)를 던진다. detail은 화면에 바로 보여 줄 수 있는 한국어 문장이다.
// - 로그인이 풀린 상태(401)면 로그인 화면으로 보낸다. 로그인 화면·현장 화면처럼 401을 직접 처리해야 하면
//   { noRedirect: true }로 끈다.
// 로그인은 HttpOnly 쿠키로 유지되므로 스크립트는 토큰을 만지지 않는다(쿠키는 브라우저가 자동으로 붙인다).

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;   // HTTP 상태 코드. 서버에 닿지 못했으면 0
    this.detail = detail;   // 한국어 오류 문장
  }
}

// FastAPI 입력 검증 오류(422)의 필드 이름을 한국어로 바꾸기 위한 표
const FIELD_NAMES = {
  email: '이메일',
  password: '비밀번호',
  name: '이름',
  kind: '기기 종류',
  endpoint: '구독 주소',
  p256dh: '구독 키',
  auth: '구독 키',
};

// 422 오류 하나({loc, type, msg})를 사람이 읽을 수 있는 문장으로
function describeValidationError(item) {
  const loc = Array.isArray(item.loc) ? item.loc : [];
  const key = loc[loc.length - 1];
  const field = FIELD_NAMES[key] || '입력값';
  const type = String(item.type || '');
  if (type === 'missing') return `${field}을(를) 입력해 주세요.`;
  if (type.includes('too_long')) return `${field}이(가) 너무 깁니다.`;
  if (type.includes('too_short')) return `${field}이(가) 너무 짧습니다.`;
  if (type === 'json_invalid') return '요청 형식이 올바르지 않습니다.';
  return `${field} 형식이 올바르지 않습니다.`;
}

// 서버가 detail을 주지 않았을 때 보여 줄 기본 문장
function defaultMessage(status) {
  if (status === 401) return '로그인이 필요합니다.';
  if (status === 403) return '권한이 없습니다.';
  if (status === 404) return '찾을 수 없습니다.';
  if (status === 409) return '이미 처리되었습니다.';
  if (status === 413) return '보내는 파일이 너무 큽니다.';
  if (status >= 500) return '서버에 문제가 생겼습니다. 잠시 뒤 다시 시도해 주세요.';
  return `요청을 처리하지 못했습니다. (오류 ${status})`;
}

// FastAPI의 detail은 문자열이거나(우리 서버가 직접 낸 오류) 목록(입력 검증 422)이다.
export function readableDetail(status, detail) {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    // 같은 문장이 여러 번 나오지 않게 중복을 지운다
    return [...new Set(detail.map(describeValidationError))].join(' ');
  }
  return defaultMessage(status);
}

// 지금 화면으로 돌아오게 next를 붙여 로그인 화면으로 보낸다
export function goLogin() {
  const next = location.pathname + location.search;
  location.href = '/login?next=' + encodeURIComponent(next);
}

// 로그인·회원가입 요청의 401은 '비밀번호가 틀림'이라는 뜻이므로 화면을 옮기지 않는다
const NO_REDIRECT_PATHS = ['/api/login', '/api/signup', '/api/logout'];

/**
 * 서버 API를 부른다.
 * @param {string} path  '/api/me' 같은 주소
 * @param {object} opts
 *   method   : 'GET'(기본), 'POST', 'DELETE' ...
 *   json     : 보낼 객체 (JSON으로 보냄)
 *   form     : 보낼 FormData (multipart, 녹음 업로드 등)
 *   headers  : 추가 머리글 (예: 현장 화면의 X-Device-Key)
 *   noRedirect: true면 401이어도 로그인 화면으로 보내지 않고 ApiError만 던진다
 *   signal   : 요청 취소용 AbortSignal
 */
export async function api(path, opts = {}) {
  const { method, json, form, headers = {}, signal } = opts;
  const noRedirect = opts.noRedirect === true || opts.redirectOn401 === false;
  const init = {
    method: method || (json !== undefined || form !== undefined ? 'POST' : 'GET'),
    headers: { Accept: 'application/json', ...headers },
    credentials: 'same-origin',
    cache: 'no-store',     // 상태 정보가 오래된 값으로 보이면 위험하므로 캐시하지 않는다
    signal,
  };
  if (json !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(json);
  } else if (form !== undefined) {
    init.body = form;      // Content-Type(경계 문자열 포함)은 브라우저가 정한다
  }

  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(0, '서버에 연결할 수 없습니다. 인터넷(와이파이) 연결을 확인해 주세요.');
  }

  let data = null;
  const type = res.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    try { data = await res.json(); } catch { data = null; }
  }

  if (!res.ok) {
    const detail = readableDetail(res.status, data && data.detail);
    // 기기 키로 부르는 요청(현장 화면)은 로그인과 상관이 없으므로 옮기지 않는다
    const deviceAuth = Object.keys(headers).some((h) => h.toLowerCase() === 'x-device-key');
    if (res.status === 401 && !noRedirect && !deviceAuth && !NO_REDIRECT_PATHS.includes(path)) {
      clearMeCache();
      goLogin();
    }
    throw new ApiError(res.status, detail);
  }
  return data;
}

// --- 내 정보 (/api/me) --------------------------------------------------------
// 머리글 메뉴와 각 화면이 같은 정보를 쓰므로 한 번 받아 둔 것을 함께 쓴다.

let meCache = null;   // Promise

export function clearMeCache() {
  meCache = null;
}

/**
 * 로그인한 사용자 정보. 로그인하지 않았으면 null (로그인 화면으로 보내지 않는다).
 * 돌려주는 모양: { user:{id,email,name}, guardians:[...], wards:[...], requests:[...] }
 * @param {{force?: boolean}} opts  force: 캐시를 버리고 새로 받는다 (마이페이지에서 무언가 바꾼 뒤)
 */
export function me({ force = false } = {}) {
  if (force || !meCache) {
    meCache = api('/api/me', { noRedirect: true }).catch((err) => {
      meCache = null;    // 실패는 기억하지 않는다 (다음에 다시 시도)
      if (err instanceof ApiError && err.status === 401) return null;
      throw err;
    });
  }
  return meCache;
}

/**
 * 로그아웃. 이 브라우저의 푸시 알림도 끈다.
 * (로그아웃한 휴대폰에 계속 다른 사람의 경보 알림이 뜨면 안 되기 때문이다.)
 * 끝나면 첫 화면으로 간다. redirectTo: null이면 이동하지 않는다.
 */
export async function logout({ redirectTo = '/' } = {}) {
  try {
    // 순환 import를 피하려고 필요할 때만 불러온다. 푸시를 끄지 못해도 로그아웃은 진행한다.
    const push = await import('./push.js');
    await push.disablePush({ quiet: true });
  } catch (err) {
    console.warn('로그아웃 중 푸시 해제 실패', err);
  }
  // 서버에 닿지 못하면 ApiError를 던진다(쿠키가 남아 있으므로 '로그아웃됨'으로 보여 주면 안 된다).
  await api('/api/logout', { method: 'POST', noRedirect: true });
  clearMeCache();
  if (redirectTo) location.href = redirectTo;
}
