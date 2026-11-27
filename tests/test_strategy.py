from strategy.signal_engine import generate_signal
def test_missing_data_is_no_trade():
    s=generate_signal({"close":100})
    assert not s.valid and s.direction=="NO_TRADE"
