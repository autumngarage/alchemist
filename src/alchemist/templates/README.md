# Alchemist templates

Versioned Jinja2 templates used at runtime. v0.1 lands `brief.md.j2` here:
the prompt rendered from each dispatched issue and handed to `conductor exec`
via `--brief-file`.

The brief template is intentionally checked into source so changes show up
in diffs and version control — brief-rendering is a load-bearing
prompt-engineering surface despite the orchestrator being thin.
