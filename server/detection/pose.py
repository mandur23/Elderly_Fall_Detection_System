"""자세 추정 래퍼. 어떤 백엔드든 JPEG 바이트를 받아 같은 Keypoints를 반환한다.

estimate()는 블로킹 함수이므로 이벤트 루프에서는 asyncio.to_thread로 호출한다.
추정기 하나를 모든 기기가 같이 쓰므로 YOLO 추론은 잠금으로 한 번에 하나씩만 돌린다.

모델을 불러오지 못하면 PoseEstimatorError를 던진다. 조용히 가짜 추정기로 바꾸면
실제로는 아무것도 감지하지 않으면서 화면에는 '정상'이 표시되기 때문이다.
"""

import json
import logging
import threading
from typing import Optional

import numpy as np

from server.detection.features import Keypoints

log = logging.getLogger("server.detection")

# COCO 키포인트 번호: 5/6 왼쪽·오른쪽 어깨, 11/12 왼쪽·오른쪽 엉덩이
COCO_LEFT_SHOULDER, COCO_RIGHT_SHOULDER = 5, 6
COCO_LEFT_HIP, COCO_RIGHT_HIP = 11, 12


class PoseEstimatorError(RuntimeError):
    pass


class PoseEstimator:
    name = "base"

    def estimate(self, frame_bytes: bytes) -> Optional[Keypoints]:
        """프레임 1장을 분석한다. 사람이 없거나 프레임을 읽을 수 없으면 None."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def keypoints_from(points: np.ndarray, conf: np.ndarray, min_conf: float) -> Optional[Keypoints]:
    """COCO 17점 배열에서 Keypoints를 만든다. 어깨·엉덩이 4점이 모두 신뢰도 기준을 넘어야 한다.

    핵심 4점 중 하나라도 불확실하면 몸통 각도·속도가 엉뚱하게 계산되므로 '사람 없음'으로 본다.
    """
    core = [COCO_LEFT_SHOULDER, COCO_RIGHT_SHOULDER, COCO_LEFT_HIP, COCO_RIGHT_HIP]
    if np.any(conf[core] < min_conf):
        return None
    visible = points[conf >= min_conf]
    xmin, ymin = visible.min(axis=0)
    xmax, ymax = visible.max(axis=0)
    return Keypoints(
        left_shoulder=(float(points[COCO_LEFT_SHOULDER][0]), float(points[COCO_LEFT_SHOULDER][1])),
        right_shoulder=(float(points[COCO_RIGHT_SHOULDER][0]), float(points[COCO_RIGHT_SHOULDER][1])),
        left_hip=(float(points[COCO_LEFT_HIP][0]), float(points[COCO_LEFT_HIP][1])),
        right_hip=(float(points[COCO_RIGHT_HIP][0]), float(points[COCO_RIGHT_HIP][1])),
        bbox=(float(xmin), float(ymin), float(xmax), float(ymax)),
        all_points=visible,
        confidence=float(conf[core].mean()),
    )


def _to_numpy(value) -> np.ndarray:
    """ultralytics 텐서(torch)든 numpy 배열이든 numpy로 바꾼다."""
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value, dtype=float)


class YoloPoseEstimator(PoseEstimator):
    """Ultralytics YOLO-Pose (AGPL-3.0). 화면에 여러 명이면 가장 확실한 사람 한 명만 본다."""
    name = "yolo"

    def __init__(self, model_path: str, min_conf: float):
        # ultralytics는 무거우므로 실제로 이 백엔드를 쓸 때만 불러온다.
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise PoseEstimatorError("ultralytics 패키지가 설치되지 않았습니다.") from e
        try:
            self._model = YOLO(model_path)
        except Exception as e:  # 파일 없음, 손상된 가중치 등
            raise PoseEstimatorError(f"YOLO-Pose 모델을 불러오지 못했습니다: {model_path} ({e})") from e
        self.min_conf = min_conf
        self._lock = threading.Lock()
        log.info("YOLO-Pose 모델 로드 완료: %s", model_path)

    def estimate(self, frame_bytes: bytes) -> Optional[Keypoints]:
        import cv2

        frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            log.debug("JPEG 디코딩 실패 (%d바이트)", len(frame_bytes))
            return None
        # 여러 기기가 같은 모델을 공유하므로 추론은 한 번에 하나씩 한다.
        with self._lock:
            result = self._model(frame, verbose=False)[0]
        if result.keypoints is None or result.boxes is None or len(result.boxes) == 0:
            return None
        best = int(np.argmax(_to_numpy(result.boxes.conf)))
        points = _to_numpy(result.keypoints.xy)[best]
        if result.keypoints.conf is not None:
            conf = _to_numpy(result.keypoints.conf)[best]
        else:
            conf = np.ones(len(points))
        kp = keypoints_from(points, conf, self.min_conf)
        if kp is not None:
            kp.image_height, kp.image_width = frame.shape[:2]
        return kp


class FakePoseEstimator(PoseEstimator):
    """테스트 전용. 프레임 바이트를 이미지가 아니라 관절 좌표 JSON으로 읽는다.

    {"none": true} -> 사람 없음
    {"ls":[x,y], "rs":[x,y], "lh":[x,y], "rh":[x,y], "bbox":[x0,y0,x1,y1]} -> Keypoints
    """
    name = "fake"

    def estimate(self, frame_bytes: bytes) -> Optional[Keypoints]:
        try:
            data = json.loads(frame_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            log.debug("가짜 프레임을 읽을 수 없음")
            return None
        if not isinstance(data, dict) or data.get("none"):
            return None
        try:
            ls, rs, lh, rh = (tuple(float(v) for v in data[k]) for k in ("ls", "rs", "lh", "rh"))
            x0, y0, x1, y1 = (float(v) for v in data["bbox"])
        except (KeyError, TypeError, ValueError):
            log.debug("가짜 프레임 형식 오류: %s", data)
            return None
        return Keypoints(
            left_shoulder=ls,
            right_shoulder=rs,
            left_hip=lh,
            right_hip=rh,
            bbox=(x0, y0, x1, y1),
            all_points=np.array([ls, rs, lh, rh], dtype=float),
            confidence=float(data.get("conf", 1.0)),
        )


def fake_frame(kind: str, offset_y: float = 0.0) -> bytes:
    """FakePoseEstimator용 프레임을 만든다 (다른 모듈 테스트용).

    kind: 'standing' - 세로로 긴 박스, 수직 몸통 (엉덩이 y=300, 몸통 길이 100)
          'lying'    - 가로로 긴 박스, 수평 몸통 (엉덩이 y=400)
          'none'     - 사람 없음
    offset_y만큼 전체를 아래로 옮긴다. 서 있는 프레임의 offset_y를 빠르게 키우면 '하강'이 된다.
    """
    if kind == "none":
        return json.dumps({"none": True}).encode("utf-8")
    dy = float(offset_y)
    if kind == "standing":
        frame = {
            "ls": [305.0, 200.0 + dy], "rs": [335.0, 200.0 + dy],
            "lh": [310.0, 300.0 + dy], "rh": [330.0, 300.0 + dy],
            "bbox": [290.0, 150.0 + dy, 350.0, 450.0 + dy],
        }
    elif kind == "lying":
        frame = {
            "ls": [220.0, 385.0 + dy], "rs": [220.0, 415.0 + dy],
            "lh": [320.0, 390.0 + dy], "rh": [320.0, 410.0 + dy],
            "bbox": [150.0, 370.0 + dy, 470.0, 430.0 + dy],
        }
    else:
        raise ValueError(f"알 수 없는 가짜 프레임 종류: {kind}")
    return json.dumps(frame).encode("utf-8")


def create_pose_estimator(settings) -> PoseEstimator:
    """settings.pose_backend에 맞는 추정기를 만든다. 알 수 없는 값이면 PoseEstimatorError."""
    backend = settings.pose_backend
    if backend == "yolo":
        return YoloPoseEstimator(settings.yolo_model_path, settings.keypoint_min_confidence)
    if backend == "fake":
        return FakePoseEstimator()
    raise PoseEstimatorError(f"알 수 없는 자세 추정 백엔드: {backend}")
