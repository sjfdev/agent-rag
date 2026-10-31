"""Mint a bearer token for AUTH_MODE=jwt deployments.

Usage:
    AUTH_JWT_SECRET=... python scripts/mint_token.py \
        --user alice --dept Tech --dept HR --group exec --ttl 3600

The same signing secret must be configured in the server environment. Tokens
are HS256 JWTs, so any gateway able to sign one can issue them without this
script.

    # quick local check
    TOKEN=$(python scripts/mint_token.py --user u1 --dept Tech --ttl 600)
    curl -H "Authorization: Bearer $TOKEN" -X POST localhost:8000/chat \
      -H 'Content-Type: application/json' \
      -d '{"message":"What is the code review process?"}'
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth
from src.config import settings
from src.validation import InvalidDepartment, validate_dept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="user id (sub claim)")
    parser.add_argument(
        "--dept", action="append", default=[], help="department (repeatable)"
    )
    parser.add_argument(
        "--group", action="append", default=[], help="ACL group (repeatable)"
    )
    parser.add_argument("--admin", action="store_true", help="grant admin rights")
    parser.add_argument(
        "--ttl", type=int, default=settings.AUTH_TOKEN_TTL, help="token lifetime (s)"
    )
    args = parser.parse_args()

    if not (settings.AUTH_JWT_SECRET or "").strip():
        print("AUTH_JWT_SECRET is not set (env or .env)", file=sys.stderr)
        return 1
    if not args.dept:
        parser.error("at least one --dept is required")

    try:
        depts = [validate_dept(dept) for dept in args.dept]
    except InvalidDepartment as exc:
        print(f"invalid department: {exc}", file=sys.stderr)
        return 1

    identity = auth.Identity(
        user_id=args.user.strip(),
        departments=tuple(depts),
        groups=tuple(args.group),
        is_admin=args.admin,
    )
    token = auth.encode_token(identity, ttl_seconds=args.ttl)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
