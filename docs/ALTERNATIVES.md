# Дополнительные варианты подключения

Основная инструкция — [приватный VPS](SELF_HOSTING.md) и [личный Site для ChatGPT](../sites/ozon-seller/README.md). Ниже описаны другие режимы существующего кода.

## Site обращается к Ozon напрямую

Этот режим не требует VPS и предоставляет пять готовых инструментов: подключение, продажи, остатки, возвраты и цену по точному артикулу. Поиск по названию и общий каталог операций работают в основном варианте с MCP.

Разверните тот же шаблон Site приватно. Вместо `OZON_MCP_URL` и `OZON_MCP_TOKEN` задайте серверные секреты:

| Имя | Значение |
| --- | --- |
| `OZON_OWNER_EMAIL` | Email владельца аккаунта ChatGPT |
| `OZON_CLIENT_ID` | Client ID магазина |
| `OZON_API_KEY` | API-ключ Seller API |

Переопубликуйте Site и проверьте подключение. Возможность прямого обращения зависит от сетевой доступности Ozon из среды Sites. При переходе на VPS используйте три секрета из основной инструкции и удалите временные копии ключа Ozon из Site.

## Демо без магазина

Если все секреты подключения отсутствуют, шаблон Sites работает на встроенных вымышленных данных пяти инструментов. Ответ содержит `source: synthetic_demo`; пример продаж — 37 штук и 12 490 рублей. Этот режим подходит для знакомства с интерфейсом.

После подключения MCP или ключа Ozon ошибки возвращаются как ошибки, без подстановки демоданных. Проверяйте источник данных в ответе инструмента.

## Локальный MCP

Для клиента с поддержкой MCP запустите сервер на loopback без bearer-моста:

```bash
docker run -d --name ozon-seller-local --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  --publish 127.0.0.1:8765:8765 \
  --mount "type=bind,source=$PWD/data,target=/data,readonly" \
  ozon-seller-mcp:0.1.0
```

Образ и каталог ключей готовятся по [инструкции VPS](SELF_HOSTING.md). Адрес — `http://127.0.0.1:8765/mcp`. [mcp.json.example](../mcp.json.example) описывает этот режим для клиента, работающего на той же машине. Для клиента с поддержкой Agent Skills используйте каталог [skills/ozon-seller](../skills/ozon-seller/).

## Secure MCP Tunnel

Локальный MCP можно связать с ChatGPT через [официальный tunnel-client](https://github.com/openai/tunnel-client). Этот способ требует доступа к созданию собственных MCP-подключений в аккаунте ChatGPT и отдельной настройки туннеля. Основной способ для Plus использует Sites и не требует этого доступа.

1. В [OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels) создайте туннель, свяжите его с нужным аккаунтом или пространством и получите runtime-ключ.
2. Настройте официальный клиент на VPS:

   ```bash
   tunnel-client init --sample sample_mcp_remote_no_auth \
     --profile ozon-seller --tunnel-id tunnel_... \
     --mcp-server-url http://127.0.0.1:8765/mcp

   read -r -s -p "OpenAI Tunnel runtime key: " CONTROL_PLANE_API_KEY
   printf '\n'
   export CONTROL_PLANE_API_KEY
   tunnel-client doctor --profile ozon-seller --explain
   tunnel-client run --profile ozon-seller
   ```

3. В **ChatGPT → Плагины → Добавить собственный MCP-сервер → Tunnel** выберите свой туннель. Для этого локального сервера авторизация — **No authentication**: внешнее подключение защищается самим приватным туннелем.
4. Проверьте обнаружение девяти инструментов в новом чате.

[Документация Secure MCP Tunnels](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
