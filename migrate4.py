# migrate4.py — 자유게시판 표를 만듭니다. 한 번만 실행하세요.

from db import connect

SQL = """
CREATE TABLE IF NOT EXISTS posts (
  id         SERIAL PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title      TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- 글을 지워도 목록의 번호가 밀리지 않게 표시만 바꿉니다.
  deleted_at TIMESTAMPTZ
);

-- 목록은 최신순으로만 봅니다. 지운 글은 빼고 찾도록 부분 인덱스를 둡니다.
CREATE INDEX IF NOT EXISTS posts_recent
  ON posts (created_at DESC) WHERE deleted_at IS NULL;
"""

with connect() as conn:
    with conn.cursor() as cur:
        cur.execute(SQL)
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'posts' ORDER BY ordinal_position")
        for n, t in cur.fetchall():
            print("  %-11s %s" % (n, t))
    conn.commit()

print("자유게시판 표를 만들었습니다")
