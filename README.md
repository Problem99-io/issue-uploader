# Issue uploader

Single-project Django setup with SQLite and HTMX.

## Quick start

```bash
python3 -m virtualenv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

`runserver` defaults to port `4200`.

Open:

- Home: http://127.0.0.1:4200/
- Agent configs: http://127.0.0.1:4200/agent-configs/
- Repositories: http://127.0.0.1:4200/repositories/
- Scan tasks: http://127.0.0.1:4200/scan-tasks/
- Issue candidates: http://127.0.0.1:4200/issue-candidates/

Use the `Agent configs` page to choose the AI model used for scans.
Use the `Repositories` page to save global tokens (GitHub + Problem99) and global Ollama URL, then import repositories by `owner/name`.
