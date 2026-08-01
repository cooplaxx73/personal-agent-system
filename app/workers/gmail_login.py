"""One-time login for a single Gmail account. Run this once per account you
want to add -- it opens your browser, you pick/log into that Google account
and click Allow. Never sees or handles your password.

Usage: python gmail_login.py <account_name>
Account name must match one of the names in ACCOUNTS in gmail_worker.py.
"""

import os
import sys

from gmail_worker import get_service, ACCOUNTS, _token_path, _scopes_for

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gmail_login.py <account_name>")
        print(f"Configured account names: {ACCOUNTS}")
        sys.exit(1)

    account = sys.argv[1]
    if account not in ACCOUNTS:
        print(f"'{account}' isn't in ACCOUNTS in gmail_worker.py: {ACCOUNTS}")
        print("Rename one of the existing placeholders there first, then rerun.")
        sys.exit(1)

    # Force a fresh consent by clearing the old token, so scope changes (e.g.
    # newly-enabled sending) actually take effect on re-login.
    tp = _token_path(account)
    if os.path.exists(tp):
        os.remove(tp)

    print(f"Logging in for account: {account}")
    print(f"Permissions requested: {_scopes_for(account)}")
    get_service(account)
    print(f"Done -- token saved for '{account}'.")
