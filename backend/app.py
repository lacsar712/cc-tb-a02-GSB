import math
import os
from datetime import datetime, timezone
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def writer_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("仅审评员可操作", 403)
        return fn(*args, **kwargs)

    return wrap


def get_rest_seconds(cur):
    cur.execute("SELECT min_rest_seconds FROM settings WHERE id = 1")
    return cur.fetchone()["min_rest_seconds"]


def fmt_time(value):
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    can_write = session.get("role") == "writer"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
        rest_seconds = get_rest_seconds(cur)
        lot_status = []
        if can_write:
            cur.execute(
                """SELECT lot, COUNT(*) AS pot_count, MAX(created_at) AS last_at
                     FROM pots GROUP BY lot ORDER BY lot"""
            )
            now = datetime.now(timezone.utc)
            for stat in cur.fetchall():
                elapsed = (now - stat["last_at"]).total_seconds() if stat["last_at"] else 0
                wait = max(0, math.ceil(rest_seconds - elapsed))
                lot_status.append(
                    {
                        "lot": stat["lot"],
                        "pot_count": stat["pot_count"],
                        "next_seq": stat["pot_count"] + 1,
                        "wait": wait,
                    }
                )
            cur.execute(
                "SELECT lot FROM cuppings UNION SELECT lot FROM pots ORDER BY 1"
            )
            all_lots = [r["lot"] for r in cur.fetchall()]
        else:
            all_lots = []
    return render_template(
        "home.html",
        rows=rows,
        can_write=can_write,
        rest_seconds=rest_seconds,
        lot_status=lot_status,
        all_lots=all_lots,
    )


@app.post("/cuppings")
@writer_required
def create():
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


@app.post("/pots")
@writer_required
def submit_pot():
    """开壶递交：同批次只接受「已成功壶数 + 1」，且距上一壶满足最短静置秒数。"""
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("请选择批次", 400)
    try:
        pot_seq = int(request.form.get("pot_seq", ""))
        aroma = float(request.form["aroma"])
        taste = float(request.form["taste"])
        liquor = float(request.form["liquor"])
    except (TypeError, ValueError):
        return ("壶序和三项评分必须是数字", 400)

    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        try:
            # 对同一批次加事务级咨询锁，避免并发递交绕开壶序计数
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lot,))
            cur.execute(
                "SELECT COUNT(*) AS n FROM pots WHERE lot = %s", (lot,)
            )
            success_count = cur.fetchone()["n"]
            expected_seq = success_count + 1

            if pot_seq != expected_seq:
                if pot_seq < expected_seq:
                    return (
                        f"重复壶序：批次「{lot}」第 {pot_seq} 壶已成功递交，"
                        f"当前只可交第 {expected_seq} 壶",
                        400,
                    )
                return (
                    f"跳壶被拒：批次「{lot}」当前应交第 {expected_seq} 壶，"
                    f"不能直接交第 {pot_seq} 壶",
                    400,
                )

            rest_seconds = get_rest_seconds(cur)
            cur.execute(
                """SELECT pot_seq, created_at FROM pots
                    WHERE lot = %s ORDER BY pot_seq DESC LIMIT 1""",
                (lot,),
            )
            last_pot = cur.fetchone()
            if last_pot is not None and rest_seconds > 0:
                elapsed = (
                    datetime.now(timezone.utc) - last_pot["created_at"]
                ).total_seconds()
                if elapsed < rest_seconds:
                    short = math.ceil(rest_seconds - elapsed)
                    return (
                        f"静置不足：批次「{lot}」上一壶（第 {last_pot['pot_seq']} 壶）"
                        f"开壶于 {fmt_time(last_pot['created_at'])} UTC，"
                        f"最短需静置 {rest_seconds} 秒，还差 {short} 秒",
                        400,
                    )

            cur.execute(
                """INSERT INTO pots (lot, pot_seq, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (lot, pot_seq, aroma, taste, liquor, score, verdict, note, session["user"]),
            )
            pot_row = cur.fetchone()
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
            )
            cup_row = cur.fetchone()
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            return ("递交失败，请重试", 500)

    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=cup_row)
    return redirect(url_for("ledger", lot=lot))


@app.get("/settings")
@writer_required
def settings_page():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        rest_seconds = get_rest_seconds(cur)
    saved = request.args.get("saved") == "1"
    return render_template(
        "settings.html", rest_seconds=rest_seconds, saved=saved, can_write=True
    )


@app.post("/settings")
@writer_required
def update_settings():
    try:
        rest_seconds = int(request.form.get("min_rest_seconds", ""))
    except (TypeError, ValueError):
        return ("最短静置秒数必须是不小于 0 的整数", 400)
    if rest_seconds < 0:
        return ("最短静置秒数不能为负", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """UPDATE settings
                  SET min_rest_seconds = %s, updated_by = %s, updated_at = now()
                WHERE id = 1""",
            (rest_seconds, session["user"]),
        )
        conn.commit()
    return redirect(url_for("settings_page", saved="1"))


@app.get("/ledger")
@login_required
def ledger():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT p.*,
                      p.created_at - LAG(p.created_at) OVER (
                          PARTITION BY p.lot ORDER BY p.pot_seq
                      ) AS gap
                 FROM pots p
                ORDER BY p.lot, p.pot_seq"""
        )
        pot_rows = cur.fetchall()
        rest_seconds = get_rest_seconds(cur)

    groups = []
    by_lot = {}
    for row in pot_rows:
        gap_seconds = None
        if row["gap"] is not None:
            gap_seconds = round(row["gap"].total_seconds())
        item = {
            "lot": row["lot"],
            "pot_seq": row["pot_seq"],
            "score": row["score"],
            "verdict": row["verdict"],
            "note": row["note"],
            "created_by": row["created_by"],
            "created_at": fmt_time(row["created_at"]),
            "gap_seconds": gap_seconds,
        }
        if row["lot"] not in by_lot:
            group = {"lot": row["lot"], "pots": []}
            by_lot[row["lot"]] = group
            groups.append(group)
        by_lot[row["lot"]]["pots"].append(item)

    return render_template(
        "ledger.html",
        groups=groups,
        rest_seconds=rest_seconds,
        can_write=session.get("role") == "writer",
    )
