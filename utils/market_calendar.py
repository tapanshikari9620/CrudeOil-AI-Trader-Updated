from datetime import date
from execution.upstox_client import UpstoxClient

class UpstoxMarketCalendar:
    """Reads exchange schedule/status from Upstox instead of hardcoding session times."""
    def __init__(self, client: UpstoxClient):
        self.client = client

    def timings(self, exchange: str, session_date: date):
        return self.client.market_timings(exchange, session_date.isoformat())

    def status(self, exchange: str):
        return self.client.market_status(exchange)
