import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .ozon import SellerError


def config_path():
    return Path(os.environ.get("OZON_CONFIG_FILE", "~/.config/ozon-seller/credentials.json")).expanduser()


def credentials(client_id, api_key):
    if not isinstance(client_id, str) or not re.fullmatch(r"[0-9]{1,20}", client_id):
        raise SellerError("invalid_client_id", "Client ID должен содержать только цифры.")
    if not isinstance(api_key, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{12,256}", api_key):
        raise SellerError("invalid_api_key", "Проверьте формат API-ключа Ozon.")
    return {"client_id": client_id, "api_key": api_key}


def load(path):
    try:
        with path.open(encoding="utf-8") as source:
            if stat.S_IMODE(os.fstat(source.fileno()).st_mode) & 0o077:
                raise SellerError("unsafe_config_permissions", "Файл ключей должен быть доступен только владельцу (0600).")
            body = json.load(source)
    except FileNotFoundError:
        raise SellerError("store_not_configured", "Подключите магазин через настройку сервера Ozon MCP.") from None
    except (OSError, ValueError, UnicodeError):
        raise SellerError("invalid_config", "Сервер не смог прочитать файл настроек магазина.") from None
    if not isinstance(body, dict):
        raise SellerError("invalid_config", "Неверный формат настроек магазина.")
    return credentials(body.get("client_id"), body.get("api_key"))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
