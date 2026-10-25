# 모바일 웹 e2e 테스트

`e2e.mjs`는 중앙 서버를 직접 띄우고, 실제 Chromium으로 웹 화면을 눌러 보며 주요 흐름을 확인합니다.
웹은 서버가 함께 내보내므로(별도 웹 서버나 빌드 없음) 서버 하나만 띄우면 됩니다.

## 준비

- Python: 서버 의존성(`server/requirements.txt`)이 설치된 파이썬. 실제 YOLO·Whisper 모델은 필요 없습니다(가짜 백엔드 사용).
- Node.js 22 이상 (내장 `WebSocket`, `fetch`, `FormData`를 씁니다).
- Playwright와 Chromium. 저장소에는 설치하지 않고 전역으로 설치합니다.

```bash
npm install -g playwright
npx playwright install chromium
# 전역 모듈을 찾도록 (설치 위치에 맞게)
export NODE_PATH="$(npm root -g)"
```

## 실행

저장소 루트에서:

```bash
node web/tests/e2e.mjs
```

파이썬은 `PYTHON` 환경변수 → 저장소의 `.venv/bin/python`(또는 `venv/bin/python`) → `python3` 순서로 찾습니다.

```bash
PYTHON=/path/to/venv/bin/python node web/tests/e2e.mjs
HEADED=1 node web/tests/e2e.mjs      # 브라우저 창을 띄워 눈으로 보기
```

40초 안팎이 걸리고, 마지막에 시나리오별 `PASS`/`FAIL` 요약을 찍습니다. 하나라도 실패하면 종료 코드 1입니다.

## 스크립트가 띄우는 서버

| 설정 | 값 | 이유 |
| --- | --- | --- |
| `FALL_POSE_BACKEND` | `fake` | 프레임 바이트를 관절 좌표 JSON으로 읽는다 (`server/detection/pose.py`의 `fake_frame`) |
| `FALL_STT_BACKEND` | `fake` | 녹음 바이트를 인식 문장(UTF-8)으로 읽는다. `"도와주세요"`를 올리면 도움 요청 |
| `FALL_COOKIE_SECURE` | `0` | `http://localhost`에서도 로그인 쿠키가 붙게 |
| `FALL_T1_SECONDS` | `0.3` | 누운 뒤 0.3초면 '상태 확인 중'으로 (기본 2초) |
| `FALL_VOICE_TIMEOUT` | `12` | 카메라 화면의 안내 + 5초 녹음 + 업로드가 들어갈 만큼 |
| `FALL_DATA_DIR` | 임시 폴더 | 매번 빈 DB로 시작. 성공하면 지우고, 실패하면 서버 로그와 함께 남긴다 |
| `FALL_PORT` | 빈 포트 | 다른 서버와 겹치지 않게 |

## 시나리오

1. 회원가입 → 로그아웃 → 틀린 비밀번호(한국어 오류) → 로그인(`next`로 마이페이지 복귀)
2. 마이페이지에서 보호자 요청 → 보호자가 다른 브라우저에서 수락 → 양쪽에 연결 표시
3. Pi 기기 추가: 키와 설정 줄·현장 화면 주소가 한 번만 보이고, 닫거나 새로 고치면 사라짐(localStorage에도 없음)
4. 보호자 화면: 스냅샷에 보호 대상의 Pi 기기가 보임
5. Pi 흉내 클라이언트(Node `WebSocket`, `?key=`로 인증)가 서 있다가 넘어짐 → 보호자 화면 SUSPECT →
   `"도와주세요"` 음성 업로드 → ALARM 카드·경보 띠 → 경보 해제(확인 창) → 서버 기록 `ALERT_HELP`
6. 현장 화면(kiosk, 800×480): 주소창에서 키가 지워짐, 정상(초록) → 넘어짐 → 상태 확인 중(노랑) →
   괜찮아요 → 서버 기록 `CLEARED_BUTTON`, `cleared_by=device:N`
7. 카메라 화면(가짜 카메라·마이크): 감지 시작 → 휴대폰 기기 등록·JPEG 전송·서버에 온라인 표시 →
   `page.routeWebSocket`으로 프레임을 가짜 자세로 바꿔 넘어짐을 만듦 → 안내 → 녹음 → multipart(`audio`, `attempt`) 업로드를 서버가 받음 →
   (가짜 STT는 webm을 못 읽으므로) 경보 → 괜찮아요로 해제 → 감지 중지
8. 권한: 관계없는 사용자는 보호자 화면·API 어디에서도 남의 기기·이벤트를 볼 수 없고 해제도 못 함
9. 관리자 현황(`/admin`, 1280×900): 기기 표에 Pi가 접속 중으로 보이고, 넘어지면 2초 자동 갱신으로 진행 중 이벤트에 나타났다가
   `"괜찮아요"` 업로드 뒤 사라짐(`CLEARED_VOICE`). 폭 1024px에서 세 탭 모두 가로로 넘치지 않음. `http://127.0.0.1:<port>/admin`으로도 열림
10. 관리자 접근 제한: `Host: evil.example`(DNS 리바인딩 흉내)이면 페이지·API 모두 404. 변경 요청에 `X-Admin-Request`가 없거나
    Origin이 다른 출처·없음이면 404. 실제 브라우저에서 다른 출처(127.0.0.1) 페이지가 부르면 CORS 사전 요청에서 막히고 계정은 그대로
11. 관리자 사용자: 검색 → 상세(기기·보호자) → 계정 정지 → Pi 연결이 4403으로 끊기고 재접속도 4403, 기존 세션 401, 로그인 화면에 "정지된 계정" →
    정지 해제 → 다시 로그인·Pi 재접속
12. 관리자 이벤트·통계: 이벤트에 오경보·연출 낙상 라벨 저장 → 오경보율 50%(화면과 `/admin/api/stats`), 라벨 필터,
    CSV 다운로드(파일 이름, UTF-8 BOM, 머리글, 필터된 한 줄과 메모), 새로 고친 뒤에도 라벨·메모 유지
13. 관리자 사용자 삭제: 다른 이메일을 적으면 삭제 버튼이 꺼져 있고, 맞게 적으면(대소문자·공백 무시) 삭제 → 그 계정으로 로그인 불가
14. 위 과정 동안 페이지에 JS 오류가 없었는지

Node의 `fetch`는 `Host` 머리글을 바꿔도 무시하고 실제 주소를 보내므로, 머리글을 직접 정해야 하는 관리자 확인(`Host`, `Origin`)은
`node:http`로 보냅니다(`rawRequest`, `adminRequest`). 접속 주소가 루프백이 아닌 경우는 서버가 127.0.0.1에서만 듣는 e2e로는 만들 수 없어
서버 테스트(`server/tests/test_admin.py`)가 확인합니다.

시나리오는 앞에서 만든 계정·기기를 이어서 씁니다. 앞 단계가 실패하면 뒤 단계는 "앞 단계 실패로 … 없음"으로 함께 실패합니다.

## 콘솔 오류 판정에서 빼는 것

아래는 페이지 코드 오류가 아니라 브라우저가 찍는 네트워크 기록이라 무시합니다.

- `Failed to load resource …`: 로그아웃 상태의 `/api/me` 401, 틀린 비밀번호 401, 저장소에 없는 `/static/audio/ask_status.mp3` 404 등 예상한 응답
- `WebSocket connection to …`: 서버가 소켓을 거절·종료했을 때의 기록

관리자 접근 제한 시나리오의 다른 출처 페이지는 CORS 차단 기록을 일부러 만들므로 오류 수집(`watchPage`)을 붙이지 않습니다.

## 실패했을 때

- 출력 끝의 `서버 로그:` 경로에서 서버 쪽 오류를 봅니다.
- `HEADED=1`로 다시 돌려 화면을 직접 봅니다.
- 한 단계는 최대 10초를 기다립니다. 느린 PC에서 시간 초과가 나면 `ctx.setDefaultTimeout` 값을 늘려 보세요.
