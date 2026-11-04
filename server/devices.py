"""연결된 기기(Pi·휴대폰 카메라 화면) 관리와 프레임 처리.

기기 하나가 접속하면 DeviceSession을 만들고, 그 기기 전용 특징 계산기와 FSM을 둔다.
기기마다 따로 두는 이유는 하강 속도·누운 시간 같은 값이 기기별 영상 흐름에 묶여 있기 때문이다.

프레임 처리 원칙:
- 이전 프레임을 아직 분석 중이면 새 프레임은 버린다(busy). 밀린 프레임을 줄 세워 처리하면
  판정이 점점 늦어지므로, 늦게라도 최신 장면을 보는 편이 낫다.
- 프레임 시각은 '받은 시각'으로 잡는다. 분석(YOLO)에 걸린 시간이 하강 속도 계산에 섞이지 않게 하기 위함이다.
- 자세 추정은 막히는 작업이라 asyncio.to_thread로 돌린다. 세션·FSM 같은 메모리 상태는
  이벤트 루프에서만 바꾼다(스레드는 결과만 돌려준다).

보호자 화면에 보내는 기기 상태는 바뀔 때만 보낸다. 감지 상태·연결·카메라 상태는 바로,
사람 감지 여부와 fps는 1초에 한 번까지만 보낸다(YOLO 결과가 프레임마다 깜빡일 수 있어서).
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

from server.config import Settings
from server.detection import FallFSM, FallState, FeatureExtractor, Keypoints, PoseEstimator

log = logging.getLogger("server.devices")

# 기기 WebSocket 전송이 이 시간 안에 끝나지 않으면 연결이 끊긴 것으로 본다.
DEVICE_SEND_TIMEOUT = 5.0
# 기기 상태 점검(카메라 응답 없음 판단, fps 갱신) 간격
STATUS_CHECK_INTERVAL = 1.0
# fps 계산에 쓰는 최근 구간(초)
FPS_WINDOW_SECONDS = 2.0
# 관리자 화면의 실시간 보기에 보여 줄 최근 분석 기록 수(초당 10장이면 15초)
LIVE_HISTORY_LEN = 150


def watcher_ids(accounts: Any, owner_id: int) -> List[int]:
    """기기·이벤트를 볼 수 있는 사람: 주인 본인 + 수락된 보호자. 보낼 때마다 DB에서 다시 읽어
    보호자 연결을 해제하면 바로 다음 메시지부터 받지 않게 한다."""
    return [owner_id] + [g for g in accounts.guardian_ids_for(owner_id) if g != owner_id]


def keypoints_view(kp: Optional[Keypoints]) -> Optional[Dict[str, Any]]:
    """관리자 화면에 그릴 관절 좌표(프레임 픽셀 기준). 사람이 없으면 None."""
    if kp is None:
        return None

    def point(p) -> List[float]:
        return [round(float(p[0]), 1), round(float(p[1]), 1)]

    return {
        "bbox": [round(float(v), 1) for v in kp.bbox],
        "shoulders": [point(kp.left_shoulder), point(kp.right_shoulder)],
        "hips": [point(kp.left_hip), point(kp.right_hip)],
        "points": [point(p) for p in kp.all_points],
        # COCO 17점 순서, 신뢰도 기준 미만은 null. 뼈대 선을 이을 때 쓴다(17점을 모르는 추정기는 null).
        "skeleton": None if kp.skeleton is None else [None if p is None else point(p) for p in kp.skeleton],
        "confidence": round(float(kp.confidence), 3),
    }


class BackgroundTasks:
    """기다리지 않고 돌려 둘 작업 모음. 참조를 들고 있어야 실행 중에 가비지 컬렉션되지 않는다."""

    def __init__(self, name: str):
        self.name = name
        self._tasks: Set[asyncio.Task] = set()

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("%s 백그라운드 작업 오류", self.name, exc_info=task.exception())

    async def wait(self) -> None:
        """남은 작업이 끝날 때까지 기다린다(테스트용)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


@dataclass
class DeviceSession:
    """접속 중인 기기 하나. 같은 기기가 다시 접속하면 새 세션으로 바뀐다."""

    device: Dict[str, Any]
    ws: Any
    extractor: FeatureExtractor
    fsm: FallFSM
    owner_name: Optional[str] = None
    connected_at: float = 0.0
    last_frame_at: Optional[float] = None
    busy: bool = False
    person_detected: bool = False
    features: Optional[Dict[str, float]] = None
    closed: bool = False
    last_analysis_sent: float = 0.0
    frames_processed: int = 0
    frames_dropped: int = 0
    frame_times: Deque[float] = field(default_factory=lambda: deque(maxlen=60))
    # 마지막으로 보호자 화면에 보낸 상태(바뀐 것만 보내기 위해)
    last_sent: Optional[Dict[str, Any]] = None
    # 관리자 화면의 실시간 보기용: 가장 최근 프레임(JPEG 바이트)과 그 분석 결과. 서버 메모리에 한 장만 두고
    # 어디에도 저장하지 않으며, 연결이 끊기면 세션과 함께 사라진다. 서버 PC의 관리자 페이지에서만 읽는다.
    last_frame: Optional[bytes] = None
    last_keypoints: Optional[Dict[str, Any]] = None
    frame_seq: int = 0
    history: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=LIVE_HISTORY_LEN))

    @property
    def device_id(self) -> int:
        return self.device["id"]

    def fps(self, now: float) -> float:
        recent = [t for t in self.frame_times if now - t <= FPS_WINDOW_SECONDS]
        if len(recent) < 2 or recent[-1] <= recent[0]:
            return 0.0
        return round((len(recent) - 1) / (recent[-1] - recent[0]), 1)


class DeviceHub:
    """접속 중인 기기 세션과 프레임 분석을 맡는다.

    events(EventService)와 서로를 참조하므로, 만든 뒤 main.py에서 hub.events = events로 잇는다.
    """

    def __init__(self, settings: Settings, accounts: Any, guardian_hub: Any, pose: PoseEstimator,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.accounts = accounts
        self.guardian_hub = guardian_hub
        self.pose = pose
        self.rf_model = None
        if settings.detection_backend == "random_forest" and pose.name == "yolo":
            import joblib
            bundle = joblib.load(settings.rf_model_path)
            self.rf_model = bundle["model"] if isinstance(bundle, dict) else bundle
            # 동시 기기 처리 시 프레임마다 스레드를 과도하게 만들지 않는다.
            if hasattr(self.rf_model, "n_jobs"):
                self.rf_model.n_jobs = 1
            if getattr(self.rf_model, "n_features_in_", 42) != 42 or 1 not in self.rf_model.classes_:
                raise ValueError("Random Forest 모델은 낙상 클래스와 42개 특징이 필요합니다")
        elif settings.detection_backend not in ("random_forest", "fsm"):
            raise ValueError("알 수 없는 감지 방식: " + settings.detection_backend)
        self.clock = clock
        # 관리자 실시간 보기의 '자세' 표시용 규칙(서 있음·누움 기준). Random Forest 모드에서도 참고로 보여 준다.
        self._posture_rules = FallFSM.from_settings(settings)
        self.events: Any = None
        self.sessions: Dict[int, DeviceSession] = {}
        self.background = BackgroundTasks("devices")
        self._monitor_task: Optional[asyncio.Task] = None
        self._pose_error_logged_at = 0.0

    # --- 시작·종료 ---------------------------------------------------------

    def start(self) -> None:
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        await self.background.cancel_all()

    # --- 연결 --------------------------------------------------------------

    async def connect(self, device: Dict[str, Any], ws: Any) -> DeviceSession:
        """새 연결을 등록한다. 같은 기기의 이전 연결은 닫는다(한 기기에 연결 하나).

        휴대폰 화면을 새로 고치면 이전 연결이 잠시 남아 있을 수 있는데, 둘 다 프레임을 보내면
        한 FSM에 두 영상이 섞이므로 새 연결만 남긴다.
        """
        owner = self.accounts.get_user(device["owner_id"])
        session = DeviceSession(
            device=device,
            ws=ws,
            extractor=FeatureExtractor(),
            fsm=FallFSM.from_settings(self.settings),
            owner_name=owner["name"] if owner else None,
            connected_at=self.clock(),
        )
        if self.rf_model is not None:
            from server.detection.random_forest import RandomForestDetector
            session.fsm = RandomForestDetector(self.rf_model, self.settings.rf_threshold)
        old = self.sessions.get(device["id"])
        self.sessions[device["id"]] = session
        if old is not None:
            old.closed = True
            log.info("기기 %s 새 연결로 교체", device["id"])
            try:
                await old.ws.close(code=4409)
            except Exception:
                pass  # 이미 끊긴 연결
        self.accounts.mark_device_seen(device["id"])
        log.info("기기 접속: device=%s (%s)", device["id"], device["kind"])
        await self.broadcast_status(device["id"])
        return session

    async def disconnect(self, session: DeviceSession) -> None:
        session.closed = True
        if self.sessions.get(session.device_id) is not session:
            return  # 새 연결로 교체된 이전 세션
        del self.sessions[session.device_id]
        self.accounts.mark_device_seen(session.device_id)
        log.info("기기 연결 끊김: device=%s (처리 %d, 버림 %d)",
                 session.device_id, session.frames_processed, session.frames_dropped)
        if self.events is not None:
            # 음성 확인을 기다리던 중이면 확인할 수 없으므로 경보로 넘어간다.
            self.events.on_device_disconnected(session.device_id)
        await self.broadcast_status(session.device_id)

    async def remove_device(self, device_id: int) -> None:
        """삭제된 기기의 연결을 끊는다. 기기 키가 유출되었을 때 기기를 지우면 바로 접속이 끊겨야 한다.

        세션을 먼저 빼므로 수신 루프가 끝나며 부르는 disconnect()는 아무것도 하지 않는다.
        4401(인증 실패)로 닫아 클라이언트가 같은 키로 다시 접속하지 않게 한다.
        """
        session = self.sessions.pop(device_id, None)
        if session is None:
            return
        session.closed = True
        log.info("기기 %s 삭제: 연결을 끊음", device_id)
        await self._close_socket(session, 4401, "device deleted")

    async def close_device(self, device_id: int, code: int, reason: str) -> None:
        """서버 쪽에서 기기 연결을 끊고 보통의 연결 끊김처럼 처리한다(주인 계정 정지).

        remove_device와 달리 기기는 남아 있으므로 보호자 화면에 '연결 끊김'을 알리고,
        음성 확인 중이었으면 확인 불가로 넘어간다(disconnect). 수신 루프가 끝나며 부르는
        disconnect()는 이미 처리된 세션이라 아무것도 하지 않는다.
        """
        session = self.sessions.get(device_id)
        if session is None:
            return
        session.closed = True
        await self._close_socket(session, code, reason)
        await self.disconnect(session)

    async def _close_socket(self, session: DeviceSession, code: int, reason: str) -> None:
        try:
            await asyncio.wait_for(session.ws.close(code=code, reason=reason), timeout=DEVICE_SEND_TIMEOUT)
        except Exception:
            pass  # 이미 끊긴 연결

    def is_online(self, device_id: int) -> bool:
        return device_id in self.sessions

    async def send(self, device_id: int, message: Dict[str, Any]) -> bool:
        """기기에 JSON 메시지를 보낸다. 접속해 있지 않거나 보내지 못하면 False."""
        session = self.sessions.get(device_id)
        if session is None or session.closed:
            return False
        try:
            await asyncio.wait_for(session.ws.send_json(message), timeout=DEVICE_SEND_TIMEOUT)
            return True
        except Exception as e:
            log.info("기기 %s 전송 실패: %s", device_id, type(e).__name__)
            return False

    def reset_detection(self, device_id: int) -> None:
        """이벤트가 끝났을 때 감시 상태로 되돌린다. 이전 영상 기록이 다음 판정에 섞이지 않게 특징 기록도 지운다."""
        session = self.sessions.get(device_id)
        if session is None:
            return
        session.fsm.reset(self.clock())
        session.extractor.reset()

    # --- 프레임 처리 ----------------------------------------------------------

    def submit_frame(self, session: DeviceSession, frame_bytes: bytes) -> bool:
        """WebSocket 수신 루프에서 부른다. 분석은 따로 돌리고 바로 돌아와 다음 프레임을 받는다.

        수신 루프가 분석을 기다리면 프레임이 소켓에 쌓여 지연이 커지므로, 분석 중이면 버린다.
        """
        if session.closed:
            return False
        if session.busy:
            session.frames_dropped += 1
            return False
        self.background.spawn(self.handle_frame(session, frame_bytes, received_at=self.clock()))
        return True

    async def handle_frame(self, session: DeviceSession, frame_bytes: bytes,
                           received_at: Optional[float] = None) -> bool:
        """프레임 한 장을 분석한다. 분석 중이라 버렸으면 False."""
        if session.busy or session.closed:
            session.frames_dropped += 1
            return False
        session.busy = True
        t = received_at if received_at is not None else self.clock()
        try:
            try:
                keypoints = await asyncio.to_thread(self.pose.estimate, frame_bytes)
            except Exception:
                # 모델 오류가 프레임마다 반복되면 로그가 넘치므로 10초에 한 번만 남긴다.
                if t - self._pose_error_logged_at >= 10.0:
                    self._pose_error_logged_at = t
                    log.exception("기기 %s 자세 추정 실패", session.device_id)
                return True
            if session.closed:
                return True  # 분석하는 사이 연결이 바뀌었다

            before = self.status_key(session)
            session.last_frame_at = t
            session.frame_times.append(t)
            session.frames_processed += 1
            session.person_detected = keypoints is not None
            features = session.extractor.extract(keypoints, t) if keypoints is not None else None
            session.features = features
            session.last_frame = frame_bytes
            session.last_keypoints = keypoints_view(keypoints)
            session.frame_seq += 1

            previous_state = session.fsm.state
            if self.rf_model is not None:
                state = session.fsm.update(keypoints, t)
                if session.device.get("kind") == "phone" and t - session.last_analysis_sent >= 0.3:
                    session.last_analysis_sent = t
                    points = []
                    bbox = None
                    if keypoints is not None and keypoints.image_width and keypoints.image_height:
                        points = [[float(x/keypoints.image_width), float(y/keypoints.image_height)] for x,y in
                                  (keypoints.left_shoulder, keypoints.right_shoulder, keypoints.left_hip, keypoints.right_hip)]
                        x0, y0, x1, y1 = keypoints.bbox
                        bbox = [x0/keypoints.image_width, y0/keypoints.image_height, x1/keypoints.image_width, y1/keypoints.image_height]
                    self.background.spawn(self.send(session.device_id, {"type":"analysis", "probability":session.fsm.probability,
                                                                     "points":points, "bbox":bbox, "state":state.value, "threshold":self.settings.rf_threshold}))
            else:
                state = session.fsm.update(features, t)
            if state != previous_state:
                log.info("기기 %s 감지 상태: %s -> %s", session.device_id, previous_state.value, state.value)
            session.history.append({"t": round(t, 3), "state": state.value, "features": features,
                                    "probability": getattr(session.fsm, "probability", None)})
            if state == FallState.SUSPECT and previous_state != FallState.SUSPECT and self.events is not None:
                await self.events.on_suspect(session, session.fsm.suspect_features or features or {})

            # 감지 상태·카메라 상태가 바뀌었으면 바로 알린다(사람 감지·fps는 점검 루프가 1초마다).
            if self.status_key(session) != before:
                self.background.spawn(self.broadcast_status(session.device_id))
            return True
        finally:
            session.busy = False

    # --- 상태 -------------------------------------------------------------

    def _camera_ok(self, session: Optional[DeviceSession], now: float) -> bool:
        if session is None or session.last_frame_at is None:
            return False
        return now - session.last_frame_at <= self.settings.camera_stale_seconds

    def _state(self, device_id: int, session: Optional[DeviceSession]) -> str:
        """진행 중인 이벤트가 있으면 그 상태(SUSPECT/ALARM), 없으면 FSM 상태(NORMAL/FALLING)."""
        if self.events is not None:
            event_state = self.events.state_for_device(device_id)
            if event_state is not None:
                return event_state
        if session is None:
            return FallState.NORMAL.value
        return session.fsm.state.value

    def status_key(self, session: DeviceSession) -> tuple:
        """바로 알려야 하는 항목만 묶은 값. 이것이 바뀌면 즉시 보낸다."""
        return (session.device_id in self.sessions and not session.closed,
                self._camera_ok(session, self.clock()),
                self._state(session.device_id, session))

    def status(self, device_id: int, device: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """보호자 화면·현장 화면에 보여 줄 기기 상태. 없는 기기면 None."""
        session = self.sessions.get(device_id)
        now = self.clock()
        if session is not None:
            device, owner_name = session.device, session.owner_name
        else:
            device = device or self.accounts.get_device(device_id)
            if device is None:
                return None
            owner = self.accounts.get_user(device["owner_id"])
            owner_name = owner["name"] if owner else None
        camera_ok = self._camera_ok(session, now)
        return {
            "device_id": device["id"],
            "name": device["name"],
            "kind": device["kind"],
            "owner_id": device["owner_id"],
            "owner_name": owner_name,
            "online": session is not None,
            "camera_ok": camera_ok,
            # 프레임이 끊겼으면 마지막 분석 결과는 더 이상 현재 모습이 아니다.
            "person_detected": bool(camera_ok and session.person_detected),
            "fps": session.fps(now) if camera_ok else 0.0,
            "state": self._state(device["id"], session),
        }

    @property
    def detector_name(self) -> str:
        """실제로 쓰는 감지 방식. Random Forest는 YOLO 자세 추정일 때만 쓴다(가짜 추정기 시험은 FSM)."""
        return "random_forest" if self.rf_model is not None else "fsm"

    def live_view(self, device_id: int) -> Optional[Dict[str, Any]]:
        """관리자 화면의 실시간 보기: 가장 최근 프레임과 그 장면의 관절·특징값·FSM 상태.

        접속해 있지 않거나 아직 프레임을 받지 못했으면 None. 'frame'은 JPEG 바이트 그대로이며 호출 측이 인코딩한다.
        """
        session = self.sessions.get(device_id)
        if session is None or session.closed or session.last_frame is None or session.last_frame_at is None:
            return None
        now = self.clock()
        fsm, features = session.fsm, session.features
        rules = self._posture_rules
        if features is None:
            posture = None
        elif rules.is_lying(features):
            posture = "lying"
        elif rules.is_standing(features):
            posture = "standing"
        else:
            posture = "between"
        # 규칙 기반 FSM만 상태 유지 시간·누운 시간을 잰다. Random Forest 감지기에는 없는 값이다.
        state_since = getattr(fsm, "state_since", 0.0)
        lying_since = getattr(fsm, "lying_since", None)
        return {
            "device_id": device_id,
            "seq": session.frame_seq,
            "frame": session.last_frame,
            "frame_age": round(now - session.last_frame_at, 2),
            "fps": session.fps(now),
            "camera_ok": self._camera_ok(session, now),
            "person_detected": session.person_detected,
            "keypoints": session.last_keypoints,
            "features": features,
            "posture": posture,
            "detector": self.detector_name,
            # Random Forest의 최근 분석 점수(0~1). 기록이 모이기 전이거나 FSM 모드면 None
            "probability": getattr(fsm, "probability", None),
            "fsm": {
                "state": fsm.state.value,
                # state_since는 첫 전이 전에는 0이라 그때는 알 수 없다
                "state_for": round(now - state_since, 2) if state_since > 0 else None,
                "lying_for": round(now - lying_since, 2) if lying_since is not None else None,
                "suspect_features": fsm.suspect_features,
            },
            "event_state": self.events.state_for_device(device_id) if self.events is not None else None,
            "state": self._state(device_id, session),
            "frames_processed": session.frames_processed,
            "frames_dropped": session.frames_dropped,
            "history": list(session.history),
        }

    def statuses_for(self, owner_ids: List[int]) -> List[Dict[str, Any]]:
        result = []
        for device in self.accounts.devices_of(owner_ids):
            status = self.status(device["id"], device)
            if status is not None:
                result.append(status)
        return result

    async def broadcast_status(self, device_id: int) -> None:
        """기기 주인과 수락된 보호자의 보호자 화면에 기기 상태를 보낸다."""
        status = self.status(device_id)
        if status is None:
            return  # 삭제된 기기
        session = self.sessions.get(device_id)
        if session is not None:
            session.last_sent = status
        await self.guardian_hub.send_to_users(watcher_ids(self.accounts, status["owner_id"]),
                                              {"type": "device", "device": status})

    async def check_all(self) -> None:
        """점검 루프 한 번: 바뀐 기기 상태만 보낸다(카메라 응답 없음, 사람 감지, fps)."""
        for device_id, session in list(self.sessions.items()):
            status = self.status(device_id)
            if status is not None and status != session.last_sent:
                await self.broadcast_status(device_id)

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_CHECK_INTERVAL)
            try:
                await self.check_all()
            except Exception:
                log.exception("기기 상태 점검 오류")
