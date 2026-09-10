# db.py — 데이터베이스 연결과 표 만들기

import os
import psycopg
from dotenv import load_dotenv

load_dotenv()                        # .env 파일을 읽어 들입니다

URL = os.environ.get("DATABASE_URL")


def connect():
    if not URL:
        raise RuntimeError(".env 에 DATABASE_URL 이 없습니다")
    return psycopg.connect(URL)


# 표 두 개를 만듭니다. 이미 있으면 그냥 넘어갑니다.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id          SERIAL PRIMARY KEY,
  google_sub  TEXT UNIQUE NOT NULL,
  email       TEXT,
  name        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS records (
  id          SERIAL PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tmdb_id     INTEGER NOT NULL,
  score       NUMERIC(2,1),
  comment     TEXT,
  watched_at  DATE,
  status      TEXT,
  spoiler     BOOLEAN NOT NULL DEFAULT false,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, tmdb_id)
);
"""


def init():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    print("표를 만들었습니다 (또는 이미 있습니다)")


if __name__ == "__main__":
    init()
