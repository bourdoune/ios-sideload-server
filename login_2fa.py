#!/usr/bin/env python3
"""One login, one pending verification code, then cache the developer token."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import time
from dotenv import load_dotenv
from apple_portal import developer, gsa

BASE = Path(__file__).resolve().parent
STATUS = Path('/tmp/2fa_status')
CODE = Path('/tmp/2fa_code')
PAUSED = BASE / '.apple_auth_required'


def status(message):
    STATUS.write_text(message + '\n')
    print(message, flush=True)


def run(*, resend_pending=False, sms=False):
    load_dotenv(BASE / '.env')
    # Session files contain tokens, and must only be readable by this user.
    os.umask(0o077)
    with (BASE / '.apple_login.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        PAUSED.touch(mode=0o600)
        CODE.unlink(missing_ok=True)
        email, password = os.environ['APPLE_ID'], os.environ['APPLE_PASS']
        status('STARTING')
        try:
            if resend_pending:
                pending = gsa._load_pending()
                if not pending or pending.get('email', '').lower() != email.lower():
                    raise RuntimeError('No pending login for the configured account')
                # One explicit delivery request in the existing session. No new
                # password login, loop, or automatic resend is performed.
                headers = gsa.anisette.get_headers()
                if sms:
                    gsa._trigger_sms(pending['adsid'], pending['idms'], headers)
                    gsa._save_pending(email, pending['adsid'], pending['idms'], 'sms')
                elif pending['method'] == 'trusteddevice':
                    gsa._trigger_trusted(pending['adsid'], pending['idms'], headers)
                else:
                    gsa._trigger_sms(pending['adsid'], pending['idms'], headers)
                result = {'status': '2fa_required'}
            else:
                result = gsa.begin_login(email, password)
            if result['status'] == '2fa_required':
                status('WAITING_FOR_CODE_1')
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    if CODE.exists():
                        code = CODE.read_text().strip()
                        CODE.unlink(missing_ok=True)
                        if re.fullmatch(r'[0-9]{6}', code):
                            status('CODE_1_SUBMITTED')
                            result = gsa.complete_2fa(email, password, code)
                            break
                    time.sleep(0.2)
                else:
                    status('TIMED_OUT')
                    return 3
            # Confirm the token works for the configured team before unpausing.
            teams = developer.list_teams()
            if not any(team.get('teamId') == os.environ['TEAM_ID'] for team in teams):
                raise RuntimeError('Configured team is not available to this Apple account')
            PAUSED.unlink(missing_ok=True)
            status('DONE_0: developer session saved and team verified')
            return 0
        except Exception as exc:
            status(f'AUTH_FAILED: {exc}')
            return 1
        finally:
            CODE.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resend-pending', action='store_true', help='Request one code in the existing pending session')
    parser.add_argument('--sms', action='store_true', help='Use SMS for an existing pending login')
    args = parser.parse_args()
    if args.sms and not args.resend_pending:
        parser.error('--sms requires --resend-pending')
    raise SystemExit(run(resend_pending=args.resend_pending, sms=args.sms))
