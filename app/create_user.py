"""Create (or reset the password of) the owner account: python -m app.create_user"""
import getpass
import sys

from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.db import Database, upgrade
from app.models import User


def main() -> int:
    settings = get_settings()
    settings.VAR_DIR.mkdir(parents=True, exist_ok=True)
    upgrade(settings.DATABASE_URL)
    email = input("Email: ").strip().lower()
    if "@" not in email:
        print("That doesn't look like an email address.")
        return 1
    pw = getpass.getpass("Password (min 10 chars): ")
    if len(pw) < 10 or pw != getpass.getpass("Repeat password: "):
        print("Passwords must match and be at least 10 characters.")
        return 1
    db = Database(settings.DATABASE_URL)
    with db.session() as s:
        user = s.scalar(select(User).where(User.email == email))
        if user:
            user.password_hash = hash_password(pw)
            print("Password updated.")
        else:
            s.add(User(email=email, password_hash=hash_password(pw)))
            print("User created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
