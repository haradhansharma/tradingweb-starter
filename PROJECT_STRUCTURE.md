# TradingWeb Project Structure

This file documents the project structure for the `tradingweb` repository.
Environment folders such as `venv` and system-generated directories (`node_modules`, `.cache`, etc.) are intentionally excluded.

## Root

- `docker-compose.yml`
- `backend/`
- `frontend/`

## backend/

- `Dockerfile`
- `manage.py`
- `requirements.txt`
- `api/`
  - `__init__.py`
  - `router.py`
- `apps/`
  - `__init__.py`
  - `users/`
    - `__init__.py`
    - `admin.py`
    - `apps.py`
    - `models.py`
    - `tests.py`
    - `views.py`
    - `migrations/`
      - `__init__.py`
- `config/`
  - `__init__.py`
  - `asgi.py`
  - `celery.py`
  - `settings.py`
  - `urls.py`
  - `wsgi.py`
- `logs/`
- `media/`
- `static/`
- `staticfiles/`
- `templates/`

## frontend/

- `astro.config.mjs`
- `Dockerfile`
- `package.json`
- `README.md`
- `tsconfig.json`
- `public/`
- `src/`
  - `components/`
    - `common/`
    - `dashboard/`
    - `trading/`
  - `constants/`
  - `hooks/`
  - `layouts/`
  - `lib/`
    - `api/`
    - `utils/`
  - `pages/`
    - `index.astro`
  - `styles/`
  - `types/`
  - `utils/`
- `.vscode/`
- `.astro/` (build artifact)
