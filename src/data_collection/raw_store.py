"""
Immutable raw-snapshot storage (full retention policy).

Every response body or manual file is saved byte-for-byte under data/raw/
before it is parsed. Files are opened in exclusive-create mode, so an
existing snapshot can never be overwritten. Each snapshot gets a sidecar
.meta.json with the request details (API key already redacted) and its
SHA-256 is recorded in fetch_log, so any stored number can be traced back
to the exact bytes it came from.

data/raw/ is gitignored: snapshots can contain licensed data (e.g. ISM).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(part: str) -> str:
    cleaned = _SAFE.sub("_", part).strip("._")
    if not cleaned:
        raise ValueError(f"unusable path component: {part!r}")
    return cleaned


class RawStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, *, source: str, target: str, kind: str, body: bytes,
             meta: dict, retrieved_at: datetime | None = None,
             extension: str = "json") -> tuple[str, str]:
        """Write one snapshot. Returns (path relative to root, sha256 hex)."""
        ts = (retrieved_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = ts.strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.root / _safe(source) / _safe(target)
        folder.mkdir(parents=True, exist_ok=True)

        base = f"{stamp}_{_safe(kind)}"
        path = folder / f"{base}.{_safe(extension)}"
        n = 1
        while path.exists():  # same microsecond: never overwrite, add a suffix
            path = folder / f"{base}_{n}.{_safe(extension)}"
            n += 1

        digest = hashlib.sha256(body).hexdigest()
        with open(path, "xb") as f:
            f.write(body)
        sidecar = {**meta, "sha256": digest, "bytes": len(body),
                   "saved_at": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        with open(path.with_name(path.name + ".meta.json"), "x", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2, sort_keys=True)

        return str(path.relative_to(self.root)), digest
