import argparse

from data_collector.historical_collector import HistoricalCollector
from data_collector.instrument_collector import InstrumentCollector
from config.settings import get_settings
from execution.upstox_client import UpstoxClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument-key", required=True)
    parser.add_argument("--unit", default="minutes")
    parser.add_argument("--interval", type=int, default=1)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--from-date")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.upstox_access_token:
        raise ValueError("UPSTOX_ACCESS_TOKEN is required for historical collection")
    metadata = InstrumentCollector().find_contract(args.instrument_key)
    client = UpstoxClient(settings.upstox_access_token, settings.upstox_api_base)
    try:
        print(HistoricalCollector(client, instrument_metadata=metadata).collect(
            args.instrument_key, args.unit, args.interval, args.to_date, args.from_date
        ))
    finally:
        client.close()


if __name__ == "__main__":
    main()
