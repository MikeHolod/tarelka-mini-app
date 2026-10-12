# Tarelka API — backend starter

This is the first backend scaffold, not yet a production-ready service. It includes a health endpoint and Telegram Mini App `initData` signature verification. Database persistence and real AI analysis are deliberately marked as TODO/not connected; do not treat stub responses as saved data.

## Run locally

1. Install Python 3.11+.
2. In this folder, create a virtual environment and install dependencies:
   `python -m venv .venv`
   `source .venv/bin/activate` (Windows: `.venv\\Scripts\\activate`)
   `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`, add the Telegram bot token, and export those values in your shell (or use your preferred env loader).
4. Start: `uvicorn app.main:app --reload`
5. Check `http://127.0.0.1:8000/health` and API docs at `/docs`.

## Endpoints

- `GET /health` — service health.
- `POST /api/auth/telegram` — validates `X-Telegram-Init-Data` signature and freshness.
- `PUT /api/profile` — validates Telegram data and validates profile fields; PostgreSQL persistence is not implemented yet.
- `POST /api/food/analyze` — intentionally returns 501 until real image upload and AI provider integration are added.

## Security notes

- Never put `BOT_TOKEN` or AI API keys in frontend JavaScript.
- Never trust a Telegram user ID sent by the browser without verifying `initData` on the server.
- Before production, exchange validated `initData` for a short-lived application session; avoid forwarding raw initData indefinitely.
- Add rate limits, logging without sensitive image content, database migrations, retention/deletion policies, and tests before launch.
