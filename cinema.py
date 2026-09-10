# -*- coding: utf-8 -*-
"""cinema.py — 부산 네 극장의 상영시간표를 모아 CSV 로 돌려줍니다.

   극장마다 받아오는 방법이 다릅니다.

     영화의전당     HTML 을 정규식으로 읽습니다. Referer 가 없으면
                    "잘 못된 경로 입니다" 만 돌아옵니다.
     CGV 서면       JSON API. 시작·종료 시각과 잔여좌석까지 들어 있습니다.
     롯데 광복      JSON API. POST 로 MethodName 을 넣는 방식입니다.
     롯데 부산본점  광복과 같은 API, 극장 코드만 다릅니다.

   잔여좌석은 CGV 와 롯데만 제공합니다. 영화의전당은 예매 화면에만 있어
   빈 값으로 둡니다.

   Render 무료 플랜은 CPU 가 약하고 싱가포르에 있어, 네 극장 7일치를 한 번에
   받으면 gunicorn 기본 시간(30초)을 넘깁니다. 그래서 극장·날짜별로 따로
   캐시해 두 번째 요청부터는 거의 즉시 끝나게 했습니다.
"""

import csv
import io
import json
import re
import subprocess
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, Response, jsonify, request

bp = Blueprint("cinema", __name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MAX_DAYS = 21
KST = timezone(timedelta(hours=9))
TIMEOUT = 9          # 한 곳이 늦으면 포기하고 나머지를 살립니다

# ── 영화의전당 ──────────────────────────────────────
DR_URL = "https://www.dureraum.org/bcc/mcontents/caleList.do"
DR_REF = "https://www.dureraum.org/bcc/main/main.do?rbsIdx=1"
DR_ROW = re.compile(
    r"([^\r\n<>|]{1,80}?)\s*\|\s*(\d*)\s*min"
    r"[\s\S]*?<li class=\"place\">\s*([^<]+?)\s*</li>"
    r"[\s\S]*?<li class=\"time\">([\s\S]*?)</li>"
)
DR_GRADE = re.compile(r"^(?:전체|G|청불|\d{1,2}세)\s*")
DR_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")

# ── CGV ────────────────────────────────────────────
CGV_URL = "https://cgv.co.kr/api/v1/booking/searchMovScnInfo"
CGV_REF = "https://cgv.co.kr/cnm/movieBook/cinema"

# ── 롯데시네마 ──────────────────────────────────────
LT_URL = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"
LT_REF = "https://www.lottecinema.co.kr/NLCHS/Ticketing"

# ── 메가박스 ────────────────────────────────────────
MB_URL = "https://www.megabox.co.kr/on/oh/ohc/Brch/schedulePage.do"
MB_REF = "https://www.megabox.co.kr/theater/time"

# 극장 목록. 여기에 한 줄 더하면 그 극장이 화면에 함께 나옵니다.
THEATERS = [
    {"key": "dureraum", "name": "영화의전당", "kind": "dureraum"},
    # CGV 는 서버 IP 를 보고 막는 경우가 있습니다. 막히면 아래 줄의
    # "on" 을 False 로 바꾸면 나머지 세 곳만 받아옵니다.
    {"key": "cgv-seomyeon", "name": "CGV 서면", "kind": "cgv",
     "site": "0005", "on": False},   # IP 차단 확인됨 (2026-09)
    {"key": "lotte-gwangbok", "name": "롯데시네마 광복",
     "kind": "lotte", "cid": "1|0101|2009"},
    {"key": "lotte-busan", "name": "롯데시네마 부산본점",
     "kind": "lotte", "cid": "1|0101|2004"},
    {"key": "megabox-seomyeon", "name": "메가박스 서면대한",
     "kind": "megabox", "brch": "0082", "on": True},
]

# ── 캐시 ────────────────────────────────────────────
# Render 무료 플랜은 15분 쉬면 잠들어 이 캐시가 사라집니다.
_lock = threading.Lock()
_cache = {}              # (극장키, "2026-09-11") → (담은 시각, rows)
_TTL = 3 * 60 * 60       # 잔여좌석이 섞여 있어 6시간은 길다고 보고 3시간


def _hhmm(v):
    """0900 · "10:20" 을 모두 "09:00" 꼴로 만듭니다."""
    s = re.sub(r"\D", "", str(v or ""))
    if len(s) < 3:
        return None
    s = s.zfill(4)
    return "%02d:%s" % (int(s[:2]), s[2:4])


def _mins(a, b):
    """시작·종료로 상영시간을 냅니다. 자정을 넘기면 하루를 더합니다."""
    try:
        h1, m1 = map(int, a.split(":"))
        h2, m2 = map(int, b.split(":"))
        d = (h2 * 60 + m2) - (h1 * 60 + m1)
        return d + 1440 if d <= 0 else d
    except Exception:
        return 120


# ── 극장별 받아오기 ─────────────────────────────────
def _fetch_dureraum(t, day):
    r = requests.get(DR_URL, params={"rbsIdx": 37, "searchDate": day},
                     headers={"User-Agent": UA, "Referer": DR_REF},
                     timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    rows, seen = [], set()
    for m in DR_ROW.finditer(r.text):
        title = DR_GRADE.sub("", m.group(1).strip()).strip()
        if not title:
            continue
        run = int(m.group(2)) if m.group(2) else 120
        hall = m.group(3).strip()
        venue = (t["name"] + " " + hall) if hall else t["name"]
        for hh, mm in DR_CLOCK.findall(m.group(4)):
            start = "%02d:%s" % (int(hh), mm)
            key = (title, venue, start)
            if key in seen:
                continue
            seen.add(key)
            # 영화의전당은 잔여좌석을 시간표에 주지 않습니다
            rows.append([title, venue, day[5:], start, run, 3, "", ""])
    return rows


def _cgv_curl(url):
    """CGV 는 requests 로 부르면 403 입니다. 헤더를 똑같이 맞춰도 막히는데,
       TLS 지문을 보고 파이썬 클라이언트를 걸러내는 것으로 보입니다.
       curl 로는 같은 요청이 200 이라 curl 을 실행해 받아옵니다."""
    try:
        out = subprocess.run(
            ["curl", "-sS", "--compressed", "-m", str(TIMEOUT),
             "-A", UA, "-e", CGV_REF, "-H", "Accept: application/json", url],
            capture_output=True, timeout=TIMEOUT + 4)
    except FileNotFoundError:
        raise RuntimeError("이 서버에 curl 이 없습니다")
    except subprocess.TimeoutExpired:
        raise RuntimeError("curl 시간 초과 — 서버에서 CGV 에 못 닿습니다")
    if out.returncode != 0:
        raise RuntimeError("curl 종료코드 %d: %s" % (
            out.returncode, out.stderr.decode("utf-8", "replace")[:90]))
    body = out.stdout.decode("utf-8", "replace")
    if not body.strip():
        raise RuntimeError("curl 이 빈 응답을 받았습니다")
    return body


def _cgv_requests(url):
    """curl 이 없는 환경을 위한 보조 경로. 막히면 예외를 냅니다."""
    try:
        r = requests.get(url, headers={
            "User-Agent": UA, "Referer": CGV_REF,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        raise RuntimeError("시간 초과 — 서버에서 CGV 에 못 닿습니다")
    except Exception as e:
        raise RuntimeError("연결 실패 %s" % type(e).__name__)
    if r.status_code != 200:
        raise RuntimeError("HTTP %d (차단으로 보입니다)" % r.status_code)
    return r.text


def _fetch_cgv(t, day):
    url = ("%s?coCd=A420&siteNo=%s&scnYmd=%s&rtctlScopCd=08"
           % (CGV_URL, t["site"], day.replace("-", "")))

    # curl 을 먼저, 안 되면 requests 로. 둘 다 실패하면 사유를 모아 던집니다.
    errs = []
    body = None
    for fn in (_cgv_curl, _cgv_requests):
        try:
            body = fn(url)
            break
        except Exception as e:
            errs.append("%s=%s" % (fn.__name__.replace("_cgv_", ""), e))
    if body is None:
        raise RuntimeError("CGV 받기 실패 (" + " / ".join(errs) + ")")

    try:
        data = (json.loads(body) or {}).get("data") or []
    except Exception:
        # HTML 이 오면 차단 안내나 점검 페이지입니다.
        # <title> 이나 본문 첫 글자를 뽑아 무엇인지 알 수 있게 합니다.
        import re as _re
        t = _re.search(r"<title[^>]*>(.*?)</title>", body, _re.S | _re.I)
        txt = _re.sub(r"<[^>]+>", " ", body)
        txt = _re.sub(r"\s+", " ", txt).strip()
        raise RuntimeError("CGV 가 HTML 을 돌려줬습니다 | title=%s | 본문=%s"
                           % ((t.group(1).strip()[:60] if t else "없음"), txt[:160]))

    rows, seen = [], set()
    for it in data:
        title = (it.get("movNm") or "").strip()
        start = _hhmm(it.get("scnsrtTm"))
        if not title or not start:
            continue
        end = _hhmm(it.get("scnendTm")) or start
        hall = (it.get("scnsNm") or "").strip()
        venue = (t["name"] + " " + hall) if hall else t["name"]
        key = (title, venue, start)
        if key in seen:
            continue
        seen.add(key)
        rows.append([title, venue, day[5:], start, _mins(start, end), 3,
                     it.get("frSeatCnt", ""), it.get("stcnt", "")])
    return rows


def _fetch_lotte(t, day):
    p = {"MethodName": "GetPlaySequence", "channelType": "HO", "osType": "W",
         "osVersion": UA, "playDate": day, "cinemaID": t["cid"],
         "representationMovieCode": ""}
    r = requests.post(LT_URL,
                      files={"paramList": (None, json.dumps(p, ensure_ascii=False))},
                      headers={"User-Agent": UA, "Referer": LT_REF},
                      timeout=TIMEOUT)
    r.raise_for_status()
    items = ((r.json() or {}).get("PlaySeqs") or {}).get("Items") or []

    rows, seen = [], set()
    for it in items:
        title = (it.get("MovieNameKR") or "").strip()
        start = _hhmm(it.get("StartTime"))
        if not title or not start:
            continue
        end = _hhmm(it.get("EndTime")) or start
        hall = (it.get("ScreenNameKR") or "").strip()
        venue = (t["name"] + " " + hall) if hall else t["name"]
        key = (title, venue, start)
        if key in seen:
            continue
        seen.add(key)

        # BookingSeatCount 는 이름과 달리 "남은 좌석" 입니다.
        # 09:30 상영이 135석 중 122 로 나오는데, 이걸 예매 수로 보면
        # 아침부터 거의 매진이라는 말이 되어 앞뒤가 맞지 않습니다.
        # 예전에는 Total 에서 빼서 남은 수를 거꾸로 표시했습니다.
        total = it.get("TotalSeatCount")
        left = it.get("BookingSeatCount")
        left = "" if left is None else left
        rows.append([title, venue, day[5:], start, _mins(start, end), 3,
                     left, total if total is not None else ""])
    return rows


def _megabox_raw(brch, day):
    """메가박스는 schedulePage.do 에 POST 로 지점 번호를 보냅니다.
       브라우저가 보내는 본문에는 날짜가 없어, 응답이 여러 날을 함께 주는지
       그날치만 주는지 확인이 필요합니다. 날짜 칸 이름 후보를 함께 넣어
       받아들이는 쪽이면 걸러지고, 무시하면 그냥 무시됩니다."""
    payload = {
        "masterType": "brch", "brchNo": brch, "brchNo1": brch,
        "firstAt": "Y",
        # 날짜 칸 이름 후보. 서버가 모르는 값은 대개 무시합니다.
        "playDe": day.replace("-", ""), "playDate": day.replace("-", ""),
    }
    r = requests.post(MB_URL, json=payload, headers={
        "User-Agent": UA, "Referer": MB_REF,
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError("HTTP %d" % r.status_code)
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


# 메가박스는 JSON 을 줍니다. 다만 회차가 megaMap 안 어디에 들어 있는지
# 판마다 다를 수 있어, 구조를 훑어 "시각과 제목이 든 목록" 을 찾아냅니다.
MB_TIME = re.compile(r"^\d{1,2}:\d{2}$")


def _mb_pick(node, out):
    """JSON 을 돌며 회차로 보이는 사전을 모읍니다.
       시작 시각처럼 생긴 값과 제목처럼 생긴 값이 함께 있으면 회차로 봅니다."""
    if isinstance(node, list):
        for v in node:
            _mb_pick(v, out)
        return
    if not isinstance(node, dict):
        return

    keys = {k.lower(): k for k in node}
    def find(*names):
        for n in names:
            if n in keys:
                return node[keys[n]]
        return None

    start = find("playstrttime", "playstrttm", "starttime", "playtm")
    title = find("movienm", "moviename", "movienmkr", "rpstmovienm")
    if start and title and MB_TIME.match(str(start).strip()):
        out.append(node)
        return

    for v in node.values():
        _mb_pick(v, out)


def _fetch_megabox(t, day):
    body = _megabox_raw(t["brch"], day)
    try:
        data = json.loads(body)
    except Exception:
        raise RuntimeError("JSON 이 아닙니다: " + body[:80].replace("\n", " "))

    found = []
    _mb_pick(data, found)
    if not found:
        raise RuntimeError("회차 목록을 못 찾았습니다 (응답 %d자). "
                           "/api/cinema/diag 의 megabox 항목을 확인하세요." % len(body))

    want = day.replace("-", "")
    rows, seen = [], set()
    for it in found:
        keys = {k.lower(): k for k in it}
        def g(*names):
            for n in names:
                if n in keys:
                    return it[keys[n]]
            return None

        de = str(g("playde", "playdate", "playymd") or "").replace("-", "")
        if de and de != want:
            continue                        # 여러 날이 섞여 오면 그날치만
        start = _hhmm(g("playstrttime", "playstrttm", "starttime", "playtm"))
        if not start:
            continue
        end = _hhmm(g("playendtime", "playendtm", "endtime")) or start
        title = str(g("movienm", "moviename", "movienmkr", "rpstmovienm") or "").strip()
        if not title:
            continue
        hall = str(g("theabnm", "theabname", "screennm", "theatnm") or "").strip()
        venue = (t["name"] + " " + hall) if hall else t["name"]

        key = (title, venue, start)
        if key in seen:
            continue
        seen.add(key)

        # 남은 좌석. 이름이 여러 가지라 있는 대로 받습니다.
        left = g("restseatcnt", "restseat", "frseatcnt", "reseatcnt")
        total = g("totseatcnt", "totalseatcount", "seatcnt")
        rows.append([title, venue, day[5:], start, _mins(start, end), 3,
                     "" if left is None else left,
                     "" if total is None else total])

    if not rows:
        raise RuntimeError("그날 회차가 없습니다 (찾은 항목 %d개)" % len(found))
    return rows


FETCH = {"dureraum": _fetch_dureraum, "cgv": _fetch_cgv,
         "lotte": _fetch_lotte, "megabox": _fetch_megabox}


def _get(t, day):
    ck = (t["key"], day)
    with _lock:
        hit = _cache.get(ck)
        if hit and (_time.time() - hit[0]) < _TTL:
            return hit[1]
    rows = FETCH[t["kind"]](t, day)
    with _lock:
        _cache[ck] = (_time.time(), rows)
    return rows


@bp.route("/api/cinema/diag")
def diag():
    """CGV 가 막힐 때 원인을 가리기 위한 진단.
       서버의 바깥 IP 와, CGV 가 실제로 돌려주는 것을 그대로 보여 줍니다."""
    out = {}

    # 이 서버가 인터넷에 어떤 IP 로 보이는지
    try:
        out["myIP"] = requests.get("https://api.ipify.org?format=json",
                                   timeout=8).json().get("ip")
    except Exception as e:
        out["myIP"] = "확인 실패 %s" % type(e).__name__

    url = ("%s?coCd=A420&siteNo=0005&scnYmd=%s&rtctlScopCd=08"
           % (CGV_URL, datetime.now(KST).strftime("%Y%m%d")))

    for name, fn in (("curl", _cgv_curl), ("requests", _cgv_requests)):
        try:
            body = fn(url)
            ok = body.lstrip().startswith("{")
            info = {"ok": ok, "len": len(body)}
            if ok:
                info["head"] = body[:120]
            else:
                # 태그를 걷어내고 사람이 읽는 글자만 남깁니다.
                # CSS 만 보여 주면 무슨 페이지인지 알 수 없습니다.
                import re as _re
                t = _re.search(r"<title[^>]*>(.*?)</title>", body, _re.S | _re.I)
                b = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
                b = _re.sub(r"<[^>]+>", " ", b)
                b = _re.sub(r"\s+", " ", b).strip()
                info["title"] = t.group(1).strip()[:80] if t else "없음"
                info["text"] = b[:400]
            out[name] = info
        except Exception as e:
            out[name] = {"ok": False, "error": str(e)[:250]}

    # ── 메가박스 응답 구조 살펴보기 ──
    # 아직 정규식을 못 맞춘 상태라, 무엇이 오는지 그대로 보여 줍니다.
    try:
        body = _megabox_raw("0082", datetime.now(KST).strftime("%Y-%m-%d"))
        mb = {"len": len(body), "isJSON": body.lstrip().startswith(("{", "["))}

        # JSON 이면 회차 목록을 찾아 첫 항목의 칸 이름을 보여 줍니다.
        try:
            data = json.loads(body)
            mb["topKeys"] = list(data)[:10]
            mm = data.get("megaMap") or {}
            mb["megaMapKeys"] = list(mm)[:20]
            found = []
            _mb_pick(data, found)
            mb["rowsFound"] = len(found)
            if found:
                mb["sampleKeys"] = sorted(found[0])[:30]
                mb["sample"] = {k: str(v)[:24] for k, v in list(found[0].items())[:12]}
        except Exception as e:
            mb["parseError"] = "%s: %s" % (type(e).__name__, str(e)[:120])
        mb["dates"] = sorted(set(re.findall(r'\b20\d{6}\b', body)))[:9]

        # 사람이 읽는 글자 일부
        txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
        txt = re.sub(r"<[^>]+>", " ", txt)
        mb["text"] = re.sub(r"\s+", " ", txt).strip()[:300]
    except Exception as e:
        mb = {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    out["megabox"] = mb

    return jsonify(out)


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

    # 특정 극장만 받고 싶을 때: ?only=cgv-seomyeon,dureraum
    only = [x.strip() for x in (request.args.get("only") or "").split(",") if x.strip()]
    picked = [t for t in THEATERS
              if (t.get("on", True) or t["key"] in only)
              and (not only or t["key"] in only)]

    jobs = [(t, (base + timedelta(days=i)).strftime("%Y-%m-%d"))
            for t in picked for i in range(days)]

    all_rows, failed = [], []
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not jobs:
                    return
                t, day = jobs.pop(0)
            try:
                got = _get(t, day)
                with qlock:
                    all_rows.extend(got)
            except Exception as e:
                # 예외 종류만 남기면 원인을 알 수 없습니다. 메시지까지 같이 적습니다.
                with qlock:
                    failed.append("%s %s — %s: %s" % (
                        t["name"], day, type(e).__name__, str(e)[:200]))
            _time.sleep(0.15)

    # 극장 넷 × 7일 = 28건. 여섯씩 동시에 받습니다.
    ths = [threading.Thread(target=worker) for _ in range(6)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()

    # 한 극장이 막혀도 나머지는 보여 줘야 합니다.
    # 전부 실패했을 때만 오류로 돌려줍니다.
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
        by = {}
        for r in all_rows:
            k = r[1].split(" ")[0]
            by[k] = by.get(k, 0) + 1
        return jsonify({"ok": True, "count": len(all_rows),
                        "byTheater": by, "failed": failed, "csv": text})
    return Response(text, mimetype="text/csv; charset=utf-8")
