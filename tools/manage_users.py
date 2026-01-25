from __future__ import annotations

import argparse
import getpass
import sys

from app.config import S
from app import user_store


def _prompt_password(label: str) -> str:
    pw = getpass.getpass(label)
    if not pw:
        raise ValueError("password required")
    return pw


def cmd_create(args: argparse.Namespace) -> int:
    password = args.password or _prompt_password("New password: ")
    user = user_store.create_user(S.USER_DB_PATH, username=args.username, password=password)
    print(f"created user {user.username} (id={user.id})")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    password = args.password or _prompt_password("New password: ")
    user_store.set_password(S.USER_DB_PATH, username=args.username, password=password)
    print(f"password updated for {args.username}")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    user_store.disable_user(S.USER_DB_PATH, username=args.username, disabled=True)
    print(f"disabled user {args.username}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    user_store.disable_user(S.USER_DB_PATH, username=args.username, disabled=False)
    print(f"enabled user {args.username}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    users = user_store.list_users(S.USER_DB_PATH)
    for u in users:
        status = "disabled" if u.disabled else "active"
        print(f"{u.username}\t{u.id}\t{status}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage gateway users.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a new user")
    p_create.add_argument("username")
    p_create.add_argument("--password")
    p_create.set_defaults(func=cmd_create)

    p_reset = sub.add_parser("reset", help="reset a user's password")
    p_reset.add_argument("username")
    p_reset.add_argument("--password")
    p_reset.set_defaults(func=cmd_reset)

    p_disable = sub.add_parser("disable", help="disable a user")
    p_disable.add_argument("username")
    p_disable.set_defaults(func=cmd_disable)

    p_enable = sub.add_parser("enable", help="enable a user")
    p_enable.add_argument("username")
    p_enable.set_defaults(func=cmd_enable)

    p_list = sub.add_parser("list", help="list users")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
