"""Anisette provider using real Mac Anisette v3 servers (adapted from iPASide v1.2.17).

Apple blocked Android-library anisette (error -80046) and Windows akd clientInfo
(HTTP 403) on gsa.apple.com. By default this module uses the community Mac
Anisette v3 protocol (anisette_remote) with a persistent per-machine identity
cached under ~/.local/share/altserver-apple/anisette/remote-v3.json.

Set ALTSERVER_ANISETTE_MODE=local to fall back to ALTSERVER_ANISETTE_SERVER.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

from . import paths
from .errors import EngineError


class AnisetteError(EngineError):
    """Device provisioning could not be set up or reached."""


PROVIDER_ENV = "ALTSERVER_ANISETTE_MODE"
CLIENT_INFO = "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"

# Used when Apple answers -23782 ("macOS 10.13 or later is required").
# Note: "Mac OS X;" passes Apple's GsService2 edge, whereas "macOS;" triggers HTTP 503.
NEWER_MAC_CLIENT_INFO = "<MacBookPro13,3> <Mac OS X;13.6.7;22G720> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"
NEWER_MAC_USER_AGENT = "akd/1.0 CFNetwork/1410.1 Darwin/22.6.0"

_NEWER_MAC_FLAG = "newer-mac-identity"
_newer_mac_active = False
_remote_user_agent: str | None = None


def provider_name() -> str:
    return "local" if os.environ.get(PROVIDER_ENV, "").strip().lower() == "local" else "remote"


def _newer_mac_flag_path() -> Path:
    return paths.anisette_dir() / _NEWER_MAC_FLAG


def newer_mac_identity_active() -> bool:
    if _newer_mac_active:
        return True
    try:
        return _newer_mac_flag_path().exists()
    except OSError:
        return False


def use_newer_mac_identity() -> bool:
    global _newer_mac_active
    if provider_name() != "remote" or newer_mac_identity_active():
        return False
    _newer_mac_active = True
    try:
        _newer_mac_flag_path().write_text("1", encoding="utf-8")
    except OSError:
        pass
    return True


def gsa_user_agent() -> str | None:
    return _remote_user_agent if provider_name() == "remote" else None


def _get_local_headers() -> dict[str, str]:
    endpoint = os.environ.get("ALTSERVER_ANISETTE_SERVER", "http://127.0.0.1:6970")
    response = requests.get(endpoint, timeout=15)
    response.raise_for_status()
    headers = response.json()
    required = ("X-Apple-I-MD", "X-Apple-I-MD-M", "X-Apple-I-MD-LU", "X-Mme-Device-Id")
    if not all(isinstance(headers.get(key), str) and headers[key] for key in required):
        raise ValueError("Anisette service returned incomplete identity headers")
    cleaned = {key: str(value).strip() for key, value in headers.items()}
    cleaned["X-MMe-Client-Info"] = CLIENT_INFO
    cleaned["X-Apple-Locale"] = cleaned.get("X-Apple-Locale", "en_US").replace("-", "_")
    cleaned.setdefault("X-Apple-I-TimeZone", "UTC")
    return cleaned


def get_headers() -> dict[str, Any]:
    global _remote_user_agent
    if provider_name() == "local":
        _remote_user_agent = None
        return _get_local_headers()

    from . import anisette_remote

    fetched = anisette_remote.fetch()
    client_info, _remote_user_agent = fetched.identity.client_info, fetched.identity.user_agent
    if newer_mac_identity_active():
        client_info, _remote_user_agent = NEWER_MAC_CLIENT_INFO, NEWER_MAC_USER_AGENT

    cleaned = {key: str(value).strip() for key, value in fetched.headers.items()}
    cleaned["X-MMe-Client-Info"] = client_info
    cleaned["X-Apple-Locale"] = cleaned.get("X-Apple-Locale", "en_US").replace("-", "_")
    cleaned.setdefault("X-Apple-I-TimeZone", "UTC")
    return cleaned
