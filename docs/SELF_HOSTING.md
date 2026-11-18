# Приватный MCP на своём VPS

Один экземпляр обслуживает один магазин. Основной вариант подключения к ChatGPT Plus — через [личный Site](../sites/ozon-seller/README.md).

## Подготовка

Нужны Linux VPS, Git, Docker, Python 3 для генерации токена и HTTPS reverse proxy, например [Caddy](https://caddyserver.com/docs/install). Домен или поддомен должен указывать на VPS; для Caddy откройте порты 80 и 443. Серверу нужен исходящий HTTPS-доступ к `api-seller.ozon.ru`.

В Ozon Seller возьмите Client ID и API-ключ с нужными правами чтения в разделе **Настройки → Seller API**. Можно использовать существующий ключ. [Документация Ozon по авторизации](https://dev.ozon.ru/start/297-Kak-rabotaet-Seller-API-avtorizatsiia-nastroika-i-pervye-shagi/).

## 1. Соберите образ и подключите магазин

Выполните на VPS:

```bash
git clone https://github.com/pdr086/ozon-seller-mcp.git
cd ozon-seller-mcp
docker build -t ozon-seller-mcp:0.1.0 .
install -d -m 700 data

docker run --rm -it --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/data,target=/data" \
  ozon-seller-mcp:0.1.0 configure
```

Введите Client ID и API-ключ в интерактивном терминале. Ввод скрыт. Команда проверит подключение и сохранит ключ в `data/credentials.json` с правами `0600`.

## 2. Создайте токен подключения

Это отдельный токен между Site и вашим MCP, а не API-ключ Ozon:

```bash
python3 - <<'PY'
import os
import secrets

fd = os.open("bridge.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as file:
    file.write("OZON_BRIDGE_TOKEN=" + secrets.token_urlsafe(48) + "\n")
PY
```

Значение после `OZON_BRIDGE_TOKEN=` понадобится в секретах Site. Файл `bridge.env` и каталог `data` исключены из Git; храните их на своём сервере.

## 3. Запустите MCP

```bash
docker run -d --name ozon-seller-mcp --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 256m --cpus 0.5 --pids-limit 64 \
  --env-file "$PWD/bridge.env" \
  --publish 127.0.0.1:8767:8767 \
  --mount "type=bind,source=$PWD/data,target=/data,readonly" \
  --entrypoint python \
  ozon-seller-mcp:0.1.0 -m uvicorn ozon_seller.bearer_bridge:app \
  --host 0.0.0.0 --port 8767 --no-access-log --no-proxy-headers

docker exec ozon-seller-mcp python -m ozon_seller check
```

Ожидаемый ответ: «Подключение к Ozon проверено». Порт 8767 доступен только на самом VPS.

## 4. Настройте HTTPS

Добавьте в конфигурацию Caddy блок со своим доменом:

```caddyfile
ozon.example.com {
    request_body {
        max_size 32KB
    }
    reverse_proxy 127.0.0.1:8767 {
        header_up Host 127.0.0.1:8767
        transport http {
            response_header_timeout 45s
        }
    }
}
```

Примените конфигурацию штатным способом для своей установки Caddy. Он получит и продлит сертификат. Для другого reverse proxy сохраните `Authorization` и установите внутренний заголовок `Host: 127.0.0.1:8767`.

Проверьте адрес без токена:

```bash
curl -i -X POST https://ozon.example.com/mcp \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Ответ **HTTP 401** подтверждает, что адрес доступен и закрыт токеном. Рабочий адрес MCP для Site — `https://ozon.example.com/mcp`.

## 5. Подключите ChatGPT

Перейдите к [инструкции Sites](../sites/ozon-seller/README.md). В Site сохраняются только адрес MCP, его токен и адрес аккаунта владельца. Client ID и API-ключ Ozon остаются на VPS.

## Обслуживание

Для замены ключа снова выполните интерактивную команду `configure` из первого шага. Работающий сервер прочитает обновлённый файл при следующем вызове. Срок действия текущего ключа можно узнать командой «Проверь подключение к Ozon».

Для обновления кода выполните `git pull --ff-only`, повторно соберите образ, остановите и удалите только контейнер `ozon-seller-mcp`, затем повторите команду запуска из третьего шага. Каталог `data` и файл `bridge.env` сохраняются. После изменения Worker переопубликуйте тот же Site и начните новый чат.

Для другого продавца создайте отдельные каталог установки, файл ключа, токен, контейнер, локальный порт и личный Site. Токен одного магазина не должен использоваться для другого.
