"""Manage local accounts for AUTH_LOCAL_LOGIN_ENABLED deployments.

Run this from the project root with the same environment as the API server, so
`AUTH_USERS_FILE` resolves to the file the app reads.

    # create an admin
    python scripts/manage_users.py add alice --dept Tech --dept HR --admin

    # list / inspect
    python scripts/manage_users.py list

    # change permissions (departments replace, not merge)
    python scripts/manage_users.py set alice --dept Finance --no-admin

    # rotate a password or lock an account out
    python scripts/manage_users.py passwd alice
    python scripts/manage_users.py disable alice
    python scripts/manage_users.py enable alice
    python scripts/manage_users.py delete alice

Password changes and permission changes bump the account's `token_version`, so
every token previously issued to that user stops working immediately.

The password is read from the terminal (never from argv, so it stays out of the
shell history). With no TTY (CI, scripts) it is read from stdin:

    printf '%s' "$NEW_PASSWORD" | python scripts/manage_users.py add bob --dept Tech
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import local_auth  # noqa: E402
from src.config import settings  # noqa: E402


def _read_password(confirm: bool) -> str:
    """Read a password from the terminal, or from stdin when there is no TTY."""
    if not sys.stdin.isatty():
        value = sys.stdin.read().strip()
        if not value:
            raise SystemExit("no password on stdin")
        return value
    first = getpass.getpass("password: ")
    if confirm and first != getpass.getpass("repeat password: "):
        raise SystemExit("passwords do not match")
    return first


def _require_enabled() -> None:
    if not settings.AUTH_LOCAL_LOGIN_ENABLED:
        print(
            "warning: AUTH_LOCAL_LOGIN_ENABLED is not set; the server will reject "
            "these credentials until you enable it.",
            file=sys.stderr,
        )
    if settings.AUTH_MODE != "jwt":
        print(
            "warning: AUTH_MODE is not 'jwt'; local accounts only work in jwt mode.",
            file=sys.stderr,
        )


def _show(record) -> None:
    if record is None:
        print("(user not found)")
        return
    flags = []
    if record["admin"]:
        flags.append("admin")
    if record["disabled"]:
        flags.append("disabled")
    print(
        "{username}\n  depts: {depts}\n  groups: {groups}\n  flags: {flags}\n"
        "  token_version: {ver}\n  updated: {updated}".format(
            username=record["username"],
            depts=", ".join(record["depts"]) or "-",
            groups=", ".join(record["groups"]) or "-",
            flags=", ".join(flags) or "-",
            ver=record["token_version"],
            updated=record["updated"] or "-",
        )
    )


def cmd_add(args) -> int:
    password = _read_password(confirm=True)
    try:
        record = local_auth.create_user(
            args.username,
            password,
            depts=args.dept,
            groups=args.group,
            admin=args.admin,
        )
    except local_auth.LocalAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created {record['username']}")
    _show(record)
    return 0


def cmd_list(_args) -> int:
    users = local_auth.list_users()
    if not users:
        print(f"no accounts in {settings.AUTH_USERS_FILE}")
        return 0
    for record in users:
        _show(record)
    print(f"\n{len(users)} account(s) in {settings.AUTH_USERS_FILE}")
    return 0


def cmd_show(args) -> int:
    _show(local_auth.get_user_public(args.username))
    return 0


def cmd_set(args) -> int:
    changes = {}
    if args.dept is not None:
        changes["depts"] = args.dept
    if args.group is not None:
        changes["groups"] = args.group
    if args.admin:
        changes["admin"] = True
    if args.no_admin:
        changes["admin"] = False
    if args.enable:
        changes["disabled"] = False
    if args.disable:
        changes["disabled"] = True
    if not changes:
        print("nothing to change (use --dept/--group/--admin/--no-admin/--disable)")
        return 1
    current = local_auth.get_user(args.username)
    if current is None:
        print(f"error: user {args.username!r} does not exist", file=sys.stderr)
        return 1
    # Check before writing: a rejected change must not be half-applied.
    was_active_admin = bool(current.get("admin")) and not current.get("disabled")
    stays_admin = changes.get("admin", current.get("admin"))
    stays_enabled = not changes.get("disabled", current.get("disabled"))
    if was_active_admin and not (stays_admin and stays_enabled):
        if local_auth.admin_count() <= 1:
            print(
                "error: that would leave no enabled administrator",
                file=sys.stderr,
            )
            return 1
    try:
        record = local_auth.update_user(args.username, **changes)
    except local_auth.LocalAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("updated")
    _show(record)
    return 0


def cmd_passwd(args) -> int:
    password = _read_password(confirm=True)
    try:
        local_auth.set_password(args.username, password)
    except local_auth.LocalAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"password changed for {args.username}; existing tokens are now invalid")
    return 0


def cmd_disable(args) -> int:
    record = local_auth.get_user(args.username)
    if record is None:
        print(f"error: user {args.username!r} does not exist", file=sys.stderr)
        return 1
    if record.get("admin") and local_auth.admin_count() <= 1:
        print(
            "error: the last enabled administrator cannot be disabled",
            file=sys.stderr,
        )
        return 1
    local_auth.update_user(args.username, disabled=True)
    print(f"disabled {args.username}; existing tokens are now invalid")
    return 0


def cmd_enable(args) -> int:
    if local_auth.get_user(args.username) is None:
        print(f"error: user {args.username!r} does not exist", file=sys.stderr)
        return 1
    try:
        local_auth.update_user(args.username, disabled=False)
    except local_auth.LocalAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"enabled {args.username}")
    return 0


def cmd_delete(args) -> int:
    record = local_auth.get_user(args.username)
    if record is None:
        print(f"error: user {args.username!r} does not exist", file=sys.stderr)
        return 1
    if record.get("admin") and local_auth.admin_count() <= 1:
        print(
            "error: the last enabled administrator cannot be deleted",
            file=sys.stderr,
        )
        return 1
    local_auth.delete_user(args.username)
    print(f"deleted {args.username}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--users-file",
        help=f"override AUTH_USERS_FILE (default: {settings.AUTH_USERS_FILE})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create an account")
    add.add_argument("username")
    add.add_argument("--dept", action="append", default=[], help="repeatable")
    add.add_argument("--group", action="append", default=[], help="repeatable")
    add.add_argument("--admin", action="store_true")
    add.set_defaults(func=cmd_add)

    listing = sub.add_parser("list", help="list accounts")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one account")
    show.add_argument("username")
    show.set_defaults(func=cmd_show)

    setter = sub.add_parser("set", help="change departments/groups/flags")
    setter.add_argument("username")
    setter.add_argument("--dept", action="append", help="replace departments")
    setter.add_argument("--group", action="append", help="replace groups")
    setter.add_argument("--admin", action="store_true")
    setter.add_argument("--no-admin", action="store_true")
    setter.add_argument("--enable", action="store_true")
    setter.add_argument("--disable", action="store_true")
    setter.set_defaults(func=cmd_set)

    passwd = sub.add_parser("passwd", help="change a password (revokes tokens)")
    passwd.add_argument("username")
    passwd.set_defaults(func=cmd_passwd)

    disable = sub.add_parser("disable", help="block login and revoke tokens")
    disable.add_argument("username")
    disable.set_defaults(func=cmd_disable)

    enable = sub.add_parser("enable", help="allow login again")
    enable.add_argument("username")
    enable.set_defaults(func=cmd_enable)

    delete = sub.add_parser("delete", help="remove an account")
    delete.add_argument("username")
    delete.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    if args.users_file:
        settings.AUTH_USERS_FILE = args.users_file
        local_auth.clear_cache()
    if args.command != "list":
        _require_enabled()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
