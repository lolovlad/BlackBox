from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, url_for
from flask_login import current_user, login_required, login_user, logout_user

from src.webui.forms import LoginForm
from src.webui.paths import TEMPLATES_DIR
from src.webui.services.auth_service import AuthService

auth_router = Blueprint("auth", __name__, template_folder=str(TEMPLATES_DIR))
auth_service = AuthService()


@auth_router.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main_blueprint.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = auth_service.authenticate(
            form.username.data or "",
            form.password.data or "",
            session_factory=current_app.extensions["session_factory"],
        )
        if user:
            login_user(user, remember=False)
            return redirect(url_for("main_blueprint.dashboard"))
        flash("Неверный логин или пароль", "error")
    return render_template("auth/login.html", form=form)


@auth_router.route("/logout", methods=["GET"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth_blueprint.login"))
