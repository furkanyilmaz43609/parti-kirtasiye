import csv
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timedelta
from io import StringIO

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

app = Flask(__name__)
app.config["SECRET_KEY"] = "pdks-super-secret-key-2026"
DATABASE = "pdks_merkez.db"
CODE_SECRET = "dynamic-code-secret-branch-2026"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
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

        CREATE TABLE IF NOT EXISTS advances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        );

        CREATE TABLE IF NOT EXISTS finance_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL UNIQUE,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('income', 'expense')),
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            entry_date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'db',
            created_at TEXT NOT NULL
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


@app.before_request
def before_request():
    init_db()


def require_admin():
    return session.get("is_admin") is True


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def generate_dynamic_code(branch_id: int):
    window = int(datetime.now().timestamp() // 20)
    payload = f"{branch_id}:{window}".encode("utf-8")
    digest = hmac.new(CODE_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    code_number = int(digest[:8], 16) % 1000000
    return f"{code_number:06d}"


def fetch_branches():
    db = get_db()
    return db.execute("SELECT * FROM branches ORDER BY name").fetchall()


def fetch_personnel():
    db = get_db()
    return db.execute(
        """
        SELECT p.*, b.name AS branch_name
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        ORDER BY p.full_name
        """
    ).fetchall()


@app.route("/", methods=["GET", "POST"])
def index():
    admin_hash = get_setting("admin_password_hash")
    mode = "setup" if not admin_hash else "login"

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if mode == "setup" and action == "setup_password":
            password = request.form.get("password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()
            if len(password) < 4:
                flash("Sifre en az 4 karakter olmali.")
            elif password != confirm_password:
                flash("Sifre ve tekrar sifre ayni olmali.")
            else:
                set_setting("admin_password_hash", hash_password(password))
                session["is_admin"] = True
                return redirect(url_for("admin"))

        if mode == "login" and action == "login":
            password = request.form.get("password", "").strip()
            if verify_password(password):
                session["is_admin"] = True
                return redirect(url_for("admin"))
            flash("Yonetici sifresi hatali.")

    return render_template("index.html", mode=mode)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not require_admin():
        return redirect(url_for("index"))

    db = get_db()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "add_branch":
            db.execute(
                """
                INSERT INTO branches (name, code, shift_start, shift_end, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request.form["name"].strip(),
                    request.form.get("code", "").strip() or None,
                    request.form.get("shift_start", "09:00"),
                    request.form.get("shift_end", "18:00"),
                    now_str(),
                ),
            )
            db.commit()
            flash("Sube eklendi.")

        elif action == "add_personnel":
            db.execute(
                """
                INSERT INTO personnel (full_name, branch_id, monthly_salary, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    request.form["full_name"].strip(),
                    int(request.form["branch_id"]),
                    float(request.form["monthly_salary"]),
                    now_str(),
                ),
            )
            db.commit()
            flash("Personel eklendi.")

        elif action == "add_advance":
            db.execute(
                "INSERT INTO advances (personnel_id, amount, note, created_at) VALUES (?, ?, ?, ?)",
                (
                    int(request.form["personnel_id"]),
                    float(request.form["amount"]),
                    request.form.get("note", "").strip(),
                    now_str(),
                ),
            )
            db.commit()
            flash("Avans kaydi eklendi.")

        elif action == "add_note":
            content = request.form.get("content", "").strip()
            if content:
                db.execute(
                    "INSERT INTO announcements (content, created_at) VALUES (?, ?)",
                    (content, now_str()),
                )
                db.commit()
                flash("Duyuru yayinlandi.")

        elif action == "add_finance":
            db.execute(
                """
                INSERT OR IGNORE INTO finance_entries
                (uid, entry_type, amount, category, description, entry_date, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'admin_form', ?)
                """,
                (
                    request.form.get("uid", os.urandom(6).hex()),
                    request.form["entry_type"],
                    float(request.form["amount"]),
                    request.form["category"].strip(),
                    request.form.get("description", "").strip(),
                    request.form.get("entry_date") or datetime.now().strftime("%Y-%m-%d"),
                    now_str(),
                ),
            )
            db.commit()
            flash("Gelir/Gider kaydi eklendi.")

        return redirect(url_for("admin"))

    branches = fetch_branches()
    personnel = fetch_personnel()

    attendance_rows = db.execute(
        """
        SELECT a.*, p.full_name, b.name AS branch_name
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        ORDER BY a.id DESC
        LIMIT 300
        """
    ).fetchall()

    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    payroll = db.execute(
        """
        SELECT
            p.id,
            p.full_name,
            p.monthly_salary,
            b.name AS branch_name,
            COALESCE(SUM(a.duration_minutes), 0) AS worked_minutes,
            (
                SELECT COALESCE(SUM(amount), 0)
                FROM advances av
                WHERE av.personnel_id = p.id
                AND substr(av.created_at, 1, 7) = ?
            ) AS total_advance
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        LEFT JOIN attendance a
            ON a.personnel_id = p.id
            AND a.checkin_at >= ?
            AND a.checkin_at IS NOT NULL
        GROUP BY p.id
        ORDER BY p.full_name
        """,
        (month_start.strftime("%Y-%m"), month_start.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()

    finance_rows = db.execute(
        "SELECT * FROM finance_entries ORDER BY entry_date DESC, id DESC LIMIT 500"
    ).fetchall()

    total_income = sum(x["amount"] for x in finance_rows if x["entry_type"] == "income")
    total_expense = sum(x["amount"] for x in finance_rows if x["entry_type"] == "expense")
    latest_note = db.execute(
        "SELECT content, created_at FROM announcements ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return render_template(
        "admin.html",
        branches=branches,
        personnel=personnel,
        attendance_rows=attendance_rows,
        payroll=payroll,
        finance_rows=finance_rows,
        total_income=total_income,
        total_expense=total_expense,
        net_balance=total_income - total_expense,
        latest_note=latest_note,
        month_start=month_start.strftime("%Y-%m"),
    )


@app.route("/sube/<int:branch_id>/ekran")
def branch_screen(branch_id):
    db = get_db()
    branch = db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if not branch:
        return "Sube bulunamadi.", 404
    return render_template("ekran.html", branch=branch, code=generate_dynamic_code(branch_id))


@app.route("/tara")
def tara():
    db = get_db()
    branches = fetch_branches()
    personnel = fetch_personnel()
    latest_note = db.execute(
        "SELECT content, created_at FROM announcements ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return render_template(
        "tara.html", branches=branches, personnel=personnel, latest_note=latest_note
    )


@app.post("/api/punch")
def api_punch():
    db = get_db()
    personnel_id = int(request.form["personnel_id"])
    branch_id = int(request.form["branch_id"])
    action = request.form["action"]
    code = request.form["code"].strip()

    if code != generate_dynamic_code(branch_id):
        return jsonify({"ok": False, "message": "Kod gecersiz veya suresi doldu."}), 400

    person = db.execute(
        "SELECT id, full_name, branch_id FROM personnel WHERE id = ? AND active = 1",
        (personnel_id,),
    ).fetchone()
    if not person:
        return jsonify({"ok": False, "message": "Personel bulunamadi."}), 404

    if person["branch_id"] != branch_id:
        return jsonify({"ok": False, "message": "Personel farkli subeye bagli."}), 400

    open_record = db.execute(
        """
        SELECT * FROM attendance
        WHERE personnel_id = ? AND branch_id = ? AND checkout_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (personnel_id, branch_id),
    ).fetchone()

    if action == "in":
        if open_record:
            return jsonify({"ok": False, "message": "Cikis yapmadan tekrar giris yapilamaz."}), 400
        db.execute(
            """
            INSERT INTO attendance (personnel_id, branch_id, date, checkin_at, source)
            VALUES (?, ?, ?, ?, 'mobile')
            """,
            (
                personnel_id,
                branch_id,
                datetime.now().strftime("%Y-%m-%d"),
                now_str(),
            ),
        )
        db.commit()
        return jsonify({"ok": True, "message": f"{person['full_name']} icin giris kaydedildi."})

    if action == "out":
        if not open_record:
            return jsonify({"ok": False, "message": "Cikis icin aktif giris bulunamadi."}), 400
        checkin_at = datetime.strptime(open_record["checkin_at"], "%Y-%m-%d %H:%M:%S")
        duration = max(0, int((datetime.now() - checkin_at).total_seconds() // 60))
        db.execute(
            """
            UPDATE attendance
            SET checkout_at = ?, duration_minutes = ?
            WHERE id = ?
            """,
            (now_str(), duration, open_record["id"]),
        )
        db.commit()
        return jsonify({"ok": True, "message": f"{person['full_name']} icin cikis kaydedildi."})

    return jsonify({"ok": False, "message": "Islem tipi hatali."}), 400


@app.get("/api/sube/<int:branch_id>/kod")
def api_branch_code(branch_id):
    db = get_db()
    branch = db.execute("SELECT id FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if not branch:
        return jsonify({"ok": False, "message": "Sube bulunamadi."}), 404
    return jsonify(
        {
            "ok": True,
            "code": generate_dynamic_code(branch_id),
            "refresh_seconds": 20,
            "server_time": now_str(),
        }
    )


@app.post("/api/finance/sync")
def api_finance_sync():
    db = get_db()
    payload = request.get_json(silent=True) or {}
    entries = payload.get("entries", [])

    for item in entries:
        uid = str(item.get("uid", "")).strip()
        if not uid:
            continue
        entry_type = item.get("entry_type")
        if entry_type not in ("income", "expense"):
            continue
        try:
            amount = float(item.get("amount", 0))
        except (TypeError, ValueError):
            continue
        category = str(item.get("category", "Genel")).strip() or "Genel"
        description = str(item.get("description", "")).strip()
        entry_date = str(item.get("entry_date", datetime.now().strftime("%Y-%m-%d"))).strip()
        db.execute(
            """
            INSERT OR IGNORE INTO finance_entries
            (uid, entry_type, amount, category, description, entry_date, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'localStorage', ?)
            """,
            (uid, entry_type, amount, category, description, entry_date, now_str()),
        )

    db.commit()
    rows = db.execute(
        "SELECT uid, entry_type, amount, category, description, entry_date FROM finance_entries"
    ).fetchall()
    return jsonify({"ok": True, "entries": [dict(row) for row in rows]})


@app.get("/api/dashboard")
def api_dashboard():
    db = get_db()
    rows = db.execute(
        "SELECT entry_type, amount, category, entry_date FROM finance_entries ORDER BY entry_date ASC"
    ).fetchall()

    total_income = sum(r["amount"] for r in rows if r["entry_type"] == "income")
    total_expense = sum(r["amount"] for r in rows if r["entry_type"] == "expense")

    today = datetime.now().date()
    weekly_points = []
    for day_offset in range(6, -1, -1):
        day = today - timedelta(days=day_offset)
        day_str = day.strftime("%Y-%m-%d")
        income = sum(
            r["amount"]
            for r in rows
            if r["entry_type"] == "income" and r["entry_date"] == day_str
        )
        expense = sum(
            r["amount"]
            for r in rows
            if r["entry_type"] == "expense" and r["entry_date"] == day_str
        )
        weekly_points.append({"date": day_str, "income": income, "expense": expense})

    expense_by_category = {}
    for r in rows:
        if r["entry_type"] == "expense":
            expense_by_category[r["category"]] = expense_by_category.get(r["category"], 0) + r["amount"]

    return jsonify(
        {
            "ok": True,
            "totals": {
                "income": total_income,
                "expense": total_expense,
                "net": total_income - total_expense,
            },
            "weekly": weekly_points,
            "expense_by_category": expense_by_category,
        }
    )


@app.get("/rapor/excel")
def export_excel():
    if not require_admin():
        return redirect(url_for("index"))

    db = get_db()
    rows = db.execute(
        """
        SELECT
            p.full_name AS isim,
            b.name AS sube,
            a.date AS tarih,
            COALESCE(a.checkin_at, '-') AS giris_saati,
            COALESCE(a.checkout_at, '-') AS cikis_saati,
            a.duration_minutes AS sure_dk
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        ORDER BY a.id DESC
        """
    ).fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Isim", "Sube", "Tarih", "Giris Saati", "Cikis Saati", "Sure (dk)"])
    for row in rows:
        writer.writerow(
            [
                row["isim"],
                row["sube"],
                row["tarih"],
                row["giris_saati"],
                row["cikis_saati"],
                row["sure_dk"],
            ]
        )

    response = Response(output.getvalue(), mimetype="application/vnd.ms-excel")
    response.headers["Content-Disposition"] = "attachment; filename=pdks_rapor.csv"
    return response


if __name__ == "__main__":
    app.run(debug=True)
