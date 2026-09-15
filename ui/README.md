# UI

The Hub serves this Jinja2 + HTMX + vanilla JavaScript UI. HTML forms and
partial operations use the versioned API with CSRF headers; persistent VM
status, tags, logs and alarm updates use `/ws/v1/events`.
