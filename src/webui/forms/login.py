from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length


class LoginForm(FlaskForm):
    username = StringField("Логин", validators=[DataRequired(), Length(min=1, max=255)])
    password = PasswordField("Пароль", validators=[DataRequired(), Length(min=1, max=255)])
    submit = SubmitField("Войти")
