"""Bounded Upstox V3 market-feed adapter; official Protobuf decoding is injected."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import uuid
from pathlib import Path

from execution.upstox_client import UpstoxClient

log = logging.getLogger(__name__)


class MarketStreamUnavailable(RuntimeError):
    pass


class MarketStream:
    AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    OFFICIAL_PROTO = Path(__file__).resolve().parents[1] / "data" / "MarketDataFeed.proto"
    MAX_SUBSCRIPTIONS = {"ltpc": 5000, "option_greeks": 3000, "full": 2000, "full_d30": 50}

    def __init__(
        self,
        access_token: str,
        decoder,
        on_message,
        reconnect_delay: float = 3,
        *,
        max_reconnects: int = 3,
        timeout: float = 15,
    ):
        if max_reconnects < 0 or max_reconnects > 10:
            raise ValueError("max_reconnects must be between 0 and 10")
        if reconnect_delay < 0 or timeout <= 0:
            raise ValueError("reconnect_delay must be non-negative and timeout positive")
        self.client = UpstoxClient(access_token, timeout=timeout)
        self.decoder = decoder
        self.on_message = on_message
        self.reconnect_delay = reconnect_delay
        self.max_reconnects = max_reconnects
        self._stop = False
        self._websocket = None

    @classmethod
    def readiness(cls, decoder=None) -> dict[str, bool]:
        try:
            websocket_available = importlib.util.find_spec("websockets") is not None
        except (ImportError, ValueError):
            websocket_available = False
        try:
            protobuf_available = importlib.util.find_spec("google.protobuf") is not None
        except (ImportError, ValueError):
            protobuf_available = False
        return {
            "websockets_dependency": websocket_available,
            "protobuf_dependency": protobuf_available,
            "official_proto_file": cls.OFFICIAL_PROTO.is_file(),
            "decoder_callable": callable(decoder),
        }

    def authorize(self) -> str:
        """Obtain the short-lived official WebSocket URL without exposing it in logs."""
        return self.client.authorize_market_data_feed()

    async def run(self, instrument_keys: list[str], mode: str = "full") -> None:
        if not instrument_keys or any(not isinstance(key, str) or not key for key in instrument_keys):
            raise ValueError("At least one valid instrument key is required")
        if mode not in self.MAX_SUBSCRIPTIONS:
            raise ValueError(f"Unsupported Upstox V3 feed mode: {mode!r}")
        if len(instrument_keys) > self.MAX_SUBSCRIPTIONS[mode]:
            raise ValueError(f"Subscription exceeds the Upstox {mode} instrument limit")
        readiness = self.readiness(self.decoder)
        missing = [name for name, available in readiness.items() if not available]
        if missing:
            raise MarketStreamUnavailable(
                "Upstox V3 WebSocket feed is not ready; missing: " + ", ".join(missing)
            )

        import websockets

        attempt = 0
        while not self._stop and attempt <= self.max_reconnects:
            try:
                uri = self.authorize()
                async with websockets.connect(uri, ping_interval=20, ping_timeout=20,
                                              max_size=8_000_000) as websocket:
                    self._websocket = websocket
                    request = {
                        "guid": str(uuid.uuid4()),
                        "method": "sub",
                        "data": {"mode": mode, "instrumentKeys": instrument_keys},
                    }
                    await websocket.send(json.dumps(request).encode("utf-8"))
                    while not self._stop:
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if not isinstance(raw, bytes):
                            log.warning("Ignoring unexpected text frame from Upstox V3 binary feed")
                            continue
                        parsed = self.decoder(raw)
                        await self._dispatch(parsed)
                self._websocket = None
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._websocket = None
                attempt += 1
                log.warning("Upstox stream attempt failed (%s)", type(exc).__name__)
                if self._stop or attempt > self.max_reconnects:
                    raise MarketStreamUnavailable(
                        f"Upstox stream stopped after {attempt} bounded attempt(s)"
                    ) from None
                await asyncio.sleep(min(self.reconnect_delay * (2 ** (attempt - 1)), 30.0))

    async def _dispatch(self, item) -> None:
        result = self.on_message(item)
        if asyncio.iscoroutine(result):
            await result

    def stop(self) -> None:
        self._stop = True
        websocket = self._websocket
        if websocket is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(websocket.close())

    def close(self) -> None:
        self.stop()
        self.client.close()
