# migrate3.py — 시간표 캐시 표를 만듭니다. 한 번만 실행하세요.

from db import connect

SQL = """
CREATE TABLE IF NOT EXISTS cinema_cache (
  theater  TEXT NOT NULL,
  day      DATE NOT NULL,
  rows     JSONB NOT NULL,
  fetched  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (theater, day)
);

-- 지난 날짜는 쓸 일이 없으니 지울 때 빨리 찾도록
CREATE INDEX IF NOT EXISTS cinema_cache_day ON cinema_cache (day);
"""

with connect() as conn:
    with conn.cursor() as cur:
        cur.execute(SQL)
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'cinema_cache' ORDER BY ordinal_position")
        for n, t in cur.fetchall():
            print("  %-9s %s" % (n, t))
    conn.commit()

print("시간표 캐시 표를 만들었습니다")
