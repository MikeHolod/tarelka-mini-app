# Tarelka API

FastAPI backend for the Tarelka Telegram Mini App.

## Required environment variables

- `BOT_TOKEN` — token from BotFather. Never commit it to GitHub.
- `DATABASE_URL` — private PostgreSQL connection string from Supabase (Dashboard → Connect). Never put it in the frontend.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set real values in .env; do not commit .env.
uvicorn app.main:app --reload
```

Open `/health`; `database` should say `connected` after configuration. `/docs` shows API endpoints.

## Implemented

- Verifies Telegram Mini App `initData` signature and freshness.
- Creates/updates the Telegram user in `public.app_user`.
- Creates default notification settings.
- Saves profile and appends a measurement record.
- Returns saved profile and up to 50 recent measurements.
- Food photo AI is not yet connected; endpoint returns 501 rather than inventing an analysis.
