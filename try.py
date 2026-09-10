from db import connect

with connect() as conn:
    with conn.cursor() as cur:

        # ① 사용자 한 명 넣기
        cur.execute(
            "INSERT INTO users (google_sub, email, name) VALUES (%s, %s, %s) "
            "ON CONFLICT (google_sub) DO NOTHING RETURNING id",
            ("test-123", "me@example.com", "갈민승"),
        )
        row = cur.fetchone()

        # 이미 있으면 위 문장이 아무것도 안 돌려주므로 다시 찾습니다
        if row:
            uid = row[0]
        else:
            cur.execute("SELECT id FROM users WHERE google_sub = %s", ("test-123",))
            uid = cur.fetchone()[0]
        print("내 번호:", uid)

        # ② 별점 하나 저장하기
        cur.execute(
            "INSERT INTO records (user_id, tmdb_id, score, comment) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, tmdb_id) "
            "DO UPDATE SET score = EXCLUDED.score, comment = EXCLUDED.comment",
            (uid, 157336, 4.5, "인터스텔라 좋았음"),
        )

        # ③ 꺼내보기
        cur.execute(
            "SELECT tmdb_id, score, comment FROM records WHERE user_id = %s",
            (uid,),
        )
        for tmdb_id, score, comment in cur.fetchall():
            print(tmdb_id, score, comment)

    conn.commit()