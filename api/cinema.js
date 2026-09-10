/*
 * api/cinema.js — 영화의전당 상영시간표를 받아 CSV 로 돌려주는 서버 함수
 *
 * 왜 서버가 필요한가
 *   브라우저는 다른 사이트를 직접 읽을 수 없습니다(CORS). 영화의전당은 허락 헤더를
 *   보내지 않으므로, 주소가 https 여도 브라우저에서는 못 읽습니다.
 *   그래서 이 함수가 대신 읽어 내 사이트의 응답으로 돌려줍니다.
 *   브라우저 → 내 함수 → 영화의전당 순서가 되어 CORS 가 걸리지 않습니다.
 *
 * 쓰는 법
 *   /api/cinema            오늘부터 7일
 *   /api/cinema?days=14    오늘부터 14일
 *   /api/cinema?start=2026-10-01&days=10
 *
 * 예의
 *   요청 사이에 1초 이상 쉽니다. 응답은 30분간 캐시해 같은 요청이 몰려도
 *   영화의전당에는 한 번만 갑니다.
 */

const BASE = "https://www.dureraum.org/bcc/mcontents/caleList.do";
const THEATER = "영화의전당";
const UA = "Mozilla/5.0 (compatible; personal-archive/1.0)";
const MAX_DAYS = 21;

/* 서버 함수 실행 시간 여유 */
export const config = { maxDuration: 60 };

/* Vercel 서버는 UTC 로 돕니다. 한국보다 9시간 느려서, 한국 새벽에 열면
   어제부터 받아오게 됩니다. 그래서 한국 시각으로 계산합니다. */
const KST = 9 * 60 * 60 * 1000;

function ymd(d) {
  const k = new Date(d.getTime() + KST);
  return k.getUTCFullYear() + "-" +
    String(k.getUTCMonth() + 1).padStart(2, "0") + "-" +
    String(k.getUTCDate()).padStart(2, "0");
}

async function getDay(day) {
  const url = BASE + "?rbsIdx=37&searchDate=" + day;
  const r = await fetch(url, { headers: { "User-Agent": UA } });
  if (!r.ok) throw new Error("HTTP " + r.status);
  const html = await r.text();

  /* 한 회차가 이렇게 나옵니다.
       ... 경주기행 | 110min ... </a></li>
       <li class="place">중극장</li>
       <li class="time"> <a ...>13:20</a> </li>
     place / time 태그를 기준으로 묶어 읽습니다. */
  const re = new RegExp(
    "([^\\r\\n<>|]{1,80}?)\\s*\\|\\s*(\\d*)\\s*min" +
    "[\\s\\S]*?<li class=\"place\">\\s*([^<]+?)\\s*</li>" +
    "[\\s\\S]*?<li class=\"time\">([\\s\\S]*?)</li>", "g");

  const rows = [];
  const seen = new Set();
  let m;
  while ((m = re.exec(html)) !== null) {
    const title = m[1].trim().replace(/^(?:전체|G|청불|\d{1,2}세)\s*/, "").trim();
    if (!title) continue;
    const run = m[2] ? parseInt(m[2], 10) : 120;
    const hall = m[3].trim();
    const times = m[4].match(/(\d{1,2}):(\d{2})/g) || [];
    for (const t of times) {
      const [hh, mm] = t.split(":");
      const start = String(parseInt(hh, 10)).padStart(2, "0") + ":" + mm;
      const venue = hall ? THEATER + " " + hall : THEATER;
      const key = title + "|" + venue + "|" + start;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push([title, venue, day.slice(5), start, run, 3]);
    }
  }
  return rows;
}

function csvCell(v) {
  const s = String(v == null ? "" : v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

export default async function handler(req, res) {
  /* 내 사이트에서만 부를 것이므로 별도 허용 헤더는 필요 없지만,
     다른 곳에 올려 쓸 수도 있으니 열어 둡니다. */
  res.setHeader("Access-Control-Allow-Origin", "*");

  const q = req.query || {};
  const days = Math.min(MAX_DAYS, Math.max(1, parseInt(q.days, 10) || 7));
  /* 시작일도 한국 시각 기준입니다. 지정하면 그날 정오(KST)를 기준점으로 삼아
     시간대 계산에서 하루가 밀리지 않게 합니다. */
  const start = /^\d{4}-\d{2}-\d{2}$/.test(q.start || "")
    ? new Date(q.start + "T12:00:00+09:00")
    : new Date();

  /* 하루씩 1초 쉬며 받으면 7일에 14초가 걸려 브라우저가 오래 기다립니다.
     동시에 3건씩만 받아 시간을 줄이되, 상대 서버에 몰아치지는 않게 합니다. */
  const dayList = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(start.getTime());
    d.setDate(d.getDate() + i);
    dayList.push(ymd(d));
  }

  const all = [];
  const failed = [];
  const queue = dayList.slice();

  async function worker() {
    while (queue.length) {
      const day = queue.shift();
      try {
        all.push(...await getDay(day));
      } catch (e) {
        failed.push(day);
      }
      await new Promise(go => setTimeout(go, 250));
    }
  }
  await Promise.all([worker(), worker(), worker()]);
  all.sort((a, b) => (a[2] + a[3]).localeCompare(b[2] + b[3]));

  if (!all.length) {
    res.status(502).json({
      ok: false,
      error: "회차를 하나도 받지 못했습니다. 페이지 구조가 바뀌었을 수 있습니다.",
      failed,
    });
    return;
  }

  const header = "title,venue,date,start,runtime,priority";
  const body = all.map(r => r.map(csvCell).join(",")).join("\n");

  /* 30분 캐시 — 같은 요청이 몰려도 영화의전당에는 한 번만 갑니다 */
  res.setHeader("Cache-Control", "public, s-maxage=1800, stale-while-revalidate=3600");

  if (q.format === "json") {
    res.status(200).json({ ok: true, count: all.length, failed, csv: header + "\n" + body });
  } else {
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.status(200).send(header + "\n" + body);
  }
}
