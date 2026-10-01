"""카메라 입력.

- PiCamera2Camera: Raspberry Pi 카메라 모듈(CSI). Bookworm의 libcamera 스택은 OpenCV로 열리지 않는다.
- OpenCVCamera: USB 웹캠 또는 시험용 영상 파일.

read()는 BGR 프레임(numpy 배열) 또는 실패 시 None을 반환한다.
"""

import logging
from typing import Optional, Union

import numpy as np

log = logging.getLogger("l3.camera")


class CameraError(RuntimeError):
    pass


class Camera:
    name = "base"

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> Optional[np.ndarray]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class OpenCVCamera(Camera):
    name = "opencv"

    def __init__(self, source: Union[int, str], width: int, height: int):
        self.source, self.width, self.height = source, width, height
        self._cap = None

    def open(self) -> None:
        import cv2
        self.close()
        cap = cv2.VideoCapture(self.source)
        if isinstance(self.source, int):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 오래된 프레임이 쌓이지 않게
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
        import cv2
        return float(self._cap.get(cv2.CAP_PROP_FPS)) if self._cap is not None else 0.0

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class PiCamera2Camera(Camera):
    name = "picamera2"

    def __init__(self, width: int, height: int, fps: int):
        self.width, self.height, self.fps = width, height, fps
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
            cam = Picamera2()
            # picamera2의 "RGB888"은 메모리상 BGR 순서라 OpenCV와 바로 호환된다.
            cfg = cam.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                controls={"FrameRate": float(self.fps)},
            )
            cam.configure(cfg)
            cam.start()
        except Exception as e:
            raise CameraError(f"Pi 카메라를 열 수 없습니다: {e}") from e
        self._cam = cam

    def read(self) -> Optional[np.ndarray]:
        if self._cam is None:
            return None
        try:
            return self._cam.capture_array("main")
        except Exception as e:
            log.error("Pi 카메라 프레임 읽기 실패: %s", e)
            return None

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass
            self._cam = None


def create_camera(backend: str, source: Union[int, str], width: int, height: int, fps: int) -> Camera:
    """backend: 'auto' | 'picamera2' | 'opencv'. 영상 파일 경로가 주어지면 항상 OpenCV를 쓴다."""
    if isinstance(source, str):
        return OpenCVCamera(source, width, height)
    if backend == "picamera2" or (backend == "auto" and PiCamera2Camera.available()):
        return PiCamera2Camera(width, height, fps)
    if backend in ("opencv", "auto"):
        return OpenCVCamera(source, width, height)
    raise CameraError(f"알 수 없는 카메라 방식: {backend}")


def rotate(frame: np.ndarray, degrees: int) -> np.ndarray:
    if degrees % 360 == 0:
        return frame
    return np.ascontiguousarray(np.rot90(frame, k=-(degrees // 90) % 4))
