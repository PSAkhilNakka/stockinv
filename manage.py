"""One-time administrator setup and database initialization."""
import argparse
import getpass
import secrets

from sqlalchemy import select
from werkzeug.security import generate_password_hash

import models as m


def main():
    parser = argparse.ArgumentParser(description="Hardware Desk administration")
    parser.add_argument("command", choices=["init-admin", "init-db"])
    parser.add_argument("--username", default="admin")
    parser.add_argument("--name", default="Store Administrator")
    parser.add_argument("--generate-password", action="store_true")
    args = parser.parse_args()
    m.init_db()
    with m.SessionLocal() as db:
        branch = m.bootstrap_store(db)
        if args.command == "init-db":
            db.commit()
            print("Database initialized. Run init-admin to create the first administrator.")
            return
        existing = db.execute(select(m.User).where(m.User.username == args.username)).scalar_one_or_none()
        if existing:
            raise SystemExit("Username already exists; create a different administrator or reset through an existing admin.")
        if args.generate_password:
            password = secrets.token_urlsafe(20)
        else:
            password = getpass.getpass("New administrator password (12+ characters): ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                raise SystemExit("Passwords did not match")
        if len(password) < 12:
            raise SystemExit("Password must contain at least 12 characters")
        user = m.User(branch_id=branch.id, username=args.username, full_name=args.name,
                      password_hash=generate_password_hash(password), role="admin", active=True)
        db.add(user)
        db.commit()
        print(f"Administrator username: {args.username}")
        if args.generate_password:
            print(f"One-time password: {password}")
        print("Sign in, then configure store name, address, state, GST status and invoice prefix before billing.")


if __name__ == "__main__":
    main()
