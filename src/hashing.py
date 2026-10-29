"""Shared file hashing with a stat-keyed cache.

The admin console and the ETL both hash every file they see. Hashing is cheap
for small files but adds up on large libraries and is repeated on every
`/admin/api/meta` call, so cache the digest keyed by
(path, mtime_ns, size) — any write changes at least one of them.
"""

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Union

PathLike = Union[str, os.PathLike]


@lru_cache(maxsize=4096)
def _sha1_cached(path: str, mtime_ns: int, size: int) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_sha1(path: PathLike) -> str:
    """SHA1 of a file, cached by its stat signature."""
    resolved = str(Path(path))
    stat = os.stat(resolved)
    return _sha1_cached(resolved, stat.st_mtime_ns, stat.st_size)


def clear_hash_cache() -> None:
    _sha1_cached.cache_clear()


def hash_cache_info():
    return _sha1_cached.cache_info()
