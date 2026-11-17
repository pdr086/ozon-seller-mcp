import argparse
import getpass
import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .ozon import Seller, SellerError
from .server import create_server


def main():
    parser = argparse.ArgumentParser(description="Ozon Seller MCP на чтение")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", help="Ввести и проверить ключ Ozon")
    commands.add_parser("check", help="Проверить сохранённый ключ Ozon")
    serve = commands.add_parser("serve", help="Запустить MCP")
    serve.add_argument("--transport", choices=["streamable-http", "stdio"], default="streamable-http")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    path = config.config_path()
    zone_name = os.environ.get("OZON_TIMEZONE", "Europe/Moscow")
    try:
        ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        parser.error("OZON_TIMEZONE должен быть действующим часовым поясом IANA.")

    if args.command == "serve":
        server = create_server(path, zone_name)
        if args.transport == "stdio":
            server.run("stdio")
        else:
            import uvicorn
            from mcp.server.transport_security import TransportSecuritySettings

            loopback = ["127.0.0.1", "localhost", "[::1]"]
            security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=loopback + [f"{host}:*" for host in loopback],
                allowed_origins=[f"http://{host}{port}" for host in loopback for port in ("", ":*")],
            )
            app = server.streamable_http_app(
                json_response=True, stateless_http=True,
                max_request_body_size=65536, transport_security=security,
            )
            uvicorn.run(app, host=args.host, port=args.port, access_log=False, proxy_headers=False)
        return 0

    try:
        if args.command == "configure":
            if not sys.stdin.isatty():
                parser.error("Запустите configure в интерактивном терминале.")
            new = config.credentials(getpass.getpass("Ozon Client ID: ").strip(), getpass.getpass("Ozon API key: ").strip())
            with Seller(new, zone_name) as seller:
                seller.connection_status()
            config.save(path, new)
        else:
            with Seller(config.load(path), zone_name) as seller:
                seller.connection_status()
    except SellerError as error:
        print(error.payload["message"], file=sys.stderr)
        return 1
    except OSError:
        print("Ошибка доступа к файлу настроек.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        return 1
    print("Подключение к Ozon проверено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
