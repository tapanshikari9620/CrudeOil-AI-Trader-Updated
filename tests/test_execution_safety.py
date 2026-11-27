import pytest
from execution.order_manager import OrderManager, LiveOrderExecutionDisabled
from risk_management.emergency_shutdown import KillSwitch

def test_live_orders_are_blocked():
    with pytest.raises(LiveOrderExecutionDisabled):
        OrderManager().place_order()

def test_kill_switch_blocks_new_trades():
    k = KillSwitch()
    assert k.permits_new_trade()
    k.trip("stale feed")
    assert not k.permits_new_trade()
    k.reset()
    assert k.permits_new_trade()

def test_option_chain_collector_uses_contracts_endpoint():
    from data_collector.option_chain_collector import OptionChainCollector

    class Fake:
        calls = []

        def option_contracts(self, key, expiry):
            self.calls.append((key, expiry))
            return {"status": "success", "data": [
                {
                    "instrument_key": "MCX_FO|crude-ce",
                    "trading_symbol": "CRUDEOIL 7000 CE",
                    "name": "CRUDEOIL",
                    "underlying_symbol": "CRUDEOIL",
                    "exchange": "MCX",
                    "segment": "MCX_FO",
                    "instrument_type": "CE",
                    "expiry": "2030-01-01",
                    "strike_price": 7000,
                    "lot_size": 10,
                    "tick_size": 1,
                },
                {
                    "instrument_key": "NSE_FO|wrong-exchange",
                    "trading_symbol": "CRUDEOIL 7000 PE",
                    "name": "CRUDEOIL",
                    "underlying_symbol": "CRUDEOIL",
                    "exchange": "NSE",
                    "segment": "NSE_FO",
                    "instrument_type": "PE",
                    "expiry": "2030-01-01",
                    "strike_price": 7000,
                    "lot_size": 10,
                    "tick_size": 1,
                },
            ]}

    client = Fake()
    out = OptionChainCollector(client).collect("MCX_FO|underlying")
    assert client.calls == [("MCX_FO|underlying", None)]
    assert len(out) == 1
    assert out[0]["instrument_key"] == "MCX_FO|crude-ce"
    assert out[0]["instrument_type"] == "CE"
