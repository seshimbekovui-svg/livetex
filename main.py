"""
Тестовый сервис для универсальной команды edna Chat Center.

СХЕМА ЗАПРОСА ОТ EDNA (подтверждена реальным вызовом):
  {
    "requestType": "POST",
    "messageId": "9bd3611a-...",     # эхом вернуть в ответе
    "threadId": "26872",
    "operatorLogin": "operator",     # логин агента, вызвавшего команду
    "clientId": "TG:6173617794:...", # эхом вернуть в ответе
    "commandCode": "request",        # то самое поле "Код" из админки —
                                      # используется для роутинга, если
                                      # несколько команд ведут на один URL
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
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("edna_test")

app = FastAPI()


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

    # Роутинг по commandCode — так один URL сможет обслуживать
    # несколько разных команд, настроенных в админке edna.
    if command_code == "request":
        result_value = "Тестовый ответ от сервиса (команда 'request' получена)"
    else:
        result_value = f"Неизвестный commandCode: {command_code}"

    data = {
        "clientId": client_id,
        "messageId": message_id,
        "params": [
            {"key": "Результат", "value": result_value}
        ],
    }

    return JSONResponse(content=data, status_code=200, media_type="application/json")
