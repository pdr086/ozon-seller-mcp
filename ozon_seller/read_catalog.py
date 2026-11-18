"""Reviewed Seller API operations exposed to the assistant."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DOCS = "https://docs.ozon.ru/api/seller/"
Identifier = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]
Offer = Annotated[str, Field(min_length=1, max_length=200)]
Cursor = Annotated[str, Field(max_length=300)]
Count = Annotated[int, Field(ge=1, le=100)]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class Products(Request):
    offer_id: list[Offer] | None = Field(None, min_length=1, max_length=100)
    product_id: list[Identifier] | None = Field(None, min_length=1, max_length=100)
    sku: list[Identifier] | None = Field(None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def has_one_selector(self):
        if sum(bool(ids) for ids in (self.offer_id, self.product_id, self.sku)) != 1:
            raise ValueError("Укажите один вид идентификатора")
        return self


class ProductFilter(Request):
    offer_id: list[Offer] | None = Field(None, max_length=100)
    product_id: list[Identifier] | None = Field(None, max_length=100)
    visibility: Literal["ALL", "VISIBLE", "INVISIBLE", "ARCHIVED"] = "ALL"


class ProductPage(Request):
    filter: ProductFilter = Field(default_factory=ProductFilter)
    limit: Count = 25
    last_id: Cursor | None = None


class StockPage(Request):
    filter: ProductFilter = Field(default_factory=ProductFilter)
    limit: Count = 25
    cursor: Cursor | None = None


class Pictures(Request):
    product_id: list[Annotated[int, Field(ge=1)]] = Field(min_length=1, max_length=25)


class WarehousePage(Request):
    limit: Count = 25
    cursor: Cursor | None = None


class Category(Request):
    description_category_id: int = Field(ge=1)
    type_id: int = Field(ge=1)
    language: Literal["DEFAULT", "RU", "EN"] = "DEFAULT"


class Dates(Request):
    since: datetime
    to: datetime
    status: Annotated[str, Field(max_length=80)] | None = None

    @model_validator(mode="after")
    def bounded_period(self):
        if self.since.utcoffset() is None or self.to.utcoffset() is None:
            raise ValueError("Укажите часовой пояс")
        if not timedelta(0) < self.to - self.since <= timedelta(days=31):
            raise ValueError("Период должен быть от 0 до 31 дня")
        return self


class Postings(Request):
    filter: Dates
    limit: Annotated[int, Field(ge=1, le=50)] = 25
    offset: Annotated[int, Field(ge=0, le=20000)] = 0
    dir: Literal["ASC", "DESC"] = "DESC"


class Accruals(Request):
    date: date


@dataclass(frozen=True)
class Operation:
    path: str
    title: str
    keywords: str
    request: type[Request]
    response: str

    def summary(self):
        return {"operation": self.path, "title": self.title, "http_method": "POST"}


OPERATIONS = {
    entry.path: entry
    for entry in (
        Operation("/v3/product/list", "Список товаров и их идентификаторы", "каталог артикул SKU product_id", ProductPage, "full"),
        Operation("/v3/product/info/list", "Карточка товара, штрихкод, цена, комиссия, статус и изображения", "название товар артикул SKU product_id offer_id", Products, "full"),
        Operation("/v4/product/info/attributes", "Характеристики и габариты товаров", "описание вес размер бренд карточка атрибут", ProductPage, "full"),
        Operation("/v2/product/pictures/info", "Изображения товара", "фото картинки карточка", Pictures, "full"),
        Operation("/v4/product/info/stocks", "Остатки товаров и резервы по FBO и FBS", "запасы наличие склад", StockPage, "full"),
        Operation("/v5/product/info/prices", "Цены, индексы и комиссии товаров", "цена скидка минимальная", StockPage, "full"),
        Operation("/v2/warehouse/list", "Склады продавца", "склад складов FBS логистика", WarehousePage, "warehouses"),
        Operation("/v1/description-category/attribute", "Характеристики категории Ozon", "категории свойства справочник", Category, "full"),
        Operation("/v3/posting/fbo/list", "Список отправлений FBO", "отправления заказ заказы статус доставка", Postings, "postings"),
        Operation("/v4/posting/fbs/list", "Список отправлений FBS и rFBS", "отправления заказ заказы статус доставка", Postings, "postings"),
        Operation("/v1/finance/accrual/types", "Виды начислений Ozon", "финансы комиссии услуги справочник", Request, "full"),
        Operation("/v1/finance/accrual/by-day", "Начисления Ozon за день", "финансы комиссии удержания начисления", Accruals, "accruals"),
    )
}


def search(query):
    terms = set(re.findall(r"[\w]+", query.casefold()))
    matches = []
    for entry in OPERATIONS.values():
        words = set(re.findall(r"[\w]+", f"{entry.title} {entry.keywords} {entry.path}".casefold()))
        score = sum(any(word.startswith(term) or (len(word) >= 4 and term.startswith(word)) for word in words) for term in terms)
        if score or not terms:
            matches.append((score, entry))
    matches.sort(key=lambda match: -match[0])
    return {
        "ok": True, "documentation": DOCS, "available_operations": len(OPERATIONS),
        "operations": [entry.summary() for _, entry in matches],
    }
