# UI

The Hub serves this Jinja2 + Tailwind + Alpine + HTMX + vanilla JavaScript UI.
HTML forms and mutations use the versioned API with CSRF headers; persistent VM
status, tags, logs and alarm updates use `/ws/v1/events`.

Admin map publishing lives on `/admin/maps`. VM create/edit only attaches an
already published `map_version`.
