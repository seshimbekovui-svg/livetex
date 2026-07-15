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
  JIRA_AUTH_TOKEN — готовая base64-строка для заголовка Authorization: Basic <...>
                    (у тебя уже есть, например "c2FtYXQuZXNoaW...")
"""

import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("edna_test")

app = FastAPI()

JIRA_URL = "https://bankkompanion.atlassian.net/rest/api/2/issue"
JIRA_AUTH_TOKEN = os.getenv("JIRA_AUTH_TOKEN", "")


async def create_jira_issue(thread_id: str, operator_login: str) -> dict:
    """Создаёт тикет в Jira и возвращает распарсенный JSON-ответ."""
    payload = {
        "fields": {
            "project": {"key": "RLCC"},
            "summary": "Жалоба от Edna",
            "issuetype": {"name": "Task"},
            "description": f"Вы можете найти чат и описание жалобы по ID: {thread_id}",
            "customfield_13045": operator_login,
            "customfield_13050": str(thread_id),
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

    try:
        issue = await create_jira_issue(thread_id, operator_login)
        issue_key = issue.get("key")
        logger.info("Тикет создан: %s (%s)", issue_key, issue.get("self"))
        result_value = f"Создан тикет {issue_key}"
    except httpx.HTTPStatusError as exc:
        logger.exception("Jira вернула ошибку")
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
