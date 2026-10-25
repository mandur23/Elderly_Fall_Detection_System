"""음성 확인 한 번: 안내 음성 재생 → 응답 녹음 → 서버에 업로드.

서버가 WebSocket으로 {"type":"voice_check","event_id":N,"attempt":n}을 보내면 client.py가 이 함수를
따로 돌린다(그동안에도 프레임은 계속 보낸다). 결과 판정(괜찮음·도움 요청·무응답)은 서버가 한다.

실패 처리 원칙
- 안내 음성이 없거나 재생에 실패해도 녹음은 한다. 현장 화면(키오스크)에 안내 문구가 떠 있기 때문이다.
- 녹음에 실패하면 녹음 대신 'error'만 보낸다. 서버가 응답 시간 초과(기본 20초)를 기다리지 않고
  바로 '확인 불가' 경보를 내게 하기 위해서다.
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx

from pi_client.audio import AudioDevice, AudioError

log = logging.getLogger("pi.voice_check")

# 결과(로그·테스트용)
UPLOADED = "uploaded"              # 녹음을 올렸고 서버가 받음
REPORTED_ERROR = "reported_error"  # 녹음 실패를 알렸고 서버가 받음
IGNORED = "ignored"                # 서버가 더는 이 회차를 기다리지 않음(이미 해제·시간 초과·다음 회차)
HTTP_ERROR = "http_error"          # 서버가 2xx가 아닌 응답
NETWORK_ERROR = "network_error"    # 서버에 닿지 못함

# 네트워크가 잠깐 끊긴 경우를 위한 재시도. 서버의 응답 대기 시간 안에 끝나도록 짧게 둔다.
UPLOAD_TRIES = 3
UPLOAD_RETRY_DELAY = 1.0
# 서버가 받는 error 필드는 짧은 이유면 충분하다.
MAX_ERROR_CHARS = 200


async def _play_prompt(audio: AudioDevice, prompt: Path) -> None:
    if not prompt.is_file():
        log.warning("안내 음성 파일이 없습니다: %s (pi_client/assets/README.md 참고). 녹음만 합니다.", prompt)
        return
    try:
        # 끝까지 재생한 뒤에 녹음을 시작해야 안내 음성이 녹음에 섞이지 않는다.
        await asyncio.to_thread(audio.play_wav, prompt)
    except AudioError as e:
        log.warning("안내 음성 재생 실패: %s. 녹음은 계속합니다.", e)
    except Exception as e:  # 장치 드라이버의 예상 못 한 예외도 녹음을 막지 않게 한다
        log.warning("안내 음성 재생 중 예외: %r. 녹음은 계속합니다.", e)


async def _record(audio: AudioDevice, seconds: float, sample_rate: int) -> Tuple[Optional[bytes], Optional[str]]:
    """(WAV 바이트, None) 또는 (None, 실패 이유)."""
    try:
        data = await asyncio.to_thread(audio.record, seconds, sample_rate)
    except AudioError as e:
        return None, str(e) or "녹음 실패"
    except Exception as e:
        return None, f"녹음 실패: {e!r}"
    return data, None


async def run_voice_check(settings, audio: AudioDevice, http: httpx.AsyncClient, event_id: int, attempt: int) -> str:
    """음성 확인을 한 번 하고 결과 문자열(UPLOADED 등)을 돌려준다. 예외는 던지지 않는다(취소 제외)."""
    log.info("음성 확인 시작: 이벤트 %s, %s회차", event_id, attempt)
    await _play_prompt(audio, Path(settings.prompt_wav))
    wav, error = await _record(audio, settings.record_seconds, settings.sample_rate)

    data: Dict[str, str] = {"attempt": str(attempt)}
    if error is None:
        files = {"audio": ("reply.wav", wav or b"", "audio/wav")}
    else:
        error = error[:MAX_ERROR_CHARS]
        log.warning("녹음 실패: %s. 서버에 '확인 불가'로 알립니다.", error)
        # 파일 이름 없는 파트로 넣어 multipart 형식을 유지한다(서버는 일반 문자열 필드로 읽는다).
        files = {"error": (None, error)}

    url = f"{settings.server_url}/api/events/{event_id}/voice"
    headers = {"X-Device-Key": settings.device_key}
    for tries in range(1, UPLOAD_TRIES + 1):
        try:
            res = await http.post(url, data=data, files=files, headers=headers)
            break
        except httpx.HTTPError as e:
            if tries == UPLOAD_TRIES:
                log.error("음성 확인 업로드 실패(이벤트 %s): %s", event_id, e)
                return NETWORK_ERROR
            log.warning("음성 확인 업로드 실패(%d/%d): %s. 다시 시도합니다.", tries, UPLOAD_TRIES, e)
            await asyncio.sleep(UPLOAD_RETRY_DELAY)

    if not res.is_success:
        log.error("음성 확인 업로드 거부(이벤트 %s): HTTP %s %s", event_id, res.status_code, res.text[:200])
        return HTTP_ERROR
    try:
        accepted = bool(res.json().get("accepted"))
    except (ValueError, AttributeError):
        accepted = False
    if not accepted:
        log.info("이벤트 %s %s회차: 서버가 이미 기다리지 않아 응답이 버려졌습니다", event_id, attempt)
        return IGNORED
    outcome = UPLOADED if error is None else REPORTED_ERROR
    log.info("음성 확인 업로드 완료: 이벤트 %s, %s회차 (%s)", event_id, attempt, outcome)
    return outcome
