"""
Seed the first admin user.

Usage (locally or via `docker exec -it lssr python seed_admin.py`):
    python seed_admin.py <email> <name> <password>
"""
import sys

from auth import hash_password
from models import SessionLocal, User, init_db


def main():
    if len(sys.argv) != 4:
        print("Usage: python seed_admin.py <email> <name> <password>")
        sys.exit(1)
    email, name, password = sys.argv[1].strip().lower(), sys.argv[2], sys.argv[3]
    init_db()
    db = SessionLocal()
    if db.query(User).filter(User.email == email).first():
        print(f"User {email} already exists.")
        sys.exit(1)
    db.add(User(email=email, name=name, hashed_password=hash_password(password),
                is_admin=True, is_active=True))
    db.commit()
    print(f"Admin {email} created.")


if __name__ == "__main__":
    main()
