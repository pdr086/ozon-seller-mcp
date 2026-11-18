"""Searchable API schemas. Documentation never grants execution permissions."""

import gzip
import json
import re
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft4Validator, RefResolver

from .read_catalog import OPERATIONS

REFERENCE = Path(__file__).with_name("reference")
PATH = re.compile(r"^/v[1-9][0-9]*/[a-z0-9_-]+(?:/[a-z0-9_-]+)*$")
MUTATIONS = frozenset({
    "update", "set", "delete", "cancel", "activate", "deactivate", "approve",
    "reject", "decline", "send", "archive", "unarchive", "import", "reset",
    "start", "create-or-update", "create-or-get", "verify", "compensate",
    "return-money", "receive-return", "ship", "delivered", "delivering",
    "last-mile", "split", "read", "bind", "unbind", "upload", "enable",
    "disable", "change-status", "change-activity", "registrate", "edit",
})
THEMES = {
    "product": "товар карточка название цена характеристики бренд описание артикул штрихкод SKU",
    "stock": "остатки наличие запасы склад",
    "posting": "отправления заказы доставка продажи",
    "finance": "финансы баланс выплаты начисления удержания комиссии реализация прибыль",
    "report": "отчёты отчеты документы выгрузка",
    "analytic": "аналитика статистика продажи спрос запросы оборачиваемость",
    "review": "отзывы оценки покупателей",
    "question": "вопросы ответы покупателей",
    "chat": "чаты переписка сообщения покупателей",
    "supply": "поставки заявки грузы",
    "warehouse": "склады склад логистика",
    "action": "акции скидки промокоды",
    "rating": "рейтинг показатели качество",
    "return": "возвраты отмены причины",
    "certificate": "сертификаты документы соответствие",
    "category": "категории справочник характеристики",
    "pricing": "цены стратегии конкуренты",
    "fbp": "партнёрские партнерские поставки склады",
    "seller": "продавец магазин компания реквизиты",
}
READ_ARTIFACT_TASKS = frozenset({
    "/v1/cargoes-label/create",
    "/v1/cargoes/label/transport-by-order/create",
    "/v3/posting/fbs/package-label/create",
})


def safe_read_path(path):
    if not isinstance(path, str) or not PATH.fullmatch(path):
        return False
    last = path.rsplit("/", 1)[-1]
    if last in MUTATIONS or last.endswith("-reset"):
        return False
    # Report jobs produce data and are explicitly granted by Ozon's read-only role.
    if last == "create" and "/report/" not in path and path not in READ_ARTIFACT_TASKS:
        return False
    return True


def readonly_methods(roles):
    result = set()
    if not isinstance(roles, list):
        raise ValueError("Unexpected Ozon roles response")
    for role in roles:
        if not isinstance(role, dict):
            raise ValueError("Unexpected Ozon role")
        name = role.get("name", "")
        if not isinstance(name, str) or not re.search(r"\bread[\s_-]*only\b", name, re.I):
            continue
        methods = role.get("methods")
        if not isinstance(methods, list) or not all(isinstance(path, str) for path in methods):
            raise ValueError("Unexpected Ozon role methods")
        result.update(path for path in methods if safe_read_path(path))
    return frozenset(result)


@lru_cache(maxsize=1)
def reference():
    with gzip.open(REFERENCE / "seller-api.json.gz", "rt", encoding="utf-8") as stream:
        return json.load(stream)


@lru_cache(maxsize=1)
def provenance():
    return json.loads((REFERENCE / "provenance.json").read_text(encoding="utf-8"))


def operation_doc(path):
    methods = reference()["paths"].get(path, {})
    return methods.get("post") or methods.get("get") or {}


def summary(path):
    reviewed = OPERATIONS.get(path)
    doc = operation_doc(path)
    return {
        "operation": path,
        "title": reviewed.title if reviewed else doc.get("summary", path),
        "http_method": "GET" if "get" in reference()["paths"].get(path, {}) else "POST",
        "schema_available": bool(doc.get("requestBody")) or reviewed is not None,
        "deprecated_in_snapshot": bool(doc.get("deprecated")),
    }


def search_catalog(paths, query):
    terms = set(re.findall(r"\w+", query.casefold().replace("ё", "е")))
    matches = []
    for path in paths:
        record = summary(path)
        related = " ".join(words for stem, words in THEMES.items() if stem in path)
        reviewed = OPERATIONS.get(path)
        text = f"{path} {record['title']} {related} {reviewed.keywords if reviewed else ''}"
        words = set(re.findall(r"\w+", text.casefold().replace("ё", "е")))
        score = sum(any(word.startswith(term) or (len(word) >= 4 and term.startswith(word)) for word in words) for term in terms)
        if not terms or score:
            matches.append((score, record))
    matches.sort(key=lambda entry: (-entry[0], entry[1]["operation"]))
    return {
        "ok": True, "available_operations": len(paths),
        "permission_source": "live_ozon_read_only_roles_and_reviewed_operations",
        "documentation": provenance(),
        "operations": [record for _, record in matches],
    }


def request_schema(path):
    doc = operation_doc(path)
    return doc.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")


def describe(path):
    reviewed = OPERATIONS.get(path)
    schema = reviewed.request.model_json_schema() if reviewed else request_schema(path)
    if schema is None:
        return {
            "ok": True, **summary(path), "documentation": provenance(),
            "input_schema": {"type": "object"}, "schema_verified": False,
            "message": "Ozon разрешает чтение этого метода, но его схема отсутствует в снимке справочника. Используйте актуальную документацию метода. Не выдумывайте параметры.",
        }
    # Include only local references reachable from this request, preserving cycles.
    components = {}
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            ref = value.get("$ref", "")
            prefix = "#/components/schemas/"
            if ref.startswith(prefix):
                name = ref[len(prefix):]
                if name not in components:
                    components[name] = reference()["components"]["schemas"][name]
                    pending.append(components[name])
            pending.extend(item for key, item in value.items() if key != "$ref")
    result = dict(schema)
    if components:
        result["components"] = {"schemas": components}
    return {"ok": True, **summary(path), "documentation": provenance(), "input_schema": result, "schema_verified": reviewed is not None}


def validate_snapshot(path, arguments):
    schema = request_schema(path)
    if schema is None:
        return []
    def block_external_reference(_uri):
        raise ValueError("External schema references are disabled")
    resolver = RefResolver.from_schema(reference(), handlers={"http": block_external_reference, "https": block_external_reference})
    # OAS 3.0 nullable fields are optional in most request schemas. Null values
    # remain the caller's responsibility and Ozon validates the final request.
    errors = Draft4Validator(schema, resolver=resolver).iter_errors(arguments)
    return [".".join(map(str, error.absolute_path)) or "$" for _, error in zip(range(8), errors)]
