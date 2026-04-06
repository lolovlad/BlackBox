from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, session, url_for

from src.webui.forms import LoginForm
from src.webui.services.auth_service import AuthService

bp = Blueprint("auth", __name__)
auth_service = AuthService()


@bp.get("/login")
@bp.post("/login")
def login():
    if session.get("auth"):
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = auth_service.authenticate(form.username.data or "", form.password.data or "")
        if user:
            session["auth"] = True
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("main.dashboard"))
        flash("Неверный логин или пароль", "error")
    return render_template("auth/login.html", form=form)


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
