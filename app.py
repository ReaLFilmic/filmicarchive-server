# app.py — 구글 로그인이 붙은 서버

import os
from flask import Flask, jsonify, session, redirect, url_for, request
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

import db
from cinema import bp as cinema_bp
from biff import bp as biff_bp

load_dotenv()

# static_url_path="" — static 폴더의 파일을 주소 맨 위에서 바로 내보냅니다
app = Flask(__name__, static_folder="static", static_url_path="")
app.json.ensure_ascii = False

# 쿠키에 서명하는 데 쓰는 열쇠. 이게 없으면 session 을 쓸 수 없습니다.
app.secret_key = os.environ["FLASK_SECRET"]

app.register_blueprint(cinema_bp)
app.register_blueprint(biff_bp)

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


@app.route("/healthz")
def healthz():
    # 서버 깨우기·상태 확인 전용. DB 를 건드리지 않아 가볍고 빠릅니다.
    return "ok", 200, {"Cache-Control": "no-store"}


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
    # /me 는 JSON 만 돌려주는 주소라, 로그인 뒤에 거기로 보내면 글자만 뜹니다.
    # 사이트 첫 화면으로 돌려보내면 화면이 /me 로 로그인 상태를 스스로 확인합니다.
    return redirect("/")


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

    # If the user id in session no longer exists in DB, clear session and treat as not logged in.
    if not row:
        session.clear()
        return jsonify({"logged_in": False}), 401

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


import datetime

UPSERT_RECORD = (
    "INSERT INTO records "
    "  (user_id, tmdb_id, score, comment, watched_at, status, spoiler) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (user_id, tmdb_id) DO UPDATE SET "
    "  score = EXCLUDED.score, comment = EXCLUDED.comment, "
    "  watched_at = EXCLUDED.watched_at, status = EXCLUDED.status, "
    "  spoiler = EXCLUDED.spoiler, updated_at = now()"
)


def _clean_record(body):
    """들어온 값을 검사해 (tmdb_id, score, comment, watched_at, status, spoiler) 로 돌려줍니다.
       문제가 있으면 (None, 오류 글) 을 돌려줍니다."""
    try:
        tmdb_id = int(body["tmdb_id"])
    except (KeyError, TypeError, ValueError):
        return None, "tmdb_id 가 필요합니다"

    score = body.get("score")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            return None, "score 가 숫자가 아닙니다"
        if not (0 <= score <= 5):
            return None, "score 는 0 에서 5 사이입니다"

    comment = body.get("comment")
    if comment is not None:
        comment = str(comment)[:4000]

    status = body.get("status")
    if status is not None:
        status = str(status)[:20]

    spoiler = bool(body.get("spoiler"))

    # 날짜가 아닌 글이 들어오면 DB 가 오류를 내 저장이 통째로 실패합니다.
    # 읽을 수 없는 날짜는 없는 값으로 바꿉니다.
    watched_at = None
    raw = body.get("watched_at")
    if raw:
        try:
            watched_at = datetime.date.fromisoformat(str(raw).strip()[:10])
        except ValueError:
            watched_at = None

    return (tmdb_id, score, comment, watched_at, status, spoiler), None


@app.route("/api/records", methods=["POST"])
@login_required
def save_record(uid):
    got, err = _clean_record(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(UPSERT_RECORD, (uid,) + got)
        conn.commit()

    return jsonify({"ok": True, "tmdb_id": got[0]})


@app.route("/api/records/bulk", methods=["POST"])
@login_required
def save_records_bulk(uid):
    """여러 기록을 한 번에 저장합니다. 왓챠피디아·레터박스 가져오기처럼
       수백 편이 한꺼번에 바뀔 때, 요청을 편수만큼 보내면 DB 연결이 몰립니다."""
    body = request.get_json(silent=True) or {}
    items = body.get("records")
    if not isinstance(items, list):
        return jsonify({"error": "records 배열이 필요합니다"}), 400

    rows, bad = [], 0
    for it in items[:500]:
        got, err = _clean_record(it if isinstance(it, dict) else {})
        if err:
            bad += 1
            continue
        rows.append((uid,) + got)

    if rows:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(UPSERT_RECORD, rows)
            conn.commit()

    return jsonify({"ok": True, "saved": len(rows), "skipped": bad})


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



# ══════════ 자유게시판 ══════════
# 글쓰기는 로그인 + 닉네임이 있어야 하고, 읽기는 누구나 됩니다.

TITLE_MAX = 80
BODY_MAX = 4000


def _need_nick(uid):
    """닉네임이 없으면 글을 쓸 수 없습니다.
       게시판에 이름 없는 글이 섞이면 누가 썼는지 알 수 없습니다."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nickname FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
    return (r and r[0]) or None


@app.route("/api/posts", methods=["GET"])
def list_posts():
    """목록. 본문은 미리보기만 잘라 보냅니다."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    size = 20
    me = session.get("user_id")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM posts WHERE deleted_at IS NULL")
            total = cur.fetchone()[0]

            cur.execute(
                "SELECT p.id, u.nickname, p.title, "
                "       left(p.body, 120), p.created_at, "
                "       (p.user_id = %s) AS mine "
                "FROM posts p JOIN users u ON u.id = p.user_id "
                "WHERE p.deleted_at IS NULL "
                "ORDER BY p.created_at DESC LIMIT %s OFFSET %s",
                (me, size, (page - 1) * size))
            rows = cur.fetchall()

    return jsonify({
        "total": total, "page": page, "size": size,
        "items": [{
            "id": r[0], "who": r[1], "title": r[2],
            "preview": r[3], "at": r[4].isoformat(), "mine": bool(r[5]),
        } for r in rows],
    })


@app.route("/api/posts/<int:pid>", methods=["GET"])
def read_post(pid):
    me = session.get("user_id")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.id, u.nickname, p.title, p.body, p.created_at, "
                "       p.updated_at, (p.user_id = %s) AS mine "
                "FROM posts p JOIN users u ON u.id = p.user_id "
                "WHERE p.id = %s AND p.deleted_at IS NULL",
                (me, pid))
            r = cur.fetchone()
    if not r:
        return jsonify({"error": "없는 글입니다"}), 404
    return jsonify({"id": r[0], "who": r[1], "title": r[2], "body": r[3],
                    "at": r[4].isoformat(), "edited": (r[5].isoformat() if r[5] else None),
                    "mine": bool(r[6])})


def _read_post_body():
    """제목·본문을 검사해 돌려줍니다. 문제가 있으면 (None, 오류응답)."""
    b = request.get_json(silent=True) or {}
    title = str(b.get("title", "")).strip()
    body = str(b.get("body", "")).strip()
    if not title:
        return None, (jsonify({"error": "제목을 적어주세요"}), 400)
    if not body:
        return None, (jsonify({"error": "내용을 적어주세요"}), 400)
    return (title[:TITLE_MAX], body[:BODY_MAX]), None


@app.route("/api/posts", methods=["POST"])
@login_required
def write_post(uid):
    # 값 검사를 먼저 합니다. DB 에 다녀오기 전에 걸러내면
    # 잘못된 요청 때문에 DB 를 건드리는 일이 없습니다.
    got, err = _read_post_body()
    if err:
        return err
    title, body = got

    if not _need_nick(uid):
        return jsonify({"error": "닉네임을 먼저 정해주세요"}), 400

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO posts (user_id, title, body) "
                "VALUES (%s, %s, %s) RETURNING id",
                (uid, title, body))
            pid = cur.fetchone()[0]
        conn.commit()
    return jsonify({"ok": True, "id": pid})


@app.route("/api/posts/<int:pid>", methods=["PUT"])
@login_required
def edit_post(uid, pid):
    got, err = _read_post_body()
    if err:
        return err
    title, body = got

    with db.connect() as conn:
        with conn.cursor() as cur:
            # user_id 조건이 반드시 있어야 합니다. 없으면 남의 글도 고쳐집니다.
            cur.execute(
                "UPDATE posts SET title = %s, body = %s, updated_at = now() "
                "WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                (title, body, pid, uid))
            n = cur.rowcount
        conn.commit()
    if not n:
        return jsonify({"error": "내 글이 아니거나 없는 글입니다"}), 403
    return jsonify({"ok": True})


@app.route("/api/posts/<int:pid>", methods=["DELETE"])
@login_required
def remove_post(uid, pid):
    with db.connect() as conn:
        with conn.cursor() as cur:
            # 실제로 지우지 않고 표시만 합니다. 되살릴 수 있고,
            # 실수로 남의 글을 지우는 사고에도 대비가 됩니다.
            cur.execute(
                "UPDATE posts SET deleted_at = now() "
                "WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                (pid, uid))
            n = cur.rowcount
        conn.commit()
    if not n:
        return jsonify({"error": "내 글이 아니거나 없는 글입니다"}), 403
    return jsonify({"ok": True})


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# Render 무료 플랜이 15분 뒤 서버를 끄지 않도록 스스로 깨우는 스레드.
# gunicorn 으로 불러올 때도 실행되도록 __main__ 밖에 둡니다.
# (RENDER_EXTERNAL_URL 이 없는 내 컴퓨터에서는 아무 일도 하지 않습니다)
import keepalive
keepalive.start()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
