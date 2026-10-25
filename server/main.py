"""중앙 서버 앱 조립.

구성 요소를 만들어 app.state에 넣고, 계정·실시간 라우터와 웹페이지를 붙인다.
시작할 때 지난 실행의 이벤트를 정리(recover)하고 기기 상태 점검을 시작하며, 끝날 때 멈춘다.

테스트는 create_app(settings, pose_estimator=가짜, transcriber=가짜)로 무거운 모델 없이 만든다.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from server.auth import Accounts, optional_user
from server.config import Settings
from server.db import Database
from server.detection import PoseEstimator, create_pose_estimator
from server.devices import DeviceHub
from server.events import EventService
from server.notify import GuardianHub, Notifier, PushSender
from server.routes_accounts import router as accounts_router
from server.routes_realtime import router as realtime_router
from server.voice import Transcriber, create_transcriber

log = logging.getLogger("server.main")

# 주소 -> web_dir 안의 파일 이름, 로그인 필요 여부
PAGES = {
    "/": ("index", False),
    "/login": ("login", False),
    "/signup": ("signup", False),
    "/mypage": ("mypage", True),
    "/camera": ("camera", True),
    "/guardian": ("guardian", True),
    "/kiosk": ("kiosk", False),   # 현장 화면은 로그인 대신 기기 키를 쓴다
}
NO_CACHE = {"Cache-Control": "no-cache"}


async def _preload_transcriber(transcriber: Transcriber) -> None:
    """첫 낙상 때 모델 로딩(수십 초)을 기다리지 않도록 미리 올린다. 실패해도 서버는 계속 돈다
    (그때는 음성 확인이 '확인 불가'로 경보 처리된다)."""
    try:
        await asyncio.to_thread(transcriber.load)
    except Exception as e:
        log.error("음성 인식 모델을 미리 올리지 못함: %s (음성 확인은 경보로 처리됩니다)", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state
    state.events.recover()
    state.devices.start()
    # 재시작 전에 보호자에게 전달하지 못한 경보를 다시 보낸다(푸시 재전송은 메모리 작업이라 사라졌다).
    state.events.background.spawn(state.events.resend_undelivered_alarms())
    preload = asyncio.create_task(_preload_transcriber(state.transcriber))
    log.info("서버 시작 (자세 추정: %s, 음성 인식: %s, 웹 푸시: %s)",
             state.settings.pose_backend, state.settings.stt_backend,
             "사용" if state.push.enabled else "꺼짐")
    try:
        yield
    finally:
        preload.cancel()
        await asyncio.gather(preload, return_exceptions=True)
        await state.devices.stop()
        await state.events.aclose()
        await state.notifier.aclose()
        state.pose.close()
        state.db.close()
        log.info("서버 종료")


def _page_handler(web_dir: Path, name: str, login_required: bool):
    async def page(request: Request):
        path = web_dir / f"{name}.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="페이지가 아직 없습니다.")
        if login_required and optional_user(request) is None:
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return FileResponse(path, media_type="text/html", headers=NO_CACHE)
    page.__name__ = f"page_{name}"
    return page


def _add_web_routes(app: FastAPI, web_dir: Path) -> None:
    """웹 역할이 만드는 페이지를 연결한다. 파일이 아직 없으면 404를 돌려준다."""
    for url, (name, login_required) in PAGES.items():
        app.add_api_route(url, _page_handler(web_dir, name, login_required), methods=["GET"],
                          include_in_schema=False)

    async def service_worker():
        path = web_dir / "sw.js"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="서비스 워커가 아직 없습니다.")
        # 서비스 워커는 자기 위치 아래만 다룰 수 있으므로 루트(/sw.js)에서 제공한다.
        # 새 버전이 바로 반영되도록 캐시하지 않는다.
        headers = dict(NO_CACHE, **{"Service-Worker-Allowed": "/"})
        return FileResponse(path, media_type="application/javascript", headers=headers)

    app.add_api_route("/sw.js", service_worker, methods=["GET"], include_in_schema=False)

    static_dir = web_dir / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    else:
        log.info("정적 파일 폴더가 없어 /static을 연결하지 않음: %s", static_dir)


def create_app(settings: Optional[Settings] = None, pose_estimator: Optional[PoseEstimator] = None,
               transcriber: Optional[Transcriber] = None, clock: Callable[[], float] = time.time) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings.db_path)
    accounts = Accounts(db, settings)
    guardian_hub = GuardianHub()
    push = PushSender(settings, accounts)
    notifier = Notifier(db, guardian_hub, push, settings)
    pose = pose_estimator or create_pose_estimator(settings)
    transcriber = transcriber or create_transcriber(settings)
    devices = DeviceHub(settings, accounts, guardian_hub, pose, clock=clock)
    events = EventService(db, accounts, devices, guardian_hub, notifier, transcriber, settings)
    devices.events = events

    app = FastAPI(title="낙상 감지 중앙 서버", lifespan=lifespan)
    state = app.state
    state.settings = settings
    state.db = db
    state.accounts = accounts
    state.guardian_hub = guardian_hub
    state.push = push
    state.notifier = notifier
    state.pose = pose
    state.transcriber = transcriber
    state.devices = devices
    state.events = events

    app.include_router(accounts_router)
    app.include_router(realtime_router)
    _add_web_routes(app, Path(settings.web_dir))
    return app
