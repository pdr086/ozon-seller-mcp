import json
from hashlib import sha256
from threading import Lock
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from .read_catalog import OPERATIONS
from . import api_knowledge

API_URL = "https://api-seller.ozon.ru"
ROLES = "/v1/roles"
ANALYTICS = "/v1/analytics/data"
STOCKS = "/v4/product/info/stocks"
RETURNS = "/v1/returns/list"
PRICES = "/v5/product/info/prices"
READ_METHODS = frozenset({ROLES, ANALYTICS, STOCKS, RETURNS, PRICES, *OPERATIONS})
PAGE_SIZE = 100
MAX_PAGES = 30
READ_BUDGET = 30
ROLE_CACHE = {}
ROLE_LOCK = Lock()

HTTP_PROBLEMS = {
    400: ("request_rejected", "Ozon отклонил параметры запроса."),
    401: ("invalid_credentials", "Проверьте Client ID и срок действия API-ключа Ozon."),
    403: ("access_denied", "Проверьте права ключа и доступность метода на тарифе Ozon."),
    429: ("rate_limited", "Ozon ограничил частоту запросов. Повторите позже."),
}


class SellerError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.payload = {"ok": False, "error": code, "message": message, **details}


def bad_response():
    return SellerError("unexpected_response", "Ozon вернул ответ неожиданного формата.")


def array(obj, field):
    if isinstance(obj, dict) and isinstance(obj.get(field), list):
        return obj[field]
    raise bad_response()


def mapping(obj, field):
    if isinstance(obj, dict) and isinstance(obj.get(field), dict):
        return obj[field]
    raise bad_response()


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise bad_response() from None
    if not result.is_finite():
        raise bad_response()
    return result


def quantity(value):
    number = amount(value)
    if number < 0 or number != number.to_integral_value():
        raise bad_response()
    return int(number)


def money(value):
    return format(amount(value), "f")


def period(start_date, end_date, zone):
    end = datetime.now(zone).date() - timedelta(days=1)
    try:
        if end_date is not None:
            end = date.fromisoformat(end_date)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=6)
    except (ValueError, TypeError):
        raise SellerError("invalid_period", "Используйте даты в формате ГГГГ-ММ-ДД.") from None
    if start > end or end - start > timedelta(days=30) or end > datetime.now(zone).date():
        raise SellerError("invalid_period", "Укажите прошедший период до 31 дня включительно.")
    return start, end


def dates(start, end, zone):
    return {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": zone.key}


def api_timestamp(day, at, zone):
    return datetime.combine(day, at, zone).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def select_fields(row, fields):
    if not isinstance(row, dict):
        raise bad_response()
    return {key: row[key] for key in fields if key in row}


def project_response(projection, response):
    if projection == "full":
        return response
    projected = select_fields(response, ("cursor", "has_next", "last_id"))
    if projection == "warehouses":
        fields = ("warehouse_id", "name", "warehouse_type", "status", "is_rfbs", "is_kgt", "working_days", "postings_limit")
        projected["warehouses"] = [select_fields(row, fields) for row in array(response, "warehouses")]
    elif projection == "postings":
        fields = ("posting_number", "order_id", "status", "substatus", "created_at", "shipment_date", "in_process_at")
        products = ("offer_id", "sku", "name", "quantity", "price")
        projected["postings"] = [
            {**select_fields(row, fields), "products": [select_fields(item, products) for item in array(row, "products")]}
            for row in array(response, "postings")
        ]
    elif projection == "accruals":
        fields = ("accrual_id", "date", "accrued_category")
        projected["accruals"] = [
            {**select_fields(row, fields), "total_amount": select_fields(mapping(row, "total_amount"), ("amount", "currency"))}
            for row in array(response, "accruals")
        ]
    else:
        raise ValueError("Unknown Ozon response projection")
    return projected


def redact_credentials(value, api_key):
    if isinstance(value, dict):
        sensitive = {"api_key", "api-key", "authorization", "access_token", "refresh_token", "client_secret"}
        return {key: redact_credentials(item, api_key) for key, item in value.items() if key.casefold() not in sensitive}
    if isinstance(value, list):
        return [redact_credentials(item, api_key) for item in value]
    if isinstance(value, str):
        return value.replace(api_key, "[скрыто]")
    return value


class Seller:
    def __init__(self, credentials, zone_name="Europe/Moscow", transport=None, report_dir=None):
        self.report_dir = report_dir
        self.zone = ZoneInfo(zone_name)
        self.permission_cache_key = sha256((credentials["client_id"] + "\0" + credentials["api_key"]).encode()).hexdigest()
        self.api_key = credentials["api_key"]
        self.http = httpx.Client(
            base_url=API_URL,
            headers={"Client-Id": credentials["client_id"], "Api-Key": credentials["api_key"]},
            transport=transport,
            timeout=httpx.Timeout(12, connect=5),
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.http.close()

    def post(self, method, payload):
        if method not in READ_METHODS and method not in self.read_methods():
            raise ValueError("Unsupported Ozon read method")
        verb = api_knowledge.summary(method)["http_method"]
        kwargs = {"params": payload} if verb == "GET" else {"json": payload}
        try:
            with self.http.stream(verb, method, **kwargs) as response:
                status = response.status_code
                if status != 200:
                    code, message = HTTP_PROBLEMS.get(status, ("ozon_unavailable", "Seller API временно недоступен."))
                    details = {"http_status": status}
                    if status == 400:
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            content.extend(chunk)
                            if len(content) > 32768:
                                break
                        try:
                            hint = json.loads(content).get("message")
                            if isinstance(hint, str):
                                details["provider_message"] = hint.replace(self.api_key, "[скрыто]")[:1500]
                        except (ValueError, AttributeError):
                            pass
                    raise SellerError(code, message, **details)
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 8 * 1024 * 1024:
                        raise bad_response()
        except httpx.HTTPError:
            raise SellerError("ozon_unavailable", "Не удалось связаться с Seller API Ozon.") from None
        try:
            body = json.loads(chunks, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise bad_response() from None
        if not isinstance(body, dict):
            raise bad_response()
        return body

    def read_methods(self):
        now = monotonic()
        with ROLE_LOCK:
            saved = ROLE_CACHE.get(self.permission_cache_key)
        if saved is not None and saved[0] > now:
            return saved[1]
        roles = self.post(ROLES, {}).get("roles")
        try:
            methods = api_knowledge.readonly_methods(roles)
        except ValueError:
            raise bad_response() from None
        with ROLE_LOCK:
            for key, entry in list(ROLE_CACHE.items()):
                if entry[0] <= now:
                    ROLE_CACHE.pop(key)
            ROLE_CACHE[self.permission_cache_key] = (now + 300, methods)
        return methods

    def find_operations(self, query):
        from . import store_reports
        result = api_knowledge.search_catalog(self.read_methods() | frozenset(OPERATIONS), query)
        reports = store_reports.search(query)
        result["operations"] = reports + result["operations"]
        result["available_store_reports"] = len(store_reports.REPORTS)
        return result

    def describe_operation(self, operation):
        from . import store_reports
        if operation in store_reports.REPORTS:
            return store_reports.describe(operation)
        if operation not in OPERATIONS and operation not in self.read_methods():
            raise SellerError("operation_not_allowed", "Эта операция не предоставлена Ozon в ролях только для чтения.")
        return api_knowledge.describe(operation)

    def connection_status(self):
        body = self.post(ROLES, {})
        expiry = body.get("expires_at")
        if expiry:
            try:
                expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")).isoformat()
            except (ValueError, AttributeError):
                expiry = None
        return {"ok": True, "connected": True, "key_expires_at": expiry, "read_only": True}

    def find_products(self, query, limit):
        def normalized(value):
            return " ".join(str(value).casefold().replace("ё", "е").split())

        terms = normalized(query).split()
        if not terms:
            raise SellerError("invalid_query", "Укажите название или идентификатор товара.")
        matched = []
        seen = set()
        cursor = ""
        examined = 0
        complete = False
        deadline = monotonic() + 23
        for _ in range(MAX_PAGES):
            if monotonic() >= deadline:
                break
            page = self.post("/v3/product/list", {
                "filter": {"visibility": "ALL"}, "limit": PAGE_SIZE, "last_id": cursor,
            })
            result = mapping(page, "result")
            items = array(result, "items")
            if not items:
                complete = True
                break
            if monotonic() >= deadline:
                break
            ids = [str(item["product_id"]) for item in items]
            details = self.post("/v3/product/info/list", {"product_id": ids})
            rows = array(details, "items")
            if len(rows) != len(ids):
                raise bad_response()
            for row in rows:
                fields = ("id", "offer_id", "sku", "name", "barcodes", "price", "currency_code", "is_archived")
                item = {key: row[key] for key in fields if key in row}
                search_fields = [item.get(key, "") for key in ("id", "offer_id", "sku", "name")]
                search_fields.extend(item.get("barcodes") or [])
                haystack = normalized(" ".join(map(str, search_fields)))
                if all(term in haystack for term in terms):
                    matched.append(item)
            examined += len(rows)
            count = result.get("total_items", result.get("total"))
            cursor = result.get("last_id")
            if not cursor or (type(count) is int and examined >= count):
                complete = True
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise bad_response()
            seen.add(cursor)

        def rank(item):
            exact_id = any(normalized(item.get(key, "")) == normalized(query) for key in ("id", "sku", "offer_id"))
            name = normalized(item.get("name", ""))
            return (not exact_id, name != normalized(query), bool(item.get("is_archived")), name)

        matched.sort(key=rank)
        return {"ok": True, "complete": complete, "examined": examined, "matched": len(matched), "items": matched[:limit]}

    def read_operation(self, operation, arguments):
        from . import store_reports
        if operation in store_reports.REPORTS:
            result = store_reports.run(self, self.report_dir, operation, arguments)
            if len(json.dumps(result, ensure_ascii=False).encode()) > 96 * 1024:
                raise SellerError("response_too_large", "Отчёт превышает лимит ответа.")
            return result
        entry = OPERATIONS.get(operation)
        if entry is None and operation not in self.read_methods():
            raise SellerError("operation_not_allowed", "Выберите доступную операцию чтения через find_ozon_operations.")
        if not isinstance(arguments, dict):
            raise SellerError("invalid_arguments", "Параметры должны быть объектом JSON.")
        if entry is not None:
            try:
                request = entry.request.model_validate(arguments)
            except ValidationError as error:
                fields = [".".join(map(str, issue["loc"])) for issue in error.errors()[:8]]
                raise SellerError("invalid_arguments", "Сверьте параметры со схемой describe_ozon_operation.", fields=fields) from None
            arguments = request.model_dump(mode="json", exclude_none=True)
        else:
            fields = api_knowledge.validate_snapshot(operation, arguments)
            if fields:
                raise SellerError("invalid_arguments", "Сверьте параметры со схемой describe_ozon_operation.", fields=fields)
        response = self.post(operation, arguments)
        if entry is not None:
            response = project_response(entry.response, response)
        encoded = json.dumps(redact_credentials(response, self.api_key), ensure_ascii=False, default=str)
        if len(encoded.encode()) > 96 * 1024:
            raise SellerError("response_too_large", "Сузьте фильтр или запросите меньше записей.", operation=operation)
        return {"ok": True, "operation": operation, "data": json.loads(encoded)}

    def sales_summary(self, start_date=None, end_date=None):
        start, end = period(start_date, end_date, self.zone)
        response = self.post(ANALYTICS, {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "dimension": ["day"],
            "metrics": ["ordered_units", "revenue"],
            "limit": 100,
            "offset": 0,
        })
        result = mapping(response, "result")
        rows = []
        for row in array(result, "data"):
            dimensions = array(row, "dimensions")
            metrics = array(row, "metrics")
            if len(dimensions) != 1 or len(metrics) != 2 or not isinstance(dimensions[0], dict):
                raise bad_response()
            try:
                day = date.fromisoformat(dimensions[0]["id"])
            except (ValueError, KeyError, TypeError):
                raise bad_response() from None
            if start <= day <= end:
                rows.append({"date": day.isoformat(), "ordered_units": quantity(metrics[0]), "ordered_amount": money(metrics[1])})
        rows.sort(key=lambda row: row["date"])
        totals = array(result, "totals")
        if len(totals) == 2:
            units, value = quantity(totals[0]), money(totals[1])
        elif not totals and not rows:
            units, value = 0, "0"
        else:
            raise bad_response()
        return {
            "ok": True, "period": dates(start, end, self.zone),
            "basis": "ozon_orders", "ordered_units": units, "ordered_amount": value,
            "days_with_rows": len(rows), "days": rows,
        }

    def paged(self, method, payload, list_field, page_key):
        """Read bounded pages. Mark an incomplete snapshot if the budget runs out."""
        rows = []
        seen = set()
        token = None
        deadline = monotonic() + READ_BUDGET
        for _ in range(MAX_PAGES):
            request = {**payload, "limit": PAGE_SIZE}
            if token is not None:
                request[page_key] = token
            body = self.post(method, request)
            page = array(body, list_field)
            if not all(isinstance(row, dict) for row in page):
                raise bad_response()
            rows.extend(page)
            if page_key == "cursor":
                token = body.get("cursor")
                if not page or token in (None, ""):
                    return rows, True
                if not isinstance(token, str):
                    raise bad_response()
            else:
                has_next = body.get("has_next")
                if not isinstance(has_next, bool):
                    raise bad_response()
                if not has_next:
                    return rows, True
                if not page or type(page[-1].get("id")) is not int:
                    raise bad_response()
                token = page[-1]["id"]
            if token in seen:
                raise bad_response()
            seen.add(token)
            if monotonic() >= deadline:
                break
        return rows, False

    def low_stock(self, threshold, scheme, limit):
        stocks, complete = self.paged(STOCKS, {"filter": {"visibility": "ALL"}}, "items", "cursor")
        matched = []
        evaluated = unknown = 0
        selected = {"fbo", "fbs"} if scheme == "both" else {scheme.lower()}
        dedup = set()
        for item in stocks:
            product_id = item.get("product_id")
            if type(product_id) is not int or not isinstance(item.get("offer_id"), str) or product_id in dedup:
                raise bad_response()
            dedup.add(product_id)
            quantities = defaultdict(lambda: {"present": 0, "reserved": 0, "available": 0})
            for stock in array(item, "stocks"):
                if not isinstance(stock, dict) or not isinstance(stock.get("type"), str):
                    raise bad_response()
                kind = stock["type"].lower()
                if kind not in selected:
                    continue
                present = quantity(stock.get("present"))
                reserved = quantity(stock.get("reserved"))
                quantities[kind]["present"] += present
                quantities[kind]["reserved"] += reserved
                quantities[kind]["available"] += max(present - reserved, 0)
            if not quantities:
                unknown += 1
                continue
            evaluated += 1
            available = sum(row["available"] for row in quantities.values())
            if available <= threshold:
                matched.append({
                    "offer_id": item["offer_id"], "available": available,
                    "stock_by_scheme": dict(quantities),
                })
        matched.sort(key=lambda item: (item["available"], item["offer_id"]))
        return {
            "ok": True, "complete": complete, "threshold": threshold, "scheme": scheme,
            "evaluated_items": evaluated, "items_without_selected_stock": unknown,
            "low_stock_count": len(matched), "items": matched[:limit],
        }

    def returns_summary(self, start_date, end_date, limit):
        start, end = period(start_date, end_date, self.zone)
        bounds = {
            "time_from": api_timestamp(start, time.min, self.zone),
            "time_to": api_timestamp(end, time.max, self.zone),
        }
        rows, complete = self.paged(RETURNS, {"filter": {"logistic_return_date": bounds}}, "returns", "last_id")
        counts = Counter()
        units = Counter()
        reasons = Counter()
        offers = Counter()
        seen = set()
        for row in rows:
            identifier = row.get("id")
            if type(identifier) is not int:
                raise bad_response()
            if identifier in seen:
                continue
            seen.add(identifier)
            kind = row.get("type") or "unknown"
            reason = row.get("return_reason_name") or "Причина не указана"
            if not isinstance(kind, str) or not isinstance(reason, str):
                raise bad_response()
            product = mapping(row, "product")
            count = quantity(product.get("quantity"))
            counts[kind] += 1
            units[kind] += count
            reasons[(kind, reason[:160])] += count
            offer = product.get("offer_id")
            if isinstance(offer, str) and offer:
                offers[(kind, offer)] += count
        return {
            "ok": True, "complete": complete, "period": dates(start, end, self.zone),
            "date_basis": "logistic_return_date", "records": len(seen),
            "by_type": [{"type": k, "records": n, "units": units[k]} for k, n in counts.most_common()],
            "top_reasons": [{"type": k, "reason": r, "units": n} for (k, r), n in reasons.most_common(limit)],
            "top_offers": [{"type": k, "offer_id": o, "units": n} for (k, o), n in offers.most_common(limit)],
        }

    def product_price(self, offer_id):
        body = self.post(PRICES, {"filter": {"offer_id": [offer_id], "visibility": "ALL"}, "limit": 100})
        for item in array(body, "items"):
            if not isinstance(item, dict):
                raise bad_response()
            if item.get("offer_id") != offer_id:
                continue
            price = mapping(item, "price")
            return {
                "ok": True, "found": True, "offer_id": offer_id,
                "currency": price.get("currency_code"), "seller_price": money(price.get("price")),
                "minimum_price": money(price["min_price"]) if price.get("min_price") is not None else None,
            }
        return {"ok": True, "found": False, "offer_id": offer_id}
