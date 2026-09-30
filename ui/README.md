# UI

The Hub serves a Jinja2 UI styled by the shared `ui/static/app.css` design
system (`bb-*` components), with Alpine, HTMX and vanilla JavaScript for
interactivity. It does not use Vue or Element Plus.
HTML forms and mutations use the versioned API with CSRF headers; persistent VM
status, tags, logs and alarm updates use `/ws/v1/events`.

Admin map publishing lives on `/admin/maps`. VM create/edit only attaches an
already published `map_version`.
