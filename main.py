"""
Сервис для универсальной команды edna Chat Center.

СХЕМА ЗАПРОСА ОТ EDNA (подтверждена реальным вызовом):
  {
    "requestType": "POST",
    "messageId": "9bd3611a-...",     # эхом вернуть в ответе
    "threadId": "26872",
    "operatorLogin": "operator",     # логин агента, вызвавшего команду
    "clientId": "TG:6173617794:...", # эхом вернуть в ответе
    "commandCode": "request",        # то самое поле "Код" из админки edna
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

При получении команды сервис:
  1. Создаёт тикет в Jira (проект RLCC).
  2. Тянет теги треда из edna, находит среди них "иерархический" тег вида
     "Уровень1 / Уровень3 / Уровень4" (три части через " / "), переводит
     каждую часть через справочник в код SMAX (SPKFirstLevel_c и т.д.),
     собирает из этого JSON-тело в формате заявки SMAX и кладёт его как
     ТЕКСТ в поле description тикета Jira (реального похода в SMAX API
     пока нет — только сборка тела на будущее).
  3. Возвращает агенту ключ созданного тикета (например, RLCC-217).

Переменные окружения (задать в Render → Environment):
  JIRA_AUTH_TOKEN       — готовая base64-строка для Authorization: Basic <...>
                          (только сам токен, без слова "Basic")
  EDNA_TAGS_API_TOKEN   — Bearer-токен для GET /api/v1/chatbot/tags
  EDNA_THREAD_API_TOKEN — Bearer-токен для GET /api/v1/threads/{id}
                          (это ДРУГОЙ токен, не тот же, что для тегов!)
"""

import json
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("edna_test")

app = FastAPI()

# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# edna API (теги и детали треда)
# ---------------------------------------------------------------------------
EDNA_API_BASE = "https://kompanion.edna.kz/api/v1"


def _clean_bearer(raw: str) -> str:
    """Убирает случайно вставленное слово 'Bearer' из переменной окружения."""
    raw = raw.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


EDNA_TAGS_API_TOKEN = _clean_bearer(os.getenv("EDNA_TAGS_API_TOKEN", ""))
EDNA_THREAD_API_TOKEN = _clean_bearer(os.getenv("EDNA_THREAD_API_TOKEN", ""))

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


async def get_thread_tag_names(thread_id: str) -> list[str]:
    """Возвращает СПИСОК названий тегов треда (не строку)."""
    if not _tags_cache:
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
    return tag_names


# ---------------------------------------------------------------------------
# Разбор иерархического тега ("Уровень1 / Уровень3 / Уровень4") и сборка
# JSON-тела заявки SMAX (пока только как текст для description в Jira).
# ---------------------------------------------------------------------------

# Справочник значений — сверено со скриншотом. Дополняй по мере появления
# новых вариантов тегов.
TAG_LEVEL_MAPPING: dict[str, dict[str, str]] = {
    "first": {
        "Кредиты": "Loans_c",
        "Платежные карты": "PaymentCards_c",
    },
    "third": {
        "Онлайн кредит": "OnlineLoan_c",
        "Платежные карты": "VISAGoldCard_c",
    },
    "fourth": {
        "Безакцептное списание": "DirectDebit_c",
        'Выходит "Ошибка приложения"': "ApplicationError_c",
    },
}


def find_hierarchical_tag(tag_names: list[str]) -> str | None:
    """Находит среди тегов треда тот, что имеет формат 'A / B / C'."""
    for name in tag_names:
        if name.count("/") == 2:
            return name
    return None


def split_and_map_tag(tag: str) -> tuple[str, str, str]:
    """
    Разбивает тег вида "Кредиты / Онлайн кредит / Безакцептное списание"
    на 3 уровня (разделитель " / ", учитывая пробелы) и переводит каждый
    уровень через справочник в код SMAX. Если значения нет в справочнике —
    подставляется исходный текст как есть (чтобы не терять данные молча).
    """
    parts = [p.strip() for p in tag.split("/")]
    parts = (parts + ["", "", ""])[:3]  # подстраховка, если частей не 3
    level1_raw, level3_raw, level4_raw = parts

    first_code = TAG_LEVEL_MAPPING["first"].get(level1_raw, level1_raw)
    third_code = TAG_LEVEL_MAPPING["third"].get(level3_raw, level3_raw)
    fourth_code = TAG_LEVEL_MAPPING["fourth"].get(level4_raw, level4_raw)

    logger.info(
        "Разбор тега '%s' -> first='%s' third='%s' fourth='%s'",
        tag, first_code, third_code, fourth_code,
    )
    return first_code, third_code, fourth_code


def build_smax_request_body(thread_id: str, spk_first: str, spk_third: str, spk_fourth: str) -> dict:
    """
    Собирает JSON-тело заявки в формате SMAX (как на скриншоте).

    ВАЖНО: часть полей ниже — статичные заглушки (RequestedByPerson,
    RequestsOffering, CustomersNumber_c, CurrentClientID_c,
    ClientOperatingSystemVersion_c, ClientApplicationVersion_c) — источник
    реальных значений для них пока не определён. Когда появится, где их
    брать (из edna, из другого API, из настроек агента) — подставим
    динамически вместо этих значений по умолчанию.
    """
    return {
        "entities": [
            {
                "entity_type": "Request",
                "properties": {
                    "RequestedByPerson": "12458",
                    "RequestedForPerson": "12458",
                    "RequestsOffering": "497669",
                    "CustomersNumber_c": "996228021941",
                    "CurrentClientID_c": "Самат Эшимбеков - 124595",
                    "SPKFirstLevel_c": spk_first,
                    "SPKThirdLevel_c": spk_third,
                    "SPKFourthLevel_c": spk_fourth,
                    "ClientOperatingSystemVersion_c": "Android - 12",
                    "ClientApplicationVersion_c": "1.5",
                    "Description": f"Вы можете найти чат и описание жалобы по ID: {thread_id}",
                    "Urgency": "SlightDisruption",
                    "RequestType": "SupportRequest",
                },
                "layout": "Id,RequestedByPerson,Status,DisplayLabel",
            }
        ],
        "operation": "CREATE",
    }


def build_jira_description(thread_id: str, tag_names: list[str]) -> str:
    """
    Если среди тегов треда есть иерархический (A / B / C) — собирает JSON
    заявки SMAX и возвращает его как текст для description в Jira.
    Если такого тега нет — просто ссылка на тред, как раньше.
    """
    hierarchical_tag = find_hierarchical_tag(tag_names)
    if not hierarchical_tag:
        return f"Вы можете найти чат и описание жалобы по ID: {thread_id}"

    spk_first, spk_third, spk_fourth = split_and_map_tag(hierarchical_tag)
    smax_body = build_smax_request_body(thread_id, spk_first, spk_third, spk_fourth)
    return json.dumps(smax_body, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Создание тикета в Jira
# ---------------------------------------------------------------------------
async def create_jira_issue(thread_id: str, operator_login: str, tag_names: list[str]) -> dict:
    """Создаёт тикет в Jira и возвращает распарсенный JSON-ответ."""
    description = build_jira_description(thread_id, tag_names)
    tag_names_joined = ", ".join(tag_names)

    payload = {
        "fields": {
            "project": {"key": "RLCC"},
            "summary": "Жалоба от Edna",
            "issuetype": {"name": "Task"},
            "description": description,
            "customfield_13045": operator_login,
            "customfield_13050": str(thread_id),
            "customfield_13049": tag_names_joined,
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


# ---------------------------------------------------------------------------
# Роуты
# ---------------------------------------------------------------------------
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
        tag_names = []

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
