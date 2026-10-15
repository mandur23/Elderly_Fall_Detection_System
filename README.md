# 비전 AI 기반 노인 낙상 감지 및 음성 확인·긴급 알림 시스템

실내 카메라 영상으로 낙상 의심 상황을 감지하고, 음성으로 사용자 상태를 확인한 뒤 필요한 경우 보호자 또는 관리자에게 알림을 전달하는 캡스톤디자인 프로젝트입니다. Raspberry Pi 기반의 교육·연구용 프로토타입을 목표로 합니다.

## 프로젝트 목표

- 카메라 영상에서 사용자의 자세와 움직임을 실시간으로 분석
- 낙상 의심 상황을 판단하고 음성 안내로 사용자 상태 확인
- 확인 결과에 따라 이벤트를 기록하고 보호자 또는 관리자에게 알림 전달
- 영상, 음성, 상태 표시 기능을 하나의 Raspberry Pi 시스템으로 통합

## 시스템 흐름

카메라 입력 → 인체 상태 분석 → 낙상 의심 판단 → 음성 상태 확인 → 알림 및 기록

## 주요 기능 (개발 계획)

| 기능 | 내용 |
| --- | --- |
| 영상 입력 | 실내 카메라 영상 수집 및 처리 |
| 낙상 감지 | 비전 AI를 이용한 자세·움직임 분석 및 낙상 의심 상황 판단 |
| 음성 확인 | 낙상 의심 시 음성 안내와 사용자 상태 확인 |
| 알림 및 기록 | 이벤트 기록과 보호자 또는 관리자 대상 알림 |
| 사용자 화면 | 현재 상태와 주요 이벤트 표시 |

## 예정 개발 환경

- **장치:** Raspberry Pi 5, 카메라, 마이크, 스피커, 디스플레이
- **소프트웨어:** Python 및 영상처리 라이브러리
- **감지 방식:** 공개 모델과 여러 분석 방법을 비교·시험한 뒤 장치 환경에 적합한 방식을 선정할 예정

## 진행 상태

**새 구조로 바꾸는 중입니다.** 라즈베리파이는 카메라·마이크·스피커·화면 입출력만 하고, 영상·음성 처리(자세 추정, 낙상 판정, 음성 인식)와 경보·알림·기록은 PC 서버가 실시간으로 합니다. 둘은 같은 공유기(LAN) 안에서 WebSocket으로 연결됩니다([통신 규약](docs/PROTOCOL.md)).

- 새 구조(`edge/`, `server/`)를 이 PC에서 실제 모델·마이크·웹캠으로 끝까지 연결해 확인했습니다: 낙상 영상 → 낙상 의심 → 안내 음성 → 마이크 응답 확인 → 경보 → 화면 버튼 해제, 연결 끊김 감지와 재접속.
- 라즈베리파이 실기기에서는 아직 확인하지 않았습니다.

이전 구조(라즈베리파이 혼자 처리)에서 얻은 결과:

- 공개 낙상 영상(UR Fall, 낙상 30·일상 40)으로 감지 성능을 측정해 판정 기준을 정했습니다. 넘어진 뒤 계속 누워 있다고 가정하면 낙상 26/30 감지, 오경보 1/40입니다.
- 합성 음성으로 Vosk 음성 확인을 시험해 '괜찮아요' 인식 문제를 고쳤습니다.
- PC에서 웹캠·마이크·Vosk·MediaPipe로 감지부터 음성 확인, 경보, 화면 해제까지 전체 흐름을 확인했습니다.

자세한 결과와 한계는 [성능 평가 기록](docs/EVALUATION.md)에 있습니다. Raspberry Pi 실기기, 텔레그램 실제 전송, 실제 노인 사용자의 낙상·목소리로는 아직 검증하지 않았습니다.

## 코드 구조

| 경로 | 실행 위치 | 역할 |
| --- | --- | --- |
| `edge/` | 라즈베리파이 | 카메라 영상·마이크 소리를 서버로 보내고, 서버가 보낸 안내 음성·경보음을 재생. 설치 스크립트와 키오스크 실행 포함 ([edge/README.md](edge/README.md)) |
| `server/` | PC | 영상·음성 실시간 처리, 낙상 판정, 음성 확인, 경보·보호자 알림, 기록, 현장 화면 ([server/README.md](server/README.md)) |
| `shared/` | 양쪽 | 통신 규약 ([docs/PROTOCOL.md](docs/PROTOCOL.md)) |

빠른 시작 (자세한 내용은 각 README):

```bash
# PC 서버
pip install -r server/requirements.txt
python -m server                 # server/config.toml에 device_token, 모델 경로 설정

# 라즈베리파이: 설치 스크립트가 패키지·서비스·키오스크를 설정하고, edge/config.toml에 서버 주소·토큰을 적음
bash edge/deploy/install.sh

# PC에서 라즈베리파이 대신 시험 (웹캠 번호나 영상 파일)
pip install -r edge/requirements.txt
python -m edge --server 127.0.0.1 --token <같은 토큰> --source 0
```

서버가 켜지면 쓰는 화면:

| 주소 | 누가 | 내용 |
| --- | --- | --- |
| `http://<서버>:8000/kiosk?device=<장치 id>` | 어르신 (라즈베리파이 화면) | 그 방의 상태와 '괜찮아요' 버튼 |
| `http://<서버>:8000/g` | 보호자 (휴대폰) | 맡은 장치 상태·경보·기록, 119 전화, 경보 끄기 |
| `http://<서버>:8000/admin` | 관리자 | 모든 장치 상태, 기록, 장치 이상, 보호자 계정 관리 |

Windows 서버 PC에서 계속 켜 두기와 자동 배포(GitHub의 새 코드를 받아 시험 후 재시작)는
`server\deploy\windows\`(`start_server.bat`, `install_autostart.bat`, `update_server.ps1`)를 씁니다([server/README.md](server/README.md)).

테스트: `python -m unittest discover -s shared/tests -t .` (`edge/tests`, `server/tests`도 같은 방식). GitHub에 push하면
`.github/workflows/tests.yml`이 자동으로 돌립니다.

### 이전 구현 (참고용)

라즈베리파이 한 대가 모두 처리하던 이전 구현입니다. 새 구조가 라즈베리파이에서 확인되면 정리할 예정입니다.

| 경로 | 역할 |
| --- | --- |
| `l3_perception/` | 카메라 입력(Pi 카메라·USB), 자세 추정(MediaPipe·YOLO 후보), 특징 계산, 낙상 의심 판정 |
| `l4_application/` | 음성 확인, 이벤트 기록(SQLite), 알림(텔레그램 시험 구현), 현장 화면, 로컬 API |
| `l2_system/` | Raspberry Pi 설치 스크립트, 서비스 파일, 설정 예시 |
| `tools/` | 장치 점검, 영상 평가, 공개 낙상 영상 준비, 자세 추정 데모, 소리 파일 생성 |
| `run_demo.py` | 장비 없이 전체 흐름을 확인하는 데모 |
| `tests/` | 단위 테스트 |

L3(감지)와 L4(응용)는 별도 프로세스이며 `127.0.0.1` 로컬 API로 통신합니다. L3는 낙상 의심까지만 판단해 보고하고, 음성 확인·경보·해제는 L4가 관리합니다. L4가 이벤트를 끝내면 L3는 하트비트 응답으로 이를 확인하고 감시를 재개합니다.

## 이전 구현 실행 방법

새 구조의 실행 방법은 위 '코드 구조'와 [edge/README.md](edge/README.md), [server/README.md](server/README.md)를 보세요. 아래는 이전 구현입니다.

Python 3.11 이상에서 저장소 최상위 폴더 기준으로 실행합니다(설정 파일을 읽는 `tomllib`이 3.11부터 있습니다).

```bash
python -m venv .venv
pip install -r requirements.txt
```

단위 테스트:

```bash
python -m unittest discover tests
```

장비 없이 데모 실행 후 브라우저에서 `http://127.0.0.1:5000` 확인:

```bash
python run_demo.py
```

주요 시나리오(앉기·비틀거림·낙상 후 응답/무응답/버튼 해제/재감지) 자동 확인:

```bash
python run_demo.py --auto
```

Raspberry Pi 설치, 장치 점검, 서비스 관리는 [설치 및 운영 안내](docs/OPERATIONS.md)를 따릅니다.

```bash
bash l2_system/setup_pi.sh
```

기기별 설정은 `config.local.toml`에 적습니다(예시: `l2_system/config.local.toml.example`). 텔레그램 토큰을 설정하지 않으면 알림을 보내지 않고 화면에 '알림 미설정'으로 표시합니다.

영상 파일로 감지 성능 측정(자세 모델이 필요하므로 `requirements-device.txt`도 설치):

```bash
python -m tools.prepare_urfall data/urfall --download
python -m tools.evaluate data/urfall/rgb/labels.csv --hold 3
```

웹캠이나 영상 파일로 자세 추정과 판정 과정을 화면에 보기(발표·시연용):

```bash
python -m tools.pose_demo
```

상업적 사용 전에 필요한 검증·규제·운영 항목은 [상용화 준비 점검](docs/COMMERCIAL_READINESS.md)에 정리했습니다.

## 사용 범위

이 시스템은 실내 안전 모니터링을 돕기 위한 교육·연구용 프로토타입입니다. 의료적 판단이나 응급기관 신고를 대신하지 않으며, 실제 긴급구조 체계와의 연계는 향후 검토 범위입니다.


## 프로젝트 문서

- [전체 문서 목록](docs/README.md)
- [프로젝트 계획](docs/PROJECT_PLAN.md)
- [요구사항](docs/REQUIREMENTS.md)
- [시스템 구성](docs/ARCHITECTURE.md)
- [장비 및 개발 환경](docs/HARDWARE.md)
- [테스트 계획](docs/TEST_PLAN.md)
- [성능 평가 기록](docs/EVALUATION.md)
- [설치 및 운영 안내](docs/OPERATIONS.md)
- [상용화 준비 점검](docs/COMMERCIAL_READINESS.md)
