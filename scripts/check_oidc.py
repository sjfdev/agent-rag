"""Check an OIDC/JWKS setup without needing real user accounts.

Two modes:

1. Offline self-test (no IdP): generates a throwaway RSA key pair, serves the
   public key as a JWKS file, signs a token with Keycloak-style claims and asks
   the configured verifier to accept it.

       python scripts/check_oidc.py --self-test

2. Against a real provider: fetches the JWKS URL and reports which keys and
   algorithms are advertised, so you can confirm network access and config
   before users try to log in.

       python scripts/check_oidc.py --jwks https://idp.example.com/realms/kb/protocol/openid-connect/certs
       python scripts/check_oidc.py --issuer https://idp.example.com/realms/kb

Exit code is non-zero on failure, so it can gate a deployment.
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src import auth
from src.config import settings

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""))


def self_test() -> int:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update({"kid": "self-test", "alg": "RS256", "use": "sig"})

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "jwks.json"
        path.write_text(json.dumps({"keys": [public]}))

        old = {
            "AUTH_MODE": settings.AUTH_MODE,
            "AUTH_JWKS_FILE": settings.AUTH_JWKS_FILE,
            "AUTH_JWKS_URL": settings.AUTH_JWKS_URL,
            "AUTH_JWT_SECRET": settings.AUTH_JWT_SECRET,
            "AUTH_CLAIM_ROLES": settings.AUTH_CLAIM_ROLES,
            "AUTH_ADMIN_ROLE": settings.AUTH_ADMIN_ROLE,
            "AUTH_ROLE_DEPT_PREFIX": settings.AUTH_ROLE_DEPT_PREFIX,
        }
        try:
            settings.AUTH_MODE = "jwt"
            settings.AUTH_JWKS_FILE = str(path)
            settings.AUTH_JWKS_URL = ""
            settings.AUTH_JWT_SECRET = ""
            settings.AUTH_CLAIM_ROLES = "realm_access.roles"
            settings.AUTH_ADMIN_ROLE = "kb-admin"
            settings.AUTH_ROLE_DEPT_PREFIX = "kb:dept:"
            auth.clear_jwks_cache()

            auth.validate_config()
            check("config valid without a shared secret", True)

            token = jwt.encode(
                {
                    "sub": "self-test-user",
                    "exp": int(time.time()) + 60,
                    "realm_access": {"roles": ["kb-admin", "kb:dept:Tech"]},
                },
                key,
                algorithm="RS256",
                headers={"kid": "self-test"},
            )
            identity = auth.decode_token(token)
            check("token verified with JWKS", identity.user_id == "self-test-user")
            check("role grants admin", identity.is_admin is True)
            check("role grants department", identity.departments == ("Tech",))

            other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            forged = jwt.encode(
                {"sub": "x", "exp": int(time.time()) + 60},
                other,
                algorithm="RS256",
                headers={"kid": "self-test"},
            )
            try:
                auth.decode_token(forged)
                check("forged token rejected", False)
            except auth.AuthError:
                check("forged token rejected", True)
        finally:
            for field, value in old.items():
                setattr(settings, field, value)
            auth.clear_jwks_cache()

    print(f"\nRESULT: {'OK' if not FAIL else 'FAILED'} - {len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


def _discover(jwks_url: str) -> tuple:
    response = requests.get(jwks_url, timeout=10)
    response.raise_for_status()
    keys = response.json().get("keys", [])
    algs = sorted({key.get("alg", "?") for key in keys})
    return keys, algs


def remote(jwks_url: str, issuer: str) -> int:
    print(f"JWKS: {jwks_url}")
    try:
        keys, algs = _discover(jwks_url)
    except Exception as exc:
        check("JWKS reachable", False, f"{type(exc).__name__}: {exc}")
        print(f"\nRESULT: FAILED - {len(PASS)} passed, {len(FAIL)} failed")
        return 1
    check("JWKS reachable", True)
    check("keys advertised", bool(keys), "no keys in the document")
    print(f"      kid(s): {', '.join(str(k.get('kid')) for k in keys)}")
    print(f"      alg(s): {', '.join(algs)}")

    if issuer:
        discovery = issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            doc = requests.get(discovery, timeout=10).json()
        except Exception as exc:
            check("issuer discovery", False, f"{type(exc).__name__}: {exc}")
        else:
            check("issuer discovery", bool(doc.get("jwks_uri")), str(doc)[:120])
            print(f"      issuer: {doc.get('issuer')}")
            print(f"      jwks_uri: {doc.get('jwks_uri')}")
            print(f"      token endpoint: {doc.get('token_endpoint')}")
            if doc.get("jwks_uri") and doc["jwks_uri"] != jwks_url:
                print("      note: discovered jwks_uri differs from --jwks")

    print("\n下一步：把 AUTH_JWKS_URL 设为上面的 jwks_uri，")
    print("按 IdP 的 claim 结构配置 AUTH_CLAIM_ROLES / AUTH_ADMIN_ROLE / AUTH_ROLE_DEPT_PREFIX，")
    print("然后重启服务并用真实账号登录管理台验证。")
    print(f"\nRESULT: {'OK' if not FAIL else 'FAILED'} - {len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="offline JWKS check")
    parser.add_argument("--jwks", help="JWKS URL to inspect")
    parser.add_argument("--issuer", help="OIDC issuer URL for discovery")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.jwks and not args.issuer:
        parser.error("use --self-test or --jwks/--issuer")

    jwks_url = args.jwks
    if not jwks_url and args.issuer:
        discovery = args.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            jwks_url = requests.get(discovery, timeout=10).json().get("jwks_uri")
        except Exception as exc:
            print(f"FAIL  discovery: {exc}")
            return 1
    return remote(jwks_url, args.issuer or "")


if __name__ == "__main__":
    raise SystemExit(main())
