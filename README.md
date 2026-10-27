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

- **중앙 서버:** 팀원 PC (Python, YOLO-Pose, faster-whisper, SQLite)
- **현장 단말:** Raspberry Pi 5, 카메라, 마이크, 스피커, 디스플레이
- **모바일:** 서버가 제공하는 모바일 웹페이지 (HTML·JavaScript) — 휴대폰 카메라 감지·음성 확인, 보호자 웹 푸시 알림, 회원가입·로그인·보호자 등록
- **감지 방식:** YOLO-Pose 관절 추정 + 몸통 각도·종횡비·하강 속도 규칙

## 진행 상태

2026-10-25에 구조를 다시 계획했습니다. Pi에서 YOLO-Pose가 너무 느려서, 영상 분석과 음성 인식을 중앙 서버(팀원 PC)로 옮기고 Pi와 모바일 웹을 클라이언트로 두는 구조로 바꿉니다. 새 구조는 처음부터 다시 작성하며, 일정과 결정 사항은 [프로젝트 계획](docs/PROJECT_PLAN.md), 구조와 통신 형식은 [시스템 구성](docs/ARCHITECTURE.md)에 있습니다.

### 빠른 실행

**Windows: `run_server.bat` 더블클릭** (macOS·Linux: `./run_server.sh`)

처음 실행하면 Python 설치(없을 때), 가상환경과 패키지 설치, HTTPS 인증서 만들기, 방화벽 설정까지 자동으로 한 뒤 서버를 켭니다. 서버와 웹이 함께 켜지고, 서버 PC 브라우저에 **관리자 화면**이 열립니다. 다음부터는 바로 켜집니다. 화면에 나온 주소로 휴대폰에서 접속합니다([휴대폰에서 웹 접속하기](docs/PHONE_ACCESS.md)). 장비·모델 없이 화면만 보려면 `run_server.bat --fake`.

새 구조의 코드는 다음과 같습니다.

| 구성 | 위치 |
| --- | --- |
| 중앙 서버 | [`server/`](server/README.md) — 실행·설정·테스트 방법 포함 |
| 모바일 웹 | `web/` — 서버(`python -m server`)가 함께 제공하므로 따로 켤 필요 없음 |
| Pi 현장 단말 클라이언트 | 별도 저장소 [Elderly_Fall_Detection_Pi_Client](https://github.com/mandur23/Elderly_Fall_Detection_Pi_Client) |

아래 코드 구조와 실행 방법은 **이전 구조(Pi 단독)의 시험 구현**에 대한 설명이며, 새 구조를 만들 때 참고용으로 남겨 둡니다. 장비 없이 가상 사람·가상 음성으로 동작을 확인할 수 있지만 실제 장비에서의 동작과 성능은 검증하지 않았습니다.

## 이전 시험 구현의 코드 구조

| 경로 | 역할 |
| --- | --- |
| `l3_perception/` | 카메라 입력, 자세 추정(MediaPipe·YOLO 후보), 특징 계산, 낙상 의심 판정 |
| `l4_application/` | 음성 확인, 이벤트 기록(SQLite), 알림(텔레그램 시험 구현), 현장 화면, 로컬 API |
| `l2_system/` | Raspberry Pi systemd 서비스 파일 |
| `run_demo.py` | 장비 없이 전체 흐름을 확인하는 데모 |
| `tests/` | 단위 테스트 |

L3(감지)와 L4(응용)는 별도 프로세스이며 `127.0.0.1` 로컬 API로 통신합니다. L3는 낙상 의심까지만 판단해 보고하고, 음성 확인·경보·해제는 L4가 관리합니다. L4가 이벤트를 끝내면 L3는 하트비트 응답으로 이를 확인하고 감시를 재개합니다.

## 실행 방법

Python 3.10~3.13에서 저장소 최상위 폴더 기준으로 실행합니다(더 새로운 Python은 numpy·PyTorch 설치 파일이 아직 없습니다).

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
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

실제 장치에서는 L4 서버와 L3 감지 프로세스를 각각 실행합니다. 자세 추정 모델(`mediapipe` 또는 `ultralytics`), 음성 인식(`vosk`, `sounddevice`)과 Vosk 한국어 모델(`data/vosk-model-small-ko`), 안내 음성 파일(`assets/audio/ask_status.wav`)이 필요합니다. 알림을 보내려면 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 환경변수를 설정합니다. 설정하지 않으면 알림을 보내지 않고 화면에 '알림 미설정'으로 표시합니다.

```bash
python -m l4_application.server
```

```bash
python -m l3_perception.fall_detector
```

## 사용 범위

이 시스템은 실내 안전 모니터링을 돕기 위한 교육·연구용 프로토타입입니다. 의료적 판단이나 응급기관 신고를 대신하지 않으며, 실제 긴급구조 체계와의 연계는 향후 검토 범위입니다.


## 프로젝트 문서

- [전체 문서 목록](docs/README.md)
- [프로젝트 계획](docs/PROJECT_PLAN.md)
- [요구사항](docs/REQUIREMENTS.md)
- [시스템 구성](docs/ARCHITECTURE.md)
- [장비 및 개발 환경](docs/HARDWARE.md)
- [테스트 계획](docs/TEST_PLAN.md)
