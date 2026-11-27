from __future__ import annotations

from urllib.parse import urlencode

import httpx

from config.settings import get_settings


def authorization_url(state: str) -> str:
    settings = get_settings()
    if not settings.upstox_client_id or not settings.upstox_redirect_uri:
        raise ValueError("Configure Upstox client ID and redirect URI")
    params = {
        "response_type": "code",
        "client_id": settings.upstox_client_id,
        "redirect_uri": settings.upstox_redirect_uri,
        "state": state,
    }
    return "https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params)


def exchange_code(code: str) -> dict:
    settings = get_settings()
    if not settings.upstox_client_id or not settings.upstox_client_secret or not settings.upstox_redirect_uri:
        raise ValueError("Missing Upstox OAuth configuration")
    data = {
        "code": code,
        "client_id": settings.upstox_client_id,
        "client_secret": settings.upstox_client_secret,
        "redirect_uri": settings.upstox_redirect_uri,
        "grant_type": "authorization_code",
    }
    response = httpx.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()
