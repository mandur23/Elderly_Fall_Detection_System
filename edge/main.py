"""python -m edge 진입점: 설정을 읽고 카메라·마이크·스피커를 연 뒤 서버에 접속한다."""

import asyncio
import logging
import signal
import sys
from typing import List, Optional
from urllib.parse import quote

from edge import __version__
from edge.camera import Camera
from edge.client import EdgeClient, FatalError
from edge.config import ConfigError, EdgeConfig, build_parser, load, server_urls
from edge.mic import DisabledMicrophone, Microphone, SoundDeviceMicrophone
from edge.speaker import NullSpeaker, SoundDeviceSpeaker, Speaker
from shared import protocol

log = logging.getLogger("edge")


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


def print_url(cfg: EdgeConfig, which: str) -> int:
    ws, http = server_urls(cfg.server)
    urls = {"ws": ws, "http": http, "health": http + protocol.HEALTH_PATH}
    if which == "kiosk":
        if not cfg.token:
            raise ConfigError("장치 토큰이 없어 현장 화면 키를 만들 수 없습니다. config.toml의 [server] token을 채우세요")
        # 서버에 라즈베리파이가 여러 대 붙을 수 있으므로 현장 화면은 이 장치의 상태만 보이게 device를 붙이고,
        # 이 장치의 화면만 서버 API를 쓸 수 있게 장치별 키(장치 토큰에서 계산, 서버도 같은 값을 계산)를 붙인다.
        key = protocol.kiosk_key(cfg.token, cfg.device_id)
        urls["kiosk"] = f"{http}{protocol.KIOSK_PATH}?device={quote(cfg.device_id, safe='')}&key={key}"
    print(urls[which])
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
    except FatalError as e:
        log.critical("%s - 프로그램을 끝냅니다", e)
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return list_devices()
    try:
        cfg = load(args)
        if args.print_url:
            return print_url(cfg, args.print_url)
        cfg.check()
    except ConfigError as e:
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
