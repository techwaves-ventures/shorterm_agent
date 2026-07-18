"""Admin CLI for the multi-tenant dashboard.

    python manage.py bootstrap                 # ensure operator tenant + login (from .env)
    python manage.py init                      # hosted one-shot: operator + demo seed data
    python manage.py set-password <email>      # set/reset a user's password (prompts)
    python manage.py create-user <email>       # create a new tenant + user (prompts)
    python manage.py list-users                # show all users and their tenants
    python manage.py seed-demo                 # create/refresh the demo tenant + sample data
"""
import getpass
import sys

from dotenv import load_dotenv

load_dotenv()

import models


def _prompt_password() -> str:
    pw = getpass.getpass("New password: ")
    if pw != getpass.getpass("Confirm password: "):
        sys.exit("Passwords did not match.")
    if not pw:
        sys.exit("Password cannot be empty.")
    return pw


def cmd_bootstrap() -> None:
    models.ensure_operator()
    op = None
    import os

    email = (os.getenv("OPERATOR_EMAIL") or "").strip().lower()
    if email:
        op = models.get_user_by_email(email)
    if op:
        print(f"Operator ready: {op.email} (tenant {op.tenant_id}).")
    else:
        print(
            "Operator tenant ready, but no login was created "
            "(set OPERATOR_EMAIL/OPERATOR_PASSWORD in .env, or run "
            "`python manage.py set-password <email>`)."
        )


def cmd_set_password(email: str) -> None:
    pw = _prompt_password()
    if models.set_password(email, pw):
        print(f"Password updated for {email}.")
    else:
        sys.exit(f"No user with email {email}.")


def cmd_create_user(email: str) -> None:
    pw = _prompt_password()
    user = models.create_user(email, pw)
    print(f"Created {user.email} (tenant {user.tenant_id}).")


def cmd_list_users() -> None:
    import db

    with db.connect() as c:
        rows = c.execute(
            "SELECT u.id, u.email, u.tenant_id, t.is_operator, t.name "
            "FROM users u JOIN tenants t ON t.id = u.tenant_id ORDER BY u.id"
        ).fetchall()
    if not rows:
        print("No users yet.")
        return
    for uid, email, tid, is_op, name in rows:
        tag = " [operator]" if is_op else ""
        print(f"#{uid}  {email}  → tenant {tid} ({name}){tag}")


def cmd_init() -> None:
    """One-shot hosted bootstrap: operator tenant/login + demo seed data.

    Run once against a fresh hosted DB (DATABASE_URL set) to make the instance
    demo-ready: `python manage.py init`. Idempotent — safe to re-run.
    """
    import db

    print(f"Database backend: {db.backend()}")
    models.ensure_operator()
    import seed_demo

    email, tid = seed_demo.seed_demo()
    print(f"Operator tenant ready (tenant {models.OPERATOR_TENANT_ID}).")
    print(f"Demo tenant ready: {email} (tenant {tid}).")
    print(f"Demo login password: {seed_demo.DEMO_PASSWORD}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    if cmd == "bootstrap":
        cmd_bootstrap()
    elif cmd == "init":
        cmd_init()
    elif cmd == "set-password":
        if not rest:
            sys.exit("Usage: python manage.py set-password <email>")
        cmd_set_password(rest[0].strip().lower())
    elif cmd == "create-user":
        if not rest:
            sys.exit("Usage: python manage.py create-user <email>")
        cmd_create_user(rest[0].strip().lower())
    elif cmd == "list-users":
        cmd_list_users()
    elif cmd == "seed-demo":
        import seed_demo

        email, tid = seed_demo.seed_demo()
        print(f"Demo tenant ready: {email} (tenant {tid}).")
        print(f"Password: {seed_demo.DEMO_PASSWORD}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
