"""
Тестовый сервис для универсальной команды edna Chat Center.

СХЕМА ЗАПРОСА ОТ EDNA (подтверждена реальным вызовом):
  {
    "requestType": "POST",
    "messageId": "9bd3611a-...",     # эхом вернуть в ответе
    "threadId": "26872",
    "operatorLogin": "operator",     # логин агента, вызвавшего команду
    "clientId": "TG:6173617794:...", # эхом вернуть в ответе
    "commandCode": "request",        # то самое поле "Код" из админки
    "params": [],                    # аргументы, введённые агентом после команды
    "host": "https://..."            # служебное, не используем
  }

СХЕМА ОТВЕТА (подтверждена примером кода от поддержки edna):
  {
    "clientId": "<эхо из запроса>",
    "messageId": "<эхо из запроса>",
    "params": [
      {"key": "<параметр>", "value": "<значение>"}
    ]
  }
Content-Type: application/json, статус 200.

При получении команды сервис создаёт тикет в Jira (проект RLCC) и
возвращает агенту ключ созданного тикета (например, RLCC-217).

Переменные окружения (задать в Render → Environment):
  JIRA_AUTH_TOKEN      — готовая base64-строка для заголовка Authorization: Basic <...>
                         (у тебя уже есть, например "c2FtYXQuZXNoaW...")
  EDNA_TAGS_API_TOKEN  — Bearer-токен для GET /api/v1/chatbot/tags
  EDNA_THREAD_API_TOKEN — Bearer-токен для GET /api/v1/threads/{id}
                         (это ДРУГОЙ токен, не тот же, что для тегов!)

Аутентификация и токен SMAX вынесены в отдельный модуль smax_client.py —
см. его докстринг и переменные окружения SMAX_LOGIN/SMAX_PASSWORD там.

Логика тегов:
  1. При старте сервиса и лениво при первом запросе (если кэш пуст) —
     подтягиваем полный список тегов /api/v1/chatbot/tags и кэшируем
     в памяти как {id: name}. Список тегов меняется редко, поэтому кэш
     без TTL — если понадобится принудительное обновление, перезапусти
     сервис на Render (Manual Deploy → Restart).
  2. На каждый вызов команды — запрашиваем /api/v1/threads/{threadId},
     берём оттуда список id тегов треда, переводим через кэш в названия
     и пишем в Jira в customfield_13049 (через запятую).
"""

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import smax_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("edna_test")

app = FastAPI()

JIRA_URL = "https://bankkompanion.atlassian.net/rest/api/2/issue"
JIRA_AUTH_TOKEN = os.getenv("JIRA_AUTH_TOKEN", "").strip()
# На случай, если в переменной окружения токен вставлен вместе со словом
# "Basic " — не дублируем его при формировании заголовка.
if JIRA_AUTH_TOKEN.lower().startswith("basic "):
    JIRA_AUTH_TOKEN = JIRA_AUTH_TOKEN[6:].strip()
logger.info(
    "JIRA_AUTH_TOKEN загружен: длина=%s, начало='%s...', конец='...%s'",
    len(JIRA_AUTH_TOKEN),
    JIRA_AUTH_TOKEN[:6],
    JIRA_AUTH_TOKEN[-4:] if len(JIRA_AUTH_TOKEN) >= 4 else JIRA_AUTH_TOKEN,
)

EDNA_API_BASE = "https://kompanion.edna.kz/api/v1"


def _clean_bearer(raw: str) -> str:
    """Убирает случайно вставленное слово 'Bearer' из переменной окружения."""
    raw = raw.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


EDNA_TAGS_API_TOKEN = _clean_bearer(os.getenv("EDNA_TAGS_API_TOKEN", ""))
EDNA_THREAD_API_TOKEN = _clean_bearer(os.getenv("EDNA_THREAD_API_TOKEN", ""))

app.include_router(smax_client.router)

# Кэш тегов в памяти: {"763": "2-линия", ...}
_tags_cache: dict[str, str] = {}


async def load_tags_cache() -> None:
    """Подтягивает полный список тегов edna и кладёт в кэш."""
    global _tags_cache
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{EDNA_API_BASE}/chatbot/tags",
            headers={"Authorization": f"Bearer {EDNA_TAGS_API_TOKEN}"},
        )
        resp.raise_for_status()
        tags = resp.json()
    _tags_cache = {str(t["id"]): t["name"] for t in tags}
    logger.info("Кэш тегов edna обновлён: %d тегов", len(_tags_cache))


@app.on_event("startup")
async def on_startup():
    try:
        await load_tags_cache()
    except Exception:
        logger.exception("Не удалось загрузить кэш тегов edna при старте")

    # Фоновый цикл сам сделает первый refresh сразу при запуске,
    # а затем будет повторять его каждые 15 минут, не блокируя сервер.
    asyncio.create_task(smax_client.smax_token_refresh_loop())


async def get_thread_tag_names(thread_id: str) -> str:
    """Возвращает названия тегов треда через запятую (или пустую строку)."""
    if not _tags_cache:
        # Кэш почему-то пуст (например, старт не удался) — пробуем ещё раз.
        try:
            await load_tags_cache()
        except Exception:
            logger.exception("Повторная загрузка кэша тегов не удалась")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{EDNA_API_BASE}/threads/{thread_id}",
            headers={"Authorization": f"Bearer {EDNA_THREAD_API_TOKEN}"},
        )
        resp.raise_for_status()
        thread = resp.json()

    tag_ids = thread.get("tags", [])
    tag_names = [_tags_cache.get(str(tid), f"Тег {tid}") for tid in tag_ids]
    logger.info("Теги треда %s: id=%s -> names=%s", thread_id, tag_ids, tag_names)
    return ", ".join(tag_names)


async def create_jira_issue(thread_id: str, operator_login: str, tag_names: str) -> dict:
    """Создаёт тикет в Jira и возвращает распарсенный JSON-ответ."""
    payload = {
        "fields": {
            "project": {"key": "RLCC"},
            "summary": "Жалоба от Edna",
            "issuetype": {"name": "Task"},
            "description": f"Вы можете найти чат и описание жалобы по ID: {thread_id}",
            "customfield_13045": operator_login,
            "customfield_13050": str(thread_id),
            "customfield_13049": tag_names,
        }
    }
    headers = {
        "Authorization": f"Basic {JIRA_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(JIRA_URL, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# Catch-all должен быть объявлен ПОСЛЕДНИМ — FastAPI матчит роуты по порядку,
# иначе этот обработчик перехватит и /healthz, и вообще всё подряд.
@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def catch_all(full_path: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info("=== Новый запрос от edna ===")
    logger.info("Body: %s", body)

    message_id = body.get("messageId")
    client_id = body.get("clientId")
    thread_id = body.get("threadId")
    operator_login = body.get("operatorLogin")
    command_code = body.get("commandCode")
    params = body.get("params", [])

    logger.info(
        "commandCode=%s threadId=%s operatorLogin=%s params=%s",
        command_code, thread_id, operator_login, params,
    )

    # Защита от служебных/пустых запросов (например, health-check пинги от
    # Render на "/"). Без threadId и commandCode это точно не вызов
    # команды агентом — тикет в Jira создавать не нужно.
    if not thread_id or not command_code:
        logger.info("Пропускаем: это не похоже на реальный вызов команды edna")
        return JSONResponse(content={"status": "ignored"}, status_code=200)

    try:
        tag_names = await get_thread_tag_names(thread_id)
    except Exception:
        logger.exception("Не удалось получить теги треда %s", thread_id)
        tag_names = ""

    try:
        issue = await create_jira_issue(thread_id, operator_login, tag_names)
        issue_key = issue.get("key")
        logger.info("Тикет создан: %s (%s)", issue_key, issue.get("self"))
        result_value = f"Создан тикет {issue_key}"
    except httpx.HTTPStatusError as exc:
        logger.error("Jira вернула ошибку %s: %s", exc.response.status_code, exc.response.text)
        result_value = f"Ошибка Jira: {exc.response.status_code} — {exc.response.text[:200]}"
    except Exception as exc:
        logger.exception("Не удалось создать тикет в Jira")
        result_value = f"Ошибка при создании тикета: {exc}"

    data = {
        "clientId": client_id,
        "messageId": message_id,
        "params": [
            {"key": "Результат", "value": result_value}
        ],
    }

    return JSONResponse(content=data, status_code=200, media_type="application/json")
