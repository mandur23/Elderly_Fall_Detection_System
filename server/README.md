# 중앙 서버

팀원 PC에서 돌리는 중앙 서버입니다. Pi와 휴대폰이 보낸 카메라 영상으로 낙상을 판정하고, 음성 확인, 계정·보호자 관리, 보호자 경보(WebSocket·웹 푸시)를 처리합니다. 전체 구조와 통신 형식은 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)에 있습니다.

> 상태: 테스트(162개)는 가짜 자세 추정·가짜 음성 인식으로 통과합니다. 실제 YOLO-Pose, Whisper, HTTPS, 웹 푸시는 서버 PC에서 아직 시험하지 않았습니다.

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
| `main.py` | 위 구성 요소를 묶어 앱을 만들고 웹페이지(`web/`)를 연결 |
| `__main__.py` | `python -m server` 실행 진입점 |

처리 흐름은 `events.py` 맨 위 설명을 먼저 읽으면 이해하기 쉽습니다.

## 설치

Python 3.10 이상, 저장소 최상위 폴더 기준입니다.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r server/requirements.txt
```

GPU(NVIDIA)가 있으면 [PyTorch 안내](https://pytorch.org/get-started/locally/)에 따라 CUDA 버전 PyTorch를 먼저 설치한 뒤 위 명령을 실행합니다. YOLO 모델(`yolov8n-pose.pt`)은 처음 실행할 때 자동으로 내려받습니다.

## 실행

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

## 환경변수

| 이름 | 기본값 | 내용 |
| --- | --- | --- |
| `FALL_HOST`, `FALL_PORT` | `0.0.0.0`, `8443` | 접속 주소 |
| `FALL_SSL_CERT`, `FALL_SSL_KEY` | 없음 | HTTPS 인증서·키 파일. 없으면 HTTP로 실행(경고 출력) |
| `FALL_COOKIE_SECURE` | `1` | HTTP로 시험할 때만 `0` |
| `FALL_DATA_DIR` | `data/` | DB(`server.db`) 위치 |
| `FALL_WEB_DIR` | `web/` | 웹페이지 폴더 |
| `FALL_POSE_BACKEND` | `yolo` | `fake`는 테스트 전용 |
| `FALL_YOLO_MODEL` | `yolov8n-pose.pt` | YOLO 모델 파일 |
| `FALL_STT_BACKEND` | `whisper` | `fake`는 테스트 전용 |
| `FALL_WHISPER_MODEL` | `small` | `base`, `small`, `medium` 등. 클수록 정확하지만 느림 |
| `FALL_DESCENT_PEAK` | `2.0` | 하강 속도 기준(몸통 길이/초) |
| `FALL_T1_SECONDS` | `2.0` | 하강 뒤 누운 자세가 이어져야 하는 시간 |
| `FALL_VOICE_TIMEOUT` | `20` | 음성 확인 요청 후 녹음을 기다리는 시간(초) |
| `FALL_VAPID_PRIVATE_KEY`, `FALL_VAPID_PUBLIC_KEY`, `FALL_VAPID_SUBJECT` | 없음 | 웹 푸시. 없으면 푸시 없이 보호자 화면 알림만 동작 |

그 밖의 임계값은 `config.py`에 있습니다.

## 테스트

```bash
pip install httpx pytest
python -m pytest server/tests -q
```

자세 추정과 음성 인식은 가짜를 씁니다. 가짜 자세 추정은 프레임 대신 관절 좌표 JSON을, 가짜 음성 인식은 녹음 대신 문장(UTF-8)을 받습니다(`detection/pose.py`의 `fake_frame`).

## 웹·Pi 클라이언트가 알아야 할 것

Pi 클라이언트는 별도 저장소 [elderly_fall_detection_pi_client](https://github.com/mandur23/elderly_fall_detection_pi_client)에 있습니다.

- **웹페이지 위치:** `web/index.html`, `login.html`, `signup.html`, `mypage.html`, `camera.html`, `guardian.html`, `kiosk.html`, 서비스 워커는 `web/sw.js`, 그 밖의 파일은 `web/static/`에 두면 서버가 자동으로 제공합니다. `mypage`, `camera`, `guardian`은 로그인하지 않으면 로그인 화면으로 보냅니다.
- **카메라 연결:** `wss://<서버>/ws/camera?device_id=N` (휴대폰은 로그인 쿠키, Pi는 `&key=<기기 키>` 또는 `X-Device-Key` 헤더). 바이너리 메시지 1개 = JPEG 1장.
  - 서버가 보내는 메시지: `{"type":"hello",...}`, `{"type":"state","state":"NORMAL|SUSPECT|ALARM"}`, `{"type":"voice_check","event_id":N,"attempt":n}`
  - 접속을 거부하는 코드: `4401` 인증 실패, `4403` 권한 없음. 이때는 재연결하지 말고 로그인이나 기기 키를 확인하게 합니다.
- **음성 확인:** `voice_check`를 받으면 안내 음성을 재생하고 몇 초 녹음해 `POST /api/events/{event_id}/voice`(multipart 파일 `audio`)로 보냅니다. 마이크 권한이 없거나 녹음에 실패하면 `audio` 없이 `error` 필드만 보냅니다(경보로 처리됨). 최대 10MB.
- **보호자 화면:** `wss://<서버>/ws/guardian`. 접속 직후 `snapshot`, 이후 `device`·`event` 메시지를 받습니다.
- **기기 등록:** 휴대폰은 `POST /api/devices {"name","kind":"phone"}`로 만든 `device_id`를 브라우저에 저장해 둡니다. Pi는 마이페이지에서 `kind:"pi"`로 만들 때 한 번만 나오는 기기 키를 Pi 설정에 넣습니다.
