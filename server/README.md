# 중앙 서버

팀원 PC에서 돌리는 중앙 서버입니다. Pi와 휴대폰이 보낸 카메라 영상으로 낙상을 판정하고, 음성 확인, 계정·보호자 관리, 보호자 경보(WebSocket·웹 푸시)를 처리합니다. 전체 구조와 통신 형식은 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)에 있습니다.

> 상태: 테스트(224개)는 가짜 자세 추정·가짜 음성 인식으로 통과합니다. 실제 YOLO-Pose, Whisper, HTTPS, 웹 푸시는 서버 PC에서 아직 시험하지 않았습니다.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `config.py` | 설정값(임계값, 대기 시간, 경로). 환경변수로 바꿀 수 있음 |
| `db.py` | SQLite 테이블과 결과 상수 |
| `auth.py` | 비밀번호 해시, 세션, 보호자 연결(요청·수락), 기기·기기 키, 푸시 구독, 권한 확인 |
| `routes_accounts.py` | 회원가입·로그인·마이페이지용 API (`/api/signup`, `/api/me`, `/api/guardians`, `/api/devices`, `/api/push/...`) |
| `detection/` | 자세 추정(YOLO-Pose), 특징 계산(몸통 각도·종횡비·하강 속도), 낙상 판정 상태 머신 |
| `devices.py` | 접속한 기기별 영상 처리와 상태(연결, 카메라, 사람 검출, FPS) |
| `events.py` | 낙상 이벤트: 음성 확인 → 해제 또는 경보 → 종료, 서버 재시작 복구 |
| `voice.py` | Whisper 음성 인식과 키워드 분류(괜찮음·도움 요청·무응답) |
| `notify.py` | 보호자 화면 연결 관리, 웹 푸시 전송·재시도, 알림 기록 |
| `routes_realtime.py` | `/ws/camera`, `/ws/guardian`, 이벤트 API(녹음 업로드, 기록, 해제, 기기 상태) |
| `admin.py` | 관리자 페이지(`/admin`)와 관리자 API. 서버 PC 자신에서 온 요청만 받는 검사(`AdminGuard`, `require_local`) |
| `main.py` | 위 구성 요소를 묶어 앱을 만들고 웹페이지(`web/`)를 연결 |
| `__main__.py` | `python -m server` 실행 진입점 |

처리 흐름은 `events.py` 맨 위 설명을 먼저 읽으면 이해하기 쉽습니다.

## 설치

Python 3.10~3.13, 저장소 최상위 폴더 기준입니다(더 새로운 Python은 numpy·PyTorch 설치 파일이 아직 없습니다).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r server/requirements.txt
```

GPU(NVIDIA)가 있으면 [PyTorch 안내](https://pytorch.org/get-started/locally/)에 따라 CUDA 버전 PyTorch를 먼저 설치한 뒤 위 명령을 실행합니다. YOLO 모델(`yolov8n-pose.pt`)은 처음 실행할 때 자동으로 내려받습니다.

## 실행

### 통합 실행 파일 (권장)

**Windows는 `run_server.bat` 더블클릭**, macOS·Linux는 `./run_server.sh` 하나로 환경 설정부터 실행까지 끝납니다. 처음 한 번만 설정하고, 다음부터는 이미 된 단계를 건너뛰어 바로 켜집니다.

1. Python 3.10~3.13을 찾고, 없으면 winget으로 Python 3.12를 설치합니다(Windows). 다른 버전으로 만들어진 `.venv`는 지우고 다시 만듭니다.
2. 가상환경 `.venv`를 만들고 `server/requirements.txt`를 설치합니다. NVIDIA GPU가 있으면 GPU용 PyTorch를 먼저 설치합니다. 목록이 바뀌었을 때만 다시 설치합니다.
3. mkcert가 없으면 설치하고(winget·brew, 안 되면 공식 배포 파일을 `tools/`에 받음), HTTPS 인증서가 없거나 서버 PC의 Wi-Fi 주소가 바뀌었으면 새로 만듭니다. 휴대폰용 `data/rootCA.pem`도 만듭니다.
4. Windows 방화벽에 서버 포트를 엽니다(관리자 권한이 없으면 처음 실행 때 뜨는 창에서 허용).
5. 아래처럼 서버를 켜고 관리자 화면을 엽니다.

설정 단계는 `auto_setup.py`에 있습니다. 직접 실행할 때의 옵션:

```bash
run_server.bat --fake              # 모델 없이 시험 (python run_server.py --auto --fake 와 같음)
python run_server.py               # 이미 설정된 환경에서 설정 단계 없이 실행
python run_server.py --make-cert   # 인증서를 강제로 다시 만들기
```

- `--auto` 없이 실행했는데 패키지가 없으면 설치 명령을 알려 주고 멈춥니다.
- `data/cert.pem`, `data/key.pem`이 있으면 HTTPS로 켭니다. 없으면 HTTP로 켜고 이 PC에서만 로그인되게 합니다(`FALL_COOKIE_SECURE=0`).
- 웹 푸시 키가 없으면 `data/vapid_private.pem`을 한 번 만들어 계속 씁니다.
- 휴대폰·Pi가 접속할 주소를 화면에 보여 주고, 인증서에 지금 Wi-Fi 주소가 빠져 있으면 경고합니다.
- 서버가 뜨면 이 PC의 브라우저로 관리자 화면을 엽니다(`--no-browser`로 끌 수 있음).

휴대폰 쪽 설정은 [docs/PHONE_ACCESS.md](../docs/PHONE_ACCESS.md)에 있습니다. 아래는 같은 일을 손으로 하는 방법입니다.

### 장비 없이 시험 (HTTP, 가짜 모델)

```bash
FALL_POSE_BACKEND=fake FALL_STT_BACKEND=fake FALL_COOKIE_SECURE=0 python -m server
```

`http://localhost:8443/docs`에서 API 문서를 볼 수 있습니다. 휴대폰 카메라·마이크·웹 푸시는 HTTPS가 아니면 동작하지 않습니다.

### 실제 시연 (HTTPS, 실제 모델)

1. 인증서 만들기 ([mkcert](https://github.com/FiloSottile/mkcert)). `192.168.0.10`은 서버 PC의 고정 IP로 바꿉니다.
   ```bash
   mkcert -install
   mkcert -cert-file data/cert.pem -key-file data/key.pem 192.168.0.10 localhost
   ```
   `mkcert -CAROOT`가 알려 주는 폴더의 `rootCA.pem`을 휴대폰(설정 → 보안 → 인증서 설치)과 Pi에 설치해야 경고 없이 접속됩니다.
2. 웹 푸시 키 만들기 (한 번만).
   ```bash
   cd data && vapid --gen && vapid --applicationServerKey && cd ..
   ```
   출력된 `Application Server Key = ...` 값이 공개 키입니다.
3. 실행.
   ```bash
   export FALL_SSL_CERT=data/cert.pem FALL_SSL_KEY=data/key.pem
   export FALL_VAPID_PRIVATE_KEY=data/private_key.pem
   export FALL_VAPID_PUBLIC_KEY=<위에서 출력된 공개 키>
   export FALL_VAPID_SUBJECT=mailto:<팀 이메일>
   python -m server
   ```

## 관리자 페이지

시스템을 운영하는 팀이 쓰는 화면입니다. 관리자 계정은 따로 없고, **서버 PC 자신의 브라우저에서만** 열립니다.

- 여는 방법: 서버 PC에서 브라우저로 `https://localhost:8443/admin`(HTTP로 시험할 때는 `http://localhost:8443/admin`)을 엽니다. `127.0.0.1`로 열어도 됩니다.
- 같은 Wi-Fi의 휴대폰·다른 PC에서 `https://<서버 IP>:8443/admin`을 열면 없는 주소와 똑같이 404가 나옵니다(관리자 페이지가 있다는 것도 알리지 않음).

**왜 서버 PC에서만 여는가.** 관리자 화면은 모든 사용자의 이메일·기기·이벤트를 보고 계정을 정지·삭제할 수 있습니다. 계정 없이 안전하게 쓰려고, 서버가 모든 `/admin` 요청을 다음과 같이 검사합니다(`admin.py` 머리말).

1. 접속한 주소가 루프백(127.0.0.0/8, ::1)이어야 합니다. 서버 앞에 프록시가 없으므로 `X-Forwarded-For`는 믿지 않습니다(uvicorn도 `proxy_headers=False`로 실행해 `FORWARDED_ALLOW_IPS`를 설정해도 접속 주소가 바뀌지 않습니다).
2. `Host` 머리글이 `localhost`, `127.0.0.1`, `[::1]` 중 하나여야 합니다. 서버 PC의 브라우저로 연 악성 사이트가 자기 도메인을 127.0.0.1로 바꿔(DNS 리바인딩) 관리자 API를 읽는 것을 막습니다.
3. 상태를 바꾸는 요청(POST·DELETE)은 같은 출처(`Origin`)와 `X-Admin-Request: 1` 머리글이 있어야 합니다. 다른 사이트가 서버 PC의 브라우저를 이용해 몰래 계정을 정지·삭제하는 것(CSRF)을 막습니다.
4. 브라우저가 붙이는 `Sec-Fetch-Site`가 있으면 `same-origin`(관리자 페이지 안의 링크·버튼) 또는 `none`(주소창에 직접 입력, 즐겨찾기)이어야 합니다. 서버 PC에서 연 다른 사이트가 링크 하나로 CSV(모든 사용자의 이벤트)를 내려받게 하는 것을 막습니다. 이 머리글이 없는 요청(curl 같은 로컬 도구)은 허용합니다.
5. 하나라도 맞지 않으면 404. 관리자 응답은 다른 사이트의 iframe에 넣을 수 없고(`frame-ancestors 'none'`) 브라우저에 캐시되지 않습니다. 관리자 화면의 스크립트·스타일(`web/admin/`)도 `/static`이 아니라 `/admin/admin.js`, `/admin/admin.css`에서 같은 검사를 거쳐 나갑니다.

**서버 PC에서 포트 포워딩·터널을 돌리지 마세요.** 서버 PC 안에서 외부 접속을 이 포트로 넘겨 주는 프로그램(ssh `-L`/`-R` 포워딩, ngrok·cloudflared 같은 터널, 포트 포워더 등)이 `Host: localhost`를 그대로 두면, 그 요청은 서버 PC 자신이 보낸 것과 구별되지 않아 관리자 페이지가 바깥에 열립니다.

관리자 동작은 사용자 계정과 묶이지 않으며, 상태를 바꾼 동작은 모두 서버 로그에 `관리자: ...`로 남습니다. 운영하지 않을 때 아예 끄려면 `FALL_ADMIN=0`으로 실행합니다.

| 탭 | 하는 일 |
| --- | --- |
| 현황 | 서버 정보(자세 추정·음성 인식 방식, 웹 푸시·HTTPS 여부, 가동 시간, 판정 기준값), 전체 개수(사용자, 기기, 보호자 연결, 오늘·진행 중 이벤트), 모든 기기의 실시간 상태(연결, 카메라, 사람 감지, FPS), 진행 중 이벤트, 최근 알림 실패. 2초마다 자동 갱신. **실시간 영상·낙상 판정**: 접속 중인 기기를 고르면 그 기기가 보낸 가장 최근 장면 위에 관절, 판정에 쓰는 어깨·엉덩이와 몸통 선, 사람 상자를 그리고(0.2초마다), 옆에 감지 상태(정상·하강·의심·경보), 자세, 누운 시간/T1, 특징값(하강 속도·몸통 각도·종횡비·움직임량)과 판정 기준, 최근 15초 그래프를 보여 줍니다. 영상은 서버 메모리의 마지막 한 장뿐이며 저장하지 않습니다. 보호자 화면이 닫혀 있어 화면으로 못 보낸 기록은 장애가 아니므로(그때는 웹 푸시로 보내고 화면을 다시 열면 받음) 실패 표에서 빼고 건수만 보여 줍니다 |
| 사용자 | 이름·이메일 검색, 사용자별 기기·보호자 수. 고르면 보호자 연결(양방향), 기기와 실시간 상태, 최근 이벤트 20건. **계정 정지**: 로그인 거부("정지된 계정입니다"), 기존 로그인 세션 삭제, 열린 보호자 화면과 그 사용자 기기의 카메라 연결 끊기, 기기의 새 접속 거부(4403). **정지 해제**로 되돌릴 수 있습니다. 단, Pi 클라이언트는 4403으로 끊기면 다시 접속하지 않으므로 정지를 푼 뒤 그 사용자의 각 Pi에서 `sudo systemctl restart pi-client`로 다시 시작해야 합니다(휴대폰 카메라 화면은 '감지 시작'을 다시 누르면 됩니다). **삭제**는 되돌릴 수 없으며(기기·이벤트·보호자 연결이 함께 지워짐) 확인 창에 그 사용자의 이메일을 적어야 합니다 |
| 이벤트·통계 | 기간·판정·검토 라벨·사용자로 거른 전체 이벤트(판정, 음성 결과, 걸린 시간, 경보 시각, 채널별 알림 성공 수). 이벤트마다 **검토 라벨**(실제 낙상, 연출 낙상, 일상 동작, 오경보)과 메모(200자)를 저장합니다. 통계: 하루별 판정 막대 그래프, 판정별·음성 결과별·라벨별 건수, 오경보율(라벨 단 이벤트 중 오경보), 평균 시간(의심→해제, 의심→경보, 경보→해제), 웹 푸시 성공률 |

**시험 결과 정리(CSV).** 이벤트·통계 탭의 `CSV 다운로드`는 지금 걸어 둔 필터 그대로 이벤트를 내려받습니다. 엑셀에서 한글이 깨지지 않게 UTF-8 BOM을 붙이고, 판정·음성 결과·검토 라벨·메모·채널별 알림 수·판정 특징값(하강 속도 등)이 들어 있습니다. 시험마다 라벨과 메모를 단 뒤 내려받아 [docs/TEST_PLAN.md](../docs/TEST_PLAN.md)의 기록에 씁니다. `=`, `+`, `-`, `@`로 시작하는 글은 엑셀이 수식으로 실행하지 않게 앞에 `'`를 붙입니다.

**녹화 영상으로 판정 시험.** 넘어지는 영상을 휴대폰 카메라 화면과 같은 조건(긴 변 640px, JPEG 0.7, 초당 10장)으로 바꿔 서버와 같은 YOLO-Pose와 감지기(기본 Random Forest, `FALL_DETECTION_BACKEND=fsm`이면 규칙 기반)에 넣고 프레임마다 특징값·점수·상태를 출력합니다. 영상이 누운 직후 끝나면 `--hold 2`로 마지막 장면을 더 넣고, 사람이 없다고 나오는 이유는 `--raw`(어깨·엉덩이 신뢰도)로 봅니다.

```bash
.venv\Scripts\python.exe -m server.replay_video 넘어짐.mp4 --hold 2
```

관리자 API(`/admin/api/...`)는 `/docs` API 문서에 나오지 않습니다. 모양은 `admin.py`와 `tests/test_admin.py`를 봅니다. 실시간 보기는 `GET /admin/api/devices/{id}/live?after=<이전 seq>`로, 새 장면이 없으면 204로 답합니다.

## 환경변수

| 이름 | 기본값 | 내용 |
| --- | --- | --- |
| `FALL_HOST`, `FALL_PORT` | `0.0.0.0`, `8443` | 접속 주소 |
| `FALL_SSL_CERT`, `FALL_SSL_KEY` | 없음 | HTTPS 인증서·키 파일. 없으면 HTTP로 실행(경고 출력) |
| `FALL_COOKIE_SECURE` | `1` | HTTP로 시험할 때만 `0` |
| `FALL_DATA_DIR` | `data/` | DB(`server.db`) 위치 |
| `FALL_WEB_DIR` | `web/` | 웹페이지 폴더 |
| `FALL_POSE_BACKEND` | `yolo` | `fake`는 테스트 전용 |
| `FALL_YOLO_MODEL` | `models/yolov8n-pose.pt` | YOLO 모델 파일 |
| `FALL_DETECTION_BACKEND` | `random_forest` | 낙상 감지 방식. `fsm`이면 규칙 기반(하강 속도·누운 자세). 가짜 자세 추정(시험)은 늘 `fsm` ([docs/RANDOM_FOREST.md](../docs/RANDOM_FOREST.md)) |
| `FALL_RF_MODEL` | `models/fall_detector_window_yolo.pkl` | Random Forest 모델(42차원). 불러올 때 코드가 실행될 수 있으므로 직접 만든 파일만 씁니다 |
| `FALL_RF_THRESHOLD` | `0.40` | Random Forest 분석 점수 기준 |
| `FALL_STT_BACKEND` | `whisper` | `fake`는 테스트 전용 |
| `FALL_WHISPER_MODEL` | `small` | `base`, `small`, `medium` 등. 클수록 정확하지만 느림 |
| `FALL_DESCENT_PEAK` | `2.0` | 하강 속도 기준(몸통 길이/초). 아래 FSM 값은 `FALL_DETECTION_BACKEND=fsm`일 때 씁니다 |
| `FALL_T1_SECONDS` | `2.0` | 하강 뒤 누운 자세가 이어져야 하는 시간. 사이에 '사람 없음'·'중간 자세'가 1초 이하로 끼는 것(뒤척임, 몸 일부가 화면 밖)은 이어서 잽니다(`config.py`의 `lying_gap_seconds`) |
| `FALL_VOICE_TIMEOUT` | `20` | 음성 확인 요청 후 녹음을 기다리는 시간(초) |
| `FALL_VAPID_PRIVATE_KEY`, `FALL_VAPID_PUBLIC_KEY`, `FALL_VAPID_SUBJECT` | 없음 | 웹 푸시. 없으면 푸시 없이 보호자 화면 알림만 동작 |
| `FALL_ADMIN` | `1` | 관리자 페이지(`/admin`). `0`이면 서버 PC에서도 열리지 않음 |

그 밖의 임계값은 `config.py`에 있습니다.

## 테스트

```bash
pip install httpx pytest
python -m pytest server/tests -q
```

자세 추정과 음성 인식은 가짜를 씁니다. 가짜 자세 추정은 프레임 대신 관절 좌표 JSON을, 가짜 음성 인식은 녹음 대신 문장(UTF-8)을 받습니다(`detection/pose.py`의 `fake_frame`).

## 웹·Pi 클라이언트가 알아야 할 것

Pi 클라이언트는 별도 저장소 [Elderly_Fall_Detection_Pi_Client](https://github.com/mandur23/Elderly_Fall_Detection_Pi_Client)에 있습니다.

- **웹페이지 위치:** `web/index.html`, `login.html`, `signup.html`, `mypage.html`, `camera.html`, `guardian.html`, `kiosk.html`, `admin.html`(관리자, 서버 PC 전용. 전용 스크립트·스타일은 `web/admin/`), 서비스 워커는 `web/sw.js`, 그 밖의 파일은 `web/static/`에 두면 서버가 자동으로 제공합니다. `mypage`, `camera`, `guardian`은 로그인하지 않으면 로그인 화면으로 보냅니다.
- **카메라 연결:** `wss://<서버>/ws/camera?device_id=N` (휴대폰은 로그인 쿠키, Pi는 `&key=<기기 키>` 또는 `X-Device-Key` 헤더). 바이너리 메시지 1개 = JPEG 1장.
  - 서버가 보내는 메시지: `{"type":"hello",...}`, `{"type":"state","state":"NORMAL|SUSPECT|ALARM"}`, `{"type":"voice_check","event_id":N,"attempt":n}`
  - 접속을 거부하는 코드: `4401` 인증 실패, `4403` 권한 없음. 이때는 재연결하지 말고 로그인이나 기기 키를 확인하게 합니다.
- **음성 확인:** `voice_check`를 받으면 안내 음성을 재생하고 몇 초 녹음해 `POST /api/events/{event_id}/voice`(multipart 파일 `audio`)로 보냅니다. 마이크 권한이 없거나 녹음에 실패하면 `audio` 없이 `error` 필드만 보냅니다(경보로 처리됨). 최대 10MB.
- **보호자 화면:** `wss://<서버>/ws/guardian`. 접속 직후 `snapshot`, 이후 `device`·`event` 메시지를 받습니다.
- **기기 등록:** 휴대폰은 `POST /api/devices {"name","kind":"phone"}`로 만든 `device_id`를 브라우저에 저장해 둡니다. Pi는 마이페이지에서 `kind:"pi"`로 만들 때 한 번만 나오는 기기 키를 Pi 설정에 넣습니다.
