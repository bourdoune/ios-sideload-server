from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import apple_client
import server_api
from apple_portal.gsa import GsaError


def test_download_reuses_existing_app_id(monkeypatch):
    monkeypatch.setattr(apple_client.gsa, 'load_session', Mock(return_value={}))
    monkeypatch.setattr(apple_client.developer, 'list_app_ids', lambda team: [
        {'identifier': 'com.example.TEST', 'appIdId': 'existing'}])
    add = Mock()
    monkeypatch.setattr(apple_client.developer, 'add_app_id', add)
    download = Mock(return_value={'encodedProfile': b'profile'})
    monkeypatch.setattr(apple_client.developer, 'download_profile', download)
    assert apple_client.download_profile('TEST', 'com.example.TEST', 'Example') == b'profile'
    download.assert_called_once_with('TEST', 'existing')
    add.assert_not_called()


def test_missing_login_never_contacts_developer_portal(monkeypatch):
    monkeypatch.setattr(apple_client.gsa, 'load_session', Mock(side_effect=GsaError('not signed in')))
    request = Mock()
    monkeypatch.setattr(apple_client.developer, 'list_app_ids', request)
    import pytest
    with pytest.raises(GsaError):
        apple_client.download_profile('TEST', 'com.example.TEST', 'Example')
    request.assert_not_called()


def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(server_api, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(server_api, 'PROFILES_DIR', str(tmp_path))
    monkeypatch.setattr(server_api, 'TEAM_ID', 'TEST')
    local = tmp_path / 'com.example.TEST.mobileprovision'
    local.write_bytes(b'existing profile')
    return local


def test_paused_login_keeps_existing_profile_without_request(monkeypatch, tmp_path):
    local = configure(monkeypatch, tmp_path)
    (tmp_path / '.apple_auth_required').touch()
    monkeypatch.setattr(server_api, 'extract_profile_expiration', lambda path: None)
    request = Mock()
    monkeypatch.setattr(server_api, 'download_apple_profile', request)
    path, renewed, _ = server_api.fetch_fresh_provisioning_profile_from_apple('com.example', 'Example')
    assert path == str(local) and not renewed
    request.assert_not_called()
    assert local.read_bytes() == b'existing profile'


def test_failed_auth_pauses_jobs_and_keeps_existing_profile(monkeypatch, tmp_path):
    local = configure(monkeypatch, tmp_path)
    expiration = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(server_api, 'extract_profile_expiration', lambda path: expiration)
    request = Mock(side_effect=GsaError('expired session'))
    monkeypatch.setattr(server_api, 'download_apple_profile', request)
    path, renewed, _ = server_api.fetch_fresh_provisioning_profile_from_apple('com.example', 'Example')
    assert path == str(local) and not renewed
    assert (tmp_path / '.apple_auth_required').exists()
    assert local.read_bytes() == b'existing profile'


def test_successful_renewal_replaces_profile_atomically(monkeypatch, tmp_path):
    local = configure(monkeypatch, tmp_path)
    old = datetime.now(timezone.utc) + timedelta(days=2)
    fresh = old + timedelta(days=5)
    monkeypatch.setattr(server_api, 'extract_profile_expiration', lambda path: fresh if path.endswith('.tmp') else old)
    request = Mock(return_value=b'fresh profile')
    monkeypatch.setattr(server_api, 'download_apple_profile', request)
    path, renewed, _ = server_api.fetch_fresh_provisioning_profile_from_apple('com.example', 'Example')
    assert path == str(local) and renewed
    assert local.read_bytes() == b'fresh profile'
    assert not list(tmp_path.glob('*.tmp'))
