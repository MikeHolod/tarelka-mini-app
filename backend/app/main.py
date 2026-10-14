from fastapi.middleware.cors import CORSMiddleware
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Tarelka API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mikeholod.github.io",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")


def db_connect():
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="Database is not configured"
        )

    url = DATABASE_URL.replace(
        "postgresql+psycopg://",
        "postgresql://",
        1
    )

    try:
        return psycopg.connect(
            url,
            row_factory=dict_row,
            connect_timeout=8
        )
    except Exception as exc:
        print(
            f"Database connection error: "
            f"{type(exc).__name__}: {exc}",
            flush=True
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Database connection failed: "
                f"{type(exc).__name__}"
            )
        ) from exc


@app.get("/health")
def health():
    database = "not_configured"
    db_error = None

    if DATABASE_URL:
        try:
            with db_connect() as conn:
                conn.execute("select 1")
            database = "connected"
        except Exception as exc:
            database = "disconnected"
            db_error = type(exc).__name__ + ": " + str(exc)

    result = {
        "ok": True,
        "service": "tarelka-api",
        "version": "0.2.0",
        "database": database,
    }

    if db_error:
        result["database_error"] = db_error

    return result


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
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram auth date") from exc
    if abs(time.time() - auth_date) > 86400:
        raise HTTPException(status_code=401, detail="Telegram login data expired")
    try:
        parsed["user"] = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram user data") from exc
    return parsed


def authenticated_user(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")
    data = verify_telegram_init_data(init_data)
    user = data.get("user", {})
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user ID missing")
    return user


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


def upsert_user(conn, user: dict) -> int:
    row = conn.execute(
        """insert into public.app_user (telegram_id, username, first_name)
           values (%s, %s, %s)
           on conflict (telegram_id) do update set
             username = excluded.username,
             first_name = excluded.first_name,
             updated_at = now()
           returning id""",
        (int(user["id"]), user.get("username"), user.get("first_name")),
    ).fetchone()
    conn.execute(
        "insert into public.notification_settings (user_id) values (%s) on conflict (user_id) do nothing",
        (row["id"],),
    )
    return row["id"]


@app.post("/api/auth/telegram")
def telegram_auth(x_telegram_init_data: str = Header(default="")):
    user = authenticated_user(x_telegram_init_data)
    with db_connect() as conn:
        user_id = upsert_user(conn, user)
    return {
        "authenticated": True,
        "database_saved": True,
        "telegram_user": {"id": user["id"], "first_name": user.get("first_name", "")},
        "user_id": user_id,
    }


@app.put("/api/profile")
def save_profile(profile: ProfileIn, x_telegram_init_data: str = Header(default="")):
    user = authenticated_user(x_telegram_init_data)
    gender = profile.gender
    if gender in ("Мужчина", "male"):
        gender = "male"
    elif gender in ("Женщина", "female"):
        gender = "female"
    elif gender is not None:
        raise HTTPException(status_code=422, detail="Gender must be Мужчина/Женщина or male/female")

    with db_connect() as conn:
        user_id = upsert_user(conn, user)
        conn.execute(
            """update public.app_user set gender=%s, height_cm=%s,
               initial_weight_kg=coalesce(%s, initial_weight_kg),
               current_weight_kg=%s, goal=%s, target_weight_kg=%s, updated_at=now()
               where id=%s""",
            (gender, profile.height_cm, profile.initial_weight_kg, profile.current_weight_kg,
             profile.goal, profile.target_weight_kg, user_id),
        )
        if any(v is not None for v in (profile.current_weight_kg, profile.waist_cm, profile.hips_cm, profile.chest_cm)):
            conn.execute(
                """insert into public.measurement (user_id, weight_kg, waist_cm, hips_cm, chest_cm)
                   values (%s, %s, %s, %s, %s)""",
                (user_id, profile.current_weight_kg, profile.waist_cm, profile.hips_cm, profile.chest_cm),
            )
    return {"saved": True, "user_id": user_id, "measurement_recorded": any(v is not None for v in (profile.current_weight_kg, profile.waist_cm, profile.hips_cm, profile.chest_cm))}


@app.get("/api/profile")
def get_profile(x_telegram_init_data: str = Header(default="")):
    user = authenticated_user(x_telegram_init_data)
    with db_connect() as conn:
        row = conn.execute(
            """select gender, height_cm, initial_weight_kg, current_weight_kg, goal,
                      target_weight_kg, created_at, updated_at
               from public.app_user where telegram_id=%s""",
            (int(user["id"]),),
        ).fetchone()
        if row is None:
            return {"profile": None}
        measurements = conn.execute(
            """select weight_kg, waist_cm, hips_cm, chest_cm, measured_at
               from public.measurement where user_id=(select id from public.app_user where telegram_id=%s)
               order by measured_at desc limit 50""",
            (int(user["id"]),),
        ).fetchall()
    row["measurements"] = measurements
    return {"profile": row}


@app.post("/api/food/analyze")
def analyze_food(x_telegram_init_data: str = Header(default="")):
    authenticated_user(x_telegram_init_data)
    # Real photo analysis is deliberately not faked; AI provider integration is the next step.
    raise HTTPException(status_code=501, detail="Photo analysis AI is not configured yet")
