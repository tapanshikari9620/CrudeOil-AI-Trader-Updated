
from __future__ import annotations

import os
import math
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _project_path(value: str | None) -> Path:
    base = value or ""
    path = Path(base)
    return path if path.is_absolute() else PROJECT_ROOT / path


class Settings(BaseModel):
    """Application settings loaded from environment variables."""

    model_config = ConfigDict(populate_by_name=True, validate_assignment=True)

    app_name: str = Field(default_factory=lambda: os.getenv("APP_NAME", "CrudeOil-AI-Trader"))
    environment: str = Field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    trading_mode: str = Field(default_factory=lambda: os.getenv("TRADING_MODE", "PAPER").upper())

    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            f"sqlite:///{(PROJECT_ROOT / 'data' / 'crude_oil_trading.db').as_posix()}",
        )
    )

    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    log_dir: Path = Field(default_factory=lambda: _project_path(os.getenv("LOG_DIR", "logs")))
    data_dir: Path = Field(default_factory=lambda: _project_path(os.getenv("DATA_DIR", "data")))
    timezone: str = Field(default_factory=lambda: os.getenv("MARKET_TIMEZONE", "Asia/Kolkata"))

    # Selected MCX product: Mini by default; Regular remains supported.
    mcx_product: str = Field(
        default_factory=lambda: os.getenv("MCX_PRODUCT", "CRUDEOILM").upper().strip()
    )

    max_trades_per_day: int = Field(
        default_factory=lambda: int(os.getenv("MAX_TRADES_PER_DAY", "5"))
    )

    # Exposure guard, not a rupee profit/loss target.
    max_paper_lots_per_trade: int = Field(
        default_factory=lambda: int(os.getenv("MAX_PAPER_LOTS_PER_TRADE", "1"))
    )

    atr_stop_multiplier: float = Field(
        default_factory=lambda: float(os.getenv("ATR_STOP_MULTIPLIER", "25.0"))
    )
    min_stop_risk_fraction: float = Field(
        default_factory=lambda: float(os.getenv("MIN_STOP_RISK_FRACTION", "0.08"))
    )
    max_stop_risk_fraction: float = Field(
        default_factory=lambda: float(os.getenv("MAX_STOP_RISK_FRACTION", "0.35"))
    )
    base_reward_multiple: float = Field(
        default_factory=lambda: float(os.getenv("BASE_REWARD_MULTIPLE", "2.0"))
    )

    upstox_client_id: str | None = Field(default_factory=lambda: os.getenv("UPSTOX_CLIENT_ID"))
    upstox_client_secret: str | None = Field(default_factory=lambda: os.getenv("UPSTOX_CLIENT_SECRET"))
    upstox_redirect_uri: str | None = Field(default_factory=lambda: os.getenv("UPSTOX_REDIRECT_URI"))
    upstox_access_token: str | None = Field(default_factory=lambda: os.getenv("UPSTOX_ACCESS_TOKEN"))

    upstox_api_base: str = Field(
        default_factory=lambda: os.getenv("UPSTOX_API_BASE", "https://api.upstox.com")
    )

    live_futures_poll_seconds: float = Field(
        default_factory=lambda: float(os.getenv("LIVE_FUTURES_POLL_SECONDS", "5"))
    )
    live_options_poll_seconds: float = Field(
        default_factory=lambda: float(os.getenv("LIVE_OPTIONS_POLL_SECONDS", "15"))
    )
    live_max_contracts_per_cycle: int = Field(
        default_factory=lambda: int(os.getenv("LIVE_MAX_CONTRACTS_PER_CYCLE", "200"))
    )
    live_api_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("LIVE_API_TIMEOUT_SECONDS", "15"))
    )
    live_retry_count: int = Field(
        default_factory=lambda: int(os.getenv("LIVE_RETRY_COUNT", "2"))
    )
    live_retry_backoff_seconds: float = Field(
        default_factory=lambda: float(os.getenv("LIVE_RETRY_BACKOFF_SECONDS", "1"))
    )
    live_quote_freshness_seconds: float = Field(
        default_factory=lambda: float(os.getenv("LIVE_QUOTE_FRESHNESS_SECONDS", "120"))
    )

    mcx_market_open_time: str = Field(
        default_factory=lambda: os.getenv("MCX_MARKET_OPEN_TIME", "09:00")
    )
    mcx_market_close_time: str = Field(
        default_factory=lambda: os.getenv("MCX_MARKET_CLOSE_TIME", "23:30")
    )
    live_contract_refresh_seconds: float = Field(
        default_factory=lambda: float(os.getenv("LIVE_CONTRACT_REFRESH_SECONDS", "900"))
    )

    @field_validator("trading_mode")
    @classmethod
    def normalize_trading_mode(cls, value: str) -> str:
        return value.upper().strip()

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper().strip()

    @field_validator("mcx_product")
    @classmethod
    def normalize_mcx_product(cls, value: str) -> str:
        return value.upper().strip()

    def validate(self) -> "Settings":
        if self.trading_mode not in {"PAPER", "LIVE"}:
            raise ValueError("TRADING_MODE must be PAPER or LIVE")

        if self.trading_mode == "LIVE":
            raise ValueError("LIVE mode is disabled in Phase 1. Use TRADING_MODE=PAPER.")

        if self.mcx_product not in {"CRUDEOIL", "CRUDEOILM"}:
            raise ValueError("MCX_PRODUCT must be CRUDEOIL or CRUDEOILM")

        if not 1 <= self.max_trades_per_day <= 5:
            raise ValueError("MAX_TRADES_PER_DAY must be between 1 and 5")

        if self.max_paper_lots_per_trade != 1:
            raise ValueError("MAX_PAPER_LOTS_PER_TRADE must be exactly 1")

        if not math.isfinite(self.atr_stop_multiplier) or self.atr_stop_multiplier <= 0:
            raise ValueError("ATR_STOP_MULTIPLIER must be finite and positive")

        if not (0 < self.min_stop_risk_fraction < self.max_stop_risk_fraction < 1):
            raise ValueError("Stop risk fractions must satisfy 0 < MIN < MAX < 1")

        if not math.isfinite(self.base_reward_multiple) or self.base_reward_multiple <= 0:
            raise ValueError("BASE_REWARD_MULTIPLE must be finite and positive")

        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")

        for name in (
            "live_futures_poll_seconds",
            "live_options_poll_seconds",
            "live_api_timeout_seconds",
            "live_quote_freshness_seconds",
            "live_contract_refresh_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name.upper()} must be finite and positive")

        if (
            self.live_retry_backoff_seconds < 0
            or not math.isfinite(self.live_retry_backoff_seconds)
        ):
            raise ValueError("LIVE_RETRY_BACKOFF_SECONDS must be finite and non-negative")

        if isinstance(self.live_retry_count, bool) or not 0 <= self.live_retry_count <= 3:
            raise ValueError("LIVE_RETRY_COUNT must be between 0 and 3")

        if (
            isinstance(self.live_max_contracts_per_cycle, bool)
            or not 1 <= self.live_max_contracts_per_cycle <= 500
        ):
            raise ValueError("LIVE_MAX_CONTRACTS_PER_CYCLE must be between 1 and 500")

        for name in ("mcx_market_open_time", "mcx_market_close_time"):
            try:
                datetime.strptime(getattr(self, name), "%H:%M")
            except ValueError as exc:
                raise ValueError(f"{name.upper()} must use HH:MM") from exc

        return self


def get_settings() -> Settings:
    return settings


settings = Settings()
