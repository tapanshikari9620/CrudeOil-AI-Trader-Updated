from __future__ import annotations

import httpx
import time


class UpstoxAPIError(RuntimeError):
    def __init__(self, message: str, *, endpoint: str = "", status_code: int | None = None,
                 request_id: str | None = None, api_error_code: str | None = None):
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.request_id = request_id
        self.api_error_code = api_error_code


class UpstoxAuthenticationError(UpstoxAPIError):
    pass


class UpstoxPermissionError(UpstoxAPIError):
    pass


class UpstoxRateLimitError(UpstoxAPIError):
    pass


class UpstoxClient:
    """Thin HTTP client for documented Upstox REST endpoints."""

    def __init__(
        self,
        access_token: str,
        base_url: str = "https://api.upstox.com",
        timeout: float = 15,
        *,
        transport: httpx.BaseTransport | None = None,
        retry_count: int = 0,
        retry_backoff_seconds: float = 1.0,
    ):
        if not access_token:
            raise ValueError("UPSTOX_ACCESS_TOKEN is required")
        self.base_url = base_url.rstrip("/")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if isinstance(retry_count, bool) or not 0 <= retry_count <= 3:
            raise ValueError("retry_count must be between 0 and 3")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        self.retry_count = retry_count
        self.retry_backoff_seconds = retry_backoff_seconds
        self.client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": "Bearer " + access_token, "Accept": "application/json"},
            transport=transport,
        )

    def close(self):
        self.client.close()

    def _get(self, path: str, params: dict | None = None):
        response = None
        for attempt in range(self.retry_count + 1):
            try:
                response = self.client.get(self.base_url + path, params=params)
            except httpx.TimeoutException as exc:
                if attempt < self.retry_count:
                    time.sleep(min(self.retry_backoff_seconds * (2 ** attempt), 30.0))
                    continue
                raise UpstoxAPIError(
                    f"Upstox GET {path} timed out", endpoint=path
                ) from exc
            except httpx.NetworkError as exc:
                if attempt < self.retry_count:
                    time.sleep(min(self.retry_backoff_seconds * (2 ** attempt), 30.0))
                    continue
                raise UpstoxAPIError(
                    f"Upstox GET {path} failed due to a network error", endpoint=path
                ) from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retry_count:
                    time.sleep(min(self.retry_backoff_seconds * (2 ** attempt), 30.0))
                    continue
            break

        assert response is not None
        diagnostics = self._safe_error_diagnostics(path, response)
        if response.status_code == 401:
            raise UpstoxAuthenticationError(
                f"Upstox returned HTTP 401 for GET {path}; cause not determined",
                **diagnostics,
            )
        if response.status_code == 403:
            raise UpstoxPermissionError(
                f"Upstox denied access to GET {path} (HTTP 403)", **diagnostics
            )
        if response.status_code == 429:
            raise UpstoxRateLimitError(
                f"Upstox rate limit reached for {path} (HTTP 429)", **diagnostics
            )
        if response.status_code >= 400:
            raise UpstoxAPIError(
                f"Upstox GET {path} failed with HTTP {response.status_code}",
                **diagnostics,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstoxAPIError(
                f"Upstox returned invalid JSON for {path}", endpoint=path,
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") == "error":
            raise UpstoxAPIError(
                f"Upstox returned an error response for {path}", endpoint=path,
                status_code=response.status_code,
            )
        return payload

    @staticmethod
    def _safe_error_diagnostics(path: str, response: httpx.Response) -> dict:
        request_id = next(
            (response.headers.get(name) for name in ("x-request-id", "request-id")
             if response.headers.get(name)),
            None,
        )
        error_code = None
        try:
            payload = response.json()
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                candidate = errors[0].get("errorCode")
                if isinstance(candidate, str) and len(candidate) <= 64:
                    error_code = candidate
        except ValueError:
            pass
        return {
            "endpoint": path,
            "status_code": response.status_code,
            "request_id": request_id,
            "api_error_code": error_code,
        }

    def user_profile(self) -> dict:
        """Validate token authentication using the official read-only profile API."""
        payload = self._get("/v2/user/profile")
        if payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
            raise UpstoxAPIError("Upstox profile response has an unexpected schema")
        return payload["data"]

    def search_instruments(self, query: str, *, instrument_types: str, exchanges: str = "MCX",
                           segments: str = "FO", page_number: int = 1, records: int = 30,
                           expiry: str | None = None) -> dict:
        """Call the official, read-only instrument search endpoint."""
        if not query or len(query) > 50:
            raise ValueError("Instrument search query must contain 1 to 50 characters")
        if not 1 <= records <= 30 or page_number < 1:
            raise ValueError("Instrument search pagination is out of range")
        params = {
            "query": query,
            "exchanges": exchanges,
            "segments": segments,
            "instrument_types": instrument_types,
            "page_number": page_number,
            "records": records,
        }
        if expiry:
            params["expiry"] = expiry
        return self._get("/v2/instruments/search", params)

    def market_quotes(self, instrument_keys: list[str]) -> dict:
        """Fetch read-only full quotes (maximum 500 keys per request)."""
        if not instrument_keys or len(instrument_keys) > 500:
            raise ValueError("market_quotes requires 1 to 500 instrument keys")
        if any(not isinstance(key, str) or not key for key in instrument_keys):
            raise ValueError("Instrument keys must be non-empty strings")
        return self._get("/v2/market-quote/quotes", {"instrument_key": ",".join(instrument_keys)})

    def authorize_market_data_feed(self) -> str:
        payload = self._get("/v3/feed/market-data-feed/authorize")
        data = payload.get("data")
        uri = data.get("authorized_redirect_uri") if isinstance(data, dict) else None
        if payload.get("status") != "success" or not isinstance(uri, str) or not uri.startswith("wss://"):
            raise UpstoxAPIError(
                "Upstox market-feed authorization response is invalid",
                endpoint="/v3/feed/market-data-feed/authorize",
            )
        return uri

    def historical_candles(self, instrument_key: str, unit: str, interval: int, to_date: str, from_date: str | None = None):
        from urllib.parse import quote

        if unit not in {"minutes", "hours", "days", "weeks", "months"}:
            raise ValueError(f"Unsupported Upstox candle unit: {unit!r}")
        if not instrument_key:
            raise ValueError("instrument_key is required")
        key = quote(instrument_key, safe="")
        path = f"/v3/historical-candle/{key}/{unit}/{interval}/{to_date}"
        if from_date:
            path += f"/{from_date}"
        return self._get(path)

    def intraday_candles(self, instrument_key: str, unit: str, interval: int):
        from urllib.parse import quote

        key = quote(instrument_key, safe="")
        return self._get(f"/v3/historical-candle/intraday/{key}/{unit}/{interval}")

    def option_contracts(self, underlying_key: str, expiry_date: str | None = None):
        params = {"instrument_key": underlying_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        return self._get("/v2/option/contract", params)

    def market_status(self, exchange: str):
        from urllib.parse import quote

        if exchange not in {"MCX", "NSE", "BSE"}:
            raise ValueError("Unsupported exchange")
        return self._get(f"/v2/market/status/{quote(exchange, safe='')}")

    def brokerage(self, instrument_key: str, quantity: int, product: str, transaction_type: str, price: float):
        params = {
            "instrument_token": instrument_key,
            "quantity": quantity,
            "product": product,
            "transaction_type": transaction_type,
            "price": price,
        }
        return self._get("/v2/charges/brokerage", params)

    def market_timings(self, exchange: str, date: str):
        from urllib.parse import quote

        return self._get(f"/v2/market/timings/{quote(date, safe='')}", {"exchange": exchange})

    def market_quote(self, instrument_keys: list[str]):
        return self.market_quotes(instrument_keys)
