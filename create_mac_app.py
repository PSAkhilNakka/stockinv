"""Create a local macOS Applications wrapper for this source checkout.

The bundle points to this checkout's virtual environment. It contains neither
store data nor a copy of the Python application.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil
import sys
from pathlib import Path


APP_NAME = "HARITHA ENTERPRISES"
EXECUTABLE = "HarithaEnterprises"


def create_app(source_dir: Path, applications_dir: Path) -> Path:
    source_dir = source_dir.resolve()
    bundle = applications_dir / f"{APP_NAME}.app"
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_dir / "static" / "haritha.icns", resources / "haritha.icns")

    details = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIdentifier": "com.harithaenterprises.stockinv",
        "CFBundleIconFile": "haritha.icns",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSApplicationCategoryType": "public.app-category.business",
        "NSHighResolutionCapable": True,
    }
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(details, stream)

    python = source_dir / ".venv" / "bin" / "python"
    launcher = source_dir / "desktop_launcher.py"
    script = (
        "#!/bin/sh\n"
        f"cd {shlex.quote(str(source_dir))}\n"
        f"exec {shlex.quote(str(python))} {shlex.quote(str(launcher))}\n"
    )
    entrypoint = macos / EXECUTABLE
    entrypoint.write_text(script, encoding="utf-8", newline="\n")
    entrypoint.chmod(0o755)
    return bundle


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This installer must run on macOS.")
    source_dir = Path(__file__).resolve().parent
    if not (source_dir / ".venv" / "bin" / "python").is_file():
        raise SystemExit("Run Install HARITHA ENTERPRISES.command first.")
    bundle = create_app(source_dir, Path.home() / "Applications")
    print(f"Created {bundle}")
    print("Keep this source folder in place. The Applications entry launches it here.")


if __name__ == "__main__":
    main()
