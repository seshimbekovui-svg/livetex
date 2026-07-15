"""
Тестовый сервис для универсальной команды edna Chat Center.

Пока НИЧЕГО не парсит и никуда не ходит — просто:
  1. Логирует всё, что прислала edna (метод, заголовки, тело).
  2. Отвечает одним и тем же статичным JSON с кодом 200.

Это нужно, чтобы:
  - убедиться, что edna вообще достучалась до сервиса;
  - увидеть в логах Render реальный формат запроса от edna;
  - проверить, что агент в АРМ увидел ответ, и в каком именно поле
    edna ожидает текст (мы вернём сразу несколько вариантов полей —
    text/message/result — чтобы понять по логам и по факту отображения,
    какое из них edna реально использует).
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("edna_test")

app = FastAPI()

STATIC_RESPONSE = {
    "text": "Статус: успешно изменено\nЗначение: Обработано оператором СПК",
    "message": "Статус: успешно изменено\nЗначение: Обработано оператором СПК",
    "result": "Статус: успешно изменено\nЗначение: Обработано оператором СПК",
}


@app.post("/command/request")
async def command_request(request: Request):
    headers = dict(request.headers)
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")

    logger.info("=== Новый запрос от edna ===")
    logger.info("Headers: %s", headers)
    logger.info("Body: %s", body)

    return JSONResponse(content=STATIC_RESPONSE, status_code=200)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
