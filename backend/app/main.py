from fastapi.middleware.cors import CORSMiddleware
import base64
from openai import OpenAI
from supabase import create_client
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

import psycopg
from psycopg.rows import dict_row
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
    except Exception as exc:
        print(f"Telegram user save error: {type(exc).__name__}: {exc}", flush=True)
        await message.answer("Не удалось начать регистрацию. Попробуй ещё раз позже.")
        return

    await state.clear()
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
