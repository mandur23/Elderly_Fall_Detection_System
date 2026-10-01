"""장비 없이 전체 흐름을 확인하는 데모.

L4 서버(가상 음성)와 L3 감지 프로세스(가상 사람)를 한 프로세스에서 띄운다.
L3와 L4는 실제 배포와 똑같이 로컬 HTTP API로 통신한다.

    python run_demo.py          # 대화형 메뉴, 브라우저에서 http://127.0.0.1:5000
    python run_demo.py --auto   # 주요 시나리오를 자동으로 실행하고 결과를 출력
"""

import argparse
import logging
import threading
import time
import webbrowser

import config
from l3_perception.fall_detector import FallDetectionPipeline
from l4_application.server import build_service, create_app

DEMO_DB = config.DATA_DIR / "demo_events.db"

MENU = """
============================================================
 낙상 감지 데모  (현장 화면: http://{host}:{port})
------------------------------------------------------------
 [가상 사람]  1 서 있기   2 넘어짐   3 천천히 앉기
              4 비틀거렸다 일어섬   5 화면 밖으로 나감
 [가상 음성]  6 "괜찮아요"   7 "도와주세요"   8 "안 괜찮아요"
              9 마이크 오류
 [화면]       c '괜찮아요' 버튼 누르기
 [기록]       e 최근 기록 보기
              0 종료
============================================================"""


def start_system(listen_seconds: float):
    service = build_service(voice_backend="simulated", db_path=DEMO_DB)
    service.listen_seconds = listen_seconds
    service.start()
    app = create_app(service, config.INTERNAL_API_TOKEN)
    threading.Thread(
        target=lambda: app.run(host=config.SERVER_HOST, port=config.SERVER_PORT, threaded=True,
                               use_reloader=False),
        daemon=True,
    ).start()

    pipeline = FallDetectionPipeline(model="mock", camera=None)
    threading.Thread(target=pipeline.run, daemon=True).start()
    return service, pipeline


def print_events(service, n=5):
    for ev in service.db.recent_events(n):
        print(f"  #{ev['id']} {ev['started_at'][11:19]} 결과={ev['result']} 음성={ev['voice_result']} "
              f"알림={ev['notify_state']} 해제={ev['cleared_by']}")


def interactive(service, pipeline):
    print(MENU.format(host=config.SERVER_HOST, port=config.SERVER_PORT))
    person = pipeline.detector
    actions = {
        "1": lambda: person.set_mode("standing"),
        "2": lambda: person.set_mode("fall"),
        "3": lambda: person.set_mode("sit"),
        "4": lambda: person.set_mode("stumble"),
        "5": lambda: person.set_mode("absent"),
        "6": lambda: service.voice.inject("괜찮아요"),
        "7": lambda: service.voice.inject("도와주세요"),
        "8": lambda: service.voice.inject("안 괜찮아요"),
        "9": lambda: service.voice.inject("DEVICE_FAIL"),
        "c": lambda: print("  해제됨" if service.clear_by_button() else "  해제할 이벤트 없음"),
        "e": lambda: print_events(service),
    }
    while True:
        try:
            cmd = input("\n선택> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd == "0":
            break
        action = actions.get(cmd)
        if action is None:
            print("  메뉴에 있는 값을 입력하세요.")
            continue
        action()
        print(f"  현재 상태: {service.get_status()['state']}")


def wait_state(service, expected, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service.state == expected:
            return True
        time.sleep(0.1)
    return False


def auto(service, pipeline):
    """주요 시나리오를 차례로 실행한다. 각 시나리오 사이에 감시 재개를 기다린다."""
    person = pipeline.detector
    results = []

    def scenario(name, steps, expected_state, expected_result=None):
        person.set_mode("standing")
        time.sleep(2.5)  # 이전 이벤트 종료 후 L3 감시 재개 대기
        before = service.db.recent_events(1)
        before_id = before[0]["id"] if before else 0
        for step in steps:
            step()
        ok = wait_state(service, expected_state)
        latest = service.db.recent_events(1)
        result = latest[0]["result"] if latest and latest[0]["id"] > before_id else None
        ok = ok and result == expected_result  # None이면 이벤트가 생기지 않아야 한다
        results.append((name, ok, service.state, result))
        if service.state != "NORMAL":
            service.clear_by_button()

    def after_suspect(fn):
        def step():
            wait_state(service, "SUSPECT")
            fn()
        return step

    scenario("천천히 앉기 -> 경보 없음",
             [lambda: person.set_mode("sit"), lambda: time.sleep(4)], "NORMAL", None)
    scenario("비틀거렸다 일어섬 -> 경보 없음",
             [lambda: person.set_mode("stumble"), lambda: time.sleep(4)], "NORMAL", None)
    scenario("넘어짐 + '괜찮아요' -> 해제",
             [lambda: person.set_mode("fall"), after_suspect(lambda: service.voice.inject("괜찮아요")),
              lambda: time.sleep(1)], "NORMAL", "CLEARED_VOICE")
    scenario("넘어짐 + '안 괜찮아요' -> 경보",
             [lambda: person.set_mode("fall"), after_suspect(lambda: service.voice.inject("안 괜찮아요"))],
             "ALARM", "ALERT_HELP")
    scenario("넘어짐 + 무응답 -> 경보",
             [lambda: person.set_mode("fall")], "ALARM", "ALERT_NO_REPLY")
    scenario("넘어짐 + 화면 버튼 -> 해제",
             [lambda: person.set_mode("fall"), after_suspect(service.clear_by_button),
              lambda: time.sleep(1)], "NORMAL", "CLEARED_BUTTON")
    scenario("해제 후 다시 넘어짐 -> 재감지",
             [lambda: person.set_mode("fall"), after_suspect(lambda: service.voice.inject("괜찮아요"))],
             "NORMAL", "CLEARED_VOICE")

    print("\n=== 자동 시나리오 결과 ===")
    for name, ok, state, result in results:
        print(f"  [{'통과' if ok else '실패'}] {name}  (상태={state}, 기록={result})")
    return all(ok for _, ok, _, _ in results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="시나리오 자동 실행")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING if args.auto else logging.INFO,
                        format="[%(asctime)s][%(name)s] %(message)s")

    if args.auto:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # 자동 실행에서는 무응답 시나리오가 빨리 끝나도록 듣기 시간을 줄인다.
    service, pipeline = start_system(listen_seconds=1.5 if args.auto else 8.0)
    if not args.auto and not args.no_browser:
        webbrowser.open(f"http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    try:
        if args.auto:
            raise SystemExit(0 if auto(service, pipeline) else 1)
        interactive(service, pipeline)
    finally:
        pipeline.running = False
        service.stop()


if __name__ == "__main__":
    main()
