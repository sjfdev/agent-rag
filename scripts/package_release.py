"""Build a self-contained delivery bundle for a private deployment.

    python scripts/package_release.py                 # source bundle + tarball
    python scripts/package_release.py --with-image    # also `docker save` the app image
    python scripts/package_release.py --out /tmp/pkg --keep-staging

What ships (nothing else: no data/, logs/, volumes/, .env, tests or private keys):

  * the production compose files, Dockerfile, Makefile, requirements and docs
  * src/, static/, scripts/, eval/, monitoring/ and the ACL example
  * PACKAGE.json (version, commit, sha256 per file) and INSTALL.md

The bundle is written to ``dist/agent-rag-<version>-delivery[.tar.gz]`` with a
``.sha256`` sidecar, so the same artefact can be handed over and verified.

This is a *vendor* tool: it reads the git tree, never the running deployment.
Building the image happens here (``--with-image``) if Docker is available; the
customer can otherwise build from the source bundle with the same Dockerfile.
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.version import __version__  # noqa: E402

TOP_FILES = [
    "Dockerfile",
    "Makefile",
    "requirements.txt",
    "docker-compose.prod.yml",
    "docker-compose.monitoring.yml",
    "main.py",
    "run_ingestion.py",
    ".env.example",
    "LICENSE",
    "README.md",
    "QUICKSTART.md",
    "DEPLOYMENT.md",
    "DEPLOYMENT_CN.md",
    "AGENT_GUIDE.md",
    "CHANGELOG.md",
    "config/acl.example.json",
]
DIRS = ["src", "static", "scripts", "eval", "monitoring"]

# Never ship these, even if they appear inside a copied directory. Secrets can
# live next to committed files (a rendered Alertmanager config has SMTP creds,
# a private license key must stay offline).
SKIP_NAMES = {
    "__pycache__",
    ".DS_Store",
    ".pytest_cache",
    ".ruff_cache",
    "alertmanager.yml",
    "alertmanager.yml.tmp",
    "smtp_password",
    "metrics_token",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".pem", ".key")


def _skip(path: Path) -> bool:
    return path.name in SKIP_NAMES or path.name.endswith(SKIP_SUFFIXES)


def _ignore(directory: str, names):
    return {name for name in names if _skip(Path(directory) / name)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        return ""


def _copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_dir():
        shutil.copytree(source, target, ignore=_ignore)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


INSTALL_TEMPLATE = """# Agent RAG 私有化交付包 / Delivery package

版本 / Version: `{version}`

构建 / Built: `{created_at}`

提交 / Commit: `{commit}`{dirty}
{license_line}
## 1. 前置条件 / Prerequisites

- Docker Engine + Compose v2（`docker compose version`）
- 8 GB 内存（Milvus 限额 5 GB）、20 GB 可用磁盘
- 一个 OpenAI 兼容的对话模型端点（客户自有 Key，写在 `.env` 的 `LLM_*`）
- 维护脚本（doctor/backup/restore）需要本机 Python 3.11；日常启动不需要

## 2. 配置 / Configure

```bash
cp .env.example .env
# 必填：LLM_API_BASE / LLM_API_KEY / LLM_MODEL
# 认证二选一：AUTH_MODE=none + CHAT_API_KEY，或 AUTH_MODE=jwt + 本地账号
# 许可 / license（本包已默认开启）：
#   LICENSE_ENABLED=true
#   LICENSE_FILE=/app/config/license.json
```

将供应商签发的 `license.json` 放到 `config/license.json`（`chmod 600`）——若本包
已包含许可，这一步可跳过，启动后在管理台「许可」卡片可核对授权方与到期日。
许可状态：有效期内容器正常；过期后进入 `LICENSE_GRACE_DAYS` 宽限期，宽限
期结束只阻止管理端写入（入库/上传/改模型），**对话与引用照常可用**；许可
缺失或签名不符时服务拒绝启动，因此**首次安装必须在启动前放好许可文件**。
续期不必停服：在管理台「许可」卡片粘贴供应商新签发的 JSON 即可（宽限期内也
可操作），保存后立即生效。`LICENSE_ENABLED=false` 仅用于评估环境。

## 3. 启动 / Start

源码包 / source bundle:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

镜像包 / image bundle:

```bash
docker load -i image/agent-rag-{version}.tar.gz
docker compose -f docker-compose.prod.yml up -d
```

首次启动后入库 / first ingestion:

```bash
docker compose -f docker-compose.prod.yml exec app python run_ingestion.py
```

## 4. 验收 / Verify

```bash
curl -sk https://$KB_SITE_ADDRESS/info | python -m json.tool   # 看 license.state
curl -sk https://$KB_SITE_ADDRESS/health                      # status=ok
docker compose -f docker-compose.prod.yml ps                  # 全部 healthy
```

管理台 `/admin`（账号见 `.env` 的 `ADMIN_TOKEN` 或本地账号），对话页 `/ui/`。

## 5. 运维 / Operations

- 每日备份：宿主执行 `python scripts/daily_backup.py`（可配 launchd/cron），
  或 `python scripts/backup.py` 手动归档
- 恢复：`python scripts/restore.py backups/<archive>.tar.gz`（先停栈）
- 健康/自检：`python scripts/doctor.py`、`python scripts/check_health.py --probe`
- 监控（可选）：`make monitor-up` / `docker compose -f docker-compose.monitoring.yml up -d`
- 升级：备份 → 替换交付包文件（保留 `data/`、`config/`、`logs/`、`volumes-prod/`）
  → `docker compose -f docker-compose.prod.yml up -d --build` → `/health` + 抽样提问

## 6. 文件清单 / Manifest

逐文件 SHA256 见 `PACKAGE.json`。校验整包：
`sha256sum -c agent-rag-{version}-delivery.tar.gz.sha256`
"""


def build(
    out_dir: Path,
    *,
    with_image: bool = False,
    image: str = "",
    license_file: str = "",
    keep_staging: bool = False,
    force: bool = False,
) -> dict:
    version = __version__
    bundle_name = f"agent-rag-{version}-delivery"
    staging = out_dir / bundle_name
    archive = out_dir / f"{bundle_name}.tar.gz"

    if staging.exists() or archive.exists():
        if not force:
            raise SystemExit(f"{staging} already exists (use --force to replace)")
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    staging.mkdir()

    for name in TOP_FILES:
        _copy_tree(ROOT / name, staging / name)
    for name in DIRS:
        _copy_tree(ROOT / name, staging / name)

    commit = _git("rev-parse", "--short", "HEAD") or "unknown"
    dirty = bool(_git("status", "--porcelain"))
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    license_line = ""
    license_meta = None
    if license_file:
        from src import license as license_module

        source = Path(license_file)
        try:
            payload = license_module.verify_file(str(source))
        except license_module.LicenseError as exc:
            raise SystemExit(f"cannot include license: {exc}") from exc
        target = staging / "config" / "license.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        os.chmod(target, 0o600)
        license_meta = {
            "licensee": payload["licensee"],
            "license_id": payload.get("license_id"),
            "expires_at": payload["expires_at"],
        }
        license_line = (
            f"\n许可 / License: `{payload['licensee']}`，"
            f"有效期至 `{payload['expires_at']}`（已包含在 config/license.json）\n"
        )

    if with_image:
        _save_image(staging, image or f"agent-rag:{version}", version)

    (staging / "INSTALL.md").write_text(
        INSTALL_TEMPLATE.format(
            version=version,
            created_at=created_at,
            commit=commit,
            dirty=" (dirty tree / 未提交改动)" if dirty else "",
            license_line=license_line,
        ),
        encoding="utf-8",
    )

    files = []
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            files.append(
                {
                    "path": str(path.relative_to(staging)),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    manifest = {
        "name": bundle_name,
        "version": version,
        "commit": commit,
        "dirty": dirty,
        "created_at": created_at,
        "with_image": with_image,
        "license": license_meta,
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
    }
    (staging / "PACKAGE.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging, arcname=bundle_name)
    digest = _sha256(archive)
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")

    if not keep_staging:
        shutil.rmtree(staging)

    manifest["archive"] = str(archive)
    manifest["archive_sha256"] = digest
    manifest["sidecar"] = str(sidecar)
    return manifest


def _save_image(staging: Path, image: str, version: str) -> None:
    if shutil.which("docker") is None:
        raise SystemExit("docker not found; omit --with-image or install Docker")
    image_dir = staging / "image"
    image_dir.mkdir()
    raw = image_dir / f"agent-rag-{version}.tar"
    result = subprocess.run(
        ["docker", "save", "-o", str(raw), image], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(
            f"docker save {image} failed (build it first): {result.stderr.strip()}"
        )
    target_path = Path(str(raw) + ".gz")
    with raw.open("rb") as source, target_path.open("wb") as raw_target:
        with gzip.GzipFile(fileobj=raw_target, mode="wb", mtime=0) as target:
            shutil.copyfileobj(source, target)
    raw.unlink()
    (image_dir / "IMAGE.txt").write_text(
        f"docker load -i image/agent-rag-{version}.tar.gz\n"
        f"# image: {image}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist", help="output directory (default dist/)")
    parser.add_argument("--with-image", action="store_true", help="include `docker save` of the app image")
    parser.add_argument("--image", default="", help="image reference (default agent-rag:<version>)")
    parser.add_argument(
        "--license",
        default="",
        help="include this signed license as config/license.json (customer-specific bundle)",
    )
    parser.add_argument("--keep-staging", action="store_true", help="keep the unpacked bundle directory")
    parser.add_argument("--force", action="store_true", help="replace an existing bundle")
    args = parser.parse_args()

    manifest = build(
        Path(args.out),
        with_image=args.with_image,
        image=args.image,
        license_file=args.license,
        keep_staging=args.keep_staging,
        force=args.force,
    )
    print(f"bundle   {manifest['archive']}  ({manifest['total_bytes'] / 1024 / 1024:.1f} MB)")
    print(f"sha256   {manifest['archive_sha256']}")
    if manifest["license"]:
        print(
            "license  {licensee} until {expires_at} (config/license.json)".format(
                **manifest["license"]
            )
        )
    if manifest["dirty"]:
        print("warning: working tree has uncommitted changes (recorded in PACKAGE.json)")
    print(f"files    {len(manifest['files'])} (+ PACKAGE.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
