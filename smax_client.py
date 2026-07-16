"""
Клиент для SMAX.

Сейчас реализована только аутентификация с фоновым автообновлением токена.
Когда понадобится дергать другие эндпоинты SMAX (другие роуты) — добавляй
сюда новые функции (например, get_ticket(), create_ticket() и т.п.),
используя get_smax_token() для авторизации. Так вся логика SMAX останется
в одном месте, а main.py (и любой другой процесс) просто импортирует
то, что нужно.

Как подключить в другом файле (например, main.py):

    import asyncio
    import smax_client

    app.include_router(smax_client.router)

    @app.on_event("startup")
    async def on_startup():
        asyncio.create_task(smax_client.smax_token_refresh_loop())

    # А там, где нужен запрос к SMAX:
    token = smax_client.get_smax_token()

Переменные окружения (задать в Render → Environment):
  SMAX_LOGIN    — логин для аутентификации в SMAX (например "webitel")
  SMAX_PASSWORD — пароль для аутентификации в SMAX
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

logger = logging.getLogger("smax_client")

SMAX_BASE_URL = "https://itsm-test3.kompanion.kg"
SMAX_AUTH_URL = f"{SMAX_BASE_URL}/auth/authentication-endpoint/authenticate/token?TENANTID=445816376"
SMAX_LOGIN = os.getenv("SMAX_LOGIN", "").strip()
SMAX_PASSWORD = os.getenv("SMAX_PASSWORD", "").strip()
SMAX_REFRESH_INTERVAL_SECONDS = 15 * 60

_smax_token: str = ""
_smax_token_updated_at: datetime | None = None

router = APIRouter()


def get_smax_token() -> str:
    """Текущий актуальный токен SMAX — вызывать перед любым запросом к SMAX API."""
    return _smax_token


async def refresh_smax_token() -> None:
    """Запрашивает новый токен у SMAX и кладёт в память модуля."""
    global _smax_token, _smax_token_updated_at

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            SMAX_AUTH_URL,
            headers={"Content-Type": "application/json"},
            json={"Login": SMAX_LOGIN, "password": SMAX_PASSWORD},
        )
        resp.raise_for_status()
        raw_text = resp.text

    # Формат ответа SMAX заранее не подтверждён — разбираем с запасом:
    # это может быть просто строка в кавычках ("AAAAA...") или JSON-объект
    # вида {"token": "..."} / {"Token": "..."}.
    token = None
    try:
        parsed = resp.json()
        if isinstance(parsed, str):
            token = parsed
        elif isinstance(parsed, dict):
            token = parsed.get("token") or parsed.get("Token") or parsed.get("access_token")
    except Exception:
        token = raw_text.strip().strip('"')

    if not token:
        logger.error("Не удалось распознать токен SMAX в ответе. Сырой ответ: %s", raw_text[:300])
        return

    _smax_token = token
    _smax_token_updated_at = datetime.now(timezone.utc)
    logger.info(
        "Токен SMAX обновлён. Длина=%s, начало='%s...'",
        len(_smax_token), _smax_token[:6],
    )


async def smax_token_refresh_loop() -> None:
    """Бесконечный фоновый цикл: обновляет токен SMAX каждые 15 минут."""
    while True:
        try:
            await refresh_smax_token()
        except Exception:
            logger.exception("Ошибка при обновлении токена SMAX")
        await asyncio.sleep(SMAX_REFRESH_INTERVAL_SECONDS)


@router.get("/smax-token-status")
async def smax_token_status():
    """Диагностика: проверить, что токен SMAX загружен (без раскрытия значения)."""
    return {
        "has_token": bool(_smax_token),
        "token_length": len(_smax_token),
        "updated_at": _smax_token_updated_at.isoformat() if _smax_token_updated_at else None,
    }
