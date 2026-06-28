"""
Проверка подлинности данных, присланных Telegram Mini App.

ЗАЧЕМ ЭТО НУЖНО:
Фронтенд (JS в браузере) теоретически может прислать СЕРВЕРУ любые данные —
включая "я пользователь с id=12345", даже если это неправда. Нельзя просто
верить тому, что говорит браузер.

Telegram решает это так: когда открывается Mini App, Telegram сам
формирует строку с данными пользователя и ПОДПИСЫВАЕТ её, используя
секретный токен твоего бота (который знает только Telegram и твой сервер,
но не знает обычный пользователь и не может его подделать).

Алгоритм (официальная документация Telegram, core.telegram.org/bots/webapps):
1. Взять initData, отделить параметр hash
2. Собрать оставшиеся пары "key=value", отсортировать по алфавиту,
   склеить через "\n"
3. Посчитать secret_key = HMAC-SHA256(bot_token, ключ="WebAppData")
4. Посчитать итоговый hash = HMAC-SHA256(data_check_string, ключ=secret_key)
5. Если он совпадает с присланным hash — данные подлинные, можно доверять
"""

import hmac
import hashlib
import json
from urllib.parse import parse_qsl


def validate_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Проверяет подпись init_data. Если всё совпадает — возвращает
    словарь с данными пользователя. Если подпись неверна — возвращает None.
    """
    try:
        # init_data приходит как строка вида "user=...&auth_date=...&hash=..."
        parsed = dict(parse_qsl(init_data))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        # Шаг 2 — собираем data-check-string
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(parsed.items())
        )

        # Шаг 3 — секретный ключ на основе токена бота
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=bot_token.encode(),
            digestmod=hashlib.sha256
        ).digest()

        # Шаг 4 — итоговый хеш
        calculated_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode(),
            digestmod=hashlib.sha256
        ).hexdigest()

        # Шаг 5 — сравнение
        if calculated_hash != received_hash:
            return None

        # Если подпись верна — достаём и возвращаем данные о пользователе
        user_json = parsed.get("user")
        if not user_json:
            return None

        return json.loads(user_json)

    except Exception:
        # Любая ошибка разбора (повреждённые данные, неверный формат) —
        # считаем данные недействительными, а не бросаем сервер в ошибку 500
        return None
