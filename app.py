import csv
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import IntegrityError as PgIntegrityError
except Exception:  # pragma: no cover
    psycopg2 = None
    PgIntegrityError = Exception

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

from pdks_logic import (
    LEAVE_TYPE_LABELS,
    LEAVE_TYPES,
    attendance_counts_as_work,
    day_in_range,
    duplicate_name_groups,
    format_duration_tr as logic_format_duration_tr,
    iter_dates,
    minutes_from_attendance_row,
    missing_minutes_for_day,
    parse_hhmm as logic_parse_hhmm,
    shift_length_minutes,
    suggest_next_code,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TR = ZoneInfo("Europe/Istanbul")

# Mesai çıkış saatinden bu kadar dakika sonra hâlâ açık giriş varsa otomatik çıkış.
SHIFT_END_AUTO_CHECKOUT_GRACE_MIN = 30
_AUTO_CLOSE_MIN_INTERVAL_SEC = 45.0
_last_auto_close_wallclock = 0.0

MIN_PASSWORD_LEN = 8
ADMIN_SESSION_IDLE_SEC = 8 * 60 * 60  # 8 saat işlem yoksa oturum düşer
# Not: IP whitelist tek başına konum doğrulaması değildir; ileride QR / cihaz
# eşleştirme gibi ikinci katman değerlendirilebilir (cihaz bağlama zaten kısmen var).

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "pdks-local-dev-change-me")

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
if DATABASE_URL:
    # Neon bazen channel_binding=require ekler; psycopg2 ile sorun çıkarabilir
    DATABASE_URL = (
        DATABASE_URL.replace("&channel_binding=require", "")
        .replace("?channel_binding=require&", "?")
        .replace("?channel_binding=require", "")
    )
DATABASE = os.path.abspath(os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "pdks_merkez.db")))

_SCHEMA_READY = False
_RENDER_HOSTED = bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"))
if _RENDER_HOSTED:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def redirect_after_admin_login(request_next: str):
    target = (request_next or "").strip()
    if (
        target.startswith("/")
        and not target.startswith("//")
        and "\r" not in target
        and "\n" not in target
        and (target == "/admin" or target.startswith("/admin?") or target.startswith("/admin/"))
    ):
        return redirect(target)
    return redirect(url_for("admin"))


class DB:
    def __init__(self, backend: str, conn):
        self.backend = backend
        self.conn = conn

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def execute(self, query: str, params=()):
        if self.backend == "sqlite":
            return self.conn.execute(query, params)
        q = query.replace("?", "%s")
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(q, params)
        except Exception:
            self.conn.rollback()
            raise
        return cur

    def executescript(self, script: str):
        if self.backend == "sqlite":
            return self.conn.executescript(script)
        cur = self.conn.cursor()
        try:
            for stmt in [s.strip() for s in script.split(";") if s.strip()]:
                cur.execute(stmt)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return cur


def get_db():
    if "db" not in g:
        if DATABASE_URL:
            if psycopg2 is None:
                raise RuntimeError("psycopg2 is required when DATABASE_URL is set")
            conn = psycopg2.connect(DATABASE_URL)
            g.db = DB("postgres", conn)
        else:
            db_dir = os.path.dirname(DATABASE)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            g.db = DB("sqlite", conn)
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _safe_add_column(db, table: str, column: str, coltype: str):
    cols = _table_columns(db, table)
    if column in cols:
        return
    try:
        if db.backend == "postgres":
            db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
        else:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        db.commit()
    except Exception:
        db.rollback()
        # Başka worker eklemiş olabilir; tekrar kontrol
        if column not in _table_columns(db, table):
            raise


def init_db():
    global _SCHEMA_READY
    db = get_db()
    if _SCHEMA_READY:
        return
    if db.backend == "postgres":
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS branches (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                code TEXT,
                shift_start TEXT NOT NULL DEFAULT '09:00',
                shift_end TEXT NOT NULL DEFAULT '18:00',
                allowed_ip TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS personnel (
                id SERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                employee_code TEXT UNIQUE,
                code_confirmed INTEGER NOT NULL DEFAULT 0,
                branch_id INTEGER NOT NULL REFERENCES branches(id),
                monthly_salary DOUBLE PRECISION NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                personnel_id INTEGER NOT NULL REFERENCES personnel(id),
                branch_id INTEGER NOT NULL REFERENCES branches(id),
                date TEXT NOT NULL,
                checkin_at TEXT,
                checkout_at TEXT,
                duration_minutes INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'mobile',
                auto_closed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id SERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS device_bindings (
                token_hash TEXT PRIMARY KEY,
                personnel_id INTEGER NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS personnel_leaves (
                id SERIAL PRIMARY KEY,
                personnel_id INTEGER NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                leave_type TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS branch_holidays (
                id SERIAL PRIMARY KEY,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                title TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT
            );
            """
        )
    else:
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
                employee_code TEXT UNIQUE,
                code_confirmed INTEGER NOT NULL DEFAULT 0,
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
                auto_closed INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (personnel_id) REFERENCES personnel(id),
                FOREIGN KEY (branch_id) REFERENCES branches(id)
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS device_bindings (
                token_hash TEXT PRIMARY KEY,
                personnel_id INTEGER NOT NULL,
                branch_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY (personnel_id) REFERENCES personnel(id) ON DELETE CASCADE,
                FOREIGN KEY (branch_id) REFERENCES branches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS personnel_leaves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                personnel_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                leave_type TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (personnel_id) REFERENCES personnel(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS branch_holidays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                title TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (branch_id) REFERENCES branches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT
            );
            """
        )
    db.commit()
    _migrate_schema(db)
    _SCHEMA_READY = True


def _table_columns(db, table: str) -> list[str]:
    if db.backend == "postgres":
        try:
            cur = db.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ?
                """,
                (table,),
            )
            return [r["column_name"] for r in cur.fetchall()]
        except Exception:
            db.rollback()
            return []
    return [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_schema(db):
    try:
        _safe_add_column(db, "branches", "allowed_ip", "TEXT")
        _safe_add_column(db, "branches", "active", "INTEGER NOT NULL DEFAULT 1")
        try:
            db.execute("UPDATE branches SET active = 1 WHERE active IS NULL")
            db.commit()
        except Exception:
            db.rollback()

        _safe_add_column(db, "personnel", "employee_code", "TEXT")
        _safe_add_column(db, "personnel", "code_confirmed", "INTEGER NOT NULL DEFAULT 0")
        _safe_add_column(db, "attendance", "auto_closed", "INTEGER NOT NULL DEFAULT 0")

        try:
            db.execute(
                """
                UPDATE attendance SET auto_closed = 1
                WHERE source = 'auto' AND (auto_closed IS NULL OR auto_closed = 0)
                """
            )
            db.commit()
        except Exception:
            db.rollback()

        # Ek tablolar (CREATE IF NOT EXISTS)
        if db.backend == "postgres":
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS personnel_leaves (
                    id SERIAL PRIMARY KEY,
                    personnel_id INTEGER NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    leave_type TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branch_holidays (
                    id SERIAL PRIMARY KEY,
                    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    title TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id SERIAL PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT
                );
                """
            )
        else:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS personnel_leaves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    personnel_id INTEGER NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    leave_type TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (personnel_id) REFERENCES personnel(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS branch_holidays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id INTEGER NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (branch_id) REFERENCES branches(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT
                );
                """
            )

        # Tekil isimlere otomatik kod (aynı isimler elle)
        people = db.execute(
            "SELECT id, full_name, employee_code, code_confirmed FROM personnel ORDER BY id"
        ).fetchall()
        by_name: dict[str, list] = {}
        for p in people:
            key = (p["full_name"] or "").strip().casefold()
            by_name.setdefault(key, []).append(p)
        used = {
            (p["employee_code"] or "").strip().upper()
            for p in people
            if (p["employee_code"] or "").strip()
        }
        next_n = 1
        for _key, group in by_name.items():
            if len(group) != 1:
                continue
            p = group[0]
            if (p["employee_code"] or "").strip():
                if not int(p["code_confirmed"] or 0):
                    try:
                        db.execute(
                            "UPDATE personnel SET code_confirmed = 1 WHERE id = ?",
                            (p["id"],),
                        )
                    except Exception:
                        db.rollback()
                continue
            while f"P{next_n:04d}" in used:
                next_n += 1
            code = f"P{next_n:04d}"
            used.add(code)
            next_n += 1
            try:
                db.execute(
                    "UPDATE personnel SET employee_code = ?, code_confirmed = 1 WHERE id = ?",
                    (code, p["id"]),
                )
            except Exception:
                db.rollback()
                used.discard(code)
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("schema migration failed")
        raise


@app.before_request
def before_request():
    path = request.path or ""
    # Teşhis ve sağlık: before_request yüzünden 500 olmasın
    if path in ("/diag", "/health") or path.startswith("/static"):
        return None
    try:
        init_db()
    except Exception:
        app.logger.exception("init_db failed")
        raise
    ep = request.endpoint or ""
    if session.get("is_admin") and ep not in ("index", "logout"):
        last = session.get("admin_last_active")
        now_ts = time.time()
        if last is not None and (now_ts - float(last)) > ADMIN_SESSION_IDLE_SEC:
            session.clear()
            flash("Oturum süresi doldu. Lütfen yeniden giriş yapın.", "warning")
            return redirect(url_for("index", next=request.path))
        session["admin_last_active"] = now_ts
        session.modified = True
    try:
        auto_close_stale_checkouts(get_db())
    except Exception:
        app.logger.exception("auto_close_stale_checkouts")
    return None


def require_admin():
    return session.get("is_admin") is True


def now_tr():
    return datetime.now(TR)


def now_str():
    return now_tr().strftime("%Y-%m-%d %H:%M:%S")


def branch_shift_deadline_tr(day_str: str, shift_end_hhmm: str | None) -> datetime | None:
    """Mağaza günü için mesai çıkışı + SHIFT_END_AUTO_CHECKOUT_GRACE_MIN (Europe/Istanbul)."""
    se = parse_hhmm(shift_end_hhmm) or "18:00"
    try:
        base = datetime.strptime(f"{day_str} {se}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return base.replace(tzinfo=TR) + timedelta(minutes=SHIFT_END_AUTO_CHECKOUT_GRACE_MIN)


def auto_close_stale_checkouts(db):
    """Açık giriş kayıtlarını mesai bitişi + tolerans sonrası kapatır (siteye gelen isteklerde, seyrek)."""
    global _last_auto_close_wallclock
    wall = time.time()
    if wall - _last_auto_close_wallclock < _AUTO_CLOSE_MIN_INTERVAL_SEC:
        return
    _last_auto_close_wallclock = wall

    now = now_tr()
    today_s = now.strftime("%Y-%m-%d")
    rows = db.execute(
        """
        SELECT a.id, a.date, a.checkin_at, b.shift_end
        FROM attendance a
        JOIN branches b ON b.id = a.branch_id
        WHERE a.checkout_at IS NULL
          AND a.checkin_at IS NOT NULL
          AND a.date <= ?
        """,
        (today_s,),
    ).fetchall()

    for row in rows:
        day_str = row["date"]
        deadline = branch_shift_deadline_tr(day_str, row["shift_end"])
        if not deadline:
            continue
        ci = _parse_ts_tr(row["checkin_at"])
        if not ci:
            continue

        if day_str == today_s:
            if now < deadline:
                continue
        else:
            # Önceki günlerden kalan açık kayıt: ilk uygun istekte kapat.
            pass

        if ci > deadline:
            co = now
        else:
            co = deadline

        checkout_s = co.strftime("%Y-%m-%d %H:%M:%S")
        duration = _minutes_between(ci, co)
        db.execute(
            """
            UPDATE attendance
            SET checkout_at = ?, duration_minutes = ?, source = 'auto', auto_closed = 1
            WHERE id = ? AND checkout_at IS NULL
            """,
            (checkout_s, duration, row["id"]),
        )
    db.commit()


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
    return logic_format_duration_tr(minutes)


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


def parse_iso_date(value: str | None):
    s = (value or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


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


def parse_hhmm(raw: str | None):
    return logic_parse_hhmm(raw)


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


def hash_device_token(token: str) -> str:
    payload = f"{app.config['SECRET_KEY']}::device::{token}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_device_binding(db):
    token = (request.cookies.get("pdks_device_token") or "").strip()
    if not token:
        return None
    token_hash = hash_device_token(token)
    row = db.execute(
        """
        SELECT d.personnel_id, d.branch_id, p.full_name, b.name AS branch_name
        FROM device_bindings d
        JOIN personnel p ON p.id = d.personnel_id
        JOIN branches b ON b.id = d.branch_id
        WHERE d.token_hash = ? AND p.active = 1 AND b.active = 1
        """,
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    db.execute(
        "UPDATE device_bindings SET last_seen_at = ? WHERE token_hash = ?",
        (now_str(), token_hash),
    )
    db.commit()
    return row


def bind_device_for_personnel(db, personnel_id: int, branch_id: int):
    token = secrets.token_urlsafe(32)
    token_hash = hash_device_token(token)
    db.execute(
        """
        INSERT INTO device_bindings (token_hash, personnel_id, branch_id, created_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token_hash, personnel_id, branch_id, now_str(), now_str()),
    )
    db.commit()
    return token


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
        ORDER BY p.employee_code, p.full_name
        """
    ).fetchall()


def fetch_personnel_admin():
    db = get_db()
    if db.backend == "postgres":
        return db.execute(
            """
            SELECT p.*, b.name AS branch_name
            FROM personnel p
            JOIN branches b ON b.id = p.branch_id
            ORDER BY p.employee_code NULLS LAST, p.full_name
            """
        ).fetchall()
    return db.execute(
        """
        SELECT p.*, b.name AS branch_name
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        ORDER BY CASE WHEN p.employee_code IS NULL OR p.employee_code = '' THEN 1 ELSE 0 END,
                 p.employee_code, p.full_name
        """
    ).fetchall()


def person_label(row) -> str:
    try:
        code = row["employee_code"]
    except (KeyError, TypeError, IndexError):
        code = None
    name = row["full_name"]
    code = (str(code).strip() if code is not None else "")
    return f"[{code}] {name}" if code else name


def write_audit(db, action: str, detail: str = "", actor: str = "admin"):
    db.execute(
        "INSERT INTO audit_log (created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
        (now_str(), actor, action, detail or ""),
    )
    db.commit()


def next_employee_code(db) -> str:
    rows = db.execute("SELECT employee_code FROM personnel").fetchall()
    return suggest_next_code([r["employee_code"] for r in rows])


def personnel_needing_manual_codes(db):
    """Aynı isimli veya kodu olmayan personeller (manuel eşleştirme gerekir)."""
    people = fetch_personnel_admin()
    dups = duplicate_name_groups(people)
    dup_ids = {int(p["id"]) for g in dups for p in g}
    missing = []
    for p in people:
        code = (p["employee_code"] or "").strip()
        confirmed = int(p["code_confirmed"] or 0) == 1
        if not code or (int(p["id"]) in dup_ids and not confirmed):
            missing.append(p)
    return missing, dups


def is_leave_or_holiday(db, personnel_id: int, branch_id: int, day_s: str) -> bool:
    pl = db.execute(
        """
        SELECT id FROM personnel_leaves
        WHERE personnel_id = ? AND start_date <= ? AND end_date >= ?
        LIMIT 1
        """,
        (personnel_id, day_s, day_s),
    ).fetchone()
    if pl:
        return True
    bh = db.execute(
        """
        SELECT id FROM branch_holidays
        WHERE branch_id = ? AND start_date <= ? AND end_date >= ?
        LIMIT 1
        """,
        (branch_id, day_s, day_s),
    ).fetchone()
    return bool(bh)


def actual_work_minutes_on_day(db, personnel_id: int, day_s: str) -> int:
    rows = db.execute(
        """
        SELECT date, checkin_at, checkout_at, duration_minutes, source, auto_closed
        FROM attendance
        WHERE personnel_id = ? AND date = ?
        """,
        (personnel_id, day_s),
    ).fetchall()
    now = now_tr()
    today_s = now.strftime("%Y-%m-%d")
    total = 0
    for r in rows:
        total += minutes_from_attendance_row(
            r,
            today_s=today_s,
            now=now,
            parse_ts_tr=_parse_ts_tr,
            minutes_between=_minutes_between,
        )
    return total


def count_leave_days_in_range(db, personnel_id: int, branch_id: int, start_d, end_d) -> int:
    n = 0
    for d in iter_dates(start_d, end_d):
        if is_leave_or_holiday(db, personnel_id, branch_id, d.strftime("%Y-%m-%d")):
            n += 1
    return n


def personnel_missing_stats(db, personnel_id: int, start_d, end_d):
    """Dönemsel eksik süre + izin günü sayısı + (ikincil) fiili çalışma."""
    prow = db.execute(
        """
        SELECT p.*, b.shift_start, b.shift_end, b.name AS branch_name
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        WHERE p.id = ?
        """,
        (personnel_id,),
    ).fetchone()
    if not prow:
        return None

    missing_total = 0
    worked_total = 0
    leave_days = 0
    counted_days = 0
    auto_closed_days = 0

    for d in iter_dates(start_d, end_d):
        day_s = d.strftime("%Y-%m-%d")
        if is_leave_or_holiday(db, personnel_id, int(prow["branch_id"]), day_s):
            leave_days += 1
            continue
        actual = actual_work_minutes_on_day(db, personnel_id, day_s)
        # Otomatik kapanmış kayıtları olan günlerde fiili süre 0 sayılır (yukarıda)
        ac = db.execute(
            """
            SELECT COUNT(*) AS c FROM attendance
            WHERE personnel_id = ? AND date = ? AND auto_closed = 1
            """,
            (personnel_id, day_s),
        ).fetchone()
        if ac and int(ac["c"] or 0) > 0 and actual == 0:
            auto_closed_days += 1
        miss = missing_minutes_for_day(
            day_s=day_s,
            shift_start=prow["shift_start"],
            shift_end=prow["shift_end"],
            is_leave=False,
            actual_minutes=actual,
        )
        if miss is None:
            continue
        missing_total += miss
        worked_total += actual
        counted_days += 1

    return {
        "personnel_id": personnel_id,
        "full_name": prow["full_name"],
        "employee_code": prow["employee_code"] or "",
        "branch_id": int(prow["branch_id"]),
        "branch_name": prow["branch_name"],
        "label": person_label(prow),
        "missing_minutes": missing_total,
        "missing_hm": format_duration_tr(missing_total),
        "worked_minutes": worked_total,
        "worked_hm": format_duration_tr(worked_total),
        "leave_days": leave_days,
        "counted_days": counted_days,
        "auto_closed_days": auto_closed_days,
        "shift_hm": format_duration_tr(
            shift_length_minutes(prow["shift_start"], prow["shift_end"])
        ),
    }


def period_missing_report(db, start_d, end_d, branch_id=None):
    q = """
        SELECT p.*, b.name AS branch_name, b.shift_start, b.shift_end
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        WHERE p.active = 1
    """
    params: list = []
    if branch_id:
        q += " AND p.branch_id = ?"
        params.append(branch_id)
    q += " ORDER BY p.employee_code, p.full_name"
    people = db.execute(q, tuple(params)).fetchall()
    rows = []
    for p in people:
        st = personnel_missing_stats(db, int(p["id"]), start_d, end_d)
        if st:
            rows.append(st)
    rows.sort(key=lambda r: (-r["missing_minutes"], r["label"].lower()))
    return rows


def branch_shift_moment_on_day_tr(day_str: str, hhmm: str | None, default_hhmm: str) -> datetime | None:
    t = parse_hhmm(hhmm) or default_hhmm
    try:
        return datetime.strptime(f"{day_str} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=TR)
    except ValueError:
        return None


def missing_today_batch(db, today_d):
    """Bugünkü eksik süre listesi — tek seferde toplu sorgu (admin hızlı açılsın)."""
    today_s = today_d.strftime("%Y-%m-%d")
    now = now_tr()
    people = db.execute(
        """
        SELECT p.id AS personnel_id, p.full_name, p.employee_code, p.branch_id,
               b.name AS branch_name, b.shift_start, b.shift_end
        FROM personnel p
        JOIN branches b ON b.id = p.branch_id
        WHERE p.active = 1
        """
    ).fetchall()
    leave_pids = {
        int(r["personnel_id"])
        for r in db.execute(
            "SELECT personnel_id FROM personnel_leaves WHERE start_date <= ? AND end_date >= ?",
            (today_s, today_s),
        ).fetchall()
    }
    holiday_branches = {
        int(r["branch_id"])
        for r in db.execute(
            "SELECT branch_id FROM branch_holidays WHERE start_date <= ? AND end_date >= ?",
            (today_s, today_s),
        ).fetchall()
    }
    actual_by_pid: dict[int, int] = {}
    for r in db.execute(
        """
        SELECT personnel_id, date, checkin_at, checkout_at, duration_minutes, source, auto_closed
        FROM attendance WHERE date = ?
        """,
        (today_s,),
    ).fetchall():
        pid = int(r["personnel_id"])
        actual_by_pid[pid] = actual_by_pid.get(pid, 0) + minutes_from_attendance_row(
            r,
            today_s=today_s,
            now=now,
            parse_ts_tr=_parse_ts_tr,
            minutes_between=_minutes_between,
        )
    out = []
    for p in people:
        pid = int(p["personnel_id"])
        bid = int(p["branch_id"])
        if pid in leave_pids or bid in holiday_branches:
            continue
        miss = missing_minutes_for_day(
            day_s=today_s,
            shift_start=p["shift_start"],
            shift_end=p["shift_end"],
            is_leave=False,
            actual_minutes=actual_by_pid.get(pid, 0),
        )
        if miss and miss > 0:
            out.append(
                {
                    "personnel_id": pid,
                    "full_name": p["full_name"],
                    "employee_code": p["employee_code"] or "",
                    "branch_name": p["branch_name"],
                    "label": person_label(p),
                    "missing_minutes": miss,
                    "missing_hm": format_duration_tr(miss),
                }
            )
    out.sort(key=lambda x: (-x["missing_minutes"], x["label"].lower()))
    return out


def fetch_today_dashboard_summary(db):
    """Bugün: içeridekiler + eksik saat odaklı özet (geç kalma eksik süreye dahildir)."""
    now = now_tr()
    today_s = now.strftime("%Y-%m-%d")
    today_d = now.date()

    inside_rows = db.execute(
        """
        SELECT p.id AS personnel_id, p.full_name, p.employee_code, b.name AS branch_name,
               a.checkin_at, b.shift_end
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        WHERE a.date = ? AND a.checkout_at IS NULL AND a.checkin_at IS NOT NULL
        ORDER BY p.employee_code, p.full_name
        """,
        (today_s,),
    ).fetchall()

    inside = [
        {
            "personnel_id": r["personnel_id"],
            "full_name": r["full_name"],
            "employee_code": r["employee_code"] or "",
            "label": person_label(r),
            "branch_name": r["branch_name"],
            "checkin_label": format_display_datetime(r["checkin_at"]),
        }
        for r in inside_rows
    ]

    past_shift_inside = []
    for r in inside_rows:
        end_m = branch_shift_moment_on_day_tr(today_s, r["shift_end"], "18:00")
        if end_m and now > end_m:
            past_shift_inside.append(
                {
                    "label": person_label(r),
                    "branch_name": r["branch_name"],
                    "checkin_label": format_display_datetime(r["checkin_at"]),
                }
            )

    # Bugün eksik süresi > 0 olan personeller (izinli değil)
    missing_today = missing_today_batch(db, today_d)

    return {
        "date_label": format_iso_date_tr(today_s),
        "inside": inside,
        "inside_count": len(inside),
        "missing_today": missing_today,
        "past_shift_inside": past_shift_inside,
    }


def personnel_work_stats(db, personnel_id: int):
    now = now_tr()
    today_d = now.date()
    mon_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date()
    week_start = today_d - timedelta(days=today_d.weekday())
    week_end = week_start + timedelta(days=6)

    today_st = personnel_missing_stats(db, personnel_id, today_d, today_d)
    week_st = personnel_missing_stats(db, personnel_id, week_start, week_end)
    month_st = personnel_missing_stats(db, personnel_id, mon_start, today_d)

    def pack(st):
        if not st:
            return {
                "missing_hm": "—",
                "worked_hm": "—",
                "leave_days": 0,
                "missing_minutes": 0,
            }
        return st

    t, w, m = pack(today_st), pack(week_st), pack(month_st)
    return {
        "today_missing_hm": t["missing_hm"] if t.get("leave_days") == 0 else "İzinli",
        "today_worked_hm": t.get("worked_hm", "—"),
        "today_leave": bool(t.get("leave_days")),
        "week_missing_hm": w["missing_hm"],
        "week_worked_hm": w.get("worked_hm", "—"),
        "week_leave_days": w.get("leave_days", 0),
        "month_missing_hm": m["missing_hm"],
        "month_worked_hm": m.get("worked_hm", "—"),
        "month_leave_days": m.get("leave_days", 0),
        "leave_days_month": m.get("leave_days", 0),
    }


def personnel_work_stats_range(db, personnel_id: int, start_date, end_date):
    st = personnel_missing_stats(db, personnel_id, start_date, end_date)
    if not st:
        return None
    return {
        "range_missing_hm": st["missing_hm"],
        "range_worked_hm": st["worked_hm"],
        "range_leave_days": st["leave_days"],
        "range_days": st["counted_days"],
        "start_label": format_iso_date_tr(start_date.strftime("%Y-%m-%d")),
        "end_label": format_iso_date_tr(end_date.strftime("%Y-%m-%d")),
        "auto_closed_days": st["auto_closed_days"],
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
            if len(password) < MIN_PASSWORD_LEN:
                flash(f"Şifre en az {MIN_PASSWORD_LEN} karakter olmalı.", "danger")
            elif password != confirm_password:
                flash("Şifre ile tekrar eşleşmiyor.", "danger")
            else:
                set_setting("admin_password_hash", hash_password(password))
                session["is_admin"] = True
                session.permanent = True
                session["admin_last_active"] = time.time()
                write_audit(get_db(), "admin_password_setup", "İlk şifre belirlendi")
                return redirect_after_admin_login(next_url)

        if mode == "login" and action == "login":
            password = request.form.get("password", "").strip()
            if verify_password(password):
                session["is_admin"] = True
                session.permanent = True
                session["admin_last_active"] = time.time()
                return redirect_after_admin_login(next_url)
            flash("Yönetici şifresi hatalı.", "danger")

    return render_template("index.html", mode=mode, next=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    import traceback

    if not require_admin():
        flash("Önce giriş yapın.", "info")
        return redirect(url_for("index", next=request.path))

    try:
        return _admin_impl()
    except Exception:
        app.logger.exception("admin_failed")
        return Response(
            "ADMIN HATA\n\n" + traceback.format_exc(),
            status=500,
            mimetype="text/plain; charset=utf-8",
        )


def _admin_impl():
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
                    write_audit(db, "add_branch", f"Mağaza: {name}")
                    flash("Mağaza eklendi.", "success")
                except (sqlite3.IntegrityError, PgIntegrityError):
                    flash("Bu isimde mağaza zaten var.", "danger")

        elif action == "delete_branch":
            bid = int(request.form["branch_id"])
            db.execute("DELETE FROM branch_holidays WHERE branch_id = ?", (bid,))
            db.execute("DELETE FROM attendance WHERE branch_id = ?", (bid,))
            db.execute("DELETE FROM personnel_leaves WHERE personnel_id IN (SELECT id FROM personnel WHERE branch_id = ?)", (bid,))
            db.execute("DELETE FROM personnel WHERE branch_id = ?", (bid,))
            db.execute("DELETE FROM branches WHERE id = ?", (bid,))
            db.commit()
            write_audit(db, "delete_branch", f"branch_id={bid}")
            flash("Mağaza ve bağlı kayıtlar silindi.", "success")

        elif action == "set_branch_ip":
            bid = int(request.form["branch_id"])
            raw = request.form.get("allowed_ip", "").strip()
            if not raw:
                flash("IP alanı boş bırakılamaz.", "danger")
            else:
                db.execute("UPDATE branches SET allowed_ip = ? WHERE id = ?", (raw, bid))
                db.commit()
                write_audit(db, "set_branch_ip", f"branch_id={bid} ip={raw}")
                flash("Mağaza IP güncellendi.", "success")

        elif action == "add_personnel":
            full_name = request.form["full_name"].strip()
            branch_id = int(request.form["branch_id"])
            code = (request.form.get("employee_code") or "").strip().upper()
            if not code:
                code = next_employee_code(db)
            try:
                db.execute(
                    """
                    INSERT INTO personnel (full_name, employee_code, code_confirmed, branch_id, monthly_salary, active, created_at)
                    VALUES (?, ?, 1, ?, 0, 1, ?)
                    """,
                    (full_name, code, branch_id, now_str()),
                )
                db.commit()
                write_audit(db, "add_personnel", f"{code} {full_name} branch={branch_id}")
                flash(f"Personel eklendi. Kod: {code}", "success")
            except (sqlite3.IntegrityError, PgIntegrityError):
                flash("Bu personel kodu zaten kullanılıyor. Farklı bir kod girin.", "danger")

        elif action == "delete_personnel":
            pid = int(request.form["personnel_id"])
            db.execute("DELETE FROM personnel_leaves WHERE personnel_id = ?", (pid,))
            db.execute("DELETE FROM attendance WHERE personnel_id = ?", (pid,))
            db.execute("DELETE FROM personnel WHERE id = ?", (pid,))
            db.commit()
            write_audit(db, "delete_personnel", f"personnel_id={pid}")
            flash("Personel ve mesai kayıtları silindi.", "success")
            return_pid = request.form.get("return_pid", type=int)
            if return_pid == pid:
                return redirect(url_for("admin"))

        elif action == "reset_device_binding":
            pid = int(request.form["personnel_id"])
            db.execute("DELETE FROM device_bindings WHERE personnel_id = ?", (pid,))
            db.commit()
            write_audit(db, "reset_device", f"personnel_id={pid}")
            flash("Cihaz eşleştirmesi sıfırlandı. Personel ilk girişte yeniden seçim yapacak.", "success")

        elif action == "add_note":
            content = request.form.get("content", "").strip()
            if content:
                db.execute(
                    "INSERT INTO announcements (content, created_at) VALUES (?, ?)",
                    (content, now_str()),
                )
                db.commit()
                write_audit(db, "add_note", content[:120])
                flash("Duyuru kaydedildi.", "success")

        elif action == "delete_note":
            note_id = int(request.form["announcement_id"])
            db.execute("DELETE FROM announcements WHERE id = ?", (note_id,))
            db.commit()
            write_audit(db, "delete_note", f"id={note_id}")
            flash("Duyuru silindi.", "success")

        elif action == "change_admin_password":
            current_password = request.form.get("current_password", "").strip()
            new_password = request.form.get("new_password", "").strip()
            if not verify_password(current_password):
                flash("Mevcut şifre hatalı.", "danger")
            elif len(new_password) < MIN_PASSWORD_LEN:
                flash(f"Yeni şifre en az {MIN_PASSWORD_LEN} karakter olmalı.", "danger")
            else:
                set_setting("admin_password_hash", hash_password(new_password))
                write_audit(db, "change_admin_password", "Şifre güncellendi")
                flash("Yönetici şifresi güncellendi.", "success")

        elif action == "set_branch_hours":
            bid = int(request.form["branch_id"])
            ss = parse_hhmm(request.form.get("shift_start"))
            se = parse_hhmm(request.form.get("shift_end"))
            allowed_ip = request.form.get("allowed_ip", "").strip()
            if not ss or not se:
                flash("Giriş/çıkış saati HH:MM formatında olmalı (ör. 09:00).", "danger")
            else:
                if allowed_ip:
                    db.execute(
                        "UPDATE branches SET shift_start = ?, shift_end = ?, allowed_ip = ? WHERE id = ?",
                        (ss, se, allowed_ip, bid),
                    )
                else:
                    db.execute(
                        "UPDATE branches SET shift_start = ?, shift_end = ? WHERE id = ?",
                        (ss, se, bid),
                    )
                db.commit()
                write_audit(db, "set_branch_hours", f"branch_id={bid} {ss}-{se}")
                flash("Mağaza mesai/IP güncellendi.", "success")

        elif action == "assign_personnel_code":
            pid = int(request.form["personnel_id"])
            code = (request.form.get("employee_code") or "").strip().upper()
            full_name = (request.form.get("full_name") or "").strip()
            if not code:
                flash("Personel kodu zorunlu.", "danger")
            else:
                try:
                    if full_name:
                        db.execute(
                            """
                            UPDATE personnel
                            SET employee_code = ?, code_confirmed = 1, full_name = ?
                            WHERE id = ?
                            """,
                            (code, full_name, pid),
                        )
                    else:
                        db.execute(
                            """
                            UPDATE personnel SET employee_code = ?, code_confirmed = 1 WHERE id = ?
                            """,
                            (code, pid),
                        )
                    db.commit()
                    write_audit(db, "assign_personnel_code", f"id={pid} code={code}")
                    flash(f"Kod kaydedildi: {code}", "success")
                except (sqlite3.IntegrityError, PgIntegrityError):
                    flash("Bu kod başka bir personele ait.", "danger")
            return redirect(url_for("admin_personnel_codes"))

        elif action == "add_leave":
            pid = int(request.form["personnel_id"])
            start_d = parse_iso_date(request.form.get("start_date"))
            end_d = parse_iso_date(request.form.get("end_date")) or start_d
            leave_type = (request.form.get("leave_type") or "mazeret").strip()
            note = (request.form.get("note") or "").strip()
            if not start_d or not end_d or end_d < start_d:
                flash("İzin tarihleri geçersiz.", "danger")
            elif leave_type not in LEAVE_TYPE_LABELS:
                flash("İzin türü geçersiz.", "danger")
            else:
                db.execute(
                    """
                    INSERT INTO personnel_leaves (personnel_id, start_date, end_date, leave_type, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        start_d.strftime("%Y-%m-%d"),
                        end_d.strftime("%Y-%m-%d"),
                        leave_type,
                        note,
                        now_str(),
                    ),
                )
                db.commit()
                write_audit(db, "add_leave", f"pid={pid} {start_d}..{end_d} {leave_type}")
                flash("İzin/tatil kaydı eklendi.", "success")

        elif action == "delete_leave":
            lid = int(request.form["leave_id"])
            db.execute("DELETE FROM personnel_leaves WHERE id = ?", (lid,))
            db.commit()
            write_audit(db, "delete_leave", f"id={lid}")
            flash("İzin kaydı silindi.", "success")

        elif action == "add_branch_holiday":
            bid = int(request.form["branch_id"])
            start_d = parse_iso_date(request.form.get("start_date"))
            end_d = parse_iso_date(request.form.get("end_date")) or start_d
            title = (request.form.get("title") or "Resmi tatil").strip()
            if not start_d or not end_d or end_d < start_d:
                flash("Tatil tarihleri geçersiz.", "danger")
            else:
                db.execute(
                    """
                    INSERT INTO branch_holidays (branch_id, start_date, end_date, title, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        bid,
                        start_d.strftime("%Y-%m-%d"),
                        end_d.strftime("%Y-%m-%d"),
                        title,
                        now_str(),
                    ),
                )
                db.commit()
                write_audit(db, "add_branch_holiday", f"branch={bid} {title}")
                flash("Mağaza tatili eklendi (tüm personel için hesap dışı).", "success")

        elif action == "delete_branch_holiday":
            hid = int(request.form["holiday_id"])
            db.execute("DELETE FROM branch_holidays WHERE id = ?", (hid,))
            db.commit()
            write_audit(db, "delete_branch_holiday", f"id={hid}")
            flash("Mağaza tatili silindi.", "success")

        elif action == "correct_attendance":
            aid = int(request.form["attendance_id"])
            checkout_raw = (request.form.get("checkout_at") or "").strip()
            clear_auto = request.form.get("clear_auto_closed") == "1"
            row = db.execute("SELECT * FROM attendance WHERE id = ?", (aid,)).fetchone()
            if not row:
                flash("Kayıt bulunamadı.", "danger")
            else:
                ci = _parse_ts_tr(row["checkin_at"])
                co = _parse_ts_tr(checkout_raw) if checkout_raw else None
                if checkout_raw and not co:
                    flash("Çıkış saati YYYY-MM-DD HH:MM:SS formatında olmalı.", "danger")
                elif co and ci and co < ci:
                    flash("Çıkış, girişten önce olamaz.", "danger")
                else:
                    duration = _minutes_between(ci, co) if ci and co else int(row["duration_minutes"] or 0)
                    auto_closed = 0 if clear_auto or co else int(row["auto_closed"] or 0)
                    new_source = "mobile" if auto_closed == 0 else (row["source"] or "mobile")
                    db.execute(
                        """
                        UPDATE attendance
                        SET checkout_at = ?, duration_minutes = ?, auto_closed = ?, source = ?
                        WHERE id = ?
                        """,
                        (
                            checkout_raw or row["checkout_at"],
                            duration,
                            auto_closed,
                            new_source,
                            aid,
                        ),
                    )
                    db.commit()
                    write_audit(db, "correct_attendance", f"id={aid} auto={auto_closed}")
                    flash("Mesai kaydı düzeltildi; artık eksik saat hesabına dahil edilebilir.", "success")

        return_pid = request.form.get("return_pid", type=int)
        if return_pid:
            return redirect(url_for("admin", pid=return_pid))
        return redirect(url_for("admin"))

    branches = fetch_branches(active_only=False)
    personnel_admin = fetch_personnel_admin()
    needing_codes, dup_groups = personnel_needing_manual_codes(db)

    q_filter = (request.args.get("q") or "").strip().casefold()
    branch_filter = request.args.get("branch_filter", type=int)
    page = max(1, request.args.get("page", default=1, type=int))
    per_page = 40

    attendance_q = """
        SELECT a.*, p.full_name, p.employee_code, b.name AS branch_name
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        WHERE 1=1
    """
    count_q = """
        SELECT COUNT(*) AS c
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        WHERE 1=1
    """
    params: list = []
    filter_sql = ""
    if branch_filter:
        filter_sql += " AND a.branch_id = ?"
        params.append(branch_filter)
    if q_filter:
        filter_sql += " AND (LOWER(p.full_name) LIKE ? OR LOWER(COALESCE(p.employee_code,'')) LIKE ? OR LOWER(b.name) LIKE ?)"
        like = f"%{q_filter}%"
        params.extend([like, like, like])
    count_row = db.execute(count_q + filter_sql, tuple(params)).fetchone()
    total_att = int(count_row["c"] or 0) if count_row else 0
    total_pages = max(1, (total_att + per_page - 1) // per_page)
    page = min(page, total_pages)
    list_params = list(params) + [per_page, (page - 1) * per_page]
    attendance_rows = db.execute(
        attendance_q
        + filter_sql
        + " ORDER BY COALESCE(a.checkout_at, a.checkin_at) DESC, a.id DESC LIMIT ? OFFSET ?",
        tuple(list_params),
    ).fetchall()

    personnel_filtered = personnel_admin
    if q_filter:
        personnel_filtered = [
            p
            for p in personnel_admin
            if q_filter in (p["full_name"] or "").casefold()
            or q_filter in (p["employee_code"] or "").casefold()
            or q_filter in (p["branch_name"] or "").casefold()
        ]

    latest_notes = db.execute(
        "SELECT id, content, created_at FROM announcements ORDER BY id DESC LIMIT 20"
    ).fetchall()

    today_summary = fetch_today_dashboard_summary(db)

    selected_pid = request.args.get("pid", type=int)
    selected_start = (request.args.get("start_date") or "").strip()
    selected_end = (request.args.get("end_date") or "").strip()
    sel_stats = None
    sel_range_stats = None
    sel_name = None
    sel_code = None
    sel_leaves = []
    if selected_pid:
        prow = db.execute(
            "SELECT full_name, employee_code FROM personnel WHERE id = ?",
            (selected_pid,),
        ).fetchone()
        if prow:
            sel_name = prow["full_name"]
            sel_code = prow["employee_code"] or ""
            sel_stats = personnel_work_stats(db, selected_pid)
            sel_leaves = db.execute(
                """
                SELECT * FROM personnel_leaves
                WHERE personnel_id = ? ORDER BY start_date DESC LIMIT 50
                """,
                (selected_pid,),
            ).fetchall()
            start_date = parse_iso_date(selected_start)
            end_date = parse_iso_date(selected_end)
            if selected_start and selected_end:
                if not start_date or not end_date:
                    flash("Tarih aralığı geçersiz.", "warning")
                elif start_date > end_date:
                    flash("Başlangıç tarihi, bitişten büyük olamaz.", "warning")
                else:
                    sel_range_stats = personnel_work_stats_range(
                        db, selected_pid, start_date, end_date
                    )

    holidays = db.execute(
        """
        SELECT h.*, b.name AS branch_name
        FROM branch_holidays h
        JOIN branches b ON b.id = h.branch_id
        ORDER BY h.start_date DESC LIMIT 40
        """
    ).fetchall()

    audit_rows = db.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 40"
    ).fetchall()

    return render_template(
        "admin.html",
        branches=branches,
        personnel=personnel_filtered,
        attendance_rows=attendance_rows,
        latest_notes=latest_notes,
        today_summary=today_summary,
        selected_pid=selected_pid,
        sel_name=sel_name,
        sel_code=sel_code,
        sel_stats=sel_stats,
        sel_range_stats=sel_range_stats,
        selected_start=selected_start,
        selected_end=selected_end,
        sel_leaves=sel_leaves,
        leave_types=LEAVE_TYPES,
        leave_type_labels=LEAVE_TYPE_LABELS,
        holidays=holidays,
        audit_rows=audit_rows,
        needing_codes=needing_codes,
        dup_groups=dup_groups,
        suggested_code=next_employee_code(db),
        q=request.args.get("q") or "",
        branch_filter=branch_filter,
        page=page,
        total_pages=total_pages,
        total_att=total_att,
        min_password_len=MIN_PASSWORD_LEN,
    )


@app.get("/admin/gun-sonu")
def gun_sonu_legacy_redirect():
    return redirect(url_for("admin"))


@app.route("/admin/personel-kodlari", methods=["GET", "POST"])
def admin_personnel_codes():
    if not require_admin():
        return redirect(url_for("index", next=request.path))
    db = get_db()
    if request.method == "POST":
        # handled via admin assign_personnel_code redirect target
        pass
    needing, dups = personnel_needing_manual_codes(db)
    all_p = fetch_personnel_admin()
    return render_template(
        "personnel_codes.html",
        needing_codes=needing,
        dup_groups=dups,
        personnel=all_p,
        suggested_code=next_employee_code(db),
    )


@app.route("/admin/eksik-saat", methods=["GET"])
def admin_missing_hours():
    if not require_admin():
        return redirect(url_for("index", next=request.path))
    db = get_db()
    start_s = (request.args.get("start") or "").strip()
    end_s = (request.args.get("end") or "").strip()
    branch_id = request.args.get("branch_id", type=int)
    now = now_tr().date()
    start_d = parse_iso_date(start_s) or now.replace(day=1)
    end_d = parse_iso_date(end_s) or now
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    rows = period_missing_report(db, start_d, end_d, branch_id=branch_id)
    return render_template(
        "eksik_saat.html",
        rows=rows,
        start=start_d.strftime("%Y-%m-%d"),
        end=end_d.strftime("%Y-%m-%d"),
        branch_id=branch_id,
        branches=fetch_branches(active_only=False),
    )


@app.get("/rapor/eksik-saat.csv")
def export_missing_hours_csv():
    if not require_admin():
        return redirect(url_for("index"))
    db = get_db()
    start_d = parse_iso_date(request.args.get("start")) or now_tr().date().replace(day=1)
    end_d = parse_iso_date(request.args.get("end")) or now_tr().date()
    branch_id = request.args.get("branch_id", type=int)
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    rows = period_missing_report(db, start_d, end_d, branch_id=branch_id)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "PersonelKodu",
            "İsim",
            "Mağaza",
            "EksikSüre",
            "EksikDakika",
            "FiiliÇalışma",
            "İzinGünü",
            "HesaplananGün",
            "OtomatikKapalıGün",
            "Başlangıç",
            "Bitiş",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["employee_code"],
                r["full_name"],
                r["branch_name"],
                r["missing_hm"],
                r["missing_minutes"],
                r["worked_hm"],
                r["leave_days"],
                r["counted_days"],
                r["auto_closed_days"],
                start_d.strftime("%Y-%m-%d"),
                end_d.strftime("%Y-%m-%d"),
            ]
        )
    resp = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = "attachment; filename=pdks_eksik_saat.csv"
    return resp


@app.route("/personel")
def personel():
    db = get_db()
    reconcile_personel_lock(db)
    device_binding = get_device_binding(db)
    if device_binding and not session.get("pdks_choice_lock"):
        session["pdks_choice_lock"] = {
            "personnel_id": int(device_binding["personnel_id"]),
            "branch_id": int(device_binding["branch_id"]),
            "full_name": device_binding["full_name"],
            "branch_name": device_binding["branch_name"],
        }
        session.modified = True
    elif device_binding:
        lock = session.get("pdks_choice_lock")
        if lock and (
            int(lock.get("personnel_id", 0)) != int(device_binding["personnel_id"])
            or int(lock.get("branch_id", 0)) != int(device_binding["branch_id"])
        ):
            session["pdks_choice_lock"] = {
                "personnel_id": int(device_binding["personnel_id"]),
                "branch_id": int(device_binding["branch_id"]),
                "full_name": device_binding["full_name"],
                "branch_name": device_binding["branch_name"],
            }
            session.modified = True
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
        device_binding=device_binding,
    )


@app.get("/health")
def health():
    """Render / denetim: tarayıcıda /health açınca durum görünmeli."""
    try:
        db = get_db()
        db.execute("SELECT 1 AS ok").fetchone()
        return Response("ok", mimetype="text/plain")
    except Exception as exc:
        app.logger.exception("health_db_failed")
        return Response(f"db_error: {exc}", status=503, mimetype="text/plain")


@app.get("/diag")
def diag():
    """Geçici teşhis — hata metnini düz yazı döner."""
    import traceback

    lines = []
    needing = []
    dups = []
    summary = {
        "date_label": "-",
        "inside": [],
        "inside_count": 0,
        "missing_today": [],
        "past_shift_inside": [],
    }
    try:
        lines.append("diag_start")
        init_db()
        lines.append("init_db: ok")
        db = get_db()
        lines.append(f"backend: {db.backend}")
        for t in (
            "branches",
            "personnel",
            "attendance",
            "personnel_leaves",
            "branch_holidays",
            "audit_log",
            "settings",
        ):
            try:
                row = db.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
                n = row["c"] if row else "?"
                cols = ", ".join(_table_columns(db, t)[:30])
                lines.append(f"table {t}: rows={n} cols=[{cols}]")
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                lines.append(f"table {t}: ERROR {type(e).__name__}: {e}")
        try:
            needing, dups = personnel_needing_manual_codes(db)
            lines.append(f"needing_codes={len(needing)} dup_groups={len(dups)}")
        except Exception as e:
            lines.append(f"needing_codes ERROR: {e}")
            lines.append(traceback.format_exc())
        try:
            summary = fetch_today_dashboard_summary(db)
            lines.append(
                f"today_summary: inside={summary['inside_count']} missing={len(summary['missing_today'])}"
            )
        except Exception as e:
            lines.append(f"today_summary ERROR: {e}")
            lines.append(traceback.format_exc())
        try:
            att = db.execute(
                """
                SELECT a.*, p.full_name, p.employee_code, b.name AS branch_name
                FROM attendance a
                JOIN personnel p ON p.id = a.personnel_id
                JOIN branches b ON b.id = a.branch_id
                ORDER BY COALESCE(a.checkout_at, a.checkin_at) DESC, a.id DESC
                LIMIT 50
                """
            ).fetchall()
            lines.append(f"attendance_sample={len(att)}")
            if att:
                r0 = att[0]
                lines.append(
                    f"att0 keys auto_closed={r0.get('auto_closed') if hasattr(r0,'get') else r0['auto_closed']} source={r0['source']}"
                )
        except Exception as e:
            att = []
            lines.append(f"attendance_query ERROR: {e}")
            lines.append(traceback.format_exc())
        try:
            html = render_template(
                "admin.html",
                branches=fetch_branches(active_only=False),
                personnel=fetch_personnel_admin(),
                attendance_rows=att,
                latest_notes=[],
                today_summary=summary,
                selected_pid=None,
                sel_name=None,
                sel_code=None,
                sel_stats=None,
                sel_range_stats=None,
                selected_start="",
                selected_end="",
                sel_leaves=[],
                leave_types=LEAVE_TYPES,
                leave_type_labels=LEAVE_TYPE_LABELS,
                holidays=[],
                audit_rows=[],
                needing_codes=needing,
                dup_groups=dups,
                suggested_code="P0001",
                q="",
                branch_filter=None,
                page=1,
                total_pages=1,
                total_att=len(att),
                min_password_len=MIN_PASSWORD_LEN,
            )
            lines.append(f"admin_template_render: ok len={len(html)}")
        except Exception as e:
            lines.append(f"admin_template ERROR: {e}")
            lines.append(traceback.format_exc())
        lines.append("DONE")
    except Exception:
        lines.append("FAIL")
        lines.append(traceback.format_exc())
    return Response("\n".join(lines), status=200, mimetype="text/plain; charset=utf-8")


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
    device_binding = get_device_binding(db)
    if device_binding and (
        int(device_binding["personnel_id"]) != personnel_id
        or int(device_binding["branch_id"]) != branch_id
    ):
        return jsonify(
            {
                "ok": False,
                "message": (
                    f"Bu cihaz yalnızca {device_binding['full_name']} / "
                    f"{device_binding['branch_name']} için kullanılabilir."
                ),
            }
        ), 403
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
    device_binding = get_device_binding(db)
    reconcile_personel_lock(db)
    personnel_id = int(request.form["personnel_id"])
    branch_id = int(request.form["branch_id"])
    action = request.form["action"]

    if device_binding and (
        int(device_binding["personnel_id"]) != personnel_id
        or int(device_binding["branch_id"]) != branch_id
    ):
        return jsonify(
            {
                "ok": False,
                "message": (
                    f"Bu cihaz yalnızca {device_binding['full_name']} / "
                    f"{device_binding['branch_name']} için kullanılabilir."
                ),
            }
        ), 403

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
        resp = jsonify({"ok": True, "message": f"{person['full_name']}: giriş kaydı alındı."})
        if not device_binding:
            token = bind_device_for_personnel(db, personnel_id, branch_id)
            resp.set_cookie(
                "pdks_device_token",
                token,
                max_age=60 * 60 * 24 * 365 * 2,
                secure=_RENDER_HOSTED,
                httponly=True,
                samesite="Lax",
            )
        session["pdks_choice_lock"] = {
            "personnel_id": personnel_id,
            "branch_id": branch_id,
            "full_name": person["full_name"],
            "branch_name": branch["name"],
        }
        session.modified = True
        return resp

    if action == "out":
        ci = _parse_ts_tr(open_record["checkin_at"])
        if not ci:
            return jsonify({"ok": False, "message": "Kayıt hatası (giriş saati)."}), 400
        duration = max(0, int((now_tr() - ci).total_seconds() // 60))
        db.execute(
            """
            UPDATE attendance
            SET checkout_at = ?, duration_minutes = ?, auto_closed = 0, source = 'mobile'
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
        SELECT p.employee_code AS kod, p.full_name AS isim, b.name AS sube, a.date AS tarih,
            COALESCE(a.checkin_at, '-') AS giris, COALESCE(a.checkout_at, '-') AS cikis,
            a.duration_minutes AS dk, a.auto_closed AS auto_closed, a.source AS source
        FROM attendance a
        JOIN personnel p ON p.id = a.personnel_id
        JOIN branches b ON b.id = a.branch_id
        ORDER BY a.id DESC
        """
    ).fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Kod", "İsim", "Mağaza", "Tarih", "Giriş", "Çıkış", "Süre", "OtomatikKapatıldı"])
    for row in rows:
        auto = "Evet" if int(row["auto_closed"] or 0) == 1 or row["source"] == "auto" else "Hayır"
        writer.writerow(
            [
                row["kod"] or "",
                row["isim"],
                row["sube"],
                format_iso_date_tr(row["tarih"]),
                format_display_datetime(row["giris"]) if row["giris"] not in ("-", None) else "—",
                format_display_datetime(row["cikis"]) if row["cikis"] not in ("-", None) else "—",
                format_duration_tr(row["dk"]),
                auto,
            ]
        )
    resp = Response(output.getvalue(), mimetype="application/vnd.ms-excel")
    resp.headers["Content-Disposition"] = "attachment; filename=pdks_mesai.csv"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
