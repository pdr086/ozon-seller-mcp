import assert from "node:assert/strict";
import test from "node:test";
import worker from "../worker/index.mjs";

const owner = { "oai-authenticated-user-email": "seller@example.test", "oai-authenticated-user-id": "test-owner" };
const env = { OZON_OWNER_EMAIL: "seller@example.test", OZON_CLIENT_ID: "test-client", OZON_API_KEY: "dummy-key" };

async function rpc(method, params, headers = {}, bindings = {}) {
  const request = new Request("https://example.test/mcp", {
    method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const response = await worker.fetch(request, bindings);
  assert.equal(response.headers.get("cache-control"), "no-store");
  return (await response.json()).result;
}

const call = (name, args = {}, headers, bindings) => rpc("tools/call", { name, arguments: args }, headers, bindings);

test("discovers five read-only tools and runs each on synthetic data", async () => {
  assert.deepEqual((await rpc("server/discover")).supportedVersions, ["2026-07-28"]);
  const listed = (await rpc("tools/list")).tools;
  assert.equal(listed.length, 5);
  assert.ok(listed.every(tool => tool.annotations.readOnlyHint && !tool.annotations.destructiveHint));
  for (const { name } of listed) {
    const result = await call(name, name === "product_price" ? { offer_id: "DEMO-01" } : {}, owner);
    assert.equal(result.isError, false, name);
    assert.equal(result.structuredContent.source, "synthetic_demo");
  }
  assert.equal((await call("sales_summary")).structuredContent.ordered_units, 37);
  assert.equal((await call("sales_summary")).structuredContent.ordered_amount, "12490.00");
  assert.equal((await call("connection_status", {}, owner)).structuredContent.verified_sites_identity, true);
  assert.equal((await call("low_stock", { threshold: 1 })).structuredContent.low_stock_count, 0);
  assert.equal((await call("product_price", { offer_id: "missing" })).structuredContent.found, false);
});

test("rejects unexpected parameters, writes and invalid dates", async () => {
  assert.equal((await call("change_price")).structuredContent.error, "unknown_tool");
  assert.equal((await call("low_stock", { scheme: "TEST" })).structuredContent.error, "invalid_arguments");
  assert.equal((await call("sales_summary", { end_date: "2026-02-31" })).structuredContent.error, "invalid_period");
});

test("checks the live owner and only sends credentials to the Ozon Seller API", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async (url, init) => {
    assert.equal(url, "https://api-seller.ozon.ru/v1/roles");
    assert.equal(init.redirect, "manual");
    assert.equal(init.headers["Api-Key"], "dummy-key");
    return Response.json({ extra_private_field: "does not leave the worker" });
  });
  assert.equal((await call("connection_status", {}, {}, env)).structuredContent.error, "owner_required");
  assert.equal((await call("connection_status", {}, { ...owner, "oai-authenticated-user-email": "other@example.test" }, env)).structuredContent.error, "owner_required");
  assert.equal((await call("connection_status", {}, owner, { OZON_CLIENT_ID: "test-client" })).structuredContent.error, "incomplete_setup");
  assert.equal(fetch.mock.callCount(), 0);
  const { structuredContent } = await call("connection_status", {}, owner, env);
  assert.deepEqual(structuredContent, { ok: true, source: "ozon_seller_api", connected: true, read_only: true, verified_sites_identity: true });
  assert.equal(fetch.mock.callCount(), 1);
});

test("private MCP accepts legacy text results and rejects other owners before fetching", async t => {
  const bindings = { OZON_OWNER_EMAIL: env.OZON_OWNER_EMAIL, OZON_MCP_URL: "https://mcp.example.test/mcp", OZON_MCP_TOKEN: "dummy-bridge-token" };
  const fetch = t.mock.method(globalThis, "fetch", async (url, init) => {
    assert.equal(url, bindings.OZON_MCP_URL);
    assert.equal(init.headers.Authorization, "Bearer dummy-bridge-token");
    assert.equal(init.redirect, "manual");
    assert.equal(JSON.parse(init.body).params.name, "connection_status");
    return Response.json({ jsonrpc: "2.0", id: 1, result: { content: [{ type: "text", text: JSON.stringify({ ok: true, connected: true, read_only: true }) }], isError: false } });
  });
  assert.equal((await call("connection_status", {}, {}, bindings)).structuredContent.error, "owner_required");
  assert.equal(fetch.mock.callCount(), 0);
  assert.deepEqual((await call("connection_status", {}, owner, bindings)).structuredContent, { ok: true, connected: true, read_only: true, source: "ozon_seller_api" });
  assert.equal(fetch.mock.callCount(), 1);
});

test("private MCP rejects a redirect without forwarding the bearer credential", async t => {
  const bindings = { OZON_OWNER_EMAIL: env.OZON_OWNER_EMAIL, OZON_MCP_URL: "https://mcp.example.test/mcp", OZON_MCP_TOKEN: "dummy-bridge-token" };
  const fetch = t.mock.method(globalThis, "fetch", async (url, init) => {
    assert.equal(url, bindings.OZON_MCP_URL);
    assert.equal(init.redirect, "manual");
    return new Response(null, { status: 307, headers: { Location: "https://other.example.test/mcp" } });
  });
  const result = await call("connection_status", {}, owner, bindings);
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.error, "mcp_http_307");
  assert.equal(fetch.mock.callCount(), 1);
});
