"""Minimal read-only HTTP helpers used for BJTU portal identity checks."""

from __future__ import annotations

import requests


AUTH_ERROR_CODES = {11009, 11011, 11012}
AUTH_ERROR_MESSAGE = "HPC token is missing or expired; refresh the selected saved account"


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session


def request_json(session: requests.Session, method: str, url: str, **kwargs):
    response = session.request(method, url, **kwargs)
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}

    if isinstance(data, dict) and data.get("code") in AUTH_ERROR_CODES:
        raise RuntimeError(AUTH_ERROR_MESSAGE)
    if not response.ok:
        raise RuntimeError(
            f"{method} {url} failed: HTTP {response.status_code} {data}"
        )
    return data
