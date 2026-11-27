from risk_management.position_sizing import calculate_position_size
from risk_management.daily_loss_limit import daily_limit_reached
def test_position_size_respects_lot():
    x=calculate_position_size(100,80,10,300)
    assert x.quantity%10==0 and x.estimated_loss<=300
def test_no_lot_fits():
    assert not calculate_position_size(100,80,100,300).allowed
def test_daily_loss_lock():
    assert daily_limit_reached(-500,500)
