"""Create a PostgreSQL custom-format backup using pg_dump.

Run this from a scheduler with DATABASE_URL and an encrypted backup volume.
Restore with pg_restore after stopping writers and taking a safety copy.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


def main():
    raw = os.environ.get("DATABASE_URL", "")
    if not raw.startswith("postgresql"):
        raise SystemExit("DATABASE_URL must point to PostgreSQL")
    parsed = urlparse(raw.replace("postgresql+psycopg://", "postgresql://", 1))
    output_dir = Path(os.environ.get("BACKUP_DIR", "./backups")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"hardware-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.dump"
    env = os.environ.copy()
    env["PGPASSWORD"] = unquote(parsed.password or "")
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-acl",
           "--host", parsed.hostname or "localhost", "--port", str(parsed.port or 5432),
           "--username", unquote(parsed.username or ""), "--file", str(filename),
           parsed.path.lstrip("/")]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        filename.unlink(missing_ok=True)
        raise SystemExit(f"Backup failed: {exc}") from exc
    print(filename)


if __name__ == "__main__":
    main()
