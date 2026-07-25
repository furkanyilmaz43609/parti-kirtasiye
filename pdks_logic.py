"""PDKS hesaplama ve kimlik yardımcıları (test edilebilir saf mantık)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

LEAVE_TYPES = (
    ("yillik", "Yıllık izin"),
    ("resmi", "Resmi tatil"),
    ("rapor", "Raporlu"),
    ("mazeret", "Mazeret izni"),
)

LEAVE_TYPE_LABELS = dict(LEAVE_TYPES)


def parse_hhmm(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if len(s) < 4:
        return None
    try:
        datetime.strptime(s, "%H:%M")
    except ValueError:
        return None
    return s


def shift_length_minutes(shift_start: str | None, shift_end: str | None) -> int:
    """Aynı takvim günü içinde mesai süresi (dakika). Gece vardiyası: bitiş < başlangıç ise +24s."""
    ss = parse_hhmm(shift_start) or "09:00"
    se = parse_hhmm(shift_end) or "18:00"
    sh, sm = map(int, ss.split(":"))
    eh, em = map(int, se.split(":"))
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if end_m <= start_m:
        end_m += 24 * 60
    return max(0, end_m - start_m)


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


def normalize_name(name: str | None) -> str:
    return (name or "").strip().casefold()


def group_personnel_by_name(personnel_rows: list[Any]) -> list[list[Any]]:
    """Aynı isimli personel grupları (manuel kod eşleştirme için)."""
    buckets: dict[str, list[Any]] = {}
    order: list[str] = []
    for p in personnel_rows:
        key = normalize_name(p["full_name"] if hasattr(p, "keys") else p.get("full_name"))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(p)
    return [buckets[k] for k in order]


def duplicate_name_groups(personnel_rows: list[Any]) -> list[list[Any]]:
    return [g for g in group_personnel_by_name(personnel_rows) if len(g) > 1]


def suggest_next_code(existing_codes: list[str | None]) -> str:
    max_n = 0
    for raw in existing_codes:
        c = (raw or "").strip().upper()
        if len(c) >= 2 and c[0] == "P" and c[1:].isdigit():
            max_n = max(max_n, int(c[1:]))
    return f"P{max_n + 1:04d}"


def day_in_range(day_s: str, start_s: str, end_s: str) -> bool:
    return start_s <= day_s <= end_s


def attendance_counts_as_work(row: Any) -> bool:
    """Otomatik kapanan kayıtlar gerçek çalışma sayılmaz."""
    try:
        if int(row["auto_closed"] or 0) == 1:
            return False
    except (KeyError, TypeError, ValueError):
        pass
    try:
        if (row["source"] or "") == "auto":
            return False
    except (KeyError, TypeError):
        pass
    return True


def minutes_from_attendance_row(
    row: Any,
    *,
    today_s: str,
    now: datetime,
    parse_ts_tr,
    minutes_between,
) -> int:
    if not attendance_counts_as_work(row):
        return 0
    if row["checkout_at"]:
        return int(row["duration_minutes"] or 0)
    if row["checkin_at"] and row["date"] == today_s:
        ci = parse_ts_tr(row["checkin_at"])
        if ci:
            return minutes_between(ci, now)
    return 0


def iter_dates(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def missing_minutes_for_day(
    *,
    day_s: str,
    shift_start: str | None,
    shift_end: str | None,
    is_leave: bool,
    actual_minutes: int,
) -> int | None:
    """
    İzinli gün → None (hesap dışı).
    Aksi halde max(0, mesai_süresi - fiili_süre).
    """
    if is_leave:
        return None
    expected = shift_length_minutes(shift_start, shift_end)
    return max(0, expected - max(0, int(actual_minutes)))
