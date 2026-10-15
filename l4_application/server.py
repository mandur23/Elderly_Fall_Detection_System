"""L4 로컬 API와 현장 화면 서버. 외부 접근을 막기 위해 127.0.0.1에만 바인딩한다.

실행: python -m l4_application.server

API
  GET  /api/state           현재 상태, 진행 중 이벤트, 장치 문제
  GET  /api/events?limit=N  최근 이벤트 기록
  POST /api/clear           화면 '괜찮아요' 버튼
  POST /api/event           L3 낙상 의심 보고 -> {"event_id": ...}
  POST /api/heartbeat       L3 상태 보고 -> {"state": ..., "event_id": ...}
  GET  /api/health          외부 감시용 상태 요약 (장치 문제 있으면 503)

/api/event, /api/heartbeat는 INTERNAL_API_TOKEN이 설정되어 있으면 X-Internal-Token 헤더를 검사한다.
"""

import hmac
import logging

from flask import Flask, abort, jsonify, render_template, request

import config
from l4_application.app_service import ApplicationService
from l4_application.audio import PromptPlayer, Siren, WavSiren
from l4_application.database import Database
from l4_application.notifier import create_notifier
from l4_application.voice_checker import SimulatedVoiceChecker, VoskVoiceChecker


def build_service(voice_backend: str = config.VOICE_BACKEND, db_path=config.DB_PATH) -> ApplicationService:
    keywords = (config.KEYWORDS_NEGATED_SAFE, config.KEYWORDS_HELP, config.KEYWORDS_SAFE)
    if voice_backend == "simulated":
        voice = SimulatedVoiceChecker(keywords)
    else:
        prompt = PromptPlayer(config.AUDIO_DIR / "ask_status.wav", config.AUDIO_DIR / "chime.wav",
                              config.PROMPT_TEXT)
        voice = VoskVoiceChecker(keywords, config.VOSK_MODEL_PATH, prompt, config.VOICE_SAMPLE_RATE,
                                 config.MIC_DEVICE, config.VOICE_GRAMMAR, config.VOICE_GRAMMAR_MIN_CONF)
    siren = WavSiren(config.AUDIO_DIR / "siren.wav") if config.LOCAL_SIREN and voice_backend != "simulated" else Siren()
    return ApplicationService(
        Database(db_path),
        voice,
        create_notifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, config.NOTIFY_TIMEOUT_SECONDS),
        voice_attempts=config.VOICE_MAX_ATTEMPTS,
        listen_seconds=config.VOICE_LISTEN_SECONDS,
        retry_delays=config.NOTIFY_RETRY_DELAYS,
        retry_check_seconds=config.NOTIFY_RETRY_CHECK_SECONDS,
        heartbeat_stale_seconds=config.HEARTBEAT_STALE_SECONDS,
        store_voice_text=config.STORE_VOICE_TEXT,
        retention_days=config.EVENT_RETENTION_DAYS,
        siren=siren,
        device_alert_after=config.DEVICE_ALERT_AFTER_SECONDS,
        daily_report_time=config.DAILY_REPORT_TIME,
        location=config.DEVICE_NAME,
        voice_check_interval=config.MIC_CHECK_INTERVAL_SECONDS,
    )


def create_app(service: ApplicationService, internal_token: str = "") -> Flask:
    app = Flask(__name__)

    def require_internal_token():
        if internal_token and not hmac.compare_digest(request.headers.get("X-Internal-Token", ""),
                                                      internal_token):
            abort(403)

    @app.get("/")
    def index():
        return render_template("kiosk.html", prompt_text=config.PROMPT_TEXT)

    @app.get("/api/state")
    def get_state():
        return jsonify(service.get_status())

    @app.get("/api/events")
    def get_events():
        limit = request.args.get("limit", default=20, type=int)
        return jsonify(service.db.recent_events(limit))

    @app.post("/api/clear")
    def post_clear():
        cleared = service.clear_by_button()
        return jsonify({"cleared": cleared, "state": service.get_status()["state"]})

    @app.post("/api/event")
    def post_event():
        require_internal_token()
        data = request.get_json(silent=True) or {}
        if data.get("event_type") != "FALL_SUSPECT":
            return jsonify({"error": "unsupported event_type"}), 400
        return jsonify({"event_id": service.report_suspect(data.get("features"))})

    @app.post("/api/heartbeat")
    def post_heartbeat():
        require_internal_token()
        return jsonify(service.heartbeat(request.get_json(silent=True) or {}))

    @app.get("/api/health")
    def get_health():
        status = service.get_status()
        body = {"state": status["state"], "problems": status["problems"]}
        return jsonify(body), (503 if status["problems"] else 200)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s][L4] %(message)s")
    service = build_service()
    service.start()
    app = create_app(service, config.INTERNAL_API_TOKEN)
    try:
        from waitress import serve
    except ImportError:
        logging.warning("waitress가 없어 Flask 개발 서버로 실행합니다 (운영 환경에서는 waitress 설치).")
        app.run(host=config.SERVER_HOST, port=config.SERVER_PORT, threaded=True)
        return
    serve(app, host=config.SERVER_HOST, port=config.SERVER_PORT, threads=8)


if __name__ == "__main__":
    main()
