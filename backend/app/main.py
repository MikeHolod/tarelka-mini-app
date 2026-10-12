import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Tarelka API", version="0.1.0")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")


@app.get("/health")
def health():
    return {"ok": True, "service": "tarelka-api", "version": "0.1.0"}


def verify_telegram_init_data(init_data: str) -> dict:
    """Validate Telegram Mini App initData using the bot token."""
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram auth is not configured")
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing Telegram signature")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")
    auth_date = int(parsed.get("auth_date", "0"))
    if abs(time.time() - auth_date) > 86400:
        raise HTTPException(status_code=401, detail="Telegram login data expired")
    try:
        parsed["user"] = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Invalid Telegram user data")
    return parsed


class ProfileIn(BaseModel):
    gender: str | None = None
    height_cm: int | None = Field(default=None, ge=80, le=250)
    initial_weight_kg: float | None = Field(default=None, ge=20, le=500)
    current_weight_kg: float | None = Field(default=None, ge=20, le=500)
    goal: str | None = None
    target_weight_kg: float | None = Field(default=None, ge=20, le=500)
    waist_cm: float | None = Field(default=None, ge=20, le=250)
    hips_cm: float | None = Field(default=None, ge=20, le=250)
    chest_cm: float | None = Field(default=None, ge=20, le=250)


@app.post("/api/auth/telegram")
def telegram_auth(x_telegram_init_data: str = Header(default="")):
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")
    data = verify_telegram_init_data(x_telegram_init_data)
    user = data.get("user", {})
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user ID missing")
    # TODO: upsert user into PostgreSQL and issue an application session.
    return {"authenticated": True, "telegram_user": {"id": user["id"], "first_name": user.get("first_name", "")}, "next": "connect_database"}


@app.put("/api/profile")
def save_profile(profile: ProfileIn, x_telegram_init_data: str = Header(default="")):
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")
    verify_telegram_init_data(x_telegram_init_data)
    # TODO: persist profile and measurement history in PostgreSQL.
    return {"saved": False, "status": "database_not_connected", "received": profile.model_dump()}


@app.post("/api/food/analyze")
def analyze_food(x_telegram_init_data: str = Header(default="")):
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")
    verify_telegram_init_data(x_telegram_init_data)
    # Intentionally not pretending to analyze food until an AI provider is configured.
    raise HTTPException(status_code=501, detail="Real photo analysis is the next integration step")
