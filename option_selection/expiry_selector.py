from datetime import date
def select_expiry(expiries: list[date], today: date, exclude_expiry_day: bool = True) -> date | None:
    valid = sorted(e for e in expiries if e >= today and not (exclude_expiry_day and e == today))
    return valid[0] if valid else None
