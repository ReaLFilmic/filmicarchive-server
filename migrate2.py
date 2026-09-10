# migrate2.py — users 표에 닉네임 칸을 더합니다. 한 번만 실행하세요.

from db import connect

SQL = """
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS nickname TEXT;

-- 커뮤니티에서 남에게 보이는 이름이므로 겹치면 안 됩니다.
-- 아직 안 정한 사람(NULL)은 여럿일 수 있어야 하므로, NULL 은 중복을 허용합니다.
CREATE UNIQUE INDEX IF NOT EXISTS users_nickname_key
  ON users (lower(nickname)) WHERE nickname IS NOT NULL;
"""

with connect() as conn:
    with conn.cursor() as cur:
        cur.execute(SQL)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'users' ORDER BY ordinal_position"
        )
        print("  users 칸:", ", ".join(r[0] for r in cur.fetchall()))
    conn.commit()

print("닉네임 칸을 더했습니다")
