"""Profile renewal using a cached developer session; never start 2FA in a job."""
import base64
from apple_portal import developer, gsa


def download_profile(team_id, bundle_id, name):
    # load_session fails locally when login is needed, before any Apple request.
    gsa.load_session()
    apps = developer.list_app_ids(team_id)
    app = next((item for item in apps if item.get('identifier') == bundle_id), None)
    if app is None:
        app = developer.add_app_id(team_id, bundle_id, name)
    profile = developer.download_profile(team_id, app['appIdId'])
    data = profile.get('encodedProfile')
    if isinstance(data, str):
        data = base64.b64decode(data, validate=True)
    if not isinstance(data, bytes) or not data:
        raise ValueError('Apple returned an empty provisioning profile')
    return data
