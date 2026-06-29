"""
Проверка подписи Telegram Mini App (initData).

Алгоритм — официальный, описанный в документации Telegram:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

1. Из строки initData убираем поле hash, остальные сортируем по алфавиту
   и склеиваем в "data_check_string" вида key=value через \n.
2. secret_key = HMAC_SHA256(bot_token, "WebAppData")
3. Считаем HMAC_SHA256(data_check_string, secret_key) и сравниваем с hash.
4. Если совпало — данные подписаны настоящим Telegram и им можно доверять.

Дополнительно проверяем auth_date — если initData старше 24 часов,
считаем её просроченной (защита от replay-атак).
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# Сколько секунд считаем initData валидной с момента её выдачи Telegram-ом
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60  # 24 часа


def validate_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Проверяет подпись initData. Если подпись верна и данные не просрочены —
    возвращает словарь с данными пользователя (минимум: id).
    Если что-то не так — возвращает None.
    """
    try:
        # parse_qsl сохраняет порядок и не схлопывает повторяющиеся ключи —
        # для initData это безопаснее, чем parse_qs
        pairs = parse_qsl(init_data, keep_blank_values=True)
    except Exception:
        return None

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    # Шаг 1 — собираем data_check_string из всех полей кроме hash,
    # отсортированных по алфавиту
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(data.items())
    )

    # Шаг 2 — secret_key = HMAC_SHA256(bot_token, "WebAppData")
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()

    # Шаг 3 — считаем итоговый хэш и сравниваем
    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # compare_digest вместо "==" — защита от timing-атак при сравнении строк
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    # Шаг 4 — проверяем, что данные не протухли
    auth_date = data.get("auth_date")
    if auth_date is not None:
        try:
            age = time.time() - int(auth_date)
        except ValueError:
            return None
        if age > MAX_AUTH_AGE_SECONDS or age < 0:
            return None

    # user приходит как JSON-строка вида {"id":123,"first_name":"..."}
    user_raw = data.get("user")
    if not user_raw:
        return None

    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None

    if "id" not in user:
        return None

    return user
