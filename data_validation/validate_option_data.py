def validate_option_quote(q: dict) -> dict:
    issues=[]
    for k in ("instrument_key","expiry","strike","option_type","ltp","bid","ask","lot_size","tick_size"):
        if q.get(k) is None: issues.append(f"missing_{k}")
    if q.get("option_type") not in {"CE","PE"}: issues.append("invalid_option_type")
    if q.get("bid") is not None and q.get("ask") is not None and q["ask"] < q["bid"]: issues.append("crossed_market")
    if q.get("ltp") is not None and q["ltp"] <= 0: issues.append("invalid_ltp")
    return {"passed":not issues,"issues":issues}
