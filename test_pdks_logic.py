"""Basit birim testleri — pdks_logic (Flask gerekmez)."""

from pdks_logic import (
    missing_minutes_for_day,
    shift_length_minutes,
    suggest_next_code,
    attendance_counts_as_work,
)


def test_shift_11h():
    assert shift_length_minutes("09:30", "20:30") == 660


def test_missing_partial():
    assert (
        missing_minutes_for_day(
            day_s="2026-07-01",
            shift_start="09:30",
            shift_end="20:30",
            is_leave=False,
            actual_minutes=480,
        )
        == 180
    )


def test_leave_excluded():
    assert (
        missing_minutes_for_day(
            day_s="2026-07-01",
            shift_start="09:30",
            shift_end="20:30",
            is_leave=True,
            actual_minutes=0,
        )
        is None
    )


def test_codes():
    assert suggest_next_code(["P0001", "P0009"]) == "P0010"


def test_auto_closed_not_work():
    assert attendance_counts_as_work({"auto_closed": 1, "source": "auto"}) is False
    assert attendance_counts_as_work({"auto_closed": 0, "source": "mobile"}) is True


if __name__ == "__main__":
    test_shift_11h()
    test_missing_partial()
    test_leave_excluded()
    test_codes()
    test_auto_closed_not_work()
    print("all ok")
