# keepalive.py — Render 무료 플랜이 15분 무요청 후 서버를 끄는 것을 막습니다
#
# 원리: 서버가 켜져 있는 동안 10분마다 "자기 자신의 공개 주소"로 요청을 보냅니다.
#       Render 는 바깥에서 들어온 요청만 '요청'으로 세므로, 반드시 공개 주소(https)로 보내야 합니다.
#       (localhost 로 보내면 소용없음)
#
# 한계: 이미 꺼진 서버를 스스로 깨울 수는 없습니다.
#       그래서 UptimeRobot / cron-job.org 같은 외부 핑을 함께 걸어두면 가장 확실합니다.
#       → 주소: https://내서비스.onrender.com/healthz , 간격: 5~10분
#
# 켜는 법: Render 는 RENDER_EXTERNAL_URL 을 자동으로 넣어줍니다. 따로 설정할 게 없습니다.
#          직접 정하려면 KEEPALIVE_URL 환경변수를 쓰세요. KEEPALIVE=0 이면 끕니다.

import os
import threading
import time

import requests

INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", 10 * 60))   # 초. 15분보다 짧아야 합니다
_started = False


def _loop(url):
    # 서버가 막 켜진 직후에는 아직 요청을 받을 준비가 안 됐을 수 있어 잠깐 기다립니다
    time.sleep(60)
    while True:
        try:
            requests.get(url, timeout=15, headers={"User-Agent": "keepalive/1.0"})
        except Exception as e:
            # 실패해도 서버가 죽으면 안 됩니다. 다음 주기에 다시 시도합니다.
            print("[keepalive] 실패:", e, flush=True)
        time.sleep(INTERVAL)


def start():
    """공개 주소가 있을 때만 백그라운드 스레드를 한 번 시작합니다."""
    global _started
    if _started or os.environ.get("KEEPALIVE") == "0":
        return
    base = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        return                      # 내 컴퓨터에서 돌릴 때는 자동으로 꺼집니다
    _started = True
    url = base.rstrip("/") + "/healthz"
    threading.Thread(target=_loop, args=(url,), daemon=True, name="keepalive").start()
    print("[keepalive] 시작:", url, "every", INTERVAL, "s", flush=True)
