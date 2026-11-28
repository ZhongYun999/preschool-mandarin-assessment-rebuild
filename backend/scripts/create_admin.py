"""Create an administrator in an explicitly selected VNext database."""

import argparse
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.api.teacher_auth import normalize_username, password_hasher
from app.auth.models import User
from app.database import create_database_engine


def create_admin(username, display_name, password):
    username = normalize_username(username)
    display_name = display_name.strip()
    if not 1 <= len(display_name) <= 64 or not 8 <= len(password) <= 128:
        raise ValueError("Invalid display name or password length")
    engine = create_database_engine()
    try:
        with Session(engine) as db, db.begin():
            if db.scalar(select(User.user_id).where(User.username == username)):
                raise ValueError("Username already exists")
            user = User(
                username=username,
                display_name=display_name,
                password_hash=password_hasher.hash(password),
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.flush()
            return str(user.user_id)
    except IntegrityError:
        raise ValueError("Username already exists") from None
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-env",
        required=True,
        help="Name of environment variable containing the VNext database URL",
    )
    args = parser.parse_args()
    if not os.environ.get(args.database_env):
        parser.error("Specified database environment variable is empty")
    os.environ["DATABASE_URL"] = os.environ[args.database_env]
    username = input("Username: ")
    display_name = input("Display name: ")
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match", file=sys.stderr)
        return 2
    try:
        user_id = create_admin(username, display_name, password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Created administrator {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
