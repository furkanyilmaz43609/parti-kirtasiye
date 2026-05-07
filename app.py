import csv
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TR = ZoneInfo("Europe/Istanbul")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "pdks-local-dev-change-me")

DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "pdks_merkez.db"))

_RENDER_HOSTED = bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"))
if _RENDER_HOSTED:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def redirect_after_admin_login(request_next: str):
    target = (request_next or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\r" not in target and "\n" not in target:
        return redirect(target)
    return redirect(url_for("admin"))


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            code TEXT,
            shift_start TEXT NOT NULL DEFAULT '09:00',
            shift_end TEXT NOT NULL DEFAULT '18:00',
            allowed_ip TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS personnel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            branch_id INTEGER NOT NULL,
            monthly_salary REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (branch_id) REFERENCES branches(id)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            checkin_at TEXT,
            checkout_at TEXT,
            duration_minutes INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'mobile',
            FOREIGN KEY (personnel_id) REFERENCES personnel(id),
            FOREIGN KEY (branch_id) REFERENCES branches(id)
        );

        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    db.commit()
    _migrate_schema(db)


def _migrate_schema(db):
    bcols = [r["name"] for r in db.execute("PRAGMA table_info(branches)").fetchall()]
    if "allowed_ip" not in bcols:
        db.execute("ALTER TABLE branches ADD COLUMN allowed_ip TEXT")
        db.commit()
        bcols.append("allowed_ip")
    if "active" not in bcols:
        db.execute("ALTER TABLE branches ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        db.commit()
    db.execute("UPDATE branches SET active = 1 WHERE active IS NULL")
    db.commit()


@app.before_request
def before_request():
    init_db()


def require_admin():
    return session.get("is_admin") is True


def now_tr():
    return datetime.now(TR)


def now_str():
    return now_tr().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _parse_ts_tr(s):
    dt = _parse_ts(s)
    if not dt:
        return None
    return dt.replace(tzinfo=TR)


def _minutes_between(start_dt, end_dt):
    return max(0, int((end_dt - start_dt).total_seconds() // 60))


def format_duration_tr(minutes: int | float | None) -> str:
    if minutes is None:
        minutes = 0
    m = int(max(0, minutes))
    h, mm = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} sa")
    if mm:
        parts.append(f"{mm} dk")
    return " ".join(parts) if parts else "0 dk"


def format_display_datetime(value) -> str:
    if not value or str(value).strip() in ("-", "—"):
        return "—"
    s = str(value).strip()
    dt = _parse_ts(s)
    if dt:
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10].split("-")[2] + "." + s[5:7] + "." + s[:4] + (s[10:] if len(s) > 10 else "")
    return s


def format_iso_date_tr(iso_day: str | None) -> str:
    if not iso_day or len(iso_day) < 10 or iso_day[4] != "-":
        return str(iso_day or "")
    return f"{iso_day[8:10]}.{iso_day[5:7]}.{iso_day[:4]}"


@app.template_filter("tr_iso_date")
def jinja_iso_date(val):
    return format_iso_date_tr(val)
@app.template_filter("tr_dt")
def jinja_tr_dt(value):
    return format_display_datetime(value)


@app.template_filter("sure_tr")
def jinja_sure_tr(value):
    try:
        return format_duration_tr(int(value))
    except (TypeError, ValueError):
        return "—"


def reconcile_personel_lock(db):
    lock = session.get("pdks_choice_lock")
    if not lock:
        return
    row = db.execute(
        """
        SELECT id FROM attendance
        WHERE personnel_id = ? AND branch_id = ? AND checkout_at IS NULL
        """,
        (lock["personnel_id"], lock["branch_id"]),
    ).fetchone()
    if not row:
        session.pop("pdks_choice_lock", None)
        session.modified = True


def choice_lock_error_response(db, personnel_id: int, branch_id: int):
    reconcile_personel_lock(db)
    lock = session.get("pdks_choice_lock")
    if not lock:
        return None
    if int(lock["personnel_id"]) != int(personnel_id) or int(lock["branch_id"]) != int(branch_id):
        return jsonify(
            {
                "ok": False,
                "message": (
                    f"Bu cihazda önce seçtiğiniz personel işlemini bitirmelisiniz: "
                    f"{lock.get('full_name', '')}. Çıkış yapmadan başka kişi veya mağaza seçilemez."
                ),
            }
        ), 400
    return None


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", maxsplit=1)[0].strip()
    return (request.remote_addr or "").strip()


def store_ip_status(branch_row, client_ip: str) -> tuple:
    raw = ""
    try:
        raw = branch_row["allowed_ip"] if branch_row else ""
    except (KeyError, TypeError):
        raw = ""
    allowed = (raw or "").strip()
    if not allowed:
        return False, "magaza_ipsiz"
    lst = [x.strip() for x in allowed.split(",") if x.strip()]
    ok = client_ip in lst
    return ok, "ok" if ok else "nomatch"


def get_setting(key: str):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str):
    db = get_db()
    db.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    db.commit()


def hash_password(password: str):
    payload = f"{app.config['SECRET_KEY']}::{password}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_password(password: str):
    stored_hash = get_setting("admin_password_hash")
    if not stored_hash:
        return False
    entered_hash = hash_password(password)
    return hmac.compare_digest(stored_hash, entered_hash)


def fetch_branches(active_only=False):
    db = get_db()
    q = "SELECT * FROM branches"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY name"
    return db.execute(q).fetchall()


def fetch_personnel_for_public():
    db = get_db()
    return db.execute(
        """
        SELECT p.*, b.name AS branch_name
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        WHERE p.active = 1 AND b.active = 1
        ORDER BY p.full_name
        """
    ).fetchall()


def fetch_personnel_admin():
    db = get_db()
    return db.execute(
        """
        SELECT p.*, b.name AS branch_name
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        ORDER BY p.full_name
        """
    ).fetchall()


def personnel_work_stats(db, personnel_id: int):
    rows = db.execute(
        """
        SELECT date, checkin_at, checkout_at, duration_minutes
        FROM attendance WHERE personnel_id = ? ORDER BY id
        """,
        (personnel_id,),
    ).fetchall()

    now = now_tr()

    today_s = now.strftime("%Y-%m-%d")
    mon_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date()
    week_start_date = now.date() - timedelta(days=(now.weekday()))
    week_end_date = week_start_date + timedelta(days=6)

    def contrib_minutes_day(iso_day: str):
        total = 0
        for r in rows:
            if r["date"] != iso_day:
                continue
            if r["checkout_at"]:
                total += int(r["duration_minutes"] or 0)
            elif r["checkin_at"]:
                ci = _parse_ts_tr(r["checkin_at"])
                if ci:
                    total += _minutes_between(ci, now)
        return total

    today_minutes = contrib_minutes_day(today_s)

    weekly_minutes = 0
    weekly_days = set()
    cur = week_start_date
    while cur <= week_end_date:
        iso = cur.strftime("%Y-%m-%d")
        m = contrib_minutes_day(iso)
        if m > 0:
            weekly_minutes += m
            weekly_days.add(iso)
        cur += timedelta(days=1)

    monthly_minutes = 0
    monthly_days = set()
    cur = mon_start
    while cur.year == now.year and cur.month == now.month and cur <= now.date():
        iso = cur.strftime("%Y-%m-%d")
        m = contrib_minutes_day(iso)
        if m > 0:
            monthly_minutes += m
            monthly_days.add(iso)
        cur += timedelta(days=1)

    def fmt_h(m):
        return round(m / 60.0, 2)

    return {
        "today_hours": fmt_h(today_minutes),
        "today_hm": format_duration_tr(today_minutes),
        "week_days": len(weekly_days),
        "week_hours": fmt_h(weekly_minutes),
        "week_hm": format_duration_tr(weekly_minutes),
        "month_days": len(monthly_days),
        "month_hours": fmt_h(monthly_minutes),
        "month_hm": format_duration_tr(monthly_minutes),
    }


@app.route("/", methods=["GET", "POST"])
def index():
    admin_hash = get_setting("admin_password_hash")
    mode = "setup" if not admin_hash else "login"
    next_url = (
        (request.form.get("next") or request.args.get("next") or "").strip()
        if request.method == "POST"
        else (request.args.get("next") or "").strip()
    )

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if mode == "setup" and action == "setup_password":
            password = request.form.get("password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()
            if len(password) < 4:
                flash("Şifre en az 4 karakter olmalı.", "danger")
            elif password != confirm_password:
                flash("Şifre ile tekrar eşleşmiyor.", "danger")
            else:
                set_setting("admin_password_hash", hash_password(password))
                session["is_admin"] = True
                return redirect_after_admin_login(next_url)

        if mode == "login" and action == "login":
            password = request.form.get("password", "").strip()
            if verify_password(password):
                session["is_admin"] = True
                return redirect_after_admin_login(next_url)
            flash("Yönetici şifresi hatalı.", "danger")

    return render_template("index.html", mode=mode, next=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not require_admin():
        flash("Önce giriş yapın.", "info")
        return redirect(url_for("index", next=request.path))

    db = get_db()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "add_branch":
            name = request.form["name"].strip()
            allowed_ip = request.form.get("allowed_ip", "").strip()
            if not allowed_ip:
                flash("Mağaza için internet çıkış IP adresi zorunludur.", "danger")
            else:
                try:
                    db.execute(
                        """
                        INSERT INTO branches (name, code, shift_start, shift_end, allowed_ip, active, created_at)
                        VALUES (?, NULL, '09:00', '18:00', ?, 1, ?)
                        """,
                        (name, allowed_ip, now_str()),
                    )
                    db.commit()
                    flash("Mağaza eklendi.", "success")
                except sqlite3.IntegrityError:
                    flash("Bu isimde mağaza zaten var.", "danger")

        elif action == "delete_branch":
            bid = int(request.form["branch_id"])
            db.execute("DELETE FROM attendance WHERE branch_id = ?", (bid,))
            db.execute("DELETE FROM personnel WHERE branch_id = ?", (bid,))
            db.execute("DELETE FROM branches WHERE id = ?", (bid,))
            db.commit()
            flash("Mağaza ve bağlı kayıtlar silindi.", "success")

        elif action == "set_branch_ip":
            bid = int(request.form["branch_id"])
            raw = request.form.get("allowed_ip", "").strip()
            if not raw:
                flash("IP alanı boş bırakılamaz.", "danger")
            else:
                db.execute("UPDATE branches SET allowed_ip = ? WHERE id = ?", (raw, bid))
                db.commit()
                flash("Mağaza IP güncellendi.", "success")

        elif action == "add_personnel":
            db.execute(
                """
                INSERT INTO personnel (full_name, branch_id, monthly_salary, active, created_at)
                VALUES (?, ?, 0, 1, ?)
                """,
                (
                    request.form["full_name"].strip(),
                    int(request.form["branch_id"]),
                    now_str(),
                ),
            )
            db.commit()
            flash("Personel eklendi.", "success")

        elif action == "delete_personnel":
            pid = int(request.form["personnel_id"])
            db.execute("DELETE FROM attendance WHERE personnel_id = ?", (pid,))
            db.execute("DELETE FROM personnel WHERE id = ?", (pid,))
            db.commit()
            flash("Personel ve mesai kayıtları silindi.", "success")
            return_pid = request.form.get("return_pid", type=int)
            if return_pid == pid:
                return redirect(url_for("admin"))

        elif action == "add_note":
            content = request.form.get("content", "").strip()
            if content:
                db.execute(
                    "INSERT INTO announcements (content, created_at) VALUES (?, ?)",
                    (content, now_str()),
                )
                db.commit()
                flash("Duyuru kaydedildi.", "success")

        elif action == "delete_note":
            note_id = int(request.form["announcement_id"])
            db.execute("DELETE FROM announcements WHERE id = ?", (note_id,))
            db.commit()
            flash("Duyuru silindi.", "success")

        return_pid = request.form.get("return_pid", type=int)
        if return_pid:
            return redirect(url_for("admin", pid=return_pid))
        return redirect(url_for("admin"))

    branches = fetch_branches(active_only=False)
    personnel_admin = fetch_personnel_admin()

    attendance_rows = db.execute(
        """
        SELECT a.*, p.full_name, b.name AS branch_name
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        ORDER BY COALESCE(a.checkout_at, a.checkin_at) DESC, a.id DESC
        LIMIT 200
        """
    ).fetchall()

    latest_notes = db.execute(
        "SELECT id, content, created_at FROM announcements ORDER BY id DESC LIMIT 20"
    ).fetchall()

    selected_pid = request.args.get("pid", type=int)
    sel_stats = None
    sel_name = None
    if selected_pid:
        prow = db.execute(
            "SELECT full_name FROM personnel WHERE id = ?",
            (selected_pid,),
        ).fetchone()
        if prow:
            sel_name = prow["full_name"]
            sel_stats = personnel_work_stats(db, selected_pid)

    return render_template(
        "admin.html",
        branches=branches,
        personnel=personnel_admin,
        attendance_rows=attendance_rows,
        latest_notes=latest_notes,
        selected_pid=selected_pid,
        sel_name=sel_name,
        sel_stats=sel_stats,
    )


@app.route("/personel")
def personel():
    db = get_db()
    reconcile_personel_lock(db)
    branches = fetch_branches(active_only=True)
    personnel_rows = fetch_personnel_for_public()
    latest_note = db.execute(
        "SELECT content, created_at FROM announcements ORDER BY id DESC LIMIT 1"
    ).fetchone()
    choice_lock = session.get("pdks_choice_lock")
    return render_template(
        "personel.html",
        branches=branches,
        personnel=personnel_rows,
        latest_note=latest_note,
        choice_lock=choice_lock,
    )


@app.get("/health")
def health():
    """Render / denetim: tarayıcıda /health açınca 'ok' görünmeli."""
    return Response("ok", mimetype="text/plain")


@app.route("/tara")
def tara_legacy():
    return redirect(url_for("personel"))


@app.route("/sube/<int:branch_id>/ekran")
def branch_screen(branch_id):
    return redirect(url_for("personel"))


@app.get("/api/personnel-durum")
def api_personnel_status():
    try:
        personnel_id = int(request.args.get("personnel_id", "0"))
        branch_id = int(request.args.get("branch_id", "0"))
    except ValueError:
        return jsonify({"ok": False, "message": "Geçersiz parametre"}), 400

    if not personnel_id or not branch_id:
        return jsonify({"ok": False, "message": "Mağaza ve personeli seçin."}), 400

    db = get_db()
    reconcile_personel_lock(db)
    chk = choice_lock_error_response(db, personnel_id, branch_id)
    if chk is not None:
        return chk
    person = db.execute(
        "SELECT id, full_name, branch_id FROM personnel WHERE id = ? AND active = 1",
        (personnel_id,),
    ).fetchone()
    if not person:
        return jsonify({"ok": False, "message": "Personel bulunamadı veya kapalı mağaza."}), 404
    if person["branch_id"] != branch_id:
        return jsonify({"ok": False, "message": "Bu personel seçilen mağazaya bağlı değil."}), 400

    branch = db.execute("SELECT * FROM branches WHERE id = ? AND active = 1", (branch_id,)).fetchone()
    if not branch:
        return jsonify({"ok": False, "message": "Mağaza kapalı."}), 404

    client_ip = get_client_ip()
    ip_ok, reason = store_ip_status(branch, client_ip)

    if reason == "magaza_ipsiz":
        return jsonify(
            {
                "ok": True,
                "next_action": "blocked",
                "client_ip": client_ip,
                "ip_ok": False,
                "message": "Bu mağaza için yöneticinin tanımladığı çıkış IP henüz yok; işlem kapalıdır.",
            }
        )

    open_record = db.execute(
        """
        SELECT id FROM attendance
        WHERE personnel_id = ? AND branch_id = ? AND checkout_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (personnel_id, branch_id),
    ).fetchone()
    next_action = "out" if open_record else "in"

    if not ip_ok:
        return jsonify(
            {
                "ok": True,
                "next_action": "blocked",
                "client_ip": client_ip,
                "ip_ok": False,
                "message": "Bu işlem için mağaza internetine (tanımlı IP) bağlı olmanız gerekir.",
            }
        )

    return jsonify(
        {"ok": True, "next_action": next_action, "client_ip": client_ip, "ip_ok": True}
    )


@app.post("/api/punch")
def api_punch():
    db = get_db()
    reconcile_personel_lock(db)
    personnel_id = int(request.form["personnel_id"])
    branch_id = int(request.form["branch_id"])
    action = request.form["action"]

    chk = choice_lock_error_response(db, personnel_id, branch_id)
    if chk is not None:
        return chk

    branch = db.execute(
        "SELECT * FROM branches WHERE id = ? AND active = 1", (branch_id,)
    ).fetchone()
    if not branch:
        return jsonify({"ok": False, "message": "Mağaza bulunamadı."}), 404

    client_ip = get_client_ip()
    ip_ok, reason = store_ip_status(branch, client_ip)
    if reason == "magaza_ipsiz":
        return jsonify(
            {
                "ok": False,
                "message": "Mağaza IP tanımı yapılmamış. Yönetici panelinden IP girilmeli.",
            }
        ), 403
    if not ip_ok:
        return jsonify(
            {
                "ok": False,
                "message": f"Tanınmayan bağlantı. Görünen IP: {client_ip}. Mağazanın çıkış IP’si ile eşleşmiyorsunuz.",
            }
        ), 403

    person = db.execute(
        "SELECT id, full_name, branch_id FROM personnel WHERE id = ? AND active = 1",
        (personnel_id,),
    ).fetchone()
    if not person:
        return jsonify({"ok": False, "message": "Personel bulunamadı."}), 404
    if person["branch_id"] != branch_id:
        return jsonify({"ok": False, "message": "Personel başka mağazaya bağlı."}), 400

    open_record = db.execute(
        """
        SELECT * FROM attendance
        WHERE personnel_id = ? AND branch_id = ? AND checkout_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (personnel_id, branch_id),
    ).fetchone()

    expected = "out" if open_record else "in"
    if action != expected:
        mes = (
            "Şimdi yalnızca çıkış yapılabilir."
            if expected == "out"
            else "Şimdi yalnızca giriş yapılabilir."
        )
        return jsonify({"ok": False, "message": mes}), 400

    if action == "in":
        db.execute(
            """
            INSERT INTO attendance (personnel_id, branch_id, date, checkin_at, source)
            VALUES (?, ?, ?, ?, 'mobile')
            """,
            (
                personnel_id,
                branch_id,
                now_tr().strftime("%Y-%m-%d"),
                now_str(),
            ),
        )
        db.commit()
        session["pdks_choice_lock"] = {
            "personnel_id": personnel_id,
            "branch_id": branch_id,
            "full_name": person["full_name"],
            "branch_name": branch["name"],
        }
        session.modified = True
        return jsonify({"ok": True, "message": f"{person['full_name']}: giriş kaydı alındı."})

    if action == "out":
        ci = _parse_ts_tr(open_record["checkin_at"])
        if not ci:
            return jsonify({"ok": False, "message": "Kayıt hatası (giriş saati)."}), 400
        duration = max(0, int((now_tr() - ci).total_seconds() // 60))
        db.execute(
            """
            UPDATE attendance
            SET checkout_at = ?, duration_minutes = ?
            WHERE id = ?
            """,
            (now_str(), duration, open_record["id"]),
        )
        db.commit()
        session.pop("pdks_choice_lock", None)
        session.modified = True
        return jsonify({"ok": True, "message": f"{person['full_name']}: çıkış kaydı alındı."})

    return jsonify({"ok": False, "message": "Geçersiz işlem."}), 400


@app.get("/rapor/excel")
def export_excel():
    if not require_admin():
        return redirect(url_for("index"))

    db = get_db()
    rows = db.execute(
        """
        SELECT p.full_name AS isim, b.name AS sube, a.date AS tarih,
            COALESCE(a.checkin_at, '-') AS giris, COALESCE(a.checkout_at, '-') AS cikis,
            a.duration_minutes AS dk
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        ORDER BY a.id DESC
        """
    ).fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["İsim", "Mağaza", "Tarih", "Giriş", "Çıkış", "Süre"])
    for row in rows:
        writer.writerow(
            [
                row["isim"],
                row["sube"],
                format_iso_date_tr(row["tarih"]),
                format_display_datetime(row["giris"]) if row["giris"] not in ("-", None) else "—",
                format_display_datetime(row["cikis"]) if row["cikis"] not in ("-", None) else "—",
                format_duration_tr(row["dk"]),
            ]
        )
    resp = Response(output.getvalue(), mimetype="application/vnd.ms-excel")
    resp.headers["Content-Disposition"] = "attachment; filename=pdks_mesai.csv"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
