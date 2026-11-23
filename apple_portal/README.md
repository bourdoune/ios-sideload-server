Apple authentication and Developer Services code adapted from iPASide v1.2.8–v1.2.17,
https://github.com/pwnapplehat/iPASide (MIT; see LICENSE).

Local changes:
- anisette_remote.py and anisette.py default to the community Mac Anisette v3 protocol
  (mac-ani.jkcoxson.com / new.ani.sidestore.io / ani3.stikstore.app) with per-host
  identity cached under ~/.local/share/altserver-apple/anisette/remote-v3.json.
  Set ALTSERVER_ANISETTE_MODE=local to use ALTSERVER_ANISETTE_SERVER instead.
- paths.py stores tokens under ~/.local/share/altserver-apple with private 0700 directories.
- gsa.py persists sk and c alongside GsIdmsToken so refresh_app_token() can re-mint
  expired developer tokens via GsService2/apptokens without password or 2FA, plus
  silent_relogin() for one-shot non-interactive fallback when no 2FA is required.
- developer.py exposes resultCode and is_session_expired on DeveloperServicesError.

login_2fa.py performs interactive login and caches the developer session.
Background renewal never triggers a 2FA push; if Apple requires interactive verification,
server_api.py writes .apple_auth_required and pauses background retries.
