from fastapi.middleware.cors import CORSMiddleware
import base64
from openai import OpenAI
from supabase import create_client
import hashlib
import hmac
import json
import os
import re
import time
from datetime import date, datetime, time as datetime_time
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import FastAPI, Header, HTTPException, File, Form, UploadFile, Request
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Tarelka API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mikeholod.github.io",
    ],
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

supabase = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY
    )
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
def analyze_food(
    food_text: str = Form(default=""),
    photo: UploadFile | None = File(default=None),
    x_telegram_init_data: str = Header(default=""),
):
    user = authenticated_user(x_telegram_init_data)

    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="OpenAI API key is not configured",
        )

    if supabase is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured",
        )

    if not food_text.strip() and photo is None:
        raise HTTPException(
            status_code=400,
            detail="Upload a food photo or enter a description",
        )

    image_bytes = None
    content_type = None
    photo_path = None

    if photo is not None:
        content_type = (photo.content_type or "").lower()

        allowed_types = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }

        if content_type not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail="Unsupported image format",
            )

        image_bytes = photo.file.read()

        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail="Image must be no larger than 10 MB",
            )

        if not image_bytes:
            raise HTTPException(
                status_code=400,
                detail="The uploaded image is empty",
            )

    prompt = (
        "Ты — дружелюбный помощник приложения «Тарелка». "
        "Оцени приём пищи по принципу Гарвардской тарелки: "
        "примерно половина — овощи и фрукты, четверть — "
        "источники белка, четверть — цельнозерновые продукты. "
        "Отвечай по-русски, коротко и доброжелательно. "
        "Укажи, что удалось распознать, что хорошо сбалансировано "
        "и что можно улучшить. Допустима лёгкая шутка. "
        "Не выдумывай ингредиенты, если их нельзя уверенно определить. "
        "Не утверждай, что знаешь точную калорийность по фотографии."
    )

    content = [
        {
            "type": "input_text",
            "text": (
                prompt
                + "\n\nОписание пользователя: "
                + (food_text.strip() or "Пользователь не добавил описание.")
            ),
        }
    ]

    if image_bytes is not None:
        encoded_image = base64.b64encode(image_bytes).decode("utf-8")

        content.append(
            {
                "type": "input_image",
                "image_url": (
                    f"data:{content_type};base64,{encoded_image}"
                ),
                "detail": "auto",
            }
        )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            max_output_tokens=500,
        )

        analysis = response.output_text

    except Exception as exc:
        print(
            f"OpenAI analysis error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=502,
            detail="Food analysis failed. Please try again.",
        ) from exc

    try:
        with db_connect() as conn:
            db_user = conn.execute(
                "select id from public.app_user where telegram_id=%s",
                (int(user["id"]),),
            ).fetchone()

            if db_user is None:
                raise HTTPException(
                    status_code=404,
                    detail="User not found",
                )

            user_id = db_user["id"]

            if image_bytes is not None:
                import uuid

                extension = {
                    "image/jpeg": "jpg",
                    "image/png": "png",
                    "image/webp": "webp",
                    "image/gif": "gif",
                }[content_type]

                photo_path = (
                    f"{user_id}/{uuid.uuid4()}.{extension}"
                )

                supabase.storage.from_("food-photos").upload(
                    photo_path,
                    image_bytes,
                    {"content-type": content_type},
                )

            entry = conn.execute(
                """
                insert into public.food_entry (
                    user_id,
                    photo_url,
                    original_analysis,
                    balance_score,
                    ai_comment
                )
                values (%s, %s, %s, %s, %s)
                returning id
                """,
                (
                    user_id,
                    photo_path,
                    {"text": analysis},
                    None,
                    analysis,
                ),
            ).fetchone()

        return {
            "success": True,
            "id": entry["id"],
            "analysis": analysis,
            "description": food_text.strip() or "Блюдо на фото",
            "photo": photo is not None,
        }

    except HTTPException:
        raise

    except Exception as exc:
        print(
            f"Food entry save error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not save food entry",
        ) from exc


class ChatMessageIn(BaseModel):
    role: str
    content: str = Field(min_length=1, max_length=10000)


@app.get("/api/chat/history")
def get_chat_history(x_telegram_init_data: str = Header(default="")):
    user = authenticated_user(x_telegram_init_data)

    with db_connect() as conn:
        user_id = upsert_user(conn, user)

        rows = conn.execute(
            """
            SELECT id, role, content, created_at
            FROM public.chat_message
            WHERE user_id = %s
            ORDER BY created_at ASC, id ASC
            LIMIT 200
            """,
            (user_id,),
        ).fetchall()

    return {"messages": rows}


@app.post("/api/chat/message")
def save_chat_message(
    message: ChatMessageIn,
    x_telegram_init_data: str = Header(default=""),
):
    user = authenticated_user(x_telegram_init_data)

    if message.role not in ("user", "assistant"):
        raise HTTPException(
            status_code=422,
            detail="Role must be user or assistant",
        )

    if not message.content.strip():
        raise HTTPException(
            status_code=422,
            detail="Message content cannot be empty",
        )

    with db_connect() as conn:
        user_id = upsert_user(conn, user)

        row = conn.execute(
            """
            INSERT INTO public.chat_message (user_id, role, content)
            VALUES (%s, %s, %s)
            RETURNING id, role, content, created_at
            """,
            (user_id, message.role, message.content.strip()),
        ).fetchone()

    return {"saved": True, "message": row}
    # Telegram Bot

TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

dp = Dispatcher()
router = Router()
class Registration(StatesGroup):
    gender = State()
    height = State()
    weight = State()
    goal = State()
    target_weight = State()
    waist = State()
    hips = State()
    chest = State()
class ProfileEdit(StatesGroup):
    weight = State()

@router.message(CommandStart())
async def start_command(message: types.Message, state: FSMContext):
    tg_user = message.from_user

    if tg_user is None:
        return

    user_data = {
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
    }

    try:
        with db_connect() as conn:
            upsert_user(conn, user_data)

            profile = conn.execute(
                """
                select gender, height_cm, initial_weight_kg, goal
                from public.app_user
                where telegram_id=%s
                """,
                (tg_user.id,),
            ).fetchone()

    except Exception as exc:
        print(
            f"Telegram user save error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Не удалось начать работу. Попробуй ещё раз позже."
        )
        return

    await state.clear()

    if (
        profile
        and profile["gender"]
        and profile["height_cm"]
        and profile["initial_weight_kg"]
        and profile["goal"]
    ):
        keyboard = types.ReplyKeyboardMarkup(
            keyboard=[
                [
                    types.KeyboardButton(text="🍽️ Записать еду"),
                    types.KeyboardButton(text="📈 Прогресс"),
                ],
                [
                    types.KeyboardButton(text="👤 Мои данные"),
                ],
            ],
            resize_keyboard=True,
        )

        await message.answer(
            "С возвращением! 🥗\n\n"
            "Что будем делать сегодня?",
            reply_markup=keyboard,
        )
        return

    await state.set_state(Registration.gender)

    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="Мужчина"),
                types.KeyboardButton(text="Женщина"),
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await message.answer(
        "Привет! 🥗\n\n"
        "Давай познакомимся и настроим Тарелку под тебя.\n\n"
        "Для начала выбери свой пол:",
        reply_markup=keyboard,
    )


@router.message(Registration.gender)
async def process_gender(message: types.Message, state: FSMContext):
    if message.text not in ("Мужчина", "Женщина"):
        await message.answer("Пожалуйста, выбери пол кнопкой ниже.")
        return

    gender = "male" if message.text == "Мужчина" else "female"

    await state.update_data(gender=gender)
    await state.set_state(Registration.height)

    await message.answer(
        "Какой у тебя рост в сантиметрах?\n\n"
        "Например: 178",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@router.message(Registration.height)
async def process_height(message: types.Message, state: FSMContext):
    try:
        height = int(message.text.strip())
        if not 80 <= height <= 250:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer(
            "Введи рост целым числом от 80 до 250 см.\n"
            "Например: 178"
        )
        return

    await state.update_data(height_cm=height)
    await state.set_state(Registration.weight)

    await message.answer(
        "Отлично! Теперь укажи свой текущий вес в килограммах.\n\n"
        "Например: 72.5"
    )

@router.message(Registration.weight)
async def process_weight(message: types.Message, state: FSMContext):
    try:
        weight = float(message.text.strip().replace(",", "."))
        if not 20 <= weight <= 500:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer(
            "Введи вес числом от 20 до 500 кг.\n"
            "Например: 72.5"
        )
        return

    await state.update_data(weight_kg=weight)
    await state.set_state(Registration.goal)

    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Постепенно похудеть")],
            [types.KeyboardButton(text="Наладить питание")],
            [types.KeyboardButton(text="Похудеть, сохранив мышцы")],
            [types.KeyboardButton(text="Следить за собой")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await message.answer(
        "Какая у тебя главная цель?",
        reply_markup=keyboard,
    )


@router.message(ProfileEdit.weight)
async def process_weight_edit(message: types.Message, state: FSMContext):
    if message.text == "👤 Мои данные":
        await state.clear()
        await show_profile(message, state)
        return
    try:
        weight = float(message.text.strip().replace(",", "."))
        if not 20 <= weight <= 500:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer(
            "Введи вес числом от 20 до 500 кг.\n"
            "Например: 64,5"
        )
        return

    telegram_user = message.from_user

    try:
        with db_connect() as conn:
            user = conn.execute(
                """
                select id
                from public.app_user
                where telegram_id=%s
                """,
                (telegram_user.id,),
            ).fetchone()

            if user is None:
                await message.answer(
                    "Не нашёл твой профиль. Попробуй начать с /start."
                )
                await state.clear()
                return

            latest = conn.execute(
                """
                select waist_cm, hips_cm, chest_cm
                from public.measurement
                where user_id=%s
                order by measured_at desc
                limit 1
                """,
                (user["id"],),
            ).fetchone()

            waist = latest["waist_cm"] if latest else None
            hips = latest["hips_cm"] if latest else None
            chest = latest["chest_cm"] if latest else None

            conn.execute(
                """
                update public.app_user
                set current_weight_kg=%s,
                    updated_at=now()
                where id=%s
                """,
                (weight, user["id"]),
            )

            conn.execute(
                """
                insert into public.measurement
                    (user_id, weight_kg, waist_cm, hips_cm, chest_cm, measured_at)
                values (%s, %s, %s, %s, %s, now())
                """,
                (user["id"], weight, waist, hips, chest),
            )

        await state.clear()

        await message.answer(
            f"Вес обновлён! Новый текущий вес: {weight:g} кг.\n\n"
            "Первоначальный вес при регистрации сохранён."
        )

    except Exception as exc:
        print(
            f"Weight edit error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Не получилось обновить вес. Попробуй немного позже."
        )
@router.message(Registration.goal)
async def process_goal(message: types.Message, state: FSMContext):
    allowed_goals = (
        "Постепенно похудеть",
        "Наладить питание",
        "Похудеть, сохранив мышцы",
        "Следить за собой",
    )

    if message.text not in allowed_goals:
        await message.answer("Пожалуйста, выбери цель одной из кнопок.")
        return

    await state.update_data(goal=message.text)
    await state.set_state(Registration.target_weight)

    await message.answer(
        "Хочешь указать желаемый вес?\n\n"
        "Напиши его в килограммах или отправь «Пропустить»."
    )

@router.message(Registration.target_weight)
async def process_target_weight(message: types.Message, state: FSMContext):
    if message.text and message.text.strip().lower() == "пропустить":
        target_weight = None
    else:
        try:
            target_weight = float(message.text.strip().replace(",", "."))
            if not 20 <= target_weight <= 500:
                raise ValueError
        except (ValueError, AttributeError):
            await message.answer(
                "Введи желаемый вес числом от 20 до 500 кг "
                "или отправь «Пропустить»."
            )
            return

    await state.update_data(target_weight_kg=target_weight)
    await state.set_state(Registration.waist)

    await message.answer(
        "Последний шаг! Хочешь указать замеры тела?\n\n"
        "Напиши значения через запятую в порядке:\n"
        "талия, бёдра, грудь.\n\n"
        "Например: 80, 95, 92\n"
        "Можно пропустить отдельный замер, поставив «-».\n"
        "Например: 80, -, 92\n\n"
        "Если не хочешь указывать замеры, напиши «Пропустить»."
    )


@router.message(Registration.waist)
async def process_measurements(message: types.Message, state: FSMContext):
    waist = None
    hips = None
    chest = None

    if not message.text or message.text.strip().lower() != "пропустить":
        try:
            values = [value.strip() for value in message.text.split(",")]

            if not 1 <= len(values) <= 3:
                raise ValueError

            measurements = []

            for value in values:
                if value in ("-", "—", ""):
                    measurements.append(None)
                else:
                    number = float(value.replace(",", "."))
                    if not 20 <= number <= 250:
                        raise ValueError
                    measurements.append(number)

            measurements += [None] * (3 - len(measurements))
            waist, hips, chest = measurements

        except (ValueError, AttributeError):
            await message.answer(
                "Не получилось разобрать замеры.\n\n"
                "Введи значения через запятую в порядке "
                "талия, бёдра, грудь.\n"
                "Например: 80, 95, 92\n"
                "Для пропуска отдельного замера используй «-».\n"
                "Или напиши «Пропустить»."
            )
            return

    await state.update_data(
        waist_cm=waist,
        hips_cm=hips,
        chest_cm=chest,
    )

    data = await state.get_data()
    telegram_user = message.from_user

    user_data = {
        "id": telegram_user.id,
        "username": telegram_user.username,
        "first_name": telegram_user.first_name,
    }

    with db_connect() as conn:
        user_id = upsert_user(conn, user_data)

        conn.execute(
            """update public.app_user
               set gender=%s,
                   height_cm=%s,
                   initial_weight_kg=%s,
                   current_weight_kg=%s,
                   goal=%s,
                   target_weight_kg=%s,
                   updated_at=now()
               where id=%s""",
            (
                data["gender"],
                data["height_cm"],
                data["weight_kg"],
                data["weight_kg"],
                data["goal"],
                data.get("target_weight_kg"),
                user_id,
            ),
        )

        conn.execute(
            """insert into public.measurement
               (user_id, weight_kg, waist_cm, hips_cm, chest_cm)
               values (%s, %s, %s, %s, %s)""",
            (
                user_id,
                data["weight_kg"],
                waist,
                hips,
                chest,
            ),
        )

        await state.clear()

    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="🍽️ Записать еду"),
                types.KeyboardButton(text="📈 Прогресс"),
            ],
            [
                types.KeyboardButton(text="👤 Мои данные"),
            ],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        "Отлично! Регистрация завершена.\n\n"
        "Теперь можешь присылать мне фото еды или описывать "
        "свой приём пищи текстом.",
        reply_markup=keyboard,
    )



FOOD_ANALYSIS_PROMPT = 'Ты — нутрициологический помощник приложения «Тарелка». Анализируй именно описанное или изображённое блюдо и отвечай по-русски, на «ты», спокойно и доброжелательно.\n\nСначала определи тип блюда: основное блюдо, суп, салат, гарнир, перекус, десерт, суши/роллы, бургер, пицца или другое. Оценивай с учётом назначения блюда и контекста. Не требуй, чтобы каждое блюдо соответствовало принципу Гарвардской тарелки: используй его только как ориентир для обычных основных приёмов пищи.\n\nОписывай только продукты и компоненты, которые можно достаточно уверенно распознать. Не додумывай скрытые ингредиенты, состав соуса, способ приготовления, точный вес порции, калорийность или БЖУ. Если сомневаешься, прямо обозначь это и предложи пользователю уточнить состав.\n\nВ начале ответа обязательно укажи честную оценку целым числом от 1 до 10 отдельной строкой: «Оценка: 8/10». Оценивай качество состава с учётом типа блюда. Не снижай оценку десерту за отсутствие овощей или белка; не занижай её только из-за теста, сыра, риса или сахара.\n\nНе предлагай автоматически добавлять фрукты к основным блюдам. Не требуй овощи к каждой пицце или бургеру. Не придумывай рекомендации ради рекомендации. Если есть конкретное реалистичное улучшение — предложи одно. Если существенных изменений не требуется, так и скажи.\n\nНачинай с оценки и сути. Не морализируй, не ругай пользователя, не называй еду плохой. Отвечай коротко, конкретно и естественно.'


def _nutrition_value(value):
    """Convert database values (including JSONB) into compact readable text."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def load_nutrition_context(conn, telegram_id: int):
    """Load the Telegram user's profile and recent food diary for the AI context."""
    profile = conn.execute(
        """
        select id, gender, height_cm, initial_weight_kg,
               current_weight_kg, target_weight_kg, goal
        from public.app_user
        where telegram_id = %s
        """,
        (telegram_id,),
    ).fetchone()

    if profile is None:
        return None, []

    food_entries = conn.execute(
        """
        select created_at, consumed_at, meal_type, detected_foods,
               original_analysis, user_correction,
               corrected_analysis, balance_score, ai_comment
        from public.food_entry
        where user_id = %s
        order by coalesce(consumed_at, created_at::date) desc, created_at desc
        limit 8
        """,
        (profile["id"],),
    ).fetchall()

    return profile, food_entries


def build_nutrition_context(profile, food_entries):
    """Build factual context without inventing missing profile or diary values."""
    if profile is None:
        return "Профиль пользователя не найден."

    gender = {"male": "мужской", "female": "женский"}.get(
        profile.get("gender"), "не указан"
    )

    profile_lines = [
        f"Пол: {gender}",
        f"Рост: {profile.get('height_cm') if profile.get('height_cm') is not None else 'не указан'} см",
        f"Вес при регистрации: {profile.get('initial_weight_kg') if profile.get('initial_weight_kg') is not None else 'не указан'} кг",
        f"Текущий вес в профиле: {profile.get('current_weight_kg') if profile.get('current_weight_kg') is not None else 'не указан'} кг",
        f"Желаемый вес: {profile.get('target_weight_kg') if profile.get('target_weight_kg') is not None else 'не указан'} кг",
        f"Цель: {profile.get('goal') or 'не указана'}",
    ]

    diary_lines = []
    for entry in food_entries:
        date = entry.get("consumed_at") or entry.get("created_at")
        if date is not None:
            date_text = date.strftime("%d.%m.%Y %H:%M") if isinstance(date, datetime) else (date.strftime("%d.%m.%Y") if hasattr(date, "strftime") else str(date))
        else:
            date_text = "дата не указана"

        corrected = (
            entry.get("corrected_analysis")
            or entry.get("user_correction")
            or entry.get("ai_comment")
            or entry.get("original_analysis")
            or entry.get("detected_foods")
        )
        detail = _nutrition_value(corrected)
        if len(detail) > 900:
            detail = detail[:900] + "…"

        score = entry.get("balance_score")
        meal_type = entry.get("meal_type")
        line = f"- {date_text}"
        if meal_type:
            line += f"; тип: {meal_type}"
        if score is not None:
            line += f"; оценка: {score}/10"
        if detail:
            line += f"; запись: {detail}"
        diary_lines.append(line)

    diary_text = "\n".join(diary_lines) if diary_lines else "Записей пока нет."

    return (
        "ДАННЫЕ ПРОФИЛЯ (используй только как контекст, не повторяй без необходимости):\n"
        + "\n".join(profile_lines)
        + "\n\nПОСЛЕДНИЕ ЗАПИСИ ДНЕВНИКА (это только сохранённые записи; не считай их полным рационом):\n"
        + diary_text
    )


def save_telegram_chat_message(conn, telegram_id: int, role: str, content: str):
    conn.execute(
        """
        insert into public.telegram_chat_message
            (telegram_id, role, content)
        values (%s, %s, %s)
        """,
        (telegram_id, role, content[:10000]),
    )


async def process_nutrition_chat(
    message: types.Message,
    user_text: str,
    image_bytes: bytes | None = None,
    image_content_type: str | None = None,
):
    telegram_user = message.from_user

    if telegram_user is None:
        return

    if not OPENAI_API_KEY:
        await message.answer("Сейчас не могу ответить. Попробуй немного позже.")
        return

    try:
        with db_connect() as conn:
            profile, food_entries = load_nutrition_context(conn, telegram_user.id)

            if profile is None:
                await message.answer(
                    "Сначала зарегистрируйся с помощью команды /start."
                )
                return

            history = conn.execute(
                """
                select role, content
                from public.telegram_chat_message
                where telegram_id = %s
                order by created_at desc, id desc
                limit 40
                """,
                (telegram_user.id,),
            ).fetchall()

            save_telegram_chat_message(
                conn,
                telegram_user.id,
                "user",
                user_text or "[Пользователь отправил изображение]",
            )

        conversation = [
            {"role": item["role"], "content": item["content"]}
            for item in reversed(history)
        ]

        current_content = [{"type": "input_text", "text": user_text or (
            "Пользователь прислал изображение. Если оно относится к питанию, "
            "помоги разобраться в нём. Не выдумывай неразборчивые данные."
        )}]

        if image_bytes is not None and image_content_type:
            encoded_image = base64.b64encode(image_bytes).decode("utf-8")
            current_content.append({
                "type": "input_image",
                "image_url": f"data:{image_content_type};base64,{encoded_image}",
                "detail": "auto",
            })

        conversation.append({"role": "user", "content": current_content})

        context_text = build_nutrition_context(profile, food_entries)
        instructions = """
Ты — персональный помощник по питанию приложения «Тарелка». Веди живой,
последовательный разговор. Твоя задача — помогать человеку постепенно
выстраивать удобные пищевые привычки, а не выдавать универсальные памятки.

ОБЩЕНИЕ
- Всегда отвечай по-русски, на «ты», спокойно, уважительно и без назидания.
- Сначала отвечай на то, о чём спросили. Обычно достаточно 2–4 коротких
  предложений. Не используй списки, если пользователь прямо не просит список.
- Не выдавай несколько вариантов ради количества. Когда нужен совет,
  предложи один подходящий вариант и коротко объясни, почему он уместен.
- Не начинай каждый ответ с «Для наладки питания...» или пересказа вопроса.
  Избегай одинаковых вступлений и заключений.
- Не заканчивай каждый ответ формальным вопросом. Задавай не более одного
  уточняющего вопроса и только если ответ действительно поможет подобрать
  следующий совет. Если вопрос не нужен — просто закончи ответ.

АДАПТИВНЫЙ ДИАЛОГ И ПРЕДПОЧТЕНИЯ
- Внимательно используй всю доступную историю переписки ниже. Считай её
  контекстом текущего разговора, а не набором независимых запросов.
- Извлекай из сообщений пользователя явно названные предпочтения, привычки,
  ограничения и нелюбимые продукты. Учитывай их в следующих ответах.
- Если пользователь сказал, что не любит продукт (например, овсянку),
  не предлагай его снова как рекомендацию, пока пользователь сам не изменит
  своё мнение или не попросит обсудить этот продукт.
- Не переспрашивай о том, что уже известно из истории. Не делай вид,
  что предпочтение известно, если пользователь его не называл.
- Если предпочтения неизвестны и от них зависит выбор, не угадывай.
  Можно предложить нейтральный пример и задать один конкретный вопрос,
  например предпочитает ли человек сладкий или несладкий завтрак.
- Не превращай разговор в анкету: узнавай привычки постепенно, по одному
  вопросу, только в естественный момент.
- Если пользователь отвечает на уточняющий вопрос, используй этот ответ
  в следующей рекомендации, а не повторяй прежний совет.

ПЕРСОНАЛИЗАЦИЯ
- Используй профиль и историю дневника, если они относятся к вопросу.
  Не выдумывай отсутствующие сведения.
- При цели «Наладить питание» фокусируйся на регулярности, разнообразии,
  сытости, удобстве и устойчивых привычках. Не своди советы автоматически
  к похудению, снижению веса или подсчёту калорий.
- Не упоминай рост и вес без причины и не делай выводов о здоровье,
  активности, аппетите или предпочтениях только по этим показателям.
- Несколько записей дневника не обязательно отражают весь рацион человека.

КОНКРЕТНЫЕ СОВЕТЫ
- Предлагай реалистичные решения из доступных продуктов, без обязательной
  покупки специальных ингредиентов.
- Не навязывай строгий режим, подсчёт калорий или идеальный рацион.
- Не называй продукты плохими, не морализируй и не вызывай чувство вины.
- Не назначай калорийность и нормы БЖУ без достаточных исходных данных.

ЗАВТРАК И ВРЕМЯ ПРИЁМА ПИЩИ
- Не утверждай, что нужно завтракать сразу после пробуждения. Завтрак через
  час или два вполне нормален, если человеку так комфортно.
- Не советуй перекусывать, пить воду или есть фрукт для «запуска метаболизма».
- Ориентируйся на голод, аппетит и удобство. Если утром нет аппетита,
  не заставляй человека есть раньше.
- Если пользователь просит идею завтрака, но его вкусы неизвестны,
  не объявляй овсянку или другой продукт «идеальным» выбором. Дай один
  простой пример как отправную точку и при необходимости уточни один
  параметр вкуса. Если предпочтения уже названы — подбирай только с их учётом.
- Если человек не любит предложенный продукт, спокойно исключи его и
  предложи альтернативу без повторного списка из четырёх-пяти блюд.
- Не добавляй фрукты к основному приёму пищи автоматически.

ДНЕВНИК, ФОТО И СКРИНШОТЫ
- Используй предыдущие сообщения, чтобы сохранять непрерывность разговора.
- Если ссылаешься на дневник, уточняй, что видишь только сохранённые записи.
- Не утверждай, что блюдо съедено сегодня или в определённое время,
  если это не подтверждается данными.
- При анализе скриншота FatSecret или другого приложения учитывай только
  реально видимые блюда, калории и БЖУ. Не додумывай нечитаемые значения.
- Сам по себе скриншот не означает просьбу сохранить еду в дневник «Тарелки».
  Не говори, что что-либо сохранено, если сохранение не выполнялось.

ТОЧНОСТЬ И БЕЗОПАСНОСТЬ
- Не выдумывай данные профиля, дневника, калорийность, БЖУ или состав блюд.
- Если информации недостаточно, честно обозначь это простыми словами.
- Не ставь диагнозы и не представляй себя врачом. По вопросам заболеваний,
  лекарств и лечебных диет рекомендуй обратиться к специалисту.
- Помогай сделать следующий реалистичный шаг, а не добиваться идеального
  питания.
""" + "\n\n" + context_text

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model="gpt-4o-mini",
            instructions=instructions,
            input=conversation,
            max_output_tokens=900,
        )

        answer = response.output_text
        if not answer:
            raise ValueError("Empty AI response")

        with db_connect() as conn:
            save_telegram_chat_message(
                conn, telegram_user.id, "assistant", answer
            )

        await message.answer(answer)

    except Exception as exc:
        print(
            f"Nutrition chat error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Не получилось ответить. Попробуй отправить сообщение ещё раз."
        )



def _moscow_today():
    return datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()


def _format_import_date(value):
    try:
        return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
    except Exception:
        return str(value or "не определена")


def _format_macro_value(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else str(round(number, 2)).replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _food_field(food, *names):
    for name in names:
        if isinstance(food, dict) and food.get(name) is not None:
            return food.get(name)
    return None


def _meal_food_totals(meal):
    foods = meal.get("foods") or []
    if not isinstance(foods, list) or not foods or not all(isinstance(item, dict) for item in foods):
        return None
    totals = {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    seen = {key: False for key in totals}
    for food in foods:
        aliases = {
            "calories": ("calories", "kcal"),
            "protein_g": ("protein_g", "protein*g", "protein", "proteins"),
            "fat_g": ("fat_g", "fat*g", "fat", "fats"),
            "carbs_g": ("carbs_g", "carbs*g", "carbs", "carbohydrates"),
        }
        for key, names in aliases.items():
            value = _food_field(food, *names)
            if value is not None:
                try:
                    totals[key] += float(value)
                    seen[key] = True
                except (TypeError, ValueError):
                    pass
    return {key: (round(value, 2) if seen[key] else None) for key, value in totals.items()}


def _render_import_preview(data):
    selected_date = data.get("date") or _moscow_today()
    date_source = data.get("date_source", "today_fallback")
    if date_source == "screenshot":
        date_note = "Дата и год распознаны на скриншоте."
    elif date_source == "screenshot_no_year":
        date_note = f"На скриншоте год не указан — использую текущий год ({date.fromisoformat(selected_date).year})."
    elif date_source == "user":
        date_note = "Дату исправили по твоему сообщению."
    else:
        date_note = "Дата на скриншоте не видна — использую сегодняшнюю дату по Москве."
    lines = [f"📅 **Дата записей: {_format_import_date(selected_date)}**", date_note, "", "**Что я распознал:**"]
    meals = data.get("meals") or []
    if not meals:
        lines.append("Не удалось уверенно выделить отдельные приёмы пищи.")
    for i, meal in enumerate(meals, 1):
        title = meal.get("name") or f"Приём пищи {i}"
        portion = meal.get("portion")
        meal_type = meal.get("meal_type")
        heading = f"{i}. **{title}**"
        meal_date = meal.get("date")
        if meal_date and meal_date != selected_date:
            heading += f" — {_format_import_date(meal_date)}"
        if meal_type:
            heading += f" ({meal_type})"
        if portion:
            heading += f" — {portion}"
        lines.append(heading)
        values = []
        for key, label in (("calories", "ккал"), ("protein_g", "белки"), ("fat_g", "жиры"), ("carbs_g", "углеводы")):
            value = _format_macro_value(meal.get(key))
            if value is not None:
                values.append(f"{label}: {value}" if key == "calories" else f"{label}: {value} г")
        if values:
            lines.append("   Итог приёма: " + " · ".join(values))
        foods = meal.get("foods") or []
        if foods:
            lines.append("   **Продукты:**")
            for food in foods:
                if isinstance(food, dict):
                    food_name = _food_field(food, "name", "title") or "Продукт без названия"
                    food_values = []
                    for key, label, aliases in (
                        ("calories", "ккал", ("calories", "kcal")),
                        ("protein_g", "Б", ("protein_g", "protein*g", "protein", "proteins")),
                        ("fat_g", "Ж", ("fat_g", "fat*g", "fat", "fats")),
                        ("carbs_g", "У", ("carbs_g", "carbs*g", "carbs", "carbohydrates")),
                    ):
                        value = _format_macro_value(_food_field(food, *aliases))
                        if value is not None:
                            food_values.append(f"{label} {value}" + (" г" if key != "calories" else ""))
                    lines.append("   • " + str(food_name) + (" — " + ", ".join(food_values) if food_values else ""))
                else:
                    lines.append("   • " + str(food))
        totals = _meal_food_totals(meal)
        meal_calories = meal.get("calories")
        if totals and meal_calories is not None and totals.get("calories") is not None:
            try:
                difference = round(float(meal_calories) - totals["calories"])
                if abs(difference) > 5:
                    lines.append(f"   ⚠️ Проверка: сумма калорий продуктов — {round(totals['calories'])} ккал, итог приёма — {round(float(meal_calories))} ккал. Разница {difference:+d} ккал. Нужно уточнить источник расхождения.")
            except (TypeError, ValueError):
                pass
        if meal.get("uncertainty"):
            lines.append("   ⚠️ Не уверен: " + str(meal["uncertainty"]))
    if data.get("period_note"):
        lines.extend(["", str(data["period_note"])])
    lines.extend(["", "Проверь дату, продукты и значения. Напиши, что исправить, или ответь **«Да»**, если всё верно. До подтверждения я ничего не сохраняю."])
    return "\n".join(lines)


def _normalize_import_dates(data):
    """Use Moscow's current year when the screenshot shows day/month but no year."""
    today = date.fromisoformat(_moscow_today())
    raw_date = data.get("date")
    year_visible = data.get("date_year_visible")
    if raw_date:
        parsed = date.fromisoformat(str(raw_date)).replace(year=today.year) if year_visible is False else date.fromisoformat(str(raw_date))
        data["date"] = parsed.isoformat()
        if year_visible is False and data.get("date_source") != "user":
            data["date_source"] = "screenshot_no_year"
        elif data.get("date_source") not in {"user", "today_fallback"}:
            data["date_source"] = "screenshot"
    else:
        data["date"] = today.isoformat()
        if data.get("date_source") != "user":
            data["date_source"] = "today_fallback"
    for meal in data.get("meals") or []:
        if not isinstance(meal, dict):
            continue
        meal_raw_date = meal.get("date")
        meal_year_visible = meal.get("date_year_visible", year_visible)
        if meal_raw_date:
            parsed_meal_date = date.fromisoformat(str(meal_raw_date))
            if meal_year_visible is False:
                parsed_meal_date = parsed_meal_date.replace(year=today.year)
            meal["date"] = parsed_meal_date.isoformat()
        else:
            meal["date"] = data["date"]
    return data


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI did not return JSON")
    return json.loads(text[start:end + 1])


def _get_pending_import(conn, telegram_id):
    return conn.execute(
        """select id, user_id, extraction, status from public.nutrition_import_pending
           where telegram_id=%s and status='pending'
           order by created_at desc limit 1""",
        (telegram_id,),
    ).fetchone()


async def begin_screenshot_import(message, image_bytes, image_content_type):
    telegram_user = message.from_user
    if telegram_user is None:
        return
    try:
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model="gpt-4o-mini",
            instructions=(
                "Ты извлекаешь данные с изображения дневника питания. Верни только JSON без markdown. "
                "Текущая дата по Москве: " + _moscow_today() + ". "
                "Схема: {date: YYYY-MM-DD или null, date_year_visible: true/false, date_source: screenshot/screenshot_no_year/today_fallback, "
                "period_note: строка или null, meals: [{date: YYYY-MM-DD или null, date_year_visible: true/false, name, meal_type, portion, "
                "foods:[{name, calories, protein_g, fat_g, carbs_g, uncertainty}], calories, protein_g, fat_g, carbs_g, uncertainty}]}. "
                "КРИТИЧЕСКИ ВАЖНО: никогда не определяй БЖУ по привычному порядку столбцов. В разных приложениях порядок разный. "
                "Каждое число связывай только с его подписью/заголовком рядом с ним: белки/Б/protein, жиры/Ж/fat, углеводы/У/carbs, калории/ккал/kcal. "
                "Сначала найди подписи столбцов или строк, затем прочитай число в соответствующем столбце/строке. "
                "Если подписи не видны или связь числа с подписью неоднозначна, верни null для сомнительного показателя и объясни это в uncertainty. "
                "Не переставляй значения местами и не угадывай по типичному составу продукта. "
                "Учитывай только реально видимые данные. Не вычисляй и не угадывай числа. "
                "Если на скриншоте видны день и месяц, но год не указан, date_year_visible=false и используй текущий год из даты по Москве, указанной выше. "
                "Если год явно виден, date_year_visible=true. Если дата отсутствует или неразборчива, date=null и date_source=today_fallback. "
                "Разделяй отдельные приёмы пищи, если они видны. Если скриншот охватывает несколько дат, укажи дату отдельно у каждого приёма. "
                "В calories/protein_g/fat_g/carbs_g указывай ИТОГ приёма пищи только если он явно показан; foods содержит отдельные продукты и их собственные значения. "
                "Не смешивай итоги приёма, итоги дня и отдельные продукты. Не превращай общий итог дня в отдельный приём пищи. "
                "Если показан только итог за день без блюд, верни meals=[] и объясни это в period_note. "
                "Числа возвращай числом, отсутствующие значения — null. Не добавляй сведения, которых нет на изображении."
            ),
            input=[{"role":"user","content":[
                {"type":"input_text","text":"Извлеки дату, блюда, порции, калории и БЖУ с этого скриншота."},
                {"type":"input_image","image_url":f"data:{image_content_type};base64,{encoded}","detail":"auto"},
            ]}],
            max_output_tokens=1200,
        )
        data = _extract_json(response.output_text)
        if not isinstance(data.get("meals"), list):
            data["meals"] = []
        data = _normalize_import_dates(data)
        with db_connect() as conn:
            user = conn.execute("select id from public.app_user where telegram_id=%s", (telegram_user.id,)).fetchone()
            if user is None:
                await message.answer("Сначала зарегистрируйся с помощью команды /start.")
                return
            conn.execute(
                """update public.nutrition_import_pending set status='cancelled', updated_at=now()
                   where telegram_id=%s and status='pending'""",
                (telegram_user.id,),
            )
            conn.execute(
                """insert into public.nutrition_import_pending
                   (telegram_id, user_id, extraction, status, source_message_id, created_at, updated_at)
                   values (%s,%s,%s,'pending',%s,now(),now())
                   on conflict (telegram_id) do update set
                     user_id=excluded.user_id, extraction=excluded.extraction,
                     status='pending', source_message_id=excluded.source_message_id,
                     created_at=now(), updated_at=now()""",
                (telegram_user.id, user["id"], Jsonb(data), message.message_id),
            )
        await message.answer(_render_import_preview(data), parse_mode="Markdown")
    except Exception as exc:
        print(f"Screenshot extraction error: {type(exc).__name__}: {exc}", flush=True)
        await message.answer("Не получилось надёжно прочитать скриншот. Попробуй отправить изображение чётче.")


async def handle_pending_screenshot_message(message):
    telegram_user = message.from_user
    if telegram_user is None or not message.text:
        return False
    text = message.text.strip()
    try:
        with db_connect() as conn:
            pending = _get_pending_import(conn, telegram_user.id)
        if pending is None:
            return False
        lowered = text.casefold().strip(".!?, ")
        if lowered in {"нет", "отмена", "отменить", "не сохраняй", "cancel"}:
            with db_connect() as conn:
                conn.execute("update public.nutrition_import_pending set status='cancelled', updated_at=now() where id=%s", (pending["id"],))
            await message.answer("Импорт скриншота отменён. В дневник ничего не добавлено.")
            return True
        confirmation = (
            lowered in {"да", "верно", "правильно", "всё верно", "все верно", "подтверждаю", "сохраняй", "да, всё верно", "да все верно"}
            or bool(re.fullmatch(r"да[!., ]*(всё|все)?[!., ]*(верно|правильно|так)?[!., ]*", lowered))
        )
        if confirmation:
            data = pending["extraction"]
            meals = data.get("meals") or []
            if not meals:
                await message.answer("На скриншоте не удалось выделить отдельные приёмы пищи. Нечего добавлять в дневник. Можешь прислать скриншот с деталями блюд.")
                return True
            # The applied migration stores consumed_at as a DATE, without a time.
            with db_connect() as conn:
                for meal in meals:
                    detected = {"source":"nutrition_screenshot", "date":data["date"], "meal":meal}
                    raw_meal_type = str(meal.get("meal_type") or "").strip().lower()
                    meal_type_map = {
                        "breakfast": "breakfast", "завтрак": "breakfast",
                        "lunch": "lunch", "обед": "lunch",
                        "dinner": "dinner", "ужин": "dinner",
                        "snack": "snack", "перекус": "snack",
                        "other": "other", "другое": "other",
                    }
                    safe_meal_type = meal_type_map.get(raw_meal_type, "other")
                    conn.execute(
                        """insert into public.food_entry
                           (user_id, meal_type, detected_foods, original_analysis, consumed_at)
                           values (%s,%s,%s,%s,%s)""",
                        (pending["user_id"], safe_meal_type, Jsonb(detected), Jsonb({"source":"nutrition_screenshot", "extraction":data}), date.fromisoformat(meal.get("date") or data["date"])),
                    )
                conn.execute("update public.nutrition_import_pending set status='confirmed', updated_at=now() where id=%s", (pending["id"],))
            saved_dates = sorted({
                date.fromisoformat(meal.get("date") or data["date"])
                for meal in meals
            })
            if len(saved_dates) == 1:
                saved_date_text = _format_import_date(saved_dates[0].isoformat())
            else:
                saved_date_text = ", ".join(_format_import_date(item.isoformat()) for item in saved_dates)
            await message.answer(f"Готово! Добавил {len(meals)} запис(ей) в дневник за {saved_date_text}. Теперь учту эти данные в разговоре.")
            summary = "Подтверждённые данные из скриншота дневника питания за " + _format_import_date(data["date"]) + ":\n" + json.dumps(meals, ensure_ascii=False) + "\nПроанализируй данные с учётом моего профиля и истории дневника. Не выдумывай калории и БЖУ сверх переданных данных."
            await process_nutrition_chat(message, summary)
            return True
        # Treat any other text as a correction; ask the model to update the structured extraction.
        data = pending["extraction"]
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model="gpt-4o-mini",
            instructions=(
                "Обнови JSON распознавания дневника питания по исправлению пользователя. "
                "Сохрани прежние данные, если пользователь их не меняет. Не придумывай факты. "
                "Не меняй местами белки, жиры и углеводы: используй только семантические подписи показателей. "
                "Если пользователь явно меняет дату, обнови date и date_source='user'. "
                "Верни только JSON с полями date, date_year_visible, date_source, period_note, meals. "
                "В meals используй поля date, date_year_visible, name, meal_type, portion, foods:[{name, calories, protein_g, fat_g, carbs_g, uncertainty}], calories, protein_g, fat_g, carbs_g, uncertainty."
            ),
            input="Текущие данные: " + json.dumps(data, ensure_ascii=False) + "\nИсправление пользователя: " + text,
            max_output_tokens=1200,
        )
        updated = _extract_json(response.output_text)
        if not updated.get("date"):
            updated["date"] = data.get("date") or _moscow_today()
            updated["date_source"] = data.get("date_source", "today_fallback")
            updated["date_year_visible"] = data.get("date_year_visible")
        elif updated.get("date") != data.get("date"):
            updated["date_source"] = "user"
            updated["date_year_visible"] = True
        if not isinstance(updated.get("meals"), list):
            updated["meals"] = data.get("meals", [])
        updated = _normalize_import_dates(updated)
        with db_connect() as conn:
            conn.execute("update public.nutrition_import_pending set extraction=%s, updated_at=now() where id=%s", (Jsonb(updated), pending["id"]))
        await message.answer(_render_import_preview(updated), parse_mode="Markdown")
        return True
    except Exception as exc:
        print(f"Pending screenshot handling error: {type(exc).__name__}: {exc}", flush=True)
        await message.answer("Не получилось обработать исправление. Попробуй сформулировать его короче.")
        return True

@router.message(lambda message: bool(message.photo) or bool(message.document))
async def process_bot_image(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        await message.answer(
            "Сначала закончи регистрацию, пожалуйста. После этого сможешь "
            "отправлять фото еды и скриншоты дневника питания."
        )
        return

    if not OPENAI_API_KEY:
        await message.answer("Сервис анализа изображений временно недоступен. Попробуй позже.")
        return

    image_file_id = None
    image_content_type = None
    image_size = None

    if message.photo:
        image_file_id = message.photo[-1].file_id
        image_content_type = "image/jpeg"
        image_size = message.photo[-1].file_size
    elif message.document:
        document = message.document
        image_file_id = document.file_id
        image_content_type = (document.mime_type or "").lower()
        image_size = document.file_size

        if not image_content_type.startswith("image/"):
            extension_to_mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }
            filename = (document.file_name or "").lower()
            image_content_type = next(
                (mime for extension, mime in extension_to_mime.items()
                 if filename.endswith(extension)),
                image_content_type,
            )

        allowed_types = {
            "image/jpeg", "image/png", "image/webp", "image/gif"
        }
        if image_content_type not in allowed_types:
            await message.answer(
                "Пока поддерживаю изображения JPG, PNG, WEBP и GIF. "
                "Попробуй отправить скриншот в одном из этих форматов."
            )
            return

    if image_size is not None and image_size > 10 * 1024 * 1024:
        await message.answer("Изображение слишком большое. Максимальный размер — 10 МБ.")
        return

    await message.answer("Изображение получил, сейчас посмотрю.")

    try:
        downloaded = await message.bot.download(image_file_id)
        if downloaded is None:
            raise ValueError("Could not download Telegram image")

        image_bytes = downloaded.read()
        if not image_bytes:
            raise ValueError("Empty image")
        if len(image_bytes) > 10 * 1024 * 1024:
            await message.answer("Изображение слишком большое. Максимальный размер — 10 МБ.")
            return

        caption = (message.caption or "").strip()
        classifier_text = (
            "Определи тип изображения. Верни строго одно значение: "
            "MEAL_PHOTO — фотография готовой еды или продуктов; "
            "NUTRITION_SCREENSHOT — скриншот приложения учёта питания, "
            "дневника еды, калорий или БЖУ; "
            "OTHER — всё остальное. Не объясняй ответ. "
            f"Подпись пользователя: {caption or 'нет'}"
        )

        encoded_image = base64.b64encode(image_bytes).decode("utf-8")
        classifier = OpenAI(api_key=OPENAI_API_KEY)
        classification = classifier.responses.create(
            model="gpt-4o-mini",
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": classifier_text},
                    {
                        "type": "input_image",
                        "image_url": f"data:{image_content_type};base64,{encoded_image}",
                        "detail": "auto",
                    },
                ],
            }],
            max_output_tokens=24,
        )

        image_kind = (classification.output_text or "").strip().upper()
        if "NUTRITION_SCREENSHOT" in image_kind:
            image_kind = "NUTRITION_SCREENSHOT"
        elif "MEAL_PHOTO" in image_kind:
            image_kind = "MEAL_PHOTO"
        else:
            image_kind = "OTHER"

        if image_kind == "NUTRITION_SCREENSHOT":
            await begin_screenshot_import(message, image_bytes, image_content_type)
            return

        if image_kind != "MEAL_PHOTO":
            await message.answer(
                "Похоже, это не фото еды и не скриншот дневника питания. "
                "Я лучше всего помогаю разбирать блюда и данные приложений питания."
            )
            return

        if supabase is None:
            await message.answer(
                "Сервис сохранения дневника временно недоступен. Попробуй позже."
            )
            return

        prompt = FOOD_ANALYSIS_PROMPT
        if caption:
            prompt += "\\n\\nКомментарий пользователя к фотографии: " + caption

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:{image_content_type};base64,{encoded_image}",
                        "detail": "auto",
                    },
                ],
            }],
            max_output_tokens=550,
        )

        analysis = response.output_text
        if not analysis:
            raise ValueError("Empty AI response")

        score_match = re.search(
            r"Оценка\s*:\s*(10|[1-9])\s*/\s*10",
            analysis,
            re.IGNORECASE,
        )
        balance_score = int(score_match.group(1)) if score_match else None

    except Exception as exc:
        print(
            f"Bot image analysis error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Не получилось разобрать изображение. Попробуй отправить его ещё раз "
            "или опиши содержимое текстом."
        )
        return

    try:
        import uuid

        telegram_user = message.from_user
        if telegram_user is None:
            return

        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }[image_content_type]

        with db_connect() as conn:
            db_user = conn.execute(
                "select id from public.app_user where telegram_id=%s",
                (telegram_user.id,),
            ).fetchone()

            if db_user is None:
                await message.answer("Сначала зарегистрируйся с помощью команды /start.")
                return

            user_id = db_user["id"]
            photo_path = f"{user_id}/{uuid.uuid4()}.{extension}"

            supabase.storage.from_("food-photos").upload(
                photo_path,
                image_bytes,
                {"content-type": image_content_type},
            )

            conn.execute(
                """
                insert into public.food_entry (
                    user_id, photo_url, original_analysis, balance_score, ai_comment
                )
                values (%s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    photo_path,
                    Jsonb({"text": analysis}),
                    balance_score,
                    analysis,
                ),
            )

            history_user_text = caption or "[Пользователь отправил фото приёма пищи]"
            save_telegram_chat_message(
                conn, telegram_user.id, "user", history_user_text
            )
            save_telegram_chat_message(
                conn, telegram_user.id, "assistant", analysis
            )

        await message.answer(analysis)

    except Exception as exc:
        print(
            f"Bot food image save error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Анализ готов, но не удалось сохранить его в дневник. Попробуй позже."
        )


@router.message(lambda message: message.text == "🍽️ Записать еду")
async def start_food_entry(message: types.Message):
    await message.answer(
        "Отлично! 🥗\n\n"
        "Отправь фотографию еды или напиши, что ты съел.\n\n"
        "Я проанализирую блюдо и сохраню результат в твой дневник."
    )

@router.message(lambda message: message.text == "📈 Прогресс")
async def show_progress(message: types.Message):
    telegram_user = message.from_user

    if telegram_user is None:
        return

    try:
        with db_connect() as conn:
            profile = conn.execute(
                """
                select initial_weight_kg,
                       current_weight_kg,
                       target_weight_kg,
                       goal
                from public.app_user
                where telegram_id=%s
                """,
                (telegram_user.id,),
            ).fetchone()

            if profile is None:
                await message.answer(
                    "Сначала зарегистрируйся с помощью команды /start."
                )
                return

            latest = conn.execute(
                """
               select weight_kg, waist_cm, hips_cm, chest_cm, measured_at
               from public.measurement
                where user_id = (
                    select id from public.app_user
                    where telegram_id=%s
                )
                order by measured_at desc
                limit 1
                """,
                (telegram_user.id,),
            ).fetchone()

        def fmt(value):
            return f"{float(value):g}".replace(".", ",")

        initial_weight = profile["initial_weight_kg"]

        current_weight = (
            latest["weight_kg"]
            if latest and latest["weight_kg"] is not None
            else profile["current_weight_kg"]
        )

        lines = ["📈 Твой прогресс", ""]

        if current_weight is not None:
            lines.append(f"Текущий вес: **{fmt(current_weight)} кг**")

        if initial_weight is not None:
            lines.append(f"Вес при регистрации: {fmt(initial_weight)} кг")

        if current_weight is not None and initial_weight is not None:
            difference = float(current_weight) - float(initial_weight)

            if difference < 0:
                change = f"−{fmt(abs(difference))}"
            elif difference > 0:
                change = f"+{fmt(difference)}"
            else:
                change = "0"

            lines.append(f"Изменение: {change} кг")

        target_weight = profile["target_weight_kg"]

        if target_weight is not None:
            lines.append(f"Желаемый вес: {fmt(target_weight)} кг")

        if latest:
            lines.extend(["", "📏 Последние замеры:"])

            for label, field in (
                ("Талия", "waist_cm"),
                ("Бёдра", "hips_cm"),
                ("Грудь", "chest_cm"),
            ):
                value = latest[field]
                if value is not None:
                    lines.append(f"{label}: {fmt(value)} см")

        if latest["measured_at"]:
                date = latest["measured_at"].strftime("%d.%m.%Y")
                lines.extend(["", f"Дата последней записи: {date}"])

        if profile["goal"]:
            lines.extend(["", f"Твоя цель: {profile['goal']}"])

        lines.extend([
            "",
            "Продолжай записывать питание и следить за изменениями! 🥗"
        ])

        await message.answer(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    except Exception as exc:
        print(
            f"Progress error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Не получилось загрузить прогресс. Попробуй немного позже."
        )


@router.message(lambda message: message.text == "👤 Мои данные")
async def show_profile(message: types.Message, state: FSMContext):
    telegram_user = message.from_user
    
    await state.clear()

    if telegram_user is None:
        return

    try:
        with db_connect() as conn:
            profile = conn.execute(
                """
                select gender,
                       height_cm,
                       initial_weight_kg,
                       current_weight_kg,
                       target_weight_kg,
                       goal
                from public.app_user
                where telegram_id=%s
                """,
                (telegram_user.id,),
            ).fetchone()

            if profile is not None:
                latest = conn.execute(
                    """
                    select weight_kg, waist_cm, hips_cm, chest_cm, measured_at
                    from public.measurement
                    where user_id = (
                        select id from public.app_user
                        where telegram_id=%s
                    )
                    order by measured_at desc
                    limit 1
                    """,
                    (telegram_user.id,),
                ).fetchone()
            else:
                latest = None

        if profile is None:
            await message.answer(
                "Не нашёл твои данные. Попробуй начать с команды /start."
            )
            return

        def fmt(value):
            return f"{float(value):g}".replace(".", ",")

        gender = {
            "male": "Мужской",
            "female": "Женский",
        }.get(profile["gender"], "Не указан")

        current_weight = (
            latest["weight_kg"]
            if latest and latest["weight_kg"] is not None
            else profile["current_weight_kg"]
        )

        lines = [
            "👤 Твои данные",
            "",
            f"Пол: {gender}",
            f"Рост: {fmt(profile['height_cm']) + ' см' if profile['height_cm'] is not None else 'Не указан'}",
            f"Вес при регистрации: {fmt(profile['initial_weight_kg']) + ' кг' if profile['initial_weight_kg'] is not None else 'Не указан'}",
            f"Текущий вес: {fmt(current_weight) + ' кг' if current_weight is not None else 'Не указан'}",
            f"Желаемый вес: {fmt(profile['target_weight_kg']) + ' кг' if profile['target_weight_kg'] is not None else 'Не указан'}",
            f"Цель: {profile['goal'] or 'Не указана'}",
            "",
            "📏 Последние замеры:",
        ]

        if latest:
            for label, field in (
                ("Талия", "waist_cm"),
                ("Бёдра", "hips_cm"),
                ("Грудь", "chest_cm"),
            ):
                value = latest[field]
                lines.append(
                    f"{label}: {fmt(value) + ' см' if value is not None else 'Не указаны'}"
                )

            if latest["measured_at"]:
                date = latest["measured_at"].strftime("%d.%m.%Y")
                lines.extend(["", f"Дата замеров: {date}"])
        else:
            lines.append("Замеры пока не добавлены.")

        await message.answer(
            "\n".join(lines),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="✏️ Изменить вес",
                            callback_data="edit_weight",
                        )
                    ]
                ]
            ),
        )

    except Exception as exc:
        print(
            f"Profile error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Не получилось загрузить твои данные. Попробуй немного позже."
        )


@router.callback_query(lambda callback: callback.data == "edit_weight")
async def start_weight_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ProfileEdit.weight)

    await callback.message.answer(
        "Введи свой текущий вес в килограммах.\n\n"
        "Например: 64,5"
    )

    await callback.answer()


@router.message(
    lambda message: bool(message.text)
    and not message.text.startswith("/")
    and message.text not in (
        "🍽️ Записать еду",
        "📈 Прогресс",
        "👤 Мои данные",
    )
)
async def process_food_text(message: types.Message, state: FSMContext):

    if await state.get_state() is not None:
        await message.answer(
            "Сначала закончи регистрацию, пожалуйста. "
            "После этого сможешь записывать еду."
        )
        return

    user_text = (message.text or "").strip()

    if not user_text:
        return

    if await handle_pending_screenshot_message(message):
        return

    profile_question = re.search(
        r"(обо мне|мои данные|мой профиль|сохран[её]нн\w+ данн|"
        r"какой у меня (рост|вес|пол|цель)|"
        r"сколько я вешу|мой рост|мой вес|моя цель)",
        user_text.lower(),
    )

    if profile_question:
        await show_profile(message, state)
        return

    if len(user_text) > 3000:
        await message.answer(
            "Сообщение получилось слишком длинным. "
            "Попробуй сократить его до 3000 символов."
        )
        return


    try:
        with db_connect() as conn:
            history = conn.execute(
                """
                select role, content
                from public.telegram_chat_message
                where telegram_id = %s
                order by created_at desc, id desc
                limit 8
                """,
                (message.from_user.id,),
            ).fetchall()

        classification_input = [
            {
                "role": item["role"],
                "content": item["content"],
            }
            for item in reversed(history)
        ]

        classification_input.append({
            "role": "user",
            "content": user_text,
        })

        classifier = OpenAI(api_key=OPENAI_API_KEY)

        classification = classifier.responses.create(
            model="gpt-4o-mini",
            instructions="""
Определи намерение пользователя в последнем сообщении.

Верни строго одно слово:

FOOD — пользователь сообщает, что он съел или выпил,
и хочет записать этот приём пищи.

CHAT — пользователь задаёт вопрос, обсуждает питание,
просит совет, делится переживаниями или продолжает разговор.

CLARIFY — намерение действительно непонятно.

Учитывай историю диалога.
Не считай каждое упоминание еды намерением её записать.
Если пользователь задаёт вопрос о блюде — это CHAT.
Не добавляй пояснений, только одно слово.
""",
            input=classification_input,
            max_output_tokens=16,
        )

        intent = classification.output_text.strip().upper()

    except Exception as exc:
        print(
            f"Intent classification error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await process_nutrition_chat(message, user_text)
        return

    if intent.strip().upper().startswith("FOOD"):
        pass
    else:
        await process_nutrition_chat(message, user_text)
        return

    food_text = user_text


    if not food_text:
        return

    if len(food_text) > 3000:
        await message.answer(
            "Описание получилось слишком длинным. "
            "Попробуй сократить его до 3000 символов."
        )
        return

    if not OPENAI_API_KEY or supabase is None:
        await message.answer(
            "Сервис анализа еды временно недоступен. Попробуй позже."
        )
        return

    await message.answer("Принял! Сейчас разберусь, что у тебя было на тарелке.")

    try:
        prompt = FOOD_ANALYSIS_PROMPT

        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"{prompt}\n\n"
                                f"Описание еды от пользователя: {food_text}"
                            ),
                        }
                    ],
                }
            ],
            max_output_tokens=500,
        )

        analysis = response.output_text

        if not analysis:
            raise ValueError("Empty AI response")

        score_match = re.search(
            r"Оценка\s*:\s*(10|[1-9])\s*/\s*10",
            analysis,
            re.IGNORECASE,
        )
        balance_score = int(score_match.group(1)) if score_match else None

    except Exception as exc:
        print(
            f"Bot food text analysis error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Не получилось проанализировать описание. "
            "Попробуй ещё раз."
        )
        return

    try:
        telegram_user = message.from_user

        with db_connect() as conn:
            db_user = conn.execute(
                "select id from public.app_user where telegram_id=%s",
                (telegram_user.id,),
            ).fetchone()

            if db_user is None:
                await message.answer(
                    "Сначала зарегистрируйся с помощью команды /start."
                )
                return

            user_id = db_user["id"]

            conn.execute(
                """
                insert into public.food_entry (
                    user_id,
                    detected_foods,
                    original_analysis,
                    balance_score,
                    ai_comment
                )
                values (%s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    Jsonb({"user_description": food_text}),
                    Jsonb({"text": analysis}),
                    balance_score,
                    analysis,
                ),
            )

            save_telegram_chat_message(
                conn, telegram_user.id, "user", food_text
            )
            save_telegram_chat_message(
                conn, telegram_user.id, "assistant", analysis
            )

        await message.answer(analysis)

    except Exception as exc:
        print(
            f"Bot food text save error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        await message.answer(
            "Анализ готов, но не удалось сохранить его в дневник. "
            "Попробуй позже."
        )

dp.include_router(router)
@app.on_event("startup")
async def setup_telegram_webhook():
    if bot is None:
        print("Telegram bot is not configured", flush=True)
        return

    if not TELEGRAM_WEBHOOK_SECRET:
        print("Telegram webhook secret is missing", flush=True)
        return

    await bot.set_webhook(
        url="https://tarelka-mini-app.onrender.com/telegram/webhook",
        secret_token=TELEGRAM_WEBHOOK_SECRET,
        drop_pending_updates=False,
    )

    print("Telegram webhook configured", flush=True)

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot is not configured")

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if not TELEGRAM_WEBHOOK_SECRET or not hmac.compare_digest(
        secret, TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        data = await request.json()
        update = types.Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as exc:
        print(f"Telegram webhook error: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=400, detail="Webhook processing failed")

    return {"ok": True}
