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
