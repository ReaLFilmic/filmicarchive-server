# app.py — 구글 로그인이 붙은 서버

import os
from flask import Flask, jsonify, session, redirect, url_for, request
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

import db

load_dotenv()

# static_url_path="" — static 폴더의 파일을 주소 맨 위에서 바로 내보냅니다
app = Flask(__name__, static_folder="static", static_url_path="")
app.json.ensure_ascii = False

# 쿠키에 서명하는 데 쓰는 열쇠. 이게 없으면 session 을 쓸 수 없습니다.
app.secret_key = os.environ["FLASK_SECRET"]

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    # 구글이 알려주는 설정 파일. 주소를 하나하나 적지 않아도 됩니다.
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@app.route("/")
def home():
    # 웹사이트를 서버가 직접 내보냅니다. 같은 주소에서 나오므로
    # 쿠키가 자동으로 붙고 CORS 를 신경 쓸 일이 없습니다.
    return app.send_static_file("index.html")


@app.route("/login")
def login():
    # 구글 로그인 화면으로 보냅니다. 돌아올 주소를 함께 알려줍니다.
    return google.authorize_redirect(url_for("callback", _external=True))


@app.route("/callback")
def callback():
    # 임시 코드를 사용자 정보로 바꿉니다. Authlib 이 교환을 대신 해줍니다.
    token = google.authorize_access_token()
    info = token["userinfo"]

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (google_sub, email, name) VALUES (%s, %s, %s) "
                "ON CONFLICT (google_sub) DO UPDATE "
                "SET email = EXCLUDED.email, name = EXCLUDED.name "
                "RETURNING id",
                (info["sub"], info.get("email"), info.get("name")),
            )
            uid = cur.fetchone()[0]
        conn.commit()

    session["user_id"] = uid          # 번호표 발급
    return redirect("/me")


@app.route("/me")
def me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"logged_in": False}), 401

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, name, nickname FROM users WHERE id = %s", (uid,)
            )
            row = cur.fetchone()

    return jsonify({
        "logged_in": True,
        "id": row[0],
        "email": row[1],
        "name": row[2],
        "nickname": row[3],           # 아직 안 정했으면 null
    })



# ── 로그인 문지기 ──
# 모든 API 앞에 붙여, 로그인 안 한 요청을 여기서 끊습니다.
from functools import wraps


def login_required(fn):
    @wraps(fn)
    def guard(*a, **kw):
        uid = session.get("user_id")
        if not uid:
            return jsonify({"error": "로그인이 필요합니다"}), 401
        return fn(uid, *a, **kw)
    return guard


@app.route("/api/records", methods=["GET"])
@login_required
def get_records(uid):
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tmdb_id, score, comment, watched_at, status, spoiler "
                "FROM records WHERE user_id = %s ORDER BY updated_at DESC",
                (uid,),
            )
            rows = cur.fetchall()

    return jsonify([
        {
            "tmdb_id": r[0],
            "score": float(r[1]) if r[1] is not None else None,
            "comment": r[2],
            "watched_at": r[3].isoformat() if r[3] else None,
            "status": r[4],
            "spoiler": bool(r[5]),
        }
        for r in rows
    ])


@app.route("/api/records", methods=["POST"])
@login_required
def save_record(uid):
    body = request.get_json(silent=True) or {}

    # 들어온 값을 먼저 검사합니다. 남이 아무 값이나 보낼 수 있습니다.
    try:
        tmdb_id = int(body["tmdb_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "tmdb_id 가 필요합니다"}), 400

    score = body.get("score")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            return jsonify({"error": "score 가 숫자가 아닙니다"}), 400
        if not (0 <= score <= 5):
            return jsonify({"error": "score 는 0 에서 5 사이입니다"}), 400

    comment = body.get("comment")
    if comment is not None:
        comment = str(comment)[:4000]

    status = body.get("status")
    if status is not None:
        status = str(status)[:20]

    spoiler = bool(body.get("spoiler"))

    # 빈 글자는 날짜가 아니므로 없는 값으로 바꿉니다
    watched_at = body.get("watched_at") or None      # 너무 긴 글을 막습니다

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO records "
                "  (user_id, tmdb_id, score, comment, watched_at, status, spoiler) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id, tmdb_id) DO UPDATE SET "
                "  score = EXCLUDED.score, comment = EXCLUDED.comment, "
                "  watched_at = EXCLUDED.watched_at, status = EXCLUDED.status, "
                "  spoiler = EXCLUDED.spoiler, updated_at = now()",
                (uid, tmdb_id, score, comment, watched_at, status, spoiler),
            )
        conn.commit()

    return jsonify({"ok": True, "tmdb_id": tmdb_id})


@app.route("/api/records/<int:tmdb_id>", methods=["DELETE"])
@login_required
def delete_record(uid, tmdb_id):
    with db.connect() as conn:
        with conn.cursor() as cur:
            # user_id 조건이 반드시 있어야 합니다. 없으면 남의 기록도 지워집니다.
            cur.execute(
                "DELETE FROM records WHERE user_id = %s AND tmdb_id = %s",
                (uid, tmdb_id),
            )
            gone = cur.rowcount
        conn.commit()

    return jsonify({"ok": True, "deleted": gone})



import re

# 한글·영문·숫자·밑줄만. 2~16자.
NICK_OK = re.compile(r"^[가-힣a-zA-Z0-9_]{2,16}$")


@app.route("/api/nickname", methods=["POST"])
@login_required
def set_nickname(uid):
    body = request.get_json(silent=True) or {}
    nick = str(body.get("nickname", "")).strip()

    if not NICK_OK.match(nick):
        return jsonify({
            "error": "닉네임은 한글·영문·숫자·밑줄 2~16자입니다"
        }), 400

    with db.connect() as conn:
        with conn.cursor() as cur:
            # 대소문자만 다른 이름도 같은 것으로 봅니다
            cur.execute(
                "SELECT id FROM users WHERE lower(nickname) = lower(%s) AND id <> %s",
                (nick, uid),
            )
            if cur.fetchone():
                return jsonify({"error": "이미 쓰는 닉네임입니다"}), 409

            cur.execute("UPDATE users SET nickname = %s WHERE id = %s", (nick, uid))
        conn.commit()

    return jsonify({"ok": True, "nickname": nick})



@app.route("/api/comments")
def public_comments():
    """한 작품에 달린 다른 사람들의 코멘트.
       로그인하지 않아도 읽을 수 있게 문지기를 붙이지 않습니다."""
    try:
        tmdb_id = int(request.args.get("tmdb_id", ""))
    except ValueError:
        return jsonify({"error": "tmdb_id 가 필요합니다"}), 400

    me = session.get("user_id")

    with db.connect() as conn:
        with conn.cursor() as cur:
            # 닉네임을 정하지 않은 사람의 글은 보여주지 않습니다.
            # 커뮤니티에 이름 없는 글이 섞이면 누가 썼는지 알 수 없습니다.
            cur.execute(
                "SELECT u.nickname, r.score, r.comment, r.spoiler, r.watched_at, "
                "       (r.user_id = %s) AS mine "
                "FROM records r JOIN users u ON u.id = r.user_id "
                "WHERE r.tmdb_id = %s "
                "  AND r.comment IS NOT NULL AND btrim(r.comment) <> '' "
                "  AND u.nickname IS NOT NULL "
                "ORDER BY r.updated_at DESC LIMIT 50",
                (me, tmdb_id),
            )
            rows = cur.fetchall()

    return jsonify([
        {
            "who": r[0],
            "score": float(r[1]) if r[1] is not None else None,
            "text": r[2],
            "spoiler": bool(r[3]),
            "at": r[4].isoformat() if r[4] else None,
            "mine": bool(r[5]),
        }
        for r in rows
    ])


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
