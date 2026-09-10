# migrate.py — 표에 칸 두 개를 더합니다. 한 번만 실행하면 됩니다.

from db import connect

SQL = """
ALTER TABLE records
  ADD COLUMN IF NOT EXISTS status  TEXT,
  ADD COLUMN IF NOT EXISTS spoiler BOOLEAN NOT NULL DEFAULT false;
"""

with connect() as conn:
    with conn.cursor() as cur:
        cur.execute(SQL)
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'records' ORDER BY ordinal_position"
        )
        for name, kind in cur.fetchall():
            print("  %-12s %s" % (name, kind))
    conn.commit()

print("칸을 더했습니다")
