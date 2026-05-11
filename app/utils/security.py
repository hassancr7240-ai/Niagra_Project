from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path


def compute_file_hash(file_path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize_filename(name: str) -> str:
    """Remove path traversal characters and unsafe chars from a filename."""
    safe = "".join(
        c for c in name if c.isalnum() or c in ("-", "_", ".", " ")
    )
    safe = safe.replace(" ", "_")
    return safe[:200] or "file"


def generate_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def is_safe_path(base: Path, target: Path) -> bool:
    """Verify target is inside base (prevents path traversal)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
