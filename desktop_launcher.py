"""Run HARITHA ENTERPRISES in a local Windows or macOS desktop window.

SQLite, uploads and the session key live in the current user's application data
folder, outside the application checkout.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import sys
import threading
import traceback
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


APP_NAME = "HARITHA ENTERPRISES"
DATA_FOLDER = "HarithaEnterprises"
ICON = Path(__file__).resolve().parent / "static" / "haritha.ico"


def default_data_dir(platform: str | None = None) -> Path:
    platform = platform or sys.platform
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / DATA_FOLDER
    if not platform.startswith("win"):
        return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / DATA_FOLDER
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        local_app_data = str(Path.home() / "AppData" / "Local")
    return Path(local_app_data) / DATA_FOLDER


def acquire_lock(data_dir: Path):
    """Prevent two desktop windows from writing to the same local store."""
    lock_file = (data_dir / "desktop.lock").open("a+b")
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise RuntimeError(f"{APP_NAME} is already open on this device.") from None
    return lock_file


def configure_data(data_dir: Path) -> Path:
    """Set all local storage paths before importing the Flask application."""
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        data_dir.chmod(0o700)
    (data_dir / "uploads").mkdir(exist_ok=True)
    (data_dir / "backups").mkdir(exist_ok=True)
    secret_file = data_dir / "session.key"
    if not secret_file.exists():
        with secret_file.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48))
    if os.name != "nt":
        secret_file.chmod(0o600)
    secret_key = secret_file.read_text(encoding="ascii").strip()
    if len(secret_key) < 32:
        raise RuntimeError("The local session key is missing or invalid.")
    db_path = data_dir / "store.sqlite3"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["UPLOAD_DIR"] = str(data_dir / "uploads")
    os.environ["SECRET_KEY"] = secret_key
    os.environ["APP_HOST"] = "127.0.0.1"
    return db_path


def _dialog(title: str, message: str) -> None:
    from tkinter import Tk, messagebox

    root = Tk()
    root.withdraw()
    if ICON.exists():
        try:
            root.iconbitmap(str(ICON))
        except Exception:
            pass
    messagebox.showerror(title, message, parent=root)
    root.destroy()


def create_first_admin(models_module) -> bool:
    """Ask for the first local administrator without writing a password to Git."""
    from tkinter import Button, Entry, Frame, Label, StringVar, Tk
    from sqlalchemy import select
    from werkzeug.security import generate_password_hash

    with models_module.SessionLocal() as db:
        if db.execute(select(models_module.User.id).limit(1)).first():
            return True

    root = Tk()
    root.title(f"{APP_NAME} · First use")
    root.resizable(False, False)
    if ICON.exists():
        try:
            root.iconbitmap(str(ICON))
        except Exception:
            pass
    frame = Frame(root, padx=24, pady=22)
    frame.pack()
    Label(frame, text=APP_NAME, font=("Segoe UI", 16, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 5)
    )
    Label(frame, text="Create the first administrator for this device.").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(0, 18)
    )
    username = StringVar(value="ravikumar")
    full_name = StringVar(value="Ravi Kumar")
    password = StringVar()
    confirm = StringVar()
    fields = (
        ("Username", username, False),
        ("Full name", full_name, False),
        ("Password", password, True),
        ("Confirm password", confirm, True),
    )
    for row, (caption, variable, hidden) in enumerate(fields, start=2):
        Label(frame, text=caption).grid(row=row, column=0, sticky="w", pady=5)
        Entry(frame, textvariable=variable, width=32, show="*" if hidden else "").grid(
            row=row, column=1, sticky="ew", padx=(14, 0), pady=5
        )
    error = StringVar()
    Label(frame, textvariable=error, fg="#b53c48", wraplength=350).grid(
        row=6, column=0, columnspan=2, sticky="w", pady=(10, 0)
    )
    created = False

    def submit():
        nonlocal created
        name = username.get().strip()
        display = full_name.get().strip()
        chosen_password = password.get()
        if not name or not display:
            error.set("Enter a username and full name.")
            return
        if len(chosen_password) < 8:
            error.set("Use at least 8 characters for the first administrator password.")
            return
        if chosen_password != confirm.get():
            error.set("The passwords do not match.")
            return
        try:
            with models_module.SessionLocal() as db:
                branch = models_module.bootstrap_store(db)
                db.add(models_module.User(
                    branch_id=branch.id,
                    username=name,
                    full_name=display,
                    password_hash=generate_password_hash(chosen_password),
                    role="admin",
                    active=True,
                ))
                db.commit()
        except Exception as exc:
            error.set(f"Could not create the account: {exc}")
            return
        created = True
        root.destroy()

    Button(frame, text="Create administrator", command=submit, padx=13, pady=7).grid(
        row=7, column=1, sticky="e", pady=(15, 0)
    )
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return created


def backup_data(data_dir: Path, db_path: Path) -> Path:
    """Create a consistent SQLite and uploads snapshot after the window closes."""
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    db_copy = backup_dir / f"store-{stamp}.sqlite3.tmp"
    archive = backup_dir / f"haritha-{stamp}.zip"
    pending = archive.with_suffix(".zip.tmp")
    try:
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(db_copy)) as target:
            source.backup(target)
        with ZipFile(pending, "w", ZIP_DEFLATED) as bundle:
            bundle.write(db_copy, "store.sqlite3")
            for file in (data_dir / "uploads").rglob("*"):
                if file.is_file():
                    bundle.write(file, Path("uploads") / file.relative_to(data_dir / "uploads"))
        pending.replace(archive)
        return archive
    finally:
        db_copy.unlink(missing_ok=True)
        pending.unlink(missing_ok=True)


def run(data_dir: Path) -> None:
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = acquire_lock(data_dir)
    try:
        db_path = configure_data(data_dir)
        import models
        from app import app
        if os.name != "nt":
            db_path.chmod(0o600)

        if not create_first_admin(models):
            return

        from werkzeug.serving import make_server
        import webview

        server = make_server("127.0.0.1", 0, app)
        port_file = data_dir / "desktop.port"
        port_file.write_text(str(server.server_port), encoding="ascii")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            webview.settings["ALLOW_DOWNLOADS"] = True
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
            webview.create_window(APP_NAME, url, width=1300, height=850,
                                  min_size=(780, 570), background_color="#f4f7fa",
                                  text_select=True)
            webview.start(private_mode=False, storage_path=str(data_dir / "webview-profile"),
                          icon=str(ICON) if sys.platform == "win32" and ICON.exists() else None)
        finally:
            port_file.unlink(missing_ok=True)
            server.shutdown()
            worker.join(timeout=10)
            server.server_close()
            models.engine.dispose()
            backup_data(data_dir, db_path)
    finally:
        lock_file.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Open {APP_NAME} on this device")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("HARITHA_DATA_DIR", default_data_dir())))
    args = parser.parse_args()
    try:
        run(args.data_dir)
    except Exception as error:
        try:
            args.data_dir.mkdir(parents=True, exist_ok=True)
            with (args.data_dir / "launcher.log").open("a", encoding="utf-8") as log:
                log.write(f"\n{datetime.now().isoformat()}\n{traceback.format_exc()}\n")
        except OSError:
            pass
        _dialog(APP_NAME, str(error))


if __name__ == "__main__":
    main()
