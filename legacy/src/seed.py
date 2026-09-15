from __future__ import annotations

import os

from src.database import TypeUser, User, db
from src.web_app import create_app


def _ensure_type_user(name: str, system_name: str, description: str) -> TypeUser:
    item = db.session.query(TypeUser).filter(TypeUser.system_name == system_name).first()
    if item is None:
        item = TypeUser(name=name, system_name=system_name, description=description)
        db.session.add(item)
        db.session.flush()
    return item


def _ensure_user(username: str, password: str, type_user_id: int) -> None:
    existing = db.session.query(User).filter(User.username == username).first()
    if existing is None:
        db.session.add(User(username=username, password=password, type_user_id=type_user_id, is_deleted=False))


def seed() -> None:
    app = create_app()
    with app.app_context():
        admin_type = _ensure_type_user(
            name="Administrator",
            system_name="admin",
            description="System administrator role",
        )
        user_type = _ensure_type_user(
            name="User",
            system_name="user",
            description="Regular user role",
        )

        admin_username = os.getenv("SEED_ADMIN_USERNAME", "admin")
        admin_password = os.getenv("SEED_ADMIN_PASSWORD", "admin")
        user_username = os.getenv("SEED_USER_USERNAME", "user")
        user_password = os.getenv("SEED_USER_PASSWORD", "user")

        _ensure_user(admin_username, admin_password, admin_type.id)
        _ensure_user(user_username, user_password, user_type.id)
        db.session.commit()
        print("Seed completed: types(admin,user) and default users are ready.")


if __name__ == "__main__":
    seed()
