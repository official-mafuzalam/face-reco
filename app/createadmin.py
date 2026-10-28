from getpass import getpass

from sqlalchemy import select

from app.auth import hash_password
from app.db import Base, SessionLocal, engine
from app.models import User


def main() -> None:
    Base.metadata.create_all(engine)
    username = input("Admin username: ").strip()
    if not username:
        raise SystemExit("Username cannot be empty.")
    password = getpass("Admin password: ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")

    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == username)) is not None:
            raise SystemExit("That username already exists.")
        db.add(User(username=username, password_hash=hash_password(password), role="admin"))
        db.commit()
    print("Admin account created.")


if __name__ == "__main__":
    main()
