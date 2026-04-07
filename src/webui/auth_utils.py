from __future__ import annotations

from functools import wraps

from flask import flash, redirect, url_for
from flask_login import current_user, login_required


def admin_required(view_func):
    """Только пользователи с type_user.system_name == \"admin\" (раздел данных)."""

    @wraps(view_func)
    @login_required
    def _wrapped(*args, **kwargs):
        tu = getattr(current_user, "type_user", None)
        role = tu.system_name if tu is not None else None
        if role != "admin":
            flash("Недостаточно прав для доступа к этому разделу.", "error")
            return redirect(url_for("main_blueprint.dashboard"))
        return view_func(*args, **kwargs)

    return _wrapped
