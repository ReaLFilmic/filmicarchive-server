# -*- coding: utf-8 -*-
"""biff.py — 부산국제영화제 상영시간표를 받아 CSV 로 돌려줍니다.

   biff.kr 의 "상영관별 시간표" 를 극장마다 한 번씩 받습니다.
   한 페이지에 그 극장의 전 기간 회차가 들어 있어, 날짜별로 나눠 담습니다.

   HTML 구조
     <div class="sch_li_tit date">10.06</div>        ← 날짜
     <div class="sch_it sch_it4">
       <span class="code en" data-scode="001">001</span>
       <div class="film_tit">
         <p class="time en">18:00</p>
         <span class="film_tit_kor">낮과 밤은 서로에게</span>
       </div>
     </div>

   상영관 이름은 페이지 안 선택 목록에서 가져옵니다.
"""

import csv
import io
import re
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, Response, jsonify, request

bp = Blueprint("biff", __name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BASE = "https://www.biff.kr/kor/html/schedule/theater.asp"
REF = "https://www.biff.kr/"
KST = timezone(timedelta(hours=9))
TIMEOUT = 12
VERSION = "biff-1"

# 극장 코드. biff.kr 의 상영관별 시간표에서 뽑았습니다.
# s_code 가 있으면 그 상영관만, 없으면 기본 상영관이 나옵니다.
# 영화의전당은 상영관이 다섯이라 따로 받아야 전부 모입니다.
THEATERS = [
    {"code": "BT", "idx": "110", "sc": "BT", "name": "영화의전당",
     "hall": "루프씨어터"},
    {"code": "BT", "idx": "110", "sc": "BH", "name": "영화의전당",
     "hall": "하늘연극장"},
    {"code": "BT", "idx": "110", "sc": "B1", "name": "영화의전당",
     "hall": "중극장"},
    {"code": "BT", "idx": "110", "sc": "B2", "name": "영화의전당",
     "hall": "소극장"},
    {"code": "BT", "idx": "110", "sc": "B3", "name": "영화의전당",
     "hall": "시네마테크"},
    {"code": "C1",  "idx": "111", "name": "CGV 센텀시티"},
    {"code": "L2",  "idx": "112", "name": "롯데시네마 센텀시티"},
    {"code": "KT",  "idx": "113", "name": "영화진흥위원회 표준시사실"},
    {"code": "SC",  "idx": "115", "name": "신세계백화점 센텀"},
    {"code": "SH",  "idx": "116", "name": "소향씨어터"},
    {"code": "BCM", "idx": "117", "name": "시청자미디어센터"},
    {"code": "DM",  "idx": "118", "name": "동서대-경남정보대 센텀캠퍼스"},
]

# 한 덩어리 = 날짜 하나 + 그 아래 회차들
BLOCK = re.compile(
    r'<div class="sch_li_tit date">\s*(\d{1,2})\.(\d{1,2})\s*</div>'
    r'([\s\S]*?)(?=<div class="sch_li_tit date">|$)')

# 회차 하나
ITEM = re.compile(
    r'<div class="sch_it[^"]*">([\s\S]*?)</div>\s*</div>')

TIME = re.compile(r'class="time en">\s*(\d{1,2}:\d{2})')
KOR = re.compile(r'class="film_tit_kor">\s*([^<]+)')
ENG = re.compile(r'class="film_tit_eng[^"]*">\s*([^<]+)')
GV = re.compile(r'class="gv_\w+"[^>]*>\s*([^<]{1,20})')

# 상영관 이름 — 페이지 안 선택 목록
HALL = re.compile(r'sc_idx=(\d+)[^>]*>\s*(?:<[^>]*>\s*)*([^<]{2,40})')

_lock = threading.Lock()
_cache = {}
_TTL = 6 * 60 * 60          # 영화제 시간표는 자주 안 바뀝니다


def _fetch(t):
    q = {"s_theater": t["code"], "s_idx": t["idx"]}
    if t.get("sc"):
        q["s_code"] = t["sc"]
    r = requests.get(BASE, params=q,
                     headers={"User-Agent": UA, "Referer": REF,
                              "Accept-Language": "ko-KR,ko;q=0.9"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text

    year = datetime.now(KST).year
    rows, seen = [], set()

    for mm, dd, body in BLOCK.findall(html):
        date = "%02d-%02d" % (int(mm), int(dd))
        for chunk in ITEM.findall(body):
            tm = TIME.search(chunk)
            ko = KOR.search(chunk)
            if not tm or not ko:
                continue
            title = re.sub(r"\s+", " ", ko.group(1)).strip()
            if not title:
                continue
            start = tm.group(1)
            if len(start) == 4:
                start = "0" + start

            key = (title, t.get("hall") or t["name"], date, start)
            if key in seen:
                continue
            seen.add(key)

            # 영화제는 상영시간이 화면에 없는 경우가 많아 기본값을 둡니다.
            venue = t["name"] + (" " + t["hall"] if t.get("hall") else "")
            rows.append([title, venue, date, start, 110, 3, "", ""])
    return rows


def _get(t):
    with _lock:
        hit = _cache.get(t["code"] + (t.get("sc") or ""))
        if hit and (_time.time() - hit[0]) < _TTL:
            return hit[1]
    rows = _fetch(t)
    with _lock:
        _cache[t["code"] + (t.get("sc") or "")] = (_time.time(), rows)
    return rows


@bp.route("/api/biff")
def biff():
    only = [x.strip() for x in (request.args.get("only") or "").split(",") if x.strip()]
    picked = [t for t in THEATERS
              if not only or t["code"] in only
              or (t["code"] + (t.get("sc") or "")) in only]

    jobs = list(picked)
    all_rows, failed = [], []
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not jobs:
                    return
                t = jobs.pop(0)
            try:
                got = _get(t)
                with qlock:
                    all_rows.extend(got)
            except Exception as e:
                with qlock:
                    failed.append("%s — %s: %s"
                                  % (t["name"], type(e).__name__, str(e)[:120]))
            _time.sleep(0.2)

    ths = [threading.Thread(target=worker) for _ in range(4)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()

    if not all_rows:
        return jsonify({"ok": False, "failed": failed,
                        "error": "회차를 하나도 받지 못했습니다."}), 502

    all_rows.sort(key=lambda r: (r[2], r[3], r[1]))

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["title", "venue", "date", "start", "runtime", "priority",
                "seatsLeft", "seatsTotal"])
    w.writerows(all_rows)
    text = buf.getvalue().rstrip("\n")

    if request.args.get("format") == "json":
        by, days = {}, {}
        for r in all_rows:
            by[r[1]] = by.get(r[1], 0) + 1
            days[r[2]] = days.get(r[2], 0) + 1
        return jsonify({"ok": True, "version": VERSION, "count": len(all_rows),
                        "byTheater": by, "byDate": days,
                        "failed": failed, "csv": text})
    return Response(text, mimetype="text/csv; charset=utf-8")
