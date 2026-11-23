"""Private per-store reports reachable through the existing read-only catalog."""
import json
import os
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal

import httpx

from .ozon import SellerError, amount, array, bad_response, mapping, quantity

DAILY = "report.daily_store"
ADS = "report.promotion_expenses"
STATS = "report.promotion_statistics"
REPORTS = {
    DAILY: "Отчёт за день: сколько продано по начислениям, заказано, себестоимость по SKU и группам, реклама и распределение расходов",
    ADS: "Продвижение: дневной расход денег на рекламу по кампаниям Ozon Performance",
    STATS: "Аналитика продвижения: показы, клики, заказы и показатели рекламных кампаний за день по данным Performance",
}
KEYWORDS = {
    DAILY: ("сегод", "заказ", "начисл", "себестоим", "продан", "набор", "ежеднев", "день", "отчет", "отчёт", "лент", "чипс"),
    ADS: ("расход", "реклам", "продвиж", "списа", "потрач", "снято", "performance"),
    STATS: ("аналитик", "реклам", "продвиж", "статистик", "показы", "клик", "кампан", "performance"),
}
AD_TYPES = {"PayPerClick", "Promotion", "Stencil", "ExternalPromotion", "Marketing",
            "InternetSiteAdvertising", "SocialMediaAdvertising", "DisplayAdvertisingPlacement",
            "BrandCommission", "BrandPromotion"}
MAX_PAGES = 100


def describe(name):
    return {"ok": True, "operation": name, "title": REPORTS[name], "source": "store_report",
            "schema_available": True, "schema_verified": True,
            "input_schema": {"type": "object", "properties": {"date": {"type": "string", "format": "date",
                             "description": "По умолчанию сегодня по времени магазина."}}, "additionalProperties": False}}


def search(query):
    return [describe(name) for name in REPORTS if not query or name in query or
            any(word in query.casefold() for word in KEYWORDS[name])]


def private_file(directory, filename):
    path = directory / filename
    if not path.exists():
        return None
    with path.open() as f:
        if os.fstat(f.fileno()).st_mode & 0o077:
            raise SellerError("unsafe_config_permissions", "Установите права 0600 на файл настроек отчёта.")
        return json.load(f)


def rub(field):
    if not isinstance(field, dict) or field.get("currency") != "RUB":
        raise bad_response()
    return amount(field.get("amount"))


def fetch_rows(seller, path, body, field, cursor_key):
    seen = set()
    for _ in range(MAX_PAGES):
        page = seller.post(path, body)
        rows = array(page, field)
        yield from rows
        cursor = page.get(cursor_key)
        if cursor_key == "cursor":
            if page.get("has_next") is False:
                return
            if page.get("has_next") is not True or not cursor:
                raise bad_response()
        elif not cursor:
            return
        if not rows or cursor in seen:
            raise SellerError("incomplete_pages", "Ozon вернул неполную последовательность страниц.")
        seen.add(cursor)
        body = {**body, cursor_key: cursor}
    raise SellerError("incomplete_pages", "Превышен лимит страниц за день.")


def finance(seller, day):
    types = array(seller.post("/v1/finance/accrual/types", {}), "accrual_types")
    labels = {x["id"]: x["name"] for x in types}
    lines, ads, seen = [], defaultdict(Decimal), set()
    for row in fetch_rows(seller, "/v1/finance/accrual/by-day", {"date": day}, "accruals", "last_id"):
        if row["accrual_id"] in seen:
            raise bad_response()
        seen.add(row["accrual_id"])
        fees = [row["non_item_fee"]] if row.get("non_item_fee") else []
        for group in (row.get("item_fees") or {}).get("fees", []):
            fees.extend(group["fees"])
        for product in (row.get("posting") or {}).get("products", []):
            fees.extend((product.get("delivery") or {}).get("services", []))
            commission = product.get("commission")
            if not commission:
                continue
            revenue = rub(commission["sale_amount"])
            if revenue:
                units = quantity(abs(amount(product["quantity"])))
                lines.append((str(product["sku"]), units * (1 if revenue > 0 else -1), revenue))
        for fee in fees:
            label = labels.get(fee["type_id"])
            if label in AD_TYPES:
                ads[label] -= rub(fee["accrued"])
    return lines, {"source": "seller_finance_posted_ad_fees", "types_in_scope": sorted(AD_TYPES),
                   "by_type_rub": {k: str(v) for k,v in ads.items()}, "net_rub": str(sum(ads.values()))}


def orders(seller, start, end):
    lines, seen = [], set()
    for path in ["/v3/posting/fbo/list", "/v4/posting/fbs/list"]:
        payload = {"filter": {"since": start.isoformat(), "to": end.isoformat()}, "dir": "ASC", "limit": 50}
        for posting in fetch_rows(seller, path, payload, "postings", "cursor"):
            key = posting["posting_number"]
            if key in seen:
                raise bad_response()
            seen.add(key)
            for product in posting["products"]:
                n = quantity(product["quantity"])
                lines.append((str(product["sku"]), n, n * rub(product["price"])))
    return lines


def aggregate(lines, products):
    totals = defaultdict(lambda: [0, 0, Decimal(0)])
    for sku, units, revenue in lines:
        rec = totals[sku]
        rec[units < 0] += abs(units)
        rec[2] += revenue
    groups = {}
    variants = {(p["group"], p["variant"]): [0,0] for p in products.values()}
    rows = []
    for sku,(sold, reversed_units, revenue) in sorted(totals.items()):
        p = products.get(sku, {})
        group = p.get("group", "unmapped")
        variant = p.get("variant", "не определён")
        pack = p.get("units_per_pack", 1)
        units = sold * pack
        cost = amount(p["unit_cost_rub"]) * units if p.get("unit_cost_rub") is not None else None
        row = {"sku": sku, "name": p.get("name"), "group": group, "variant": variant, "packs": sold,
               "units": units, "reversed_packs": reversed_units, "net_revenue_rub": str(revenue),
               "cost_rub": str(cost) if cost is not None else None}
        rows.append(row)
        counts = variants.setdefault((group, variant), [0,0])
        counts[0] += sold
        counts[1] += units
        g = groups.setdefault(group, {"packs": 0, "units": 0, "reversed_packs": 0, "known_cost_rub": 0, "packs_without_cost": 0})
        g["packs"] += sold
        g["units"] += units
        g["reversed_packs"] += reversed_units
        if cost is None:
            g["packs_without_cost"] += sold
        else:
            g["known_cost_rub"] += cost
    for g in groups.values():
        g["known_cost_rub"] = str(g["known_cost_rub"])
    return {"ok": True, "items": rows, "groups": groups,
            "variants": [{"group": group, "variant": variant, "packs": n, "units": pieces}
                         for (group,variant),(n,pieces) in sorted(variants.items())]}


def performance(directory, day, kind, transport=None):
    creds = private_file(directory, "performance.json")
    if not creds:
        raise SellerError("performance_not_connected", "Рекламный кабинет Ozon Performance не подключён.")
    with httpx.Client(base_url="https://api-performance.ozon.ru", timeout=8, follow_redirects=False,
                      trust_env=False, transport=transport) as client:
        def request(method, path, **kwargs):
            try:
                with client.stream(method, path, **kwargs) as response:
                    if response.status_code != 200:
                        raise SellerError("performance_api_error", "Ozon Performance отклонил запрос.", http_status=response.status_code)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 4 * 1024 * 1024:
                            raise SellerError("response_too_large", "Отчёт кампаний превышает доступный объём.")
                    return json.loads(body)
            except (httpx.HTTPError, ValueError):
                raise SellerError("performance_unavailable", "Не удалось прочитать ответ Ozon Performance.") from None
        token = request("POST", "/api/client/token", json={"client_id": creds["client_id"],
                        "client_secret": creds["client_secret"], "grant_type": "client_credentials"}).get("access_token")
        if not isinstance(token, str) or not token:
            raise bad_response()
        endpoint = "expense" if kind == ADS else "daily"
        report = request("GET", f"/api/client/statistics/{endpoint}/json", params={"dateFrom": day, "dateTo": day},
                         headers={"Authorization": "Bearer " + token})
    rows = report if isinstance(report, list) else array(report, "rows")
    if len(rows) > 2000 or (isinstance(report, dict) and (report.get("hasNext") or report.get("nextPageToken"))):
        raise SellerError("incomplete_pages", "Отчёт кампаний превышает доступный объём.")
    fields = ("id", "campaignId", "date", "title", "campaignTitle", "moneySpent", "bonusSpent", "prepaymentSpent",
              "views", "clicks", "orders", "ordersMoney", "ctr", "drr")
    result = {"ok": True, "source": f"ozon_performance_{endpoint}", "date": day,
              "campaign_rows": [{k:r[k] for k in fields if k in r} for r in rows]}
    if kind == ADS:
        result["money_spent_rub"] = str(sum(amount(str(r.get("moneySpent")).replace(",", ".")) for r in rows))
    return result


def allocate(total, rule):
    if not rule or total < 0:
        return None
    fixed = min(total, amount(rule["cap_rub"]))
    return {rule["first_group"]: str(fixed), rule["other_group"]: str(total-fixed)}


def run(seller, directory, name, args):
    now = datetime.now(seller.zone)
    try:
        if not isinstance(args, dict) or set(args) - {"date"}:
            raise ValueError
        day = date.fromisoformat(args["date"]) if "date" in args else now.date()
        if day > now.date():
            raise ValueError
    except (TypeError, ValueError):
        raise SellerError("invalid_arguments", "Используйте сегодняшнюю или прошедшую дату ГГГГ-ММ-ДД.") from None
    key = day.isoformat()
    if name != DAILY:
        return performance(directory, key, name)
    profile = private_file(directory, "store-report.json") or {}
    products = profile.get("products", {})
    result = {"ok": True, "date": key, "as_of": now.isoformat(), "timezone": seller.zone.key,
              "ordered_basis": "Отправления FBO и FBS за период, включая отменённые заказы.",
              "sales_basis": "Положительные начисления реализации, возвраты отдельно. Себестоимость по проданным единицам.",
              "allocation_note": "Один дневной расход для обеих выборок. Не складывать повторно."}
    try:
        lines, booked = finance(seller, key)
        result["accrued_sales"] = aggregate(lines, products)
        result["booked_ad_fees"] = booked
    except SellerError as e:
        result["accrued_sales"] = e.payload
    try:
        start = datetime.combine(day, time.min, seller.zone)
        end = now if day == now.date() else datetime.combine(day, time.max, seller.zone)
        result["ordered"] = aggregate(orders(seller, start, end), products)
    except SellerError as e:
        result["ordered"] = e.payload
    try:
        ad = performance(directory, key, ADS)
        result["promotion"] = {k:v for k,v in ad.items() if k != "campaign_rows"}
        ad_total = amount(ad["money_spent_rub"])
        source = ad["source"]
    except SellerError as e:
        result["promotion"] = e.payload
        booked = result.get("booked_ad_fees")
        ad_total = amount(booked["net_rub"]) if booked else None
        source = "seller_finance_posted_ad_fees" if booked else None
    result["advertising_allocation"] = {"source": source, "groups_rub": allocate(ad_total, profile.get("allocation"))
                                        if ad_total is not None else None}
    return result
