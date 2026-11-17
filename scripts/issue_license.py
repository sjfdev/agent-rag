"""Issue and verify offline licenses for private deployments.

The server only needs the license file; the private key never leaves the
vendor machine. Keep ``config/license_private.pem`` in a password manager or
an offline drive and never commit it (it is gitignored).

    # one-time: create the signing key (public key is printed for embedding)
    python scripts/issue_license.py --init-keys

    # issue a one-year license for a customer
    python scripts/issue_license.py --licensee "ACME Corp" --days 365 \
        --edition enterprise --depts 5 --docs 100000 \
        --features sso,audit --out /tmp/acme-license.json

    # verify what the customer received (uses the embedded/public key)
    python scripts/issue_license.py --verify /tmp/acme-license.json

Ship the resulting ``license.json`` to ``config/license.json`` on the customer
machine and set ``LICENSE_ENABLED=true``. That is a contract anchor rather than
DRM: the source is MIT-licensed, so enforcement depends on the agreement, not
on hiding the check.
"""

import argparse
import json
import os
import secrets
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import license as license_module
from src.config import settings

DEFAULT_KEY = "./config/license_private.pem"
DEFAULT_OUT = "./config/license.json"


def init_keys(path: str) -> int:
    target = Path(path)
    if target.exists():
        print(f"{target} already exists; refusing to overwrite", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    import base64

    print(f"wrote {target} (0600)")
    print("public key (embed in src/license.py or set LICENSE_PUBLIC_KEY):")
    print(base64.b64encode(public).decode("ascii"))
    print("keep the private key offline; anyone who has it can forge licenses")
    return 0


def _feature_list(raw) -> list:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def build_payload(
    licensee: str,
    *,
    edition: str = "enterprise",
    days: int = 365,
    expires=None,
    issued_at=None,
    not_before=None,
    depts: int = 0,
    docs: int = 0,
    features=None,
    license_id: str = "",
) -> dict:
    """Build a validated payload; shared by the CLI and the local web console.

    Dates may be ``date`` objects or ISO strings; ``expires`` overrides ``days``.
    """
    today = date.today()

    def as_date(value, fallback):
        if value in (None, ""):
            return fallback
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    issued = as_date(issued_at, today)
    expiry = as_date(expires, issued + timedelta(days=int(days)))
    activation = as_date(not_before, None)
    payload = {
        "v": license_module.LICENSE_VERSION,
        "license_id": license_id or f"LIC-{issued:%Y%m%d}-{secrets.token_hex(3).upper()}",
        "licensee": (licensee or "").strip(),
        "edition": (edition or "enterprise").strip(),
        "issued_at": issued.isoformat(),
        "expires_at": expiry.isoformat(),
        "max_depts": int(depts or 0),
        "max_docs": int(docs or 0),
        "features": _feature_list(features),
    }
    if activation:
        payload["not_before"] = activation.isoformat()
    license_module._validate_payload(payload)
    return payload


def issue(args) -> int:
    try:
        payload = build_payload(
            args.licensee,
            edition=args.edition,
            days=args.days,
            expires=args.expires,
            issued_at=args.issued_at,
            not_before=args.not_before,
            depts=args.depts,
            docs=args.docs,
            features=args.features,
            license_id=args.license_id or "",
        )
    except (ValueError, license_module.LicenseError) as exc:
        print(f"invalid license: {exc}", file=sys.stderr)
        return 1

    try:
        key = license_module.load_private_key(args.key)
        blob = license_module.sign_payload(payload, key)
        # Self-check with the vendor public key before writing anything.
        license_module.verify_blob(blob)
    except license_module.LicenseError as exc:
        print(f"cannot sign license: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(blob, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    print(f"licensee={payload['licensee']} edition={payload['edition']} "
          f"expires={payload['expires_at']} depts={payload['max_depts']} "
          f"docs={payload['max_docs']}")
    return 0


def verify(path: str, public_key: str) -> int:
    try:
        payload = license_module.verify_file(path, public_key)
    except license_module.LicenseError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print(f"OK    {payload['licensee']} ({payload.get('edition', 'standard')})")
    print(f"      expires {payload['expires_at']}  id={payload.get('license_id')}")
    print(f"      limits: {payload.get('max_depts', 0)} dept(s), "
          f"{payload.get('max_docs', 0)} doc(s); features={payload.get('features', [])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-keys", action="store_true", help="create an Ed25519 signing key")
    parser.add_argument("--key", default=DEFAULT_KEY, help=f"private key (default {DEFAULT_KEY})")
    parser.add_argument("--licensee", help="customer name")
    parser.add_argument("--license-id", help="license id (generated when omitted)")
    parser.add_argument("--edition", default="enterprise", help="edition label")
    parser.add_argument("--days", type=int, default=365, help="validity in days (default 365)")
    parser.add_argument("--expires", help="explicit expiry date YYYY-MM-DD")
    parser.add_argument("--issued-at", help="issue date YYYY-MM-DD (default today)")
    parser.add_argument("--not-before", help="activation date YYYY-MM-DD")
    parser.add_argument("--depts", type=int, default=0, help="department limit (0 = unlimited)")
    parser.add_argument("--docs", type=int, default=0, help="document limit (0 = unlimited)")
    parser.add_argument("--features", default="", help="comma-separated feature flags")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"output file (default {DEFAULT_OUT})")
    parser.add_argument("--verify", help="verify an existing license file")
    parser.add_argument("--public-key", default="", help="base64/PEM key for --verify")
    args = parser.parse_args()

    if args.init_keys:
        return init_keys(args.key)
    if args.verify:
        return verify(args.verify, args.public_key or settings.LICENSE_PUBLIC_KEY)
    if not args.licensee:
        parser.error("--licensee is required unless --init-keys/--verify is used")
    return issue(args)


if __name__ == "__main__":
    raise SystemExit(main())
