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


def get_setting(cur):
    cur.execute("SELECT min_rest_seconds FROM settings WHERE id = 1")
    row = cur.fetchone()
    return row["min_rest_seconds"] if row else 30


def pot_timeline(cur):
    """按时间升序取全部壶次，并补上每壶与同批次上一壶的间隔（秒）。"""
    cur.execute(
        """SELECT lot, pot_no, created_by, created_at
           FROM pots ORDER BY created_at, id"""
    )
    pots = cur.fetchall()
    prev_at_by_lot = {}
    for pot in pots:
        prev_at = prev_at_by_lot.get(pot["lot"])
        if prev_at is None:
            pot["gap"] = None
        else:
            pot["gap"] = round((pot["created_at"] - prev_at).total_seconds())
        prev_at_by_lot[pot["lot"]] = pot["created_at"]
        pot["created_at"] = pot["created_at"].strftime("%Y-%m-%d %H:%M:%S")
    pots.reverse()  # 时间线最新在前，便于新壶插到表头
    return pots


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
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
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


@app.get("/pots")
@login_required
def pots_page():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT name FROM batches ORDER BY id")
        batches = [r["name"] for r in cur.fetchall()]
        min_rest = get_setting(cur)
        pots = pot_timeline(cur)
    can_write = session.get("role") == "writer"
    return render_template(
        "pots.html", rows=pots, batches=batches, min_rest=min_rest, can_write=can_write
    )


@app.post("/pots")
@login_required
def submit_pot():
    if session.get("role") != "writer":
        return ("仅审评员可开壶递交，观察员只可查看壶次台账", 403)

    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("请选择批次", 422)
    try:
        pot_no = int(request.form.get("pot_no", "").strip())
    except ValueError:
        return ("壶序必须是正整数", 422)
    if pot_no < 1:
        return ("壶序必须是正整数", 422)

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        # 同一批次的开壶串行化，避免并发下壶序计数竞争
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lot,))
        min_rest = get_setting(cur)
        cur.execute("SELECT COUNT(*) AS n FROM pots WHERE lot = %s", (lot,))
        done = cur.fetchone()["n"]
        expected = done + 1
        if pot_no != expected:
            return (
                f"壶序 {pot_no} 拒交：批次「{lot}」已成功递交 {done} 壶，"
                f"当前只接受第 {expected} 壶，不可跳壶或重复壶序。",
                422,
            )

        gap = None
        if done >= 1:
            cur.execute(
                "SELECT created_at FROM pots WHERE lot = %s ORDER BY pot_no DESC LIMIT 1",
                (lot,),
            )
            last_at = cur.fetchone()["created_at"]
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
            gap = round(elapsed)
            if elapsed < min_rest:
                missing = math.ceil(min_rest - elapsed)
                return (
                    f"第 {pot_no} 壶拒交：距「{lot}」上一壶静置不足，"
                    f"最短需静置 {min_rest} 秒，还差 {missing} 秒。",
                    422,
                )

        cur.execute(
            """INSERT INTO pots (lot, pot_no, created_by)
               VALUES (%s, %s, %s)
               RETURNING lot, pot_no, created_by, created_at""",
            (lot, pot_no, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
        row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        row["gap"] = gap

    if request.headers.get("HX-Request"):
        return render_template("_pot_row.html", row=row)
    return redirect(url_for("pots_page"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    can_write = session.get("role") == "writer"
    error = ""
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if request.method == "POST":
            if not can_write:
                return ("仅审评员可修改静置秒数，观察员只可查看", 403)
            try:
                seconds = int(request.form.get("min_rest_seconds", "").strip())
            except ValueError:
                seconds = -1
            if seconds < 0 or seconds > 2147483647:
                error = "最短静置秒数必须是 0 到 2147483647 之间的整数"
            else:
                cur.execute(
                    """INSERT INTO settings (id, min_rest_seconds)
                       VALUES (1, %s)
                       ON CONFLICT (id) DO UPDATE SET min_rest_seconds = EXCLUDED.min_rest_seconds""",
                    (seconds,),
                )
                conn.commit()
        min_rest = get_setting(cur)
    return render_template(
        "settings.html", min_rest=min_rest, can_write=can_write, error=error
    )
