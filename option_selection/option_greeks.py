def validate_delta(delta: float | None) -> bool:
    return delta is not None and -1.0 <= float(delta) <= 1.0
