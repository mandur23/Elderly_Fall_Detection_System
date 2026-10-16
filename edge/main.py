"""python -m edge 진입점: 설정을 읽고 카메라·마이크·스피커를 연 뒤 서버에 접속한다."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from edge import __version__
from edge.camera import Camera
from edge.client import EdgeClient, FatalError, UpdateRequested
from edge.config import ConfigError, EdgeConfig, build_parser, load, save_settings, server_urls
from edge.mic import DisabledMicrophone, Microphone, SoundDeviceMicrophone
from edge.speaker import NullSpeaker, SoundDeviceSpeaker, Speaker
from edge.update import RESTART_EXIT_CODE
from shared import protocol

log = logging.getLogger("edge")

WAITING_PAGE = Path(__file__).resolve().parent / "deploy" / "waiting.html"   # 키오스크가 켜자마자 여는 대기 화면


def make_microphone(cfg: EdgeConfig) -> Microphone:
    return SoundDeviceMicrophone(cfg.mic_device) if cfg.mic else DisabledMicrophone()


def make_speaker(cfg: EdgeConfig) -> Speaker:
    if cfg.speaker == "null":
        return NullSpeaker(cfg.volume)
    return SoundDeviceSpeaker(cfg.speaker_device, cfg.volume)


def list_devices() -> int:
    try:
        import sounddevice as sd
    except Exception as e:  # PortAudio가 없으면 OSError
        print(f"sounddevice를 불러올 수 없습니다: {e}", file=sys.stderr)
        return 1
    print(sd.query_devices())
    print("\n> 기본 입력, < 기본 출력. --mic-device / --speaker-device에 번호나 이름 일부를 지정하세요.")
    return 0


def kiosk_url(cfg: EdgeConfig) -> str:
    _, http = server_urls(cfg.server)
    if not cfg.token:
        raise ConfigError("장치 토큰이 없어 현장 화면 키를 만들 수 없습니다. config.toml의 [server] token을 채우세요")
    # 서버에 라즈베리파이가 여러 대 붙을 수 있으므로 현장 화면은 이 장치의 상태만 보이게 device를 붙이고,
    # 이 장치의 화면만 서버 API를 쓸 수 있게 장치별 키(장치 토큰에서 계산, 서버도 같은 값을 계산)를 붙인다.
    key = protocol.kiosk_key(cfg.token, cfg.device_id)
    return f"{http}{protocol.KIOSK_PATH}?device={quote(cfg.device_id, safe='')}&key={key}"


def waiting_url(cfg: EdgeConfig, page: Path = WAITING_PAGE) -> Tuple[str, Optional[str]]:
    """대기 화면(file://.../waiting.html) 주소와 설정 오류(없으면 None).

    설정이 맞으면 현장 화면·health 주소와 장치 이름을 붙인다(대기 화면이 health가 응답하면 현장 화면으로 넘어감).
    틀리면 error(설명)를 붙인다(대기 화면이 '설정이 필요합니다'를 보임).
    """
    params: Dict[str, str] = {"name": cfg.name or cfg.device_id}
    error = None
    try:
        _, http = server_urls(cfg.server)
        params["server"] = http
        params.update(kiosk=kiosk_url(cfg), health=http + protocol.HEALTH_PATH)
    except ConfigError as e:
        error = str(e)
        params["error"] = error
    return _page_url(page, params), error


def _page_url(page: Path, params: Dict[str, str]) -> str:
    return f"{page.as_uri()}?{urlencode(params, quote_via=quote)}"


def print_url(cfg: EdgeConfig, which: str) -> int:
    if which == "waiting":
        url, error = waiting_url(cfg)
        print(url)
        if error:
            print(f"설정 오류: {error}", file=sys.stderr)
        return 2 if error else 0
    ws, http = server_urls(cfg.server)
    urls = {"ws": ws, "http": http, "health": http + protocol.HEALTH_PATH}
    if which == "kiosk":
        urls["kiosk"] = kiosk_url(cfg)
    print(urls[which])
    return 0


def save_config(args: argparse.Namespace) -> int:
    """--server·--token·--device-id·--name을 설정 파일에 저장한다(설치 스크립트용). 토큰은 화면에 보이지 않는다."""
    given: List[Tuple[Tuple[str, str], str, Optional[str]]] = [
        (("server", "address"), "--server", args.server),
        (("server", "token"), "--token", args.token),
        (("device", "id"), "--device-id", args.device_id),
        (("device", "name"), "--name", args.name),
    ]
    values: Dict[Tuple[str, str], str] = {}
    for key, option, value in given:
        if value is None:
            continue
        if not value.strip():
            raise ConfigError(f"{option} 값이 비어 있습니다")
        values[key] = value.strip()
    if not values:
        raise ConfigError("저장할 값이 없습니다 (--server, --token, --device-id, --name)")
    if ("server", "address") in values:
        server_urls(values[("server", "address")])   # 형식 확인
    save_settings(args.config, values)
    shown = [f"{key[1]}={'(숨김)' if key == ('server', 'token') else value}" for key, value in values.items()]
    print(f"설정 저장: {args.config} ({', '.join(shown)})")
    return 0


async def _run(client: EdgeClient) -> int:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, client.stop)
        except (NotImplementedError, AttributeError):
            pass  # Windows: Ctrl+C는 KeyboardInterrupt로 처리
    try:
        await client.run()
    except UpdateRequested as e:
        # systemd가 다시 띄우면서 ExecStartPre(python -m edge.update)가 서버의 새 코드를 받는다.
        log.warning("%s - 업데이트하려고 프로그램을 다시 시작합니다 (종료 코드 %d)", e, RESTART_EXIT_CODE)
        return RESTART_EXIT_CODE
    except FatalError as e:
        log.critical("%s - 프로그램을 끝냅니다", e)
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return list_devices()
    try:
        if args.save_config:
            return save_config(args)
        cfg = load(args)
        if args.print_url:
            return print_url(cfg, args.print_url)
        cfg.check()
    except ConfigError as e:
        if args.print_url == "waiting":
            # 설정 파일을 읽지 못해도 대기 화면에 이유를 보인다(kiosk.sh가 이 주소를 연다).
            print(_page_url(WAITING_PAGE, {"error": str(e)}))
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    log.info("fall-edge %s 시작: 장치 %s(%s), 서버 %s", __version__, cfg.device_id, cfg.name or "-", cfg.ws_url)

    camera = Camera(cfg.camera, cfg.source, cfg.rotation, cfg.loop_file)
    mic = make_microphone(cfg)
    speaker = make_speaker(cfg)
    if not speaker.real:
        log.warning("스피커: 시험용 가짜 출력(null)입니다. 소리가 나지 않습니다")
    camera.start()
    mic.start()
    speaker.start()
    client = EdgeClient(cfg, camera, mic, speaker)
    try:
        return asyncio.run(_run(client))
    except KeyboardInterrupt:
        return 0
    finally:
        log.info("종료합니다")
        camera.stop()
        mic.stop()
        speaker.close()
