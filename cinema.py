# -*- coding: utf-8 -*-
"""cinema.py — 영화의전당 상영시간표를 받아 CSV 로 돌려줍니다.

   원래 Vercel 서버 함수(api/cinema.js)로 만든 것을 Flask 로 옮긴 것입니다.
   파싱 규칙은 그대로 두었고, 달라진 점은 세 가지입니다.

     ① Referer 를 붙입니다. 없으면 영화의전당이 "잘 못된 경로 입니다" 를 돌려줍니다.
     ② 캐시를 직접 들고 있습니다. Vercel 은 응답 헤더로 캐시를 맡길 수 있었지만
        Render 는 그런 앞단이 없어서, 30분간 메모리에 담아 둡니다.
     ③ 동시 요청은 3건으로 제한합니다(원본과 같음).
"""

import csv
import io
import re
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, Response, jsonify, request

bp = Blueprint("cinema", __name__)

BASE = "https://www.dureraum.org/bcc/mcontents/caleList.do"
REFERER = "https://www.dureraum.org/bcc/main/main.do?rbsIdx=1"
THEATER = "영화의전당"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_DAYS = 21
KST = timezone(timedelta(hours=9))

# 한 회차를 뽑는 규칙. 원본 js 의 정규식을 그대로 옮겼습니다.
ROW = re.compile(
    r"([^\r\n<>|]{1,80}?)\s*\|\s*(\d*)\s*min"
    r"[\s\S]*?<li class=\"place\">\s*([^<]+?)\s*</li>"
    r"[\s\S]*?<li class=\"time\">([\s\S]*?)</li>"
)
GRADE = re.compile(r"^(?:전체|G|청불|\d{1,2}세)\s*")
CLOCK = re.compile(r"(\d{1,2}):(\d{2})")

# ── 캐시 ────────────────────────────────────────────
# Render 무료 플랜은 15분 쉬면 잠들어 이 캐시가 사라집니다.
# 깨어난 뒤 첫 요청은 영화의전당에 다시 가게 됩니다.
_lock = threading.Lock()
_cache = {}          # 키 → (담은 시각, CSV 글자)
_TTL = 30 * 60


def _fetch_day(day):
    """하루치를 받아 [제목, 극장, 날짜, 시각, 상영시간, 우선순위] 목록으로."""
    r = requests.get(
        BASE,
        params={"rbsIdx": 37, "searchDate": day},
        headers={"User-Agent": UA, "Referer": REFERER},
        timeout=25,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text

    rows, seen = [], set()
    for m in ROW.finditer(html):
        title = GRADE.sub("", m.group(1).strip()).strip()
        if not title:
            continue
        run = int(m.group(2)) if m.group(2) else 120
        hall = m.group(3).strip()
        venue = (THEATER + " " + hall) if hall else THEATER
        for hh, mm in CLOCK.findall(m.group(4)):
            start = "%02d:%s" % (int(hh), mm)
            key = (title, venue, start)
            if key in seen:
                continue
            seen.add(key)
            rows.append([title, venue, day[5:], start, run, 3])
    return rows


@bp.route("/api/cinema")
def cinema():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    days = max(1, min(MAX_DAYS, days))

    start_q = request.args.get("start", "")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", start_q):
        base = datetime.strptime(start_q, "%Y-%m-%d").replace(tzinfo=KST)
    else:
        base = datetime.now(KST)

    day_list = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    ckey = day_list[0] + ":" + str(days)

    with _lock:
        hit = _cache.get(ckey)
        if hit and (_time.time() - hit[0]) < _TTL:
            return _csv_response(hit[1])

    # 하루씩 순서대로 받으면 7일에 오래 걸리고, 한꺼번에 보내면 상대 서버에
    # 몰아치게 됩니다. 3건씩만 동시에 받습니다.
    all_rows, failed = [], []
    queue = list(day_list)
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not queue:
                    return
                day = queue.pop(0)
            try:
                got = _fetch_day(day)
                with qlock:
                    all_rows.extend(got)
            except Exception:
                with qlock:
                    failed.append(day)
            _time.sleep(0.25)

    ths = [threading.Thread(target=worker) for _ in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    if not all_rows:
        return jsonify({
            "ok": False,
            "error": "회차를 하나도 받지 못했습니다. 페이지 구조가 바뀌었을 수 있습니다.",
            "failed": failed,
        }), 502

    all_rows.sort(key=lambda r: r[2] + r[3])

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["title", "venue", "date", "start", "runtime", "priority"])
    w.writerows(all_rows)
    text = buf.getvalue().rstrip("\n")

    with _lock:
        _cache[ckey] = (_time.time(), text)

    if request.args.get("format") == "json":
        return jsonify({"ok": True, "count": len(all_rows),
                        "failed": failed, "csv": text})
    return _csv_response(text)


def _csv_response(text):
    return Response(text, mimetype="text/csv; charset=utf-8")
