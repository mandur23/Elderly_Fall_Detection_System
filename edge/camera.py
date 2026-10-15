"""카메라 입력과 JPEG 인코딩.

- PiCameraSource: 라즈베리파이 카메라 모듈(CSI). Bookworm 이후 libcamera 스택은 OpenCV로 열리지 않아 picamera2를 쓴다.
- OpenCVSource: USB 웹캠, 시험용 영상 파일(파일 속도대로 실시간처럼 내보냄), 스트림 URL.

Camera는 별도 스레드에서 카메라를 읽고, 보낼 수 있을 때(wanted)만 서버 설정(fps·해상도·품질)대로
JPEG로 만들어 sink(jpeg, 촬영 시각)로 넘긴다. 시각은 time.monotonic()이다.
카메라가 끊기거나 영상이 멈추면 1·2·5·10초 간격으로 다시 연다. 영상은 파일로 저장하지 않는다.
"""

import logging
import os
import threading
import time
from typing import Callable, Dict, Optional, Sequence

import cv2
import numpy as np

from shared import protocol

log = logging.getLogger("edge.camera")

RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0)
STALL_SECONDS = 3.0   # 이 시간 동안 장면이 없으면 카메라 문제로 보고 다시 연다

_ROTATE = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


class CameraError(RuntimeError):
    pass


class FrameSource:
    """read()는 BGR 장면(numpy 배열) 또는 실패·파일 끝이면 None."""
    name = "base"
    is_file = False

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> Optional[np.ndarray]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def file_fps(self) -> float:
        return 0.0


class OpenCVSource(FrameSource):
    name = "opencv"

    def __init__(self, source, width: int, height: int, fps: float):
        self.source, self.width, self.height, self.fps = source, width, height, fps
        self.is_file = isinstance(source, str) and "://" not in source
        self._cap = None

    def open(self) -> None:
        self.close()
        if self.is_file and not os.path.isfile(self.source):
            raise CameraError(f"영상 파일이 없습니다: {self.source}")
        cap = cv2.VideoCapture(self.source)
        if isinstance(self.source, int):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 오래된 장면이 쌓이지 않게
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"카메라를 열 수 없습니다 (소스: {self.source})")
        self._cap = cap

    def read(self) -> Optional[np.ndarray]:
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        return frame if ok else None

    @property
    def file_fps(self) -> float:
        if not self.is_file or self._cap is None:
            return 0.0
        fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        return fps if 0.0 < fps <= 240.0 else 0.0

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __str__(self) -> str:
        return f"opencv({self.source})"


class PiCameraSource(FrameSource):
    name = "picamera2"

    def __init__(self, camera_num: int, width: int, height: int, fps: float):
        self.camera_num, self.width, self.height, self.fps = camera_num, width, height, fps
        self._cam = None

    @staticmethod
    def available() -> bool:
        try:
            from picamera2 import Picamera2
            return len(Picamera2.global_camera_info()) > 0
        except Exception:
            return False

    def open(self) -> None:
        self.close()
        try:
            from picamera2 import Picamera2
            cam = Picamera2(self.camera_num)
            # picamera2의 "RGB888"은 메모리상 BGR 순서라 OpenCV와 바로 맞는다.
            cfg = cam.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                controls={"FrameRate": float(self.fps)},
            )
            cam.configure(cfg)
            cam.start()
        except Exception as e:
            raise CameraError(f"라즈베리파이 카메라를 열 수 없습니다: {e}") from e
        self._cam = cam

    def read(self) -> Optional[np.ndarray]:
        if self._cam is None:
            return None
        try:
            return self._cam.capture_array("main")
        except Exception as e:
            log.error("라즈베리파이 카메라 장면 읽기 실패: %s", e)
            return None

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass
            self._cam = None

    def __str__(self) -> str:
        return f"picamera2(#{self.camera_num}, {self.width}x{self.height})"


def make_source(backend: str, source, width: int, height: int, fps: float) -> FrameSource:
    """backend: auto | picamera2 | opencv. 영상 파일·URL이면 항상 OpenCV.

    auto는 라즈베리파이 카메라가 있으면 picamera2, 없으면 OpenCV 웹캠(기본 0번).
    """
    if isinstance(source, str):
        return OpenCVSource(source, width, height, fps)
    if backend == "picamera2" or (backend == "auto" and PiCameraSource.available()):
        return PiCameraSource(source or 0, width, height, fps)
    if backend in ("opencv", "auto"):
        return OpenCVSource(0 if source is None else source, width, height, fps)
    raise CameraError(f"알 수 없는 카메라 방식: {backend}")


def prepare_frame(frame: np.ndarray, width: int, height: int, rotation: int) -> np.ndarray:
    """회전 전 기준으로 width×height 안에 들어오게 줄이고(비율 유지, 키우지 않음) 회전한다."""
    if frame.ndim == 3 and frame.shape[2] == 4:
        frame = frame[:, :, :3]
    h, w = frame.shape[:2]
    if w > width or h > height:
        scale = min(width / w, height / h)
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    if rotation in _ROTATE:
        frame = cv2.rotate(frame, _ROTATE[rotation])
    return frame


def encode_jpeg(frame: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise CameraError("JPEG 인코딩 실패")
    return buf.tobytes()


class RateLimiter:
    """장면을 fps 이하로 고른다. 카메라 주기가 fps의 배수가 아니어도 평균이 fps에 가깝게 맞춘다."""

    TOLERANCE = 0.25  # 주기의 이 비율만큼 일찍 온 장면도 받는다 (30fps 카메라 -> 10fps에서 3장마다 1장)

    def __init__(self):
        self._next: Optional[float] = None

    def ready(self, t: float, fps: float) -> bool:
        if fps <= 0:
            return True
        period = 1.0 / fps
        if self._next is not None and t < self._next - self.TOLERANCE * period:
            return False
        base = self._next if self._next is not None and t - self._next < period else t
        self._next = base + period
        return True


Sink = Callable[[bytes, float], None]


class Camera:
    """카메라 스레드. set_sink()로 받을 곳을 정하고 start()한다."""

    def __init__(self, backend: str = "auto", source=None, rotation: int = 0, loop_file: bool = True,
                 video: Optional[Dict] = None, opener: Callable[..., FrameSource] = make_source,
                 retry_delays: Sequence[float] = RETRY_DELAYS, stall_seconds: float = STALL_SECONDS):
        self.backend, self.source, self.rotation, self.loop_file = backend, source, rotation, loop_file
        self._video = {**protocol.DEFAULT_VIDEO, **(video or {})}
        self._opener, self._retry_delays, self._stall_seconds = opener, tuple(retry_delays), stall_seconds
        self._lock = threading.Lock()
        self._generation = 0          # 해상도가 바뀌면 올려서 카메라를 다시 연다
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._sink: Optional[Sink] = None
        self._wanted: Callable[[], bool] = lambda: False
        self._thread: Optional[threading.Thread] = None
        self._last_frame = 0.0
        self._beat = time.monotonic()
        self._error = "카메라를 여는 중입니다"
        self.frames_read = 0
        self.opens = 0

    # --- 다른 스레드에서 부르는 것 ---
    def set_sink(self, sink: Optional[Sink], wanted: Callable[[], bool]) -> None:
        """sink(jpeg, 촬영 시각)는 카메라 스레드에서 불린다. wanted()가 False면 인코딩하지 않는다."""
        self._sink, self._wanted = sink, wanted

    @property
    def video(self) -> Dict:
        with self._lock:
            return dict(self._video)

    def apply(self, video: Dict) -> None:
        """서버가 준 영상 설정(width, height, fps, jpeg_quality)을 적용한다. 해상도가 바뀌면 다시 연다."""
        with self._lock:
            new = {**self._video, **video}
            if (new["width"], new["height"]) != (self._video["width"], self._video["height"]):
                self._generation += 1
                self._wake.set()
            self._video = new

    def status(self):
        """(정상 여부, 오류 설명)."""
        if self.backend == "none":
            return False, "카메라를 쓰지 않도록 설정되어 있습니다"
        ok = time.monotonic() - self._last_frame < self._stall_seconds and not self._error
        return ok, "" if ok else (self._error or "카메라에서 영상을 받지 못합니다")

    def stalled_for(self) -> float:
        """카메라 스레드가 멈춰 있는 시간(초). 드라이버가 응답하지 않으면 커진다."""
        if self._thread is None or not self._thread.is_alive():
            return 0.0
        return time.monotonic() - self._beat

    def start(self) -> None:
        if self.backend == "none" or self._thread is not None:
            return
        self._stop.clear()
        # 드라이버 호출에서 멈춰도 프로그램은 끝낼 수 있게 daemon 스레드로 둔다.
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # --- 카메라 스레드 ---
    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _set_error(self, message: str) -> None:
        if message != self._error:
            log.warning("카메라: %s", message)
        self._error = message

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self._beat = time.monotonic()
            with self._lock:
                video, generation = dict(self._video), self._generation
            try:
                src = self._opener(self.backend, self.source, video["width"], video["height"], video["fps"])
                src.open()
            except Exception as e:
                self._set_error(str(e))
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                attempt += 1
                log.info("카메라를 %.0f초 뒤 다시 엽니다", delay)
                self._wait(delay)
                continue
            self.opens += 1
            log.info("카메라 열림: %s", src)
            try:
                reason, got_frames = self._pump(src, generation)
            except Exception as e:
                log.exception("카메라 처리 중 오류")
                self._set_error(f"카메라 처리 오류: {e}")
                reason, got_frames = "failed", False
            finally:
                src.close()
            if got_frames:
                attempt = 0
            if reason == "failed" and not self._stop.is_set():
                delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                attempt += 1
                log.info("카메라를 %.0f초 뒤 다시 엽니다", delay)
                self._wait(delay)
            elif reason == "ended":
                self._set_error("영상 파일이 끝났습니다")
                while not self._stop.is_set() and self._generation == generation:
                    self._beat = time.monotonic()
                    self._wait(1.0)

    def _pump(self, src: FrameSource, generation: int):
        """장면을 읽어 넘긴다. (끝난 이유, 장면을 하나라도 읽었는지)를 돌려준다.

        이유: stop(종료), reconfigure(해상도 변경), loop(파일 반복), ended(파일 끝), failed(카메라 문제).
        """
        limiter = RateLimiter()
        file_fps = src.file_fps
        t0 = None
        index = 0
        last_ok = time.monotonic()
        while not self._stop.is_set():
            if self._generation != generation:
                return "reconfigure", index > 0
            self._beat = time.monotonic()
            with self._lock:
                video = dict(self._video)
            due = None
            if src.is_file:
                # 파일 속도대로(실시간처럼) 읽는다. 촬영 시각은 장면이 나올 예정 시각.
                fps = file_fps or float(video["fps"])
                now = time.monotonic()
                if t0 is None:
                    t0 = now
                due = t0 + index / fps
                if due - now > 0:
                    if self._stop.wait(due - now):
                        return "stop", index > 0
                elif now - due > 1.0:  # 많이 밀렸으면 기준을 다시 잡는다
                    t0, due = now - index / fps, now
            frame = src.read()
            now = time.monotonic()
            if frame is None:
                if src.is_file:
                    if index == 0:
                        self._set_error("영상 파일에서 장면을 읽을 수 없습니다")
                        return "failed", False
                    return ("loop" if self.loop_file else "ended"), True
                if now - last_ok > self._stall_seconds:
                    self._set_error("카메라에서 영상을 받지 못합니다")
                    return "failed", index > 0
                self._stop.wait(0.02)
                continue
            ts = due if due is not None else now
            index += 1
            last_ok = self._last_frame = now
            self.frames_read += 1
            if self._error:
                log.info("카메라 정상")
                self._error = ""
            sink = self._sink
            if sink is not None and self._wanted() and limiter.ready(ts, float(video["fps"])):
                image = prepare_frame(frame, int(video["width"]), int(video["height"]), self.rotation)
                sink(encode_jpeg(image, int(video["jpeg_quality"])), ts)
        return "stop", index > 0
