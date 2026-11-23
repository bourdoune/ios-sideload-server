"""Profile renewal using a cached developer session with safe silent token recovery."""
import base64
import os
from apple_portal import developer, gsa
from apple_portal.developer import DeveloperServicesError
from apple_portal.gsa import GsaError


def _is_session_expired(exc: DeveloperServicesError) -> bool:
    if getattr(exc, "is_session_expired", False):
        return True
    text = str(exc).lower()
    return text.startswith("1100:") or "session has expired" in text


def _recover_expired_session() -> None:
    """Refresh an expired developer token without triggering interactive 2FA."""
    try:
        gsa.refresh_app_token()
        return
    except GsaError as refresh_exc:
        email = os.getenv("APPLE_ID", "").strip()
        password = os.getenv("APPLE_PASS", "")
        if not (email and password):
            raise refresh_exc
        gsa.silent_relogin(email, password)


def _download_once(team_id: str, bundle_id: str, name: str) -> bytes:
    apps = developer.list_app_ids(team_id)
    app = next((item for item in apps if item.get("identifier") == bundle_id), None)
    if app is None:
        app = developer.add_app_id(team_id, bundle_id, name)
    profile = developer.download_profile(team_id, app["appIdId"])
    data = profile.get("encodedProfile")
    if isinstance(data, str):
        data = base64.b64decode(data, validate=True)
    if not isinstance(data, bytes) or not data:
        raise ValueError("Apple returned an empty provisioning profile")
    return data


def download_profile(team_id, bundle_id, name):
    # load_session fails locally when no login exists, before any Apple request.
    gsa.load_session()
    try:
        return _download_once(team_id, bundle_id, name)
    except DeveloperServicesError as exc:
        if not _is_session_expired(exc):
            raise
        _recover_expired_session()
        try:
            return _download_once(team_id, bundle_id, name)
        except DeveloperServicesError as retry_exc:
            if _is_session_expired(retry_exc):
                raise GsaError(f"Apple developer session expired: {retry_exc}") from retry_exc
            raise
