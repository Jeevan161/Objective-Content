"""
scripts/create_admin.py
------------------------
One-off bootstrap: create (or promote) an admin user. Run once after migrating,
since self-registration creates only INACTIVE non-admin accounts.

Usage (from backend/, with the venv active):
    python -m scripts.create_admin --email admin@nxtwave.co.in --password 'secret' --name 'Admin'

If the email already exists, it is promoted to an active admin (and the password
is reset when --password is given).
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import User


def main() -> int:
    ap = argparse.ArgumentParser(description="Create or promote an admin user.")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--name", default="Admin")
    args = ap.parse_args()

    email = args.email.strip().lower()
    with SessionLocal() as s:
        user = s.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, name=args.name,
                        password_hash=hash_password(args.password),
                        role=User.ROLE_ADMIN, is_active=True)
            s.add(user)
            action = "created"
        else:
            user.role = User.ROLE_ADMIN
            user.is_active = True
            user.password_hash = hash_password(args.password)
            action = "promoted"
        s.commit()
        print(f"Admin {action}: {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
