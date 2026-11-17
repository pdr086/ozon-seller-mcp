const protocol = "2026-07-28";
const instructions = "Неофициальный плагин для Ozon Seller. Только чтение. Отличай демо от данных магазина, указывай период и неполный охват. Продажи означают заказанные штуки и сумму заказов, а не выплаты. Для цены нужен точный артикул. Не запрашивай секреты в чате.";
const dayMs = 86_400_000;
const timezone = "Europe/Moscow";
const dates = {
  start_date: { type: "string", format: "date" },
  end_date: { type: "string", format: "date" },
};
const limitSchema = { type: "integer", minimum: 1, maximum: 50 };
const endpoints = {
  roles: "/v1/roles",
  sales: "/v1/analytics/data",
  stock: "/v4/product/info/stocks",
  returns: "/v1/returns/list",
  prices: "/v5/product/info/prices",
};

function tool(name, title, description, properties = {}, required = []) {
  return {
    name, title, description,
    inputSchema: { type: "object", properties, required, additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  };
}

const tools = [
  tool("connection_status", "Проверить Ozon", "Проверьте режим демо или доступ к Seller API."),
  tool("sales_summary", "Продажи Ozon", "Заказанные штуки и сумма заказов. По умолчанию за семь полных дней по Москве.", dates),
  tool("low_stock", "Заканчивающиеся товары", "Остатки FBO и FBS за вычетом резерва. Порог по умолчанию — 5 шт.", {
    threshold: { type: "integer", minimum: 0, maximum: 100_000 },
    scheme: { type: "string", enum: ["FBO", "FBS", "both"] },
    limit: limitSchema,
  }),
  tool("returns_summary", "Возвраты Ozon", "Причины и товары по дате возврата в логистике. По умолчанию за семь полных дней по Москве.", { ...dates, limit: limitSchema }),
  tool("product_price", "Цена товара", "Текущая цена продавца по точному артикулу. Только чтение.", {
    offer_id: { type: "string", minLength: 1, maxLength: 200 },
  }, ["offer_id"]),
];

class ToolError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const malformed = () => new ToolError("unexpected_response", "Seller API вернул ответ неожиданного формата.");
const invalid = () => new ToolError("invalid_arguments", "Проверьте имена и значения параметров инструмента.");

function array(value) {
  if (!Array.isArray(value)) throw malformed();
  return value;
}

function quantity(value) {
  if (typeof value === "string" && /^\d+$/.test(value)) value = Number(value);
  if (!Number.isSafeInteger(value) || value < 0) throw malformed();
  return value;
}

function money(value) {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value)) return value;
  throw malformed();
}

function date(value) {
  const parsed = typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) && Date.parse(value);
  if (!Number.isFinite(parsed) || isoDay(parsed) !== value) {
    throw new ToolError("invalid_period", "Укажите дату в формате ГГГГ-ММ-ДД.");
  }
  return parsed;
}

const isoDay = ms => new Date(ms).toISOString().slice(0, 10);

function period(args) {
  const today = date(new Intl.DateTimeFormat("sv-SE", { timeZone: timezone, dateStyle: "short" }).format(new Date()));
  const end = args.end_date === undefined ? today - dayMs : date(args.end_date);
  const start = args.start_date === undefined ? end - 6 * dayMs : date(args.start_date);
  if (start > end || end > today || end - start > 30 * dayMs) {
    throw new ToolError("invalid_period", "Укажите период до 31 дня включительно без будущих дат.");
  }
  return { start_date: isoDay(start), end_date: isoDay(end), timezone };
}

async function readJson(message, maxBytes) {
  const reader = message.body?.getReader();
  if (!reader) throw malformed();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let size = 0;
  let json = "";
  for (;;) {
    const part = await reader.read();
    if (part.done) break;
    size += part.value.byteLength;
    if (size > maxBytes) {
      await reader.cancel();
      throw malformed();
    }
    json += decoder.decode(part.value, { stream: true });
  }
  return JSON.parse(json + decoder.decode());
}

function sellerClient(env) {
  const deadline = Date.now() + 15_000;
  return async (endpoint, payload) => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new ToolError("timeout", "Seller API не успел ответить.");
    let response;
    try {
      response = await fetch(`https://api-seller.ozon.ru${endpoint}`, {
        method: "POST", redirect: "error", signal: AbortSignal.timeout(Math.min(remaining, 8_000)),
        headers: { "Client-Id": env.OZON_CLIENT_ID, "Api-Key": env.OZON_API_KEY, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      throw new ToolError("ozon_unavailable", "Не удалось связаться с Seller API Ozon.");
    }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {});
      const messages = {
        400: "Ozon отклонил параметры запроса.", 401: "Проверьте Client ID и срок действия ключа Ozon.",
        403: "Проверьте права ключа, сетевые ограничения и доступность метода на тарифе Ozon.",
        429: "Ozon ограничил частоту запросов. Повторите позже.",
      };
      throw new ToolError(`ozon_http_${response.status}`, messages[response.status] ?? "Seller API временно недоступен.");
    }
    try {
      const result = await readJson(response, 4 * 1024 * 1024);
      if (!object(result)) throw malformed();
      return result;
    } catch {
      throw malformed();
    }
  };
}

async function paged(post, endpoint, payload, field, pageKey) {
  const rows = [];
  const seen = new Set();
  let token;
  const deadline = Date.now() + 10_000;
  for (let page = 0; page < 15 && Date.now() < deadline; page++) {
    const result = await post(endpoint, { ...payload, limit: 100, [pageKey]: token });
    const items = array(result[field]);
    if (!items.every(object)) throw malformed();
    rows.push(...items);
    if (pageKey === "cursor") {
      token = result.cursor;
      if (!items.length || token === "" || token == null) return { rows, complete: true };
      if (typeof token !== "string") throw malformed();
    } else {
      if (typeof result.has_next !== "boolean") throw malformed();
      if (!result.has_next) return { rows, complete: true };
      token = items.at(-1)?.id;
      if (!Number.isSafeInteger(token)) throw malformed();
    }
    if (seen.has(token)) throw malformed();
    seen.add(token);
  }
  return { rows, complete: false };
}

async function sales(post, args) {
  const range = period(args);
  const { result } = await post(endpoints.sales, {
    date_from: range.start_date, date_to: range.end_date,
    dimension: ["day"], metrics: ["ordered_units", "revenue"], limit: 100, offset: 0,
  });
  const days = array(result?.data).map(row => {
    const day = row?.dimensions?.[0]?.id;
    if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day) || row?.dimensions?.length !== 1 || row?.metrics?.length !== 2) throw malformed();
    return { date: day, ordered_units: quantity(row.metrics[0]), ordered_amount: money(row.metrics[1]) };
  }).filter(row => row.date >= range.start_date && row.date <= range.end_date).sort((a, b) => a.date.localeCompare(b.date));
  const totals = array(result?.totals);
  if (totals.length !== 2 && (totals.length || days.length)) throw malformed();
  const [units, amount] = totals.length ? totals : [0, "0"];
  return { period: range, basis: "ozon_orders", ordered_units: quantity(units), ordered_amount: money(amount), days };
}

async function stock(post, { threshold = 5, scheme = "both", limit = 10 }) {
  const { rows, complete } = await paged(post, endpoints.stock, { filter: { visibility: "ALL" } }, "items", "cursor");
  const items = [];
  const seen = new Set();
  let evaluated = 0;
  for (const product of rows) {
    if (!Number.isSafeInteger(product.product_id) || seen.has(product.product_id) || typeof product.offer_id !== "string") throw malformed();
    seen.add(product.product_id);
    const byScheme = {};
    for (const row of array(product.stocks)) {
      const type = row?.type?.toUpperCase?.();
      if (!['FBO', 'FBS'].includes(type) || (scheme !== "both" && scheme !== type)) continue;
      const free = Math.max(quantity(row.present) - quantity(row.reserved), 0);
      byScheme[type] = (byScheme[type] ?? 0) + free;
    }
    const quantities = Object.values(byScheme);
    if (!quantities.length) continue;
    evaluated++;
    const available = quantities.reduce((sum, free) => sum + free, 0);
    if (available <= threshold) items.push({ offer_id: product.offer_id, available, available_by_scheme: byScheme });
  }
  items.sort((a, b) => a.available - b.available || a.offer_id.localeCompare(b.offer_id));
  return { complete, threshold, scheme, evaluated_items: evaluated, items_without_selected_stock: rows.length - evaluated, low_stock_count: items.length, items: items.slice(0, limit) };
}

async function returns(post, args) {
  const range = period(args);
  const timeFrom = new Date(`${range.start_date}T00:00:00+03:00`).toISOString();
  const timeTo = new Date(`${range.end_date}T23:59:59.999+03:00`).toISOString();
  const { rows, complete } = await paged(post, endpoints.returns, { filter: { logistic_return_date: { time_from: timeFrom, time_to: timeTo } } }, "returns", "last_id");
  const seen = new Set();
  const types = new Map();
  const reasons = new Map();
  const offers = new Map();
  const count = (map, type, field, label, units) => {
    const key = JSON.stringify([type, label]);
    const row = map.get(key) ?? { type, [field]: label, units: 0 };
    row.units += units;
    map.set(key, row);
  };
  for (const row of rows) {
    if (!Number.isSafeInteger(row.id)) throw malformed();
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    const type = row.type || "unknown";
    const reason = row.return_reason_name || "Причина не указана";
    if (typeof type !== "string" || typeof reason !== "string") throw malformed();
    const units = quantity(row.product?.quantity);
    const current = types.get(type) ?? { type, records: 0, units: 0 };
    current.records++;
    current.units += units;
    types.set(type, current);
    count(reasons, type, "reason", reason.slice(0, 160), units);
    const offer = row.product?.offer_id;
    if (typeof offer === "string" && offer) count(offers, type, "offer_id", offer, units);
  }
  const top = map => [...map.values()].sort((a, b) => b.units - a.units).slice(0, args.limit ?? 10);
  return { period: range, complete, date_basis: "logistic_return_date", records: seen.size, by_type: [...types.values()], top_reasons: top(reasons), top_offers: top(offers) };
}

async function price(post, { offer_id }) {
  const result = await post(endpoints.prices, { filter: { offer_id: [offer_id], visibility: "ALL" }, limit: 100 });
  const match = array(result.items).find(item => item?.offer_id === offer_id);
  if (!match) return { found: false, offer_id };
  const { currency_code, price, min_price } = match.price ?? {};
  return { found: true, offer_id, currency: currency_code ?? null, seller_price: money(price), minimum_price: min_price == null ? null : money(min_price) };
}

async function demoPost(endpoint, payload) {
  switch (endpoint) {
    case endpoints.roles: return {};
    case endpoints.sales: return { result: { data: [{ dimensions: [{ id: payload.date_to }], metrics: [37, "12490.00"] }], totals: [37, "12490.00"] } };
    case endpoints.stock: return { items: [{ product_id: 1, offer_id: "DEMO-01", stocks: [{ type: "fbo", present: 3, reserved: 1 }] }], cursor: "" };
    case endpoints.returns: return { returns: [{ id: 1, type: "RETURN", return_reason_name: "Демо: не подошёл размер", product: { offer_id: "DEMO-01", quantity: 2 } }], has_next: false };
    case endpoints.prices: return { items: [{ offer_id: "DEMO-01", price: { currency_code: "RUB", price: "349.00", min_price: "299.00" } }] };
    default: throw malformed();
  }
}

function validate(tool, args) {
  if (!object(args)) throw invalid();
  const { properties, required } = tool.inputSchema;
  if (required.some(key => !Object.hasOwn(args, key))) throw invalid();
  for (const [key, value] of Object.entries(args)) {
    if (!Object.hasOwn(properties, key)) throw invalid();
    const field = properties[key];
    if (field.type === "integer" && (!Number.isSafeInteger(value) || value < field.minimum || value > field.maximum)) throw invalid();
    if (field.type === "string" && (typeof value !== "string" || !value.trim() || value.length > (field.maxLength ?? 20))) throw invalid();
    if (field.enum && !field.enum.includes(value)) throw invalid();
  }
}

async function call(params, request, env) {
  const tool = tools.find(item => item.name === params?.name);
  if (!tool) throw new ToolError("unknown_tool", "Такого инструмента нет. Изменения магазина не поддерживаются.");
  const args = params.arguments ?? {};
  validate(tool, args);
  const { OZON_OWNER_EMAIL, OZON_CLIENT_ID, OZON_API_KEY } = env;
  const demo = !(OZON_OWNER_EMAIL || OZON_CLIENT_ID || OZON_API_KEY);
  const viewer = request.headers.get("oai-authenticated-user-email");
  const identityVerified = Boolean(viewer && request.headers.get("oai-authenticated-user-id"));
  if (!demo) {
    if (!(OZON_OWNER_EMAIL && OZON_CLIENT_ID && OZON_API_KEY)) throw new ToolError("incomplete_setup", "Заполните три секрета в настройках своего Site.");
    if (!identityVerified || viewer.toLowerCase() !== OZON_OWNER_EMAIL.trim().toLowerCase()) {
      throw new ToolError("owner_required", "Данные магазина доступны только владельцу, указанному в настройках Site.");
    }
  }
  const post = demo ? demoPost : sellerClient(env);
  const handlers = {
    connection_status: async () => {
      await post(endpoints.roles, {});
      return { connected: !demo, read_only: true, verified_sites_identity: identityVerified };
    },
    sales_summary: sales, low_stock: stock, returns_summary: returns, product_price: price,
  };
  const data = await handlers[tool.name](post, args);
  return { ok: true, source: demo ? "synthetic_demo" : "ozon_seller_api", ...data };
}

function toolResult(data, isError = false) {
  return { content: [{ type: "text", text: JSON.stringify(data) }], structuredContent: data, isError };
}

export default {
  async fetch(request, env = {}) {
    const headers = { "Cache-Control": "no-store" };
    const path = new URL(request.url).pathname;
    if (path === "/" && request.method === "GET") return new Response(instructions, { headers: { ...headers, "Content-Type": "text/plain; charset=utf-8" } });
    if (path !== "/mcp") return new Response(null, { status: 404 });
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "POST" } });

    const reply = (id, body) => Response.json({ jsonrpc: "2.0", id, ...body }, { headers });
    let rpc;
    try {
      rpc = await readJson(request, 32 * 1024);
    } catch {
      return reply(null, { error: { code: -32700, message: "Parse error" } });
    }
    if (!object(rpc) || rpc.jsonrpc !== "2.0" || typeof rpc.method !== "string") {
      return reply(null, { error: { code: -32600, message: "Invalid Request" } });
    }
    if (!Object.hasOwn(rpc, "id")) return new Response(null, { status: 202, headers });

    switch (rpc.method) {
      case "server/discover":
        return reply(rpc.id, { result: { supportedVersions: [protocol], capabilities: { tools: {} }, instructions } });
      case "initialize":
        return reply(rpc.id, { result: { protocolVersion: [protocol, "2025-11-25", "2025-06-18"].includes(rpc.params?.protocolVersion) ? rpc.params.protocolVersion : protocol, serverInfo: { name: "ozon-seller-sites", version: "0.1.0" }, capabilities: { tools: {} }, instructions } });
      case "ping": return reply(rpc.id, { result: {} });
      case "tools/list": return reply(rpc.id, { result: { tools } });
      case "tools/call": {
        let result;
        try {
          result = toolResult(await call(rpc.params, request, env));
        } catch (error) {
          result = toolResult(error instanceof ToolError ? { ok: false, error: error.code, message: error.message } : { ok: false, error: "internal_error", message: "Не удалось выполнить запрос к Ozon." }, true);
        }
        return reply(rpc.id, { result });
      }
      default: return reply(rpc.id, { error: { code: -32601, message: "Method not found" } });
    }
  },
};
