# Ozon Seller MCP

Открытый навык и MCP-сервер для аналитики магазина Ozon. Вы запускаете сервер на своём Linux VPS, вводите ключ своего магазина и подключаете сервер в ChatGPT через Secure MCP Tunnel. Один экземпляр обслуживает один магазин.

Пять инструментов проверяют подключение, показывают заказы по дням, остатки FBO и FBS, причины возвратов и текущую цену по артикулу. Все инструменты работают на чтение.

Поиск товара принимает часть названия, SKU, артикул, product_id или штрихкод. Дополнительно MCP умеет искать операции Seller API, возвращать их схемы и выполнять методы из [каталога чтения](ozon_seller/read_catalog.py). Каталог охватывает карточки, характеристики, изображения, цены, остатки, склады, отправления и начисления. Для вывода отправлений и складов сервер выбирает поля без контактов и адресов. Лимиты запроса и доступность конкретного метода зависят также от прав ключа Ozon.

Для личного аккаунта Plus есть отдельный [шаблон ChatGPT Sites](sites/ozon-seller/README.md). Он запускает те же пять инструментов в личном Site и обращается к Ozon напрямую. Начните с демоданных и проверьте вызов в обычном мобильном чате перед добавлением ключа.

## 1. Подготовьте VPS и ключ Ozon

На VPS понадобятся Git, Docker и доступ по HTTPS к Seller API и OpenAI. Для установки через Secure MCP Tunnel достаточно исходящих соединений. Отдельный домен и публичный HTTP-порт для MCP не нужны.

В Ozon Seller откройте **Настройки → Seller API** и создайте ключ на чтение с доступом к товарам, аналитике и возвратам. Запишите `Client ID` и ключ. Новые ключи действуют три месяца. [Авторизация Seller API](https://dev.ozon.ru/start/297-Kak-rabotaet-Seller-API-avtorizatsiia-nastroika-i-pervye-shagi/) · [Срок действия ключей](https://dev.ozon.ru/news/784-Izmeneniia-v-srokakh-deistviia-kliuchei-Seller-API/)

Проверьте доступность **Добавить собственный MCP-сервер** в веб-версии ChatGPT до настройки. OpenAI документирует чтение для Pro и доступ для Business и Enterprise/Edu с соответствующими разрешениями. Подключение произвольного MCP на Plus по этой инструкции не подтверждено. [Тарифы и разрешения](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

## 2. Запустите свой MCP

Выполните на VPS:

```bash
git clone https://github.com/pdr086/ozon-seller-mcp.git
cd ozon-seller-mcp
docker build -t ozon-seller-mcp:0.1.0 .
install -d -m 700 data
```

Введите свои данные в интерактивном терминале:

```bash
docker run --rm -it --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/data,target=/data" \
  ozon-seller-mcp:0.1.0 configure
```

Команда скрывает ввод, проверяет ключ в Ozon и сохраняет его в `data/credentials.json` с правами `0600`. Запустите сервер с этим каталогом на чтение:

```bash
docker run -d --name ozon-seller-mcp --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  --publish 127.0.0.1:8765:8765 \
  --mount "type=bind,source=$PWD/data,target=/data,readonly" \
  ozon-seller-mcp:0.1.0

docker exec ozon-seller-mcp python -m ozon_seller check
```

Ожидаемый результат — «Подключение к Ozon проверено». MCP доступен на самом VPS по `http://127.0.0.1:8765/mcp`. Для смены ключа повторите `configure`. Для другого часового пояса добавьте в команду запуска контейнера `--env OZON_TIMEZONE=Asia/Yekaterinburg`. По умолчанию используется `Europe/Moscow`.

## 3. Соедините VPS с ChatGPT

1. В [OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels) создайте туннель, сопоставьте его с тем же аккаунтом или рабочим пространством ChatGPT и получите отдельный runtime-ключ с правом использования туннеля.
2. Скачайте [официальный tunnel-client](https://github.com/openai/tunnel-client/releases/latest) для архитектуры VPS. На VPS подготовьте профиль. Вместо `tunnel_...` укажите идентификатор своего туннеля.

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

   Оставьте процесс запущенным для проверки. Для постоянной работы запускайте `tunnel-client` с этим профилем под диспетчером служб, используя закрытые настройки окружения. [Руководство OpenAI по туннелю](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
3. В веб-версии [ChatGPT → Плагины](https://chatgpt.com/plugins) выберите **Добавить → Добавить собственный MCP-сервер → Tunnel**. Укажите свой туннель. Если форма спрашивает тип авторизации, выберите **No authentication** для этого приватного туннеля. Создайте личный плагин и проверьте, что ChatGPT обнаружил пять инструментов. Ключ Ozon уже хранится на VPS.
4. Откройте новый обычный **Чат** на телефоне под тем же аккаунтом. Выберите установленный плагин через `@` и спросите «Проверь подключение к Ozon». Затем запросите данные магазина и сравните один результат с кабинетом продавца.

## Что спросить

| Пример | Что покажет сервер |
|---|---|
| «Покажи продажи за неделю» | Заказанные штуки и сумму заказов за семь полных дней. Начисления и выплаты показываются в других отчётах Ozon. |
| «Что заканчивается на FBS? Порог 3 штуки» | Остаток за вычетом резерва. Результат отмечает неполный охват каталога. |
| «Проанализируй возвраты за неделю» | Количество записей и штук, типы, частые причины и артикулы по дате возврата в логистике. |
| «Какая сейчас цена у артикула X?» | Цену продавца и валюту, которую вернул Ozon. |

Права и тариф Seller API могут ограничивать доступ к отдельным отчётам. Плагин сообщает об ошибке вместо пустого отчёта. API-ключ передаётся только официальному Seller API. ChatGPT получает выбранные поля и агрегаты.

## Где находится навык

[SKILL.md](skills/ozon-seller/SKILL.md) содержит правила вызова инструментов и объясняет значения показателей. Сервер передаёт этот текст при инициализации MCP. Для установки через туннель достаточно настроить свой MCP и создать личный плагин. Для клиента с отдельной поддержкой Agent Skills можно скачать репозиторий и взять весь каталог [skills/ozon-seller](skills/ozon-seller/).

`plugin.json` описывает пакет навыка. `mcp.json.example` показывает адрес MCP для клиента на том же VPS. Код сервера находится в [ozon_seller](ozon_seller/), инструкция по методам поддерживается в [документации Seller API](https://docs.ozon.ru/api/seller/).

[Лицензия MIT](LICENSE)
