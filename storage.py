"""Resolve local attachment files after a store moves between devices."""

from __future__ import annotations

import os
from pathlib import Path


UPLOAD_SCHEME = "upload://"


def upload_root() -> Path:
    return Path(os.environ.get("UPLOAD_DIR", Path(__file__).resolve().parent / "uploads")).resolve()


def _upload_key_path(key: str) -> Path:
    parts = key.split("/")
    if not parts or any(not part or part in (".", "..") or ":" in part or "\\" in part for part in parts):
        raise ValueError("Invalid local upload reference")
    root = upload_root()
    destination = (root / Path(*parts)).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("Local upload reference leaves the upload directory")
    return destination


def upload_reference(key: str) -> str:
    """Store a file reference that stays valid when UPLOAD_DIR changes."""
    _upload_key_path(key)
    return UPLOAD_SCHEME + key


def resolve_local_upload(stored_path: str) -> Path:
    """Resolve new references and existing absolute paths, including restored stores."""
    if stored_path.startswith(UPLOAD_SCHEME):
        return _upload_key_path(stored_path[len(UPLOAD_SCHEME):])

    legacy = Path(stored_path)
    if legacy.is_file():
        return legacy

    # Older records kept an absolute path. Their last four components are the
    # stable branch/owner/id/file key, which a backup restores under UPLOAD_DIR.
    parts = stored_path.replace("\\", "/").split("/")
    if len(parts) >= 4:
        branch, owner_type, owner_id, filename = parts[-4:]
        if branch.isdigit() and owner_type in {"product", "purchase", "expense", "settings"} and owner_id.isdigit():
            try:
                restored = _upload_key_path("/".join((branch, owner_type, owner_id, filename)))
            except ValueError:
                pass
            else:
                if restored.is_file():
                    return restored
    return legacy
