from functools import partial
from pathlib import Path
from typing import Annotated, Literal

import anyio
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import config
from .ozon import Seller, SellerError

SKILL = Path(__file__).resolve().parents[1] / "skills/ozon-seller/SKILL.md"
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

Date = Annotated[str, Field(description="Дата ГГГГ-ММ-ДД, граница периода включительно.", pattern=r"^\d{4}-\d{2}-\d{2}$")]
Limit = Annotated[int, Field(ge=1, le=25, description="Максимальное число строк в каждом списке.")]


def create_server(path, zone_name):
    instructions = SKILL.read_text(encoding="utf-8").split("---", 2)[2].strip()
    server = MCPServer(
        name="ozon-seller",
        title="Ozon Seller",
        version="0.1.0",
        instructions=instructions,
    )

    def call(method, *args):
        try:
            with Seller(config.load(path), zone_name) as seller:
                return getattr(seller, method)(*args)
        except SellerError as error:
            return error.payload

    @server.tool(annotations=READ_ONLY)
    async def connection_status() -> dict:
        """Проверить связь с одним подключённым магазином Ozon. Показывает статус и срок действия ключа."""
        return await anyio.to_thread.run_sync(partial(call, "connection_status"))

    @server.tool(annotations=READ_ONLY)
    async def sales_summary(start_date: Date | None = None, end_date: Date | None = None) -> dict:
        """Продажи по дням: заказано штук и на какую сумму. По умолчанию 7 полных дней без сегодняшнего. Сумма заказов не равна выплате или прибыли."""
        return await anyio.to_thread.run_sync(partial(call, "sales_summary", start_date, end_date))

    @server.tool(annotations=READ_ONLY)
    async def low_stock(
        threshold: Annotated[int, Field(ge=0, le=10000, description="Верхняя граница остатка в штуках.")] = 5,
        scheme: Literal["both", "FBO", "FBS"] = "both",
        limit: Limit = 15,
    ) -> dict:
        """Какие товары заканчиваются. Текущий остаток за вычетом резерва по FBO и FBS, включая ноль. Проверьте complete перед выводом по всему каталогу."""
        return await anyio.to_thread.run_sync(partial(call, "low_stock", threshold, scheme, limit))

    @server.tool(annotations=READ_ONLY)
    async def returns_summary(start_date: Date | None = None, end_date: Date | None = None, limit: Limit = 10) -> dict:
        """Возвраты и отмены по дате возврата в логистике. Показывает типы, причины и артикулы, без данных покупателей. По умолчанию 7 полных дней."""
        return await anyio.to_thread.run_sync(partial(call, "returns_summary", start_date, end_date, limit))

    @server.tool(annotations=READ_ONLY)
    async def product_price(offer_id: Annotated[str, Field(min_length=1, max_length=200)]) -> dict:
        """Показать текущую цену продавца по точному артикулу offer_id. Инструмент только читает цену."""
        return await anyio.to_thread.run_sync(partial(call, "product_price", offer_id))

    return server
