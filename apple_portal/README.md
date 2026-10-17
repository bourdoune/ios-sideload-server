Apple authentication and Developer Services code adapted from iPASide v1.2.8,
commit 6d86605, https://github.com/pwnapplehat/iPASide (MIT; see LICENSE).

Local changes:
- anisette.py uses the existing local anisette service, retaining its device identity.
- paths.py stores tokens under ~/.local/share/altserver-apple with private directories.
- gsa.py, developer.py, errors.py and tls.py retain upstream behavior.

login_2fa.py performs interactive login once and caches the scoped developer token.
Background renewal uses that token and never starts password authentication or 2FA.
Apple's hsc=409 without au means the code was already pushed; do not trigger another.
