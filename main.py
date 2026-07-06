"""
Бэкенд журнала сделок v5 — с авторизацией через Telegram и PostgreSQL.

Каждый запрос должен содержать заголовок
"Authorization: tma <init_data>" — это подпись Telegram,
из которой сервер достаёт user_id и работает ТОЛЬКО с данными этого юзера.

Логика баланса на дашборде: сумма result_r (Risk Reward) по всем сделкам —
без долларов и без стартового депозита. /settings оставлен для совместимости,
но в расчётах дашборда больше не участвует.

Эндпоинты:
  GET  /settings             — устаревший эндпоинт (оставлен для совместимости)
  POST /settings             — устаревший эндпоинт (оставлен для совместимости)
  GET  /stats/summary        — сводка для дашборда (суммарный R, график R)
  GET  /trades               — список сделок с фильтрами (журнал)
  GET  /trades/years         — список лет, в которых есть сделки
  GET  /trades/symbols       — список уникальных тикеров
  GET  /trades/{trade_id}    — одна сделка (деталь)
  PATCH /trades/{trade_id}   — обновить заметку/теги
  DELETE /trades/{trade_id}  — удалить сделку
  POST /trades               — добавить сделку вручную
  GET  /stats/by-tag         — PnL по тегам/сетапам (аналитика)
  GET  /tags                 — список всех тегов пользователя
  DELETE /account/data       — удалить все сделки пользователя

Как запустить локально:
   pip install fastapi uvicorn sqlalchemy psycopg2-binary --break-system-packages
   (нужен установленный и запущенный PostgreSQL)
   uvicorn main:app --reload
"""

import os
import json
import secrets
from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel

from database import engine, get_db
from models import Base, Trade, UserSettings, FavoriteSymbol, ShareLink, User, TradeAttachment
from telegram_auth import validate_telegram_init_data

# Создаём таблицы в базе при первом запуске (если их ещё нет)
Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Токен твоего бота — без него невозможно проверить подпись.
# В реальном проекте это переменная окружения, никогда не пиши токен в код напрямую.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_СВОЕГО_БОТА")


# ---------- Pydantic-схемы (формат входных/выходных данных) ----------

class NewTrade(BaseModel):
    asset: str
    direction: Literal["long", "short"]
    trade_date: Optional[datetime] = None
    result_r: Optional[float] = None
    outcome: Optional[Literal["win", "loss", "breakeven"]] = None
    note: Optional[str] = None
    tags: Optional[list[str]] = None
    source: Literal["manual", "auto"] = "manual"

    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    symbol: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    size: Optional[float] = None
    leverage: Optional[float] = None
    risk_percent: Optional[float] = None
    risk_amount: Optional[float] = None
    risk_type: Optional[str] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class UpdateTrade(BaseModel):
    """Для PATCH — все поля опциональны, обновляются только те что переданы."""
    note: Optional[str] = None
    tags: Optional[list[str]] = None
    symbol: Optional[str] = None
    direction: Optional[str] = None
    outcome: Optional[str] = None
    result_r: Optional[float] = None
    trade_date: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    size: Optional[float] = None
    leverage: Optional[float] = None
    risk_amount: Optional[float] = None
    risk_type: Optional[str] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None


class SettingsUpdate(BaseModel):
    starting_balance: float


# ---------- Авторизация ----------

def get_current_user_id(authorization: str = Header(...)) -> int:
    """
    Эта функция вызывается перед КАЖДЫМ запросом, который требует авторизации.
    Она достаёт init_data из заголовка, проверяет подпись, и если всё ок —
    возвращает user_id. Если подпись неверна — сразу прерывает запрос ошибкой 401.
    """
    if not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Неверный формат авторизации")

    init_data = authorization[4:]  # убираем префикс "tma "
    user_data = validate_telegram_init_data(init_data, BOT_TOKEN)

    if user_data is None:
        raise HTTPException(status_code=401, detail="Подпись Telegram недействительна")

    return user_data["id"]


def get_current_user_data(authorization: str = Header(...)) -> dict:
    """Возвращает полный user_data из Telegram initData."""
    if not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Неверный формат авторизации")
    init_data = authorization[4:]
    user_data = validate_telegram_init_data(init_data, BOT_TOKEN)
    if user_data is None:
        raise HTTPException(status_code=401, detail="Подпись Telegram недействительна")
    return user_data


def upsert_user(user_data: dict, db: Session):
    """Создаёт запись пользователя при первом входе или обновляет last_seen_at."""
    uid = user_data.get("id")
    if not uid:
        return
    user = db.query(User).filter(User.user_id == uid).first()
    if user:
        user.last_seen_at = datetime.utcnow()
        user.visits_count = (user.visits_count or 0) + 1
        # Обновляем имя если изменилось
        user.first_name = user_data.get("first_name") or user.first_name
        user.last_name = user_data.get("last_name") or user.last_name
        user.username = user_data.get("username") or user.username
    else:
        user = User(
            user_id=uid,
            first_name=user_data.get("first_name"),
            last_name=user_data.get("last_name"),
            username=user_data.get("username"),
            language_code=user_data.get("language_code"),
            is_premium=bool(user_data.get("is_premium", False)),
        )
        db.add(user)
    db.commit()


# ---------- Настройки пользователя (стартовый баланс) ----------

def get_or_create_settings(user_id: int, db: Session) -> UserSettings:
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        settings = UserSettings(user_id=user_id, starting_balance=0.0)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


# ---------- Расчёт статистики (баланс счёта в $, % прибыли) ----------

def calculate_stats(trades: list[Trade]) -> dict:
    """
    Главная логика дашборда (без долларов и без стартового баланса):
    - total_r_balance = сумма result_r всех сделок — это и есть "баланс" на дашборде
    - r_curve = накопительная сумма R по сделкам в хронологическом порядке (для графика)
    - winrate считается по полю outcome (win/loss/breakeven), а не по сумме $;
      сделки без outcome не участвуют в винрейте, но участвуют в сумме R, если у них есть result_r.
    """
    if not trades:
        return {
            "total_trades": 0, "winrate": 0, "profit_factor": 0,
            "total_r": 0, "current_streak": 0,
            "r_curve": [0],
        }

    trades_with_outcome = [t for t in trades if t.outcome is not None]
    wins = [t for t in trades_with_outcome if t.outcome == "win"]
    losses = [t for t in trades_with_outcome if t.outcome == "loss"]

    winrate = round(len(wins) / len(trades_with_outcome) * 100, 1) if trades_with_outcome else 0

    # Profit factor через R: сумма положительных R / |сумма отрицательных R|
    positive_r = sum(t.result_r for t in trades if t.result_r is not None and t.result_r > 0)
    negative_r = abs(sum(t.result_r for t in trades if t.result_r is not None and t.result_r < 0))
    profit_factor = round(positive_r / negative_r, 2) if negative_r > 0 else float(positive_r > 0)

    total_r = round(sum(t.result_r or 0 for t in trades), 2)

    streak = 0
    if trades_with_outcome:
        last_was_win = trades_with_outcome[-1].outcome == "win"
        for t in reversed(trades_with_outcome):
            if t.outcome == "breakeven":
                break
            if (t.outcome == "win") == last_was_win:
                streak += 1 if last_was_win else -1
            else:
                break

    # График R — накопительная сумма по сделкам в хронологическом порядке
    # Если result_r не заполнен (авто-синхронизация без риска) — используем pnl_usd
    r_curve = [0]
    running = 0
    for t in trades:
        running += t.result_r if t.result_r is not None else (t.pnl_usd or 0)
        r_curve.append(round(running, 2))

    return {
        "total_trades": len(trades), "winrate": winrate,
        "profit_factor": profit_factor, "total_r": total_r,
        "current_streak": streak,
        "r_curve": r_curve,
    }


def trade_to_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "asset": t.asset,
        "symbol": t.symbol or t.asset,
        "direction": t.direction,
        "risk_percent": t.risk_percent,
        "risk_amount": t.risk_amount,
        "risk_type": t.risk_type,
        "result_r": t.result_r,
        "outcome": t.outcome,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "size": t.size,
        "leverage": t.leverage,
        "pnl_usd": t.pnl_usd,
        "pnl_pct": t.pnl_pct,
        "source": t.source,
        "note": t.note,
        "tags": t.tags or [],
        "trade_date": t.trade_date.strftime("%d.%m.%Y %H:%M") if t.trade_date else None,
        "opened_at": t.opened_at.strftime("%d.%m.%Y %H:%M") if t.opened_at else None,
        "closed_at": t.closed_at.strftime("%d.%m.%Y %H:%M") if t.closed_at else None,
        "created_at": t.created_at.strftime("%d.%m.%Y %H:%M") if t.created_at else None,
    }


def sort_key(t: Trade):
    """Сортируем по дате сделки, если она указана пользователем,
    иначе по дате создания записи — это и определяет порядок графика баланса."""
    return t.trade_date or t.created_at or datetime.min


# ---------- Настройки (стартовый баланс) ----------

@app.get("/settings")
def get_settings(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    settings = get_or_create_settings(user_id, db)
    return {"starting_balance": settings.starting_balance}


@app.post("/settings")
def update_settings(
    update: SettingsUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    settings = get_or_create_settings(user_id, db)
    settings.starting_balance = update.starting_balance
    db.commit()
    db.refresh(settings)
    return {"status": "ok", "starting_balance": settings.starting_balance}


# ---------- Дашборд ----------

@app.get("/stats/summary")
def get_summary(
    user_id: int = Depends(get_current_user_id),
    user_data: dict = Depends(get_current_user_data),
    db: Session = Depends(get_db),
    year: Optional[int] = None,
    month: Optional[int] = None,
    result: Optional[Literal["win", "loss", "breakeven"]] = None,
):
    """Сводка для главного экрана. Заодно фиксирует визит пользователя."""
    upsert_user(user_data, db)
    query = db.query(Trade).filter(Trade.user_id == user_id)

    if year:
        query = query.filter(func.extract('year', Trade.trade_date) == year)
    if month and year:
        query = query.filter(func.extract('month', Trade.trade_date) == month)
    if result:
        query = query.filter(Trade.outcome == result)

    trades = sorted(query.all(), key=sort_key)
    stats = calculate_stats(trades)

    # recent_trades только для общего вида (без фильтров)
    if not year and not month and not result:
        recent = [trade_to_dict(t) for t in reversed(trades[-5:])]
    else:
        recent = []

    return {"stats": stats, "recent_trades": recent}


# ---------- Журнал сделок (с фильтрами) ----------

@app.get("/trades")
def get_trades(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    result: Optional[Literal["win", "loss", "breakeven"]] = None,
    source: Optional[Literal["manual", "auto"]] = None,
    symbol: Optional[str] = None,
    tag: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,  # 1-12, имеет смысл только вместе с year
):
    """Список сделок с опциональными фильтрами — под экран Журнала
    (вкладки: Общая / По месяцам / По годам / По винрейту / По тикерам
    все реализованы через эти же query-параметры)."""
    query = db.query(Trade).filter(Trade.user_id == user_id)

    if result:
        query = query.filter(Trade.outcome == result)
    if source:
        query = query.filter(Trade.source == source)
    if symbol:
        query = query.filter(Trade.symbol == symbol)

    trades = query.all()
    trades = sorted(trades, key=sort_key)

    # Фильтр по тегу — отдельно, потому что tags хранится как JSON-массив
    if tag:
        trades = [t for t in trades if t.tags and tag in t.tags]

    # Фильтр по году/месяцу — берём дату сделки (trade_date, либо created_at,
    # та же логика, что и в sort_key, чтобы фильтр и сортировка были согласованы)
    if year is not None:
        trades = [t for t in trades if sort_key(t).year == year]
    if month is not None:
        trades = [t for t in trades if sort_key(t).month == month]

    stats = calculate_stats(trades)

    return {
        "trades": [trade_to_dict(t) for t in reversed(trades)],
        "stats": stats,
    }


@app.get("/trades/years")
def get_available_years(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Список лет, в которых есть хотя бы одна сделка — для вкладки 'По годам'."""
    trades = db.query(Trade).filter(Trade.user_id == user_id).all()
    years = sorted({sort_key(t).year for t in trades}, reverse=True)
    return {"years": years}


@app.get("/trades/symbols")
def get_available_symbols(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Список уникальных тикеров, по которым есть сделки — для вкладки 'По тикерам'."""
    trades = db.query(Trade).filter(Trade.user_id == user_id).all()
    symbols = sorted({t.symbol or t.asset for t in trades})
    return {"symbols": symbols}


@app.post("/trades")
def add_trade(
    new_trade: NewTrade,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    trade = Trade(
        user_id=user_id,  # привязываем сделку именно к этому пользователю
        asset=new_trade.asset,
        direction=new_trade.direction,
        result_r=new_trade.result_r,
        outcome=new_trade.outcome,
        pnl_usd=new_trade.pnl_usd,
        pnl_pct=new_trade.pnl_pct,
        note=new_trade.note,
        trade_date=new_trade.trade_date or datetime.utcnow(),  # дата обязательна — если не передана, берём "сегодня"
        tags=new_trade.tags or [],
        source=new_trade.source,
        symbol=new_trade.symbol or new_trade.asset,
        entry_price=new_trade.entry_price,
        exit_price=new_trade.exit_price,
        size=new_trade.size,
        leverage=new_trade.leverage,
        risk_amount=new_trade.risk_amount,
        risk_type=new_trade.risk_type,
        risk_percent=new_trade.risk_percent,
        opened_at=new_trade.opened_at,
        closed_at=new_trade.closed_at,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return {"status": "ok", "trade": trade_to_dict(trade)}


# ---------- Деталь сделки ----------

@app.get("/trades/{trade_id}")
def get_trade(
    trade_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    trade = db.query(Trade).filter(
        Trade.id == trade_id, Trade.user_id == user_id
    ).first()

    if not trade:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    return trade_to_dict(trade)


@app.patch("/trades/{trade_id}")
def update_trade(
    trade_id: int,
    update: UpdateTrade,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Обновление заметки/тегов — основной кейс для авто-импортированных сделок,
    куда юзер дописывает контекст вручную."""
    trade = db.query(Trade).filter(
        Trade.id == trade_id, Trade.user_id == user_id
    ).first()

    if not trade:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    if update.note is not None:
        trade.note = update.note
    if update.tags is not None:
        trade.tags = update.tags
    if update.symbol is not None:
        trade.symbol = update.symbol
    if update.direction is not None:
        trade.direction = update.direction
    if update.outcome is not None:
        trade.outcome = update.outcome
    if update.result_r is not None:
        trade.result_r = update.result_r
    if update.trade_date is not None:
        trade.trade_date = update.trade_date
    if update.entry_price is not None:
        trade.entry_price = update.entry_price
    if update.exit_price is not None:
        trade.exit_price = update.exit_price
    if update.size is not None:
        trade.size = update.size
    if update.leverage is not None:
        trade.leverage = update.leverage
    if update.risk_amount is not None:
        trade.risk_amount = update.risk_amount
    if update.risk_type is not None:
        trade.risk_type = update.risk_type
    if update.pnl_usd is not None:
        trade.pnl_usd = update.pnl_usd
    if update.pnl_pct is not None:
        trade.pnl_pct = update.pnl_pct

    db.commit()
    db.refresh(trade)
    return {"status": "ok", "trade": trade_to_dict(trade)}


@app.delete("/trades/{trade_id}")
def delete_trade(
    trade_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    trade = db.query(Trade).filter(
        Trade.id == trade_id, Trade.user_id == user_id
    ).first()

    if not trade:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    db.delete(trade)
    db.commit()
    return {"status": "ok"}


# ---------- Аналитика ----------

@app.get("/stats/by-tag")
def stats_by_tag(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Суммарный R по тегам (сетап/сессия) — под экран Аналитики.
    Считаем в Python, а не в SQL, потому что tags — JSON-массив
    и одна сделка может относиться к нескольким тегам сразу."""
    trades = db.query(Trade).filter(Trade.user_id == user_id).all()

    by_tag: dict[str, dict] = {}
    for t in trades:
        for tag in (t.tags or []):
            if tag not in by_tag:
                by_tag[tag] = {"tag": tag, "total_r": 0, "count": 0}
            by_tag[tag]["total_r"] += t.result_r or 0
            by_tag[tag]["count"] += 1

    result = sorted(by_tag.values(), key=lambda x: x["total_r"], reverse=True)
    for r in result:
        r["total_r"] = round(r["total_r"], 2)

    return {"by_tag": result}


# ---------- Теги ----------

@app.get("/tags")
def get_tags(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Список всех уникальных тегов, которые юзер когда-либо использовал —
    для автокомплита на экране деталей сделки."""
    trades = db.query(Trade).filter(Trade.user_id == user_id).all()

    unique_tags = set()
    for t in trades:
        for tag in (t.tags or []):
            unique_tags.add(tag)

    return {"tags": sorted(unique_tags)}


# ---------- Избранные тикеры ----------

DEFAULT_FAVORITE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "XAUUSD", "EURUSD", "GBPUSD"]


@app.get("/favorites")
def get_favorites(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Список избранных тикеров для выпадающего списка "Актив".
    Если у юзера ещё нет ни одного избранного — отдаём дефолтный набор
    (но НЕ сохраняем его в базу, пока юзер сам что-то не добавит/уберёт —
    иначе у каждого нового юзера будет 5 "призрачных" записей в БД)."""
    favorites = db.query(FavoriteSymbol).filter(FavoriteSymbol.user_id == user_id).order_by(FavoriteSymbol.created_at).all()

    if not favorites:
        return {"symbols": DEFAULT_FAVORITE_SYMBOLS, "is_default": True}

    return {"symbols": [f.symbol for f in favorites], "is_default": False}


@app.post("/favorites/{symbol}")
def add_favorite(
    symbol: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Добавить тикер в избранное. Если у юзера до этого был только
    дефолтный набор (ничего не сохранено в БД) — сначала материализуем
    дефолтный набор в базу, чтобы новый тикер встал рядом с ним, а не заменил его."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Тикер не может быть пустым")

    existing = db.query(FavoriteSymbol).filter(FavoriteSymbol.user_id == user_id).all()

    if not existing:
        # Материализуем дефолтный набор, чтобы он не потерялся
        for default_symbol in DEFAULT_FAVORITE_SYMBOLS:
            db.add(FavoriteSymbol(user_id=user_id, symbol=default_symbol))
        existing_symbols = set(DEFAULT_FAVORITE_SYMBOLS)
    else:
        existing_symbols = {f.symbol for f in existing}

    if symbol not in existing_symbols:
        db.add(FavoriteSymbol(user_id=user_id, symbol=symbol))

    db.commit()

    favorites = db.query(FavoriteSymbol).filter(FavoriteSymbol.user_id == user_id).order_by(FavoriteSymbol.created_at).all()
    return {"symbols": [f.symbol for f in favorites]}


@app.delete("/favorites/{symbol}")
def remove_favorite(
    symbol: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Убрать тикер из избранного. Если у юзера был только дефолтный набор —
    материализуем его минус удаляемый тикер, чтобы убрать именно этот тикер,
    а не просто остаться с пустым избранным (что вернуло бы дефолт снова)."""
    symbol = symbol.strip().upper()

    existing = db.query(FavoriteSymbol).filter(FavoriteSymbol.user_id == user_id).all()

    if not existing:
        for default_symbol in DEFAULT_FAVORITE_SYMBOLS:
            if default_symbol != symbol:
                db.add(FavoriteSymbol(user_id=user_id, symbol=default_symbol))
    else:
        db.query(FavoriteSymbol).filter(
            FavoriteSymbol.user_id == user_id, FavoriteSymbol.symbol == symbol
        ).delete()

    db.commit()

    favorites = db.query(FavoriteSymbol).filter(FavoriteSymbol.user_id == user_id).order_by(FavoriteSymbol.created_at).all()
    return {"symbols": [f.symbol for f in favorites]}


# ---------- Удаление всех данных ----------

@app.delete("/account/data")
def delete_all_data(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Полный сброс: удаляет ВСЕ сделки, избранные тикеры пользователя
    и сбрасывает стартовый баланс обратно в 0. Безвозвратно — подтверждение
    должно быть запрошено на фронтенде ДО вызова этого эндпоинта."""
    deleted_trades = db.query(Trade).filter(Trade.user_id == user_id).delete()
    db.query(FavoriteSymbol).filter(FavoriteSymbol.user_id == user_id).delete()

    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if settings:
        settings.starting_balance = 0.0

    db.commit()
    return {"status": "ok", "deleted_trades": deleted_trades}


# ---------- Шаринг журнала (публичные read-only эндпоинты) ----------

@app.post("/share/generate")
def generate_share_link(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Создаёт или возвращает существующий публичный токен пользователя."""
    link = db.query(ShareLink).filter(ShareLink.user_id == user_id).first()
    if link:
        link.is_active = True
        db.commit()
        db.refresh(link)
    else:
        token = secrets.token_urlsafe(16)
        link = ShareLink(user_id=user_id, token=token, is_active=True)
        db.add(link)
        db.commit()
        db.refresh(link)
    return {"token": link.token, "is_active": link.is_active}


@app.delete("/share")
def revoke_share_link(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Отзывает публичную ссылку — она перестаёт работать."""
    link = db.query(ShareLink).filter(ShareLink.user_id == user_id).first()
    if link:
        link.is_active = False
        db.commit()
    return {"status": "ok"}


@app.get("/share/status")
def get_share_status(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Возвращает текущий статус публичной ссылки пользователя."""
    link = db.query(ShareLink).filter(ShareLink.user_id == user_id).first()
    if not link:
        return {"token": None, "is_active": False}
    return {"token": link.token if link.is_active else None, "is_active": link.is_active}


@app.get("/public/{token}/stats")
def public_stats(token: str, db: Session = Depends(get_db)):
    """Публичная статистика по токену — без авторизации."""
    link = db.query(ShareLink).filter(
        ShareLink.token == token, ShareLink.is_active == True
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена или отозвана")

    trades = db.query(Trade).filter(Trade.user_id == link.user_id).order_by(Trade.trade_date).all()
    stats = calculate_stats(trades)
    return {"stats": stats}


@app.get("/public/{token}/trades")
def public_trades(token: str, db: Session = Depends(get_db)):
    """Публичный список сделок по токену — без авторизации."""
    link = db.query(ShareLink).filter(
        ShareLink.token == token, ShareLink.is_active == True
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Ссылка не найдена или отозвана")

    trades = db.query(Trade).filter(Trade.user_id == link.user_id)\
        .order_by(Trade.trade_date.desc()).all()
    return {"trades": [trade_to_dict(t) for t in trades]}


@app.get("/s/{token}")
def share_page(token: str, db: Session = Depends(get_db)):
    """Публичная HTML-страница журнала — отдаётся напрямую браузеру."""

    link = db.query(ShareLink).filter(
        ShareLink.token == token, ShareLink.is_active == True
    ).first()

    if not link:
        html = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Журнал не найден</title>
<style>body{background:#0D1117;color:#8B949E;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center}h1{color:#E6EDF3;font-size:24px;margin-bottom:8px}p{font-size:14px}</style>
</head><body><div><h1>🔗 Ссылка не найдена</h1><p>Возможно, владелец отозвал доступ к журналу.</p></div></body></html>"""
        return HTMLResponse(content=html, status_code=404)

    trades_raw = db.query(Trade).filter(Trade.user_id == link.user_id)\
        .order_by(Trade.trade_date.asc()).all()
    all_trades = [trade_to_dict(t) for t in trades_raw]
    stats = calculate_stats(trades_raw)

    import json
    trades_json = json.dumps(all_trades, ensure_ascii=False)
    stats_json  = json.dumps(stats,       ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Журнал трейдера</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
:root{{
  --bg:#0D1117;--surface:#161B22;--surface-2:#1C2128;
  --border:rgba(255,255,255,0.08);--border-soft:rgba(255,255,255,0.04);
  --text:#E6EDF3;--text-secondary:#8B949E;--text-muted:#6E7681;--text-faint:#484F58;
  --success:#3DDC97;--success-bg:rgba(61,220,151,0.12);
  --danger:#E5534B;--danger-bg:rgba(229,83,75,0.12);
  --accent:#58A6FF;--accent-bg:rgba(88,166,255,0.12);
  --warning:#D29922;--warning-bg:rgba(210,153,34,0.12);
  --font:'Inter',-apple-system,sans-serif;
  --mono:'JetBrains Mono',monospace;
  --radius:12px;--radius-sm:8px;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased;min-height:100vh}}
/* Layout */
.wrap{{max-width:640px;margin:0 auto;padding:0 16px}}
/* Header */
.hdr{{padding:28px 0 20px}}
.hdr-badge{{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted);margin-bottom:12px}}
.hdr-badge::before{{content:'';width:6px;height:6px;border-radius:50%;background:var(--success);box-shadow:0 0 6px var(--success);flex-shrink:0}}
.hdr h1{{font-size:26px;font-weight:700;letter-spacing:-.02em;margin-bottom:3px}}
.hdr-sub{{font-size:13px;color:var(--text-muted)}}
/* Stats card */
.stats-card{{background:linear-gradient(135deg,#141920 0%,#0f1419 100%);border:1px solid var(--border);border-radius:var(--radius);padding:18px;margin-bottom:14px}}
.stats-top{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:4px}}
.stats-card-label{{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:rgba(255,255,255,.4)}}
.stats-pnl{{font-family:var(--mono);font-size:38px;font-weight:600;letter-spacing:-.02em;line-height:1;margin-bottom:16px}}
.stats-pnl.positive{{color:var(--success)}}.stats-pnl.negative{{color:var(--danger)}}.stats-pnl.neutral{{color:var(--text-muted)}}
canvas#chart{{width:100%;height:60px;display:block;margin-bottom:16px}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;border-top:1px solid var(--border);padding-top:14px}}
.metric-label{{font-size:11px;color:rgba(255,255,255,.4);margin-bottom:3px}}
.metric-value{{font-family:var(--mono);font-size:17px;font-weight:600;color:rgba(255,255,255,.9)}}
/* Mode switch */
.mode-switch{{display:flex;background:rgba(255,255,255,.07);border-radius:6px;padding:2px;gap:2px;flex-shrink:0}}
.mode-btn{{font-size:11px;font-weight:600;padding:3px 9px;border:none;background:none;color:rgba(255,255,255,.35);border-radius:4px;cursor:pointer;font-family:var(--font)}}
.mode-btn.active{{background:rgba(255,255,255,.13);color:rgba(255,255,255,.9)}}
/* Risk row */
.risk-row{{display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:14px}}
.risk-row span{{font-size:12px;color:var(--text-secondary);flex:1}}
.risk-field{{display:flex;align-items:center;gap:4px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:5px 8px}}
.risk-cur{{font-size:12px;color:var(--text-muted);font-family:var(--mono)}}
.risk-field input{{width:60px;border:none;background:none;font-family:var(--mono);font-size:13px;color:var(--text);outline:none;text-align:right}}
.risk-row-hidden{{display:none}}
/* Tabs */
.tabs{{display:flex;gap:0;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--border);margin-bottom:10px}}
.tabs::-webkit-scrollbar{{display:none}}
.tab{{font-size:13px;font-weight:500;padding:8px 4px;margin-right:16px;border:none;background:none;color:var(--text-muted);white-space:nowrap;cursor:pointer;font-family:var(--font);border-bottom:2px solid transparent;position:relative;top:1px;flex-shrink:0}}
.tab.active{{color:var(--text);border-bottom-color:var(--accent);font-weight:600}}
/* Subfilter */
.subfilter{{margin-bottom:10px;display:none}}
.subfilter.visible{{display:block}}
.subfilter select{{width:100%;padding:9px 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:14px;font-family:var(--font);outline:none}}
/* Summary row */
.summary-row{{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-secondary);margin-bottom:8px;flex-wrap:wrap}}
.summary-dot{{color:var(--text-faint)}}
.summary-rr{{font-weight:600;font-family:var(--mono)}}
.summary-rr.positive{{color:var(--success)}}.summary-rr.negative{{color:var(--danger)}}
/* Table */
.trades-table{{width:100%;border-collapse:collapse;font-size:13px}}
.trades-table th{{text-align:left;font-size:10px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;padding:0 0 9px;border-bottom:1px solid var(--border);white-space:nowrap}}
.trades-table th+th{{padding-left:10px}}
.trades-table td{{padding:10px 0;border-bottom:1px solid var(--border-soft);vertical-align:top}}
.trades-table td+td{{padding-left:10px}}
.trades-table tbody tr{{cursor:pointer;transition:background .1s}}
.trades-table tbody tr:hover td{{background:rgba(255,255,255,.025)}}
.trades-table tbody tr:last-child td{{border-bottom:none}}
.cell-date{{font-family:var(--mono);font-size:11px;color:var(--text-secondary);white-space:nowrap}}
.cell-symbol{{font-weight:600;white-space:nowrap}}
.dir-badge{{font-size:11px;font-weight:600;padding:2px 7px;border-radius:999px;display:inline-block;white-space:nowrap}}
.dir-badge.long{{background:var(--success-bg);color:var(--success)}}.dir-badge.short{{background:var(--danger-bg);color:var(--danger)}}
.cell-r{{font-family:var(--mono);font-weight:600;white-space:nowrap}}
.cell-r.positive{{color:var(--success)}}.cell-r.negative{{color:var(--danger)}}
.out-badge{{font-size:11px;font-weight:600;padding:2px 7px;border-radius:999px;display:inline-flex;align-items:center;gap:4px;white-space:nowrap}}
.out-badge::before{{content:'●';font-size:7px}}
.out-badge.win{{background:var(--success-bg);color:var(--success)}}.out-badge.loss{{background:var(--danger-bg);color:var(--danger)}}.out-badge.breakeven{{background:rgba(255,255,255,.06);color:var(--text-muted)}}
/* Detail expand */
.detail-inner{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:11px 13px;margin:2px 0 4px;display:grid;grid-template-columns:1fr 1fr;gap:8px 16px}}
.di-label{{font-size:10px;color:var(--text-muted);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em}}
.di-value{{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--text)}}
.trade-note{{font-size:12px;color:var(--text-secondary);background:var(--surface-2);border-left:2px solid var(--border);padding:8px 12px;border-radius:0 var(--radius-sm) var(--radius-sm) 0;margin-top:6px;line-height:1.5;white-space:pre-wrap}}
/* Empty */
.empty{{text-align:center;padding:48px 0;color:var(--text-muted)}}
.empty-icon{{font-size:32px;margin-bottom:10px}}
.empty-title{{font-size:15px;font-weight:600;color:var(--text-secondary);margin-bottom:4px}}
/* Footer */
.footer{{text-align:center;padding:24px 0 36px;font-size:12px;color:var(--text-muted)}}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="hdr-badge">Публичный журнал</div>
    <h1>Журнал трейдера</h1>
    <div class="hdr-sub" id="hdr-sub">—</div>
  </div>

  <div class="stats-card">
    <div class="stats-top">
      <div>
        <div class="stats-card-label" id="pnl-label">Суммарный R</div>
        <div class="stats-pnl neutral" id="stat-pnl">—</div>
      </div>
      <div class="mode-switch">
        <button class="mode-btn active" data-mode="r">R</button>
        <button class="mode-btn" data-mode="usd">$</button>
        <button class="mode-btn" data-mode="pct">%</button>
      </div>
    </div>
    <canvas id="chart"></canvas>
    <div class="metrics">
      <div><div class="metric-label">Винрейт</div><div class="metric-value" id="stat-wr">—</div></div>
      <div><div class="metric-label">Profit factor</div><div class="metric-value" id="stat-pf">—</div></div>
      <div><div class="metric-label">Сделок</div><div class="metric-value" id="stat-cnt">—</div></div>
    </div>
  </div>

  <div class="risk-row" id="risk-row">
    <span>Риск на сделку (для расчёта $ и %)</span>
    <div class="risk-field">
      <span class="risk-cur">$</span>
      <input type="number" id="risk-usd" placeholder="50" step="1" min="1">
    </div>
    <div class="risk-field">
      <span class="risk-cur">%</span>
      <input type="number" id="risk-pct" placeholder="0.25" step="0.01" min="0.01">
    </div>
  </div>

  <div class="tabs" id="tabs">
    <button class="tab active" data-view="all">Общая</button>
    <button class="tab" data-view="month">По месяцам</button>
    <button class="tab" data-view="year">По годам</button>
    <button class="tab" data-view="winrate">По итогу</button>
  </div>

  <div class="subfilter" id="subfilter">
    <select id="subfilter-select"></select>
  </div>

  <div class="summary-row">
    <span id="sum-count">0 сделок</span>
    <span class="summary-dot">·</span>
    <span id="sum-wr">винрейт —</span>
    <span class="summary-dot">·</span>
    <span class="summary-rr" id="sum-rr">R —</span>
  </div>

  <div id="trades-wrap">
    <div class="empty"><div class="empty-icon">⏳</div><div class="empty-title">Загрузка...</div></div>
  </div>
</div>

<div class="footer">Журнал трейдера · PnL Tracker</div>

<script>
const ALL_TRADES = {trades_json};
const INIT_STATS = {stats_json};

let pnlMode = 'r';
let view = 'all';
let subfilter = '';
let riskUsd = 0, riskPct = 0;

/* ── Утилиты ── */
function fmtR(v){{
  if(v===null||v===undefined)return'—';
  return(v>0?'+':'')+v.toFixed(1)+'R';
}}
function fmtPnl(v){{
  if(v===null||v===undefined)return'—';
  if(pnlMode==='usd'){{
    if(!riskUsd)return fmtR(v);
    const u=v*riskUsd; return(u>0?'+':'')+u.toFixed(0)+'$';
  }}
  if(pnlMode==='pct'){{
    if(!riskPct)return fmtR(v);
    const p=v*riskPct; return(p>0?'+':'')+p.toFixed(2)+'%';
  }}
  return fmtR(v);
}}
function pnlClass(v){{return v>0?'positive':v<0?'negative':'neutral'}}
function outcomeLabel(o){{return o==='win'?'прибыль':o==='loss'?'убыток':o==='breakeven'?'безубыток':'—'}}
function tradesWord(n){{
  const m10=n%10,m100=n%100;
  if(m10===1&&m100!==11)return'сделка';
  if([2,3,4].includes(m10)&&![12,13,14].includes(m100))return'сделки';
  return'сделок';
}}

/* ── Фильтрация ── */
function getMonthKey(t){{
  const d=t.trade_date||t.created_at||'';
  const m=d.match(/([0-9]{{4}})[.]?([0-9]{{2}})/);
  return m?m[1]+'-'+m[2]:null;
}}
function getYearKey(t){{
  const d=t.trade_date||t.created_at||'';
  const m=d.match(/([0-9]{{4}})/);
  return m?m[1]:null;
}}
function filterTrades(){{
  let rows=[...ALL_TRADES].reverse(); // desc order
  if(view==='month'&&subfilter) rows=rows.filter(t=>getMonthKey(t)===subfilter);
  if(view==='year'&&subfilter)  rows=rows.filter(t=>getYearKey(t)===subfilter);
  if(view==='winrate'&&subfilter) rows=rows.filter(t=>t.outcome===subfilter);
  return rows;
}}

/* ── Статистика по набору строк ── */
function calcStats(rows){{
  const total=rows.length;
  const decided=rows.filter(t=>t.outcome==='win'||t.outcome==='loss');
  const wins=decided.filter(t=>t.outcome==='win').length;
  const wr=decided.length?Math.round(wins/decided.length*100):null;
  const totalR=rows.reduce((s,t)=>s+(t.result_r||0),0);
  return{{total,wr,totalR}};
}}

/* ── График ── */
function drawChart(values){{
  const canvas=document.getElementById('chart');
  const ctx=canvas.getContext('2d');
  const dpr=window.devicePixelRatio||1;
  const rect=canvas.getBoundingClientRect();
  canvas.width=rect.width*dpr; canvas.height=rect.height*dpr;
  ctx.scale(dpr,dpr);
  if(!values||values.length<2){{
    ctx.strokeStyle='#262C36';ctx.lineWidth=1.5;ctx.setLineDash([4,4]);
    ctx.beginPath();ctx.moveTo(0,rect.height/2);ctx.lineTo(rect.width,rect.height/2);ctx.stroke();return;
  }}
  const min=Math.min(...values,0),max=Math.max(...values,0),range=max-min||1;
  const pos=values[values.length-1]>=0;
  const lc=pos?'#3DDC97':'#E5534B',fa=pos?'rgba(61,220,151,.22)':'rgba(229,83,75,.22)',fb=pos?'rgba(61,220,151,0)':'rgba(229,83,75,0)';
  const pts=values.map((v,i)=>{{return{{x:(i/(values.length-1))*rect.width,y:rect.height-((v-min)/range)*rect.height}}}});
  const g=ctx.createLinearGradient(0,0,0,rect.height);g.addColorStop(0,fa);g.addColorStop(1,fb);
  ctx.beginPath();pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
  ctx.lineTo(pts[pts.length-1].x,rect.height);ctx.lineTo(pts[0].x,rect.height);ctx.closePath();
  ctx.fillStyle=g;ctx.fill();
  ctx.strokeStyle=lc;ctx.lineWidth=2;ctx.setLineDash([]);
  ctx.beginPath();pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));ctx.stroke();
}}

/* ── Рендер таблицы ── */
function renderTable(){{
  const rows=filterTrades();
  const wrap=document.getElementById('trades-wrap');

  // Сводка
  const s=calcStats(rows);
  document.getElementById('sum-count').textContent=s.total+' '+tradesWord(s.total);
  document.getElementById('sum-wr').textContent=s.wr!==null?'винрейт '+s.wr+'%':'винрейт —';
  const rrEl=document.getElementById('sum-rr');
  rrEl.textContent=s.total?fmtPnl(s.totalR):'R —';
  rrEl.className='summary-rr '+(s.total?pnlClass(s.totalR):'');

  if(!rows.length){{
    wrap.innerHTML='<div class="empty"><div class="empty-icon">📋</div><div class="empty-title">Нет сделок за этот период</div></div>';
    return;
  }}

  const trs=rows.map((t,idx)=>{{
    const isLong=t.direction==='long';
    const rCls=pnlClass(t.result_r);
    const oCls=t.outcome==='win'?'win':t.outcome==='loss'?'loss':'breakeven';
    const date=(t.trade_date||t.created_at||'—').split(' ')[0];
    const hasD=t.entry_price||t.exit_price||t.size||t.leverage;
    const hasN=t.note&&t.note.trim();
    const exp=hasD||hasN;
    return `<tr class="trade-row" data-idx="${{idx}}" ${{exp?'':'style="cursor:default"'}}>
      <td class="cell-date">${{date}}</td>
      <td class="cell-symbol">${{t.symbol||t.asset}}</td>
      <td><span class="dir-badge ${{isLong?'long':'short'}}">${{isLong?'лонг':'шорт'}}</span></td>
      <td class="cell-r ${{rCls}}">${{fmtPnl(t.result_r)}}</td>
      <td><span class="out-badge ${{oCls}}">${{outcomeLabel(t.outcome)}}</span></td>
    </tr>
    ${{exp?`<tr id="det-${{idx}}" style="display:none"><td colspan="5" style="padding:0 0 6px">
      ${{hasD?`<div class="detail-inner">
        ${{t.entry_price?`<div><div class="di-label">Вход</div><div class="di-value">${{t.entry_price}}</div></div>`:''}}
        ${{t.exit_price?`<div><div class="di-label">Выход</div><div class="di-value">${{t.exit_price}}</div></div>`:''}}
        ${{t.size?`<div><div class="di-label">Размер</div><div class="di-value">${{t.size}}</div></div>`:''}}
        ${{t.leverage?`<div><div class="di-label">Leverage</div><div class="di-value">${{t.leverage}}x</div></div>`:''}}
      </div>`:''}}</p>
      ${{hasN?`<div class="trade-note">${{t.note}}</div>`:''}}
    </td></tr>`:''}}`
  }}).join('');

  wrap.innerHTML=`<table class="trades-table">
    <thead><tr><th>Дата</th><th>Тикер</th><th>Напр.</th><th>R</th><th>Итог</th></tr></thead>
    <tbody>${{trs}}</tbody>
  </table>`;

  wrap.querySelectorAll('.trade-row').forEach(row=>{{
    const idx=row.dataset.idx;
    const det=document.getElementById('det-'+idx);
    if(!det)return;
    row.addEventListener('click',()=>{{det.style.display=det.style.display==='none'?'':'none'}});
  }});
}}

/* ── Субфильтр ── */
function buildSubfilter(){{
  const sub=document.getElementById('subfilter');
  const sel=document.getElementById('subfilter-select');
  if(view==='month'){{
    const months={{...new Set(ALL_TRADES.map(getMonthKey).filter(Boolean))}};
    const keys=[...new Set(ALL_TRADES.map(getMonthKey).filter(Boolean))].sort().reverse();
    const names={{'01':'Январь','02':'Февраль','03':'Март','04':'Апрель','05':'Май','06':'Июнь','07':'Июль','08':'Август','09':'Сентябрь','10':'Октябрь','11':'Ноябрь','12':'Декабрь'}};
    sel.innerHTML=keys.map(k=>{{const[y,m]=k.split('-');return`<option value="${{k}}">${{names[m]||m}} ${{y}}</option>`}}).join('');
    subfilter=keys[0]||'';sel.value=subfilter;sub.classList.add('visible');
  }}else if(view==='year'){{
    const keys=[...new Set(ALL_TRADES.map(getYearKey).filter(Boolean))].sort().reverse();
    sel.innerHTML=keys.map(k=>`<option value="${{k}}">${{k}}</option>`).join('');
    subfilter=keys[0]||'';sel.value=subfilter;sub.classList.add('visible');
  }}else if(view==='winrate'){{
    sel.innerHTML=`<option value="win">Прибыль</option><option value="loss">Убыток</option><option value="breakeven">Безубыток</option>`;
    subfilter='win';sel.value=subfilter;sub.classList.add('visible');
  }}else{{
    subfilter='';sub.classList.remove('visible');
  }}
}}

/* ── События ── */
document.querySelectorAll('.tab').forEach(btn=>{{
  btn.addEventListener('click',()=>{{
    document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    view=btn.dataset.view;
    buildSubfilter();
    renderTable();
  }});
}});

document.getElementById('subfilter-select').addEventListener('change',e=>{{
  subfilter=e.target.value;renderTable();
}});

document.querySelectorAll('.mode-btn').forEach(btn=>{{
  btn.addEventListener('click',()=>{{
    pnlMode=btn.dataset.mode;
    document.querySelectorAll('.mode-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    const riskRow=document.getElementById('risk-row');
    riskRow.style.display=(pnlMode==='r'?'none':'');
    document.getElementById('pnl-label').textContent=pnlMode==='r'?'Суммарный R':'Суммарный PnL';
    updateMainPnl();
    renderTable();
  }});
}});

document.getElementById('risk-usd').addEventListener('input',e=>{{riskUsd=parseFloat(e.target.value)||0;renderTable();updateMainPnl()}});
document.getElementById('risk-pct').addEventListener('input',e=>{{riskPct=parseFloat(e.target.value)||0;renderTable();updateMainPnl()}});

/* ── Главный PnL ── */
function updateMainPnl(){{
  const v=INIT_STATS.total_r||0;
  const el=document.getElementById('stat-pnl');
  el.textContent=fmtPnl(v);
  el.className='stats-pnl '+pnlClass(v);
}}

/* ── Инициализация ── */
(function init(){{
  // Шапка
  const cnt=INIT_STATS.total_trades||0;
  document.getElementById('hdr-sub').textContent=cnt+' '+tradesWord(cnt);
  // Статы
  updateMainPnl();
  document.getElementById('stat-wr').textContent=cnt?INIT_STATS.winrate+'%':'—';
  document.getElementById('stat-pf').textContent=cnt?INIT_STATS.profit_factor:'—';
  document.getElementById('stat-cnt').textContent=cnt||'0';
  // График
  requestAnimationFrame(()=>drawChart(INIT_STATS.r_curve));
  // Таблица
  document.getElementById('risk-row').style.display='none';
  renderTable();
}})();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------- Просмотр пользователей (только для владельца) ----------

ADMIN_USER_ID = 778995374  # твой Telegram ID

@app.get("/admin/users")
def admin_users(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    days: Optional[int] = None,        # активные за последние N дней
    premium: Optional[bool] = None,    # только Telegram Premium
    limit: int = 100,
):
    """Список пользователей — только для владельца."""
    if user_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Нет доступа")

    query = db.query(User)

    if days is not None:
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=days)
        query = query.filter(User.last_seen_at >= since)

    if premium is not None:
        query = query.filter(User.is_premium == premium)

    users = query.order_by(User.last_seen_at.desc()).limit(limit).all()

    return {
        "total": query.count(),
        "users": [{
            "user_id": u.user_id,
            "first_name": u.first_name,
            "username": u.username,
            "is_premium": u.is_premium,
            "language_code": u.language_code,
            "first_seen_at": u.first_seen_at.strftime("%d.%m.%Y") if u.first_seen_at else None,
            "last_seen_at": u.last_seen_at.strftime("%d.%m.%Y %H:%M") if u.last_seen_at else None,
            "visits_count": u.visits_count,
        } for u in users]
    }


@app.get("/admin/stats")
def admin_stats(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Общая статистика по всем пользователям."""
    if user_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Нет доступа")

    from datetime import timedelta
    now = datetime.utcnow()

    total_users = db.query(User).count()
    dau = db.query(User).filter(User.last_seen_at >= now - timedelta(days=1)).count()
    wau = db.query(User).filter(User.last_seen_at >= now - timedelta(days=7)).count()
    mau = db.query(User).filter(User.last_seen_at >= now - timedelta(days=30)).count()
    total_trades = db.query(Trade).count()
    premium_users = db.query(User).filter(User.is_premium == True).count()

    return {
        "total_users": total_users,
        "dau": dau,
        "wau": wau,
        "mau": mau,
        "total_trades": total_trades,
        "premium_users": premium_users,
    }


# ---------- BingX интеграция ----------

import hmac as hmac_lib
import hashlib
import urllib.request
import urllib.parse

BINGX_BASE = "https://open-api.bingx.com"


def bingx_sign(params: dict, secret: str) -> str:
    # BingX требует строку параметров БЕЗ сортировки, в оригинальном порядке
    # timestamp должен быть последним
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac_lib.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


def bingx_get(path: str, api_key: str, secret: str, extra: dict = None) -> dict:
    params = {}
    if extra:
        params.update(extra)
    params["timestamp"] = int(datetime.utcnow().timestamp() * 1000)
    params["signature"] = bingx_sign(params, secret)
    url = BINGX_BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-BX-APIKEY": api_key,
        "Content-Type": "application/json",
    })
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


@app.post("/exchange/bingx/connect")
def bingx_connect(
    body: dict,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Сохраняет BingX API ключи и проверяет подключение."""
    api_key = body.get("api_key", "").strip()
    secret_key = body.get("secret_key", "").strip()

    if not api_key or not secret_key:
        raise HTTPException(status_code=400, detail="Укажи оба ключа")

    # Проверяем ключи — запрашиваем баланс
    try:
        data = bingx_get("/openApi/swap/v2/user/balance", api_key, secret_key)
        if data.get("code") != 0:
            raise HTTPException(status_code=400, detail=f"BingX: {data.get('msg', 'Ошибка')}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось подключиться к BingX: {str(e)}")

    # Сохраняем ключи
    settings = get_or_create_settings(user_id, db)
    settings.bingx_api_key = api_key
    settings.bingx_secret_key = secret_key
    db.commit()

    balance = data.get("data", {}).get("balance", {})
    return {
        "status": "connected",
        "balance": balance.get("equity", "—"),
        "currency": "USDT",
    }


@app.delete("/exchange/bingx/disconnect")
def bingx_disconnect(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Удаляет BingX API ключи."""
    settings = get_or_create_settings(user_id, db)
    settings.bingx_api_key = None
    settings.bingx_secret_key = None
    settings.bingx_last_sync = None
    db.commit()
    return {"status": "disconnected"}


@app.get("/exchange/bingx/status")
def bingx_status(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Возвращает статус подключения BingX."""
    settings = get_or_create_settings(user_id, db)
    connected = bool(settings.bingx_api_key and settings.bingx_secret_key)
    return {
        "connected": connected,
        "last_sync": settings.bingx_last_sync.strftime("%d.%m.%Y %H:%M") if settings.bingx_last_sync else None,
    }


class BingxSyncRequest(BaseModel):
    risk_usd: Optional[float] = None  # глобальный риск в $ для расчёта result_r


def safe_float(v):
    try:
        result = float(v)
        return result if result != 0 else None
    except Exception:
        return None


@app.post("/exchange/bingx/sync")
def bingx_sync(
    body: BingxSyncRequest = BingxSyncRequest(),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Синхронизирует сделки с BingX через Trade/Fill History.
    Источник: /openApi/swap/v2/trade/fillHistory — реальные исполнения с точными
    ценами входа/выхода и PnL. Один ордер на закрытие = одна запись в журнале.
    """
    settings = get_or_create_settings(user_id, db)
    if not settings.bingx_api_key or not settings.bingx_secret_key:
        raise HTTPException(status_code=400, detail="BingX не подключён")

    api_key = settings.bingx_api_key
    secret = settings.bingx_secret_key

    end_ts = int(datetime.utcnow().timestamp() * 1000)
    start_ts = end_ts - 90 * 24 * 60 * 60 * 1000  # 90 дней

    # Шаг 1: получаем список торгуемых символов через income
    symbols = set()
    try:
        r = bingx_get("/openApi/swap/v2/user/income", api_key, secret,
            {"incomeType": "REALIZED_PNL", "startTime": start_ts, "endTime": end_ts, "limit": 1000})
        if r.get("code") == 0:
            for item in (r.get("data") or []):
                sym = item.get("symbol")
                if sym:
                    symbols.add(sym)
    except Exception:
        pass

    # Шаг 2: для каждого символа тянем fillHistory (реальные исполнения)
    all_fills = []
    debug_log = []
    for sym in symbols:
        try:
            r = bingx_get("/openApi/swap/v2/trade/fillHistory", api_key, secret, {
                "symbol": sym,
                "startTime": start_ts,
                "endTime": end_ts,
                "limit": 100,
            })
            code = r.get("code")
            msg = r.get("msg", "")
            data = r.get("data")
            debug_log.append(f"fillHistory {sym}: code={code} data_keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
            if code == 0 and data is not None:
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = (data.get("fill_history_orders") or data.get("fill_orders") or
                             data.get("fills") or data.get("orders") or data.get("data") or [])
                else:
                    items = []
                debug_log.append(f"  -> items={len(items)} sample={str(items[0])[:300] if items else 'empty'}")
                all_fills.extend(items)
        except Exception as e:
            debug_log.append(f"fillHistory {sym}: exception={str(e)}")
            continue

    # Если fillHistory ничего не вернул — пробуем allFillOrders
    if not all_fills:
        for sym in symbols:
            try:
                r = bingx_get("/openApi/swap/v2/trade/allFillOrders", api_key, secret, {
                    "symbol": sym,
                    "startTime": start_ts,
                    "endTime": end_ts,
                    "limit": 100,
                })
                code = r.get("code")
                msg = r.get("msg", "")
                data = r.get("data")
                debug_log.append(f"allFillOrders {sym}: code={code} data_keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                if code == 0 and data is not None:
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        items = (data.get("fill_orders") or data.get("orders") or
                                 data.get("data") or [])
                    else:
                        items = []
                    debug_log.append(f"  -> items={len(items)}")
                    all_fills.extend(items)
            except Exception as e:
                debug_log.append(f"allFillOrders {sym}: exception={str(e)}")
                continue

    if not all_fills:
        detail = "Нет данных. " + " | ".join(debug_log) if debug_log else "symbols пустой — income вернул 0 записей"
        raise HTTPException(status_code=400, detail=detail)

    # Дебаг: смотрим что возвращает positionHistory
    position_data = {}
    _pos_debug = []
    sym = list(symbols)[0] if symbols else "BTC-USDT"
    try:
        r = bingx_get("/openApi/swap/v1/trade/positionHistory", api_key, secret,
            {"symbol": sym, "pageIndex": 1, "pageSize": 3})
        _pos_debug.append(f"v1 code={r.get('code')} msg={r.get('msg')} data_keys={list((r.get('data') or {}).keys()) if isinstance(r.get('data'), dict) else type(r.get('data')).__name__} sample={str(list((r.get('data') or {}).values())[:1])[:200]}")
    except Exception as e:
        _pos_debug.append(f"v1 err={e}")
    try:
        r2 = bingx_get("/openApi/swap/v2/trade/positionHistory", api_key, secret,
            {"symbol": sym, "pageIndex": 1, "pageSize": 3})
        _pos_debug.append(f"v2 code={r2.get('code')} msg={r2.get('msg')} data_keys={list((r2.get('data') or {}).keys()) if isinstance(r2.get('data'), dict) else type(r2.get('data')).__name__}")
    except Exception as e:
        _pos_debug.append(f"v2 err={e}")
    raise HTTPException(status_code=400, detail=" || ".join(_pos_debug))

    # Сортируем по времени DESC (новые сначала)
    all_fills.sort(
        key=lambda f: str(f.get("filledTime") or f.get("time") or f.get("createTime") or ""),
        reverse=True
    )

    added = 0
    skipped = 0

    for fill in all_fills:
        # Уникальный ID: orderId или filledOrderId
        order_id = str(
            fill.get("orderId") or fill.get("filledOrderId") or fill.get("tradeId") or ""
        )
        if not order_id:
            skipped += 1
            continue

        # Пропускаем ордера открытия (side=BUY для LONG, side=SELL для SHORT)
        # Нас интересуют только ордера закрытия — они имеют реализованный PnL
        pnl_usd = safe_float(
            fill.get("realisedPNL") or fill.get("realisedPnl") or
            fill.get("realizedPnl") or fill.get("profit") or fill.get("pnl")
        )
        # Пропускаем нулевые PnL (ордера открытия или частичные исполнения без закрытия)
        if pnl_usd is None or pnl_usd == 0:
            skipped += 1
            continue

        # Дубли
        existing = db.query(Trade).filter(
            Trade.user_id == user_id,
            Trade.source == "auto",
            Trade.asset == order_id
        ).first()
        if existing:
            skipped += 1
            continue

        # Символ
        symbol_raw = fill.get("symbol") or ""
        symbol_clean = symbol_raw.replace("-USDT", "USDT").replace("-SWAP", "").replace("-", "")

        # Направление: positionSide (LONG/SHORT) или side (BUY=long, SELL=short)
        pos_side = str(fill.get("positionSide") or "").upper()
        side = str(fill.get("side") or "").upper()
        if pos_side == "SHORT":
            direction = "short"
        elif pos_side == "LONG":
            direction = "long"
        elif side == "SELL":
            # SELL в контексте фьючерсов = закрытие LONG или открытие SHORT
            # Если есть realisedPnl — скорее всего закрытие, определяем по знаку
            direction = "long"  # консервативно, чаще SELL = закрыть лонг
        else:
            direction = "long"

        outcome = "win" if pnl_usd > 0 else "loss"

        # Цены: fillPrice / price — реальная цена исполнения
        # exit_price — цена исполнения из fillHistory
        exit_price = safe_float(fill.get("price") or fill.get("fillPrice") or fill.get("avgPrice"))

        # entry_price и leverage — ищем в positionHistory по символу и времени
        entry_price = None
        leverage = None
        sym_positions = position_data.get(symbol_raw) or position_data.get(symbol_clean) or []
        if sym_positions:
            fill_ts_ms = 0
            try:
                from datetime import timezone
                ft = fill.get("filledTime") or ""
                if ft:
                    dt = datetime.fromisoformat(str(ft).replace("Z", "+00:00"))
                    fill_ts_ms = int(dt.astimezone(timezone.utc).timestamp() * 1000)
            except Exception:
                pass
            # Берём позицию, время закрытия которой ближайшее к времени филла
            best = min(sym_positions,
                key=lambda p: abs(p["closeTime"] - fill_ts_ms) if p["closeTime"] else 999999999999)
            entry_price = best.get("avgPrice")
            lev_raw = best.get("leverage")
            try:
                leverage = float(str(lev_raw).replace("X","").replace("x","")) if lev_raw else None
            except Exception:
                leverage = None
        size = safe_float(fill.get("quoteQty") or fill.get("filledVolume") or fill.get("qty") or fill.get("quantity"))
        # leverage уже взят из positionHistory выше

        fill_ts_raw = fill.get("filledTime") or fill.get("time") or fill.get("createTime") or fill.get("updateTime")
        try:
            if fill_ts_raw is None:
                trade_date = datetime.utcnow()
            elif isinstance(fill_ts_raw, (int, float)):
                trade_date = datetime.utcfromtimestamp(int(fill_ts_raw) / 1000)
            elif str(fill_ts_raw).isdigit():
                trade_date = datetime.utcfromtimestamp(int(fill_ts_raw) / 1000)
            else:
                # ISO строка типа "2026-07-01T09:48:23.000+08:00"
                from datetime import timezone
                dt = datetime.fromisoformat(str(fill_ts_raw).replace("Z", "+00:00"))
                trade_date = dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            trade_date = datetime.utcnow()

        # result_r если передан глобальный риск
        result_r = None
        if body.risk_usd and body.risk_usd > 0:
            result_r = round(pnl_usd / body.risk_usd, 2)

        trade = Trade(
            user_id=user_id,
            asset=order_id,
            symbol=symbol_clean,
            direction=direction,
            outcome=outcome,
            result_r=result_r,
            pnl_usd=round(pnl_usd, 4),
            entry_price=entry_price,
            exit_price=exit_price,
            size=size,
            leverage=leverage,
            source="auto",
            trade_date=trade_date,
            tags=[],
        )
        db.add(trade)
        added += 1

    settings.bingx_last_sync = datetime.utcnow()
    db.commit()

    return {
        "status": "ok",
        "added": added,
        "skipped": skipped,
        "total_fetched": len(all_fills),
        "symbols": list(symbols),
        "debug": f"символов: {len(symbols)}, филлов: {len(all_fills)}",
    }


@app.get("/exchange/bingx/debug")
def bingx_debug(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Диагностика: пробует несколько эндпоинтов и возвращает raw ответы."""
    settings = get_or_create_settings(user_id, db)
    if not settings.bingx_api_key or not settings.bingx_secret_key:
        raise HTTPException(status_code=400, detail="BingX не подключён")

    api_key = settings.bingx_api_key
    secret = settings.bingx_secret_key

    end_ts = int(datetime.utcnow().timestamp() * 1000)
    start_ts = end_ts - 30 * 24 * 60 * 60 * 1000  # 30 дней

    results = {}

    # 1. income — чтобы знать символы
    try:
        r = bingx_get("/openApi/swap/v2/user/income", api_key, secret,
            {"incomeType": "REALIZED_PNL", "startTime": start_ts, "endTime": end_ts, "limit": 10})
        results["income"] = {"code": r.get("code"), "msg": r.get("msg"), "count": len(r.get("data") or [])}
        symbols = list(set(x.get("symbol") for x in (r.get("data") or []) if x.get("symbol")))
        results["symbols"] = symbols[:5]
    except Exception as e:
        results["income"] = {"error": str(e)}
        symbols = []

    sym = symbols[0] if symbols else "BTC-USDT"

    # 2. fillHistory
    try:
        r = bingx_get("/openApi/swap/v2/trade/fillHistory", api_key, secret,
            {"symbol": sym, "startTime": start_ts, "endTime": end_ts, "limit": 3})
        results["fillHistory"] = {"code": r.get("code"), "msg": r.get("msg"), "data_keys": list((r.get("data") or {}).keys()) if isinstance(r.get("data"), dict) else str(type(r.get("data"))), "sample": (r.get("data") or {}) if not isinstance(r.get("data"), list) else r.get("data", [])[:1]}
    except Exception as e:
        results["fillHistory"] = {"error": str(e)}

    # 3. allFillOrders
    try:
        r = bingx_get("/openApi/swap/v2/trade/allFillOrders", api_key, secret,
            {"symbol": sym, "startTime": start_ts, "endTime": end_ts, "limit": 3})
        results["allFillOrders"] = {"code": r.get("code"), "msg": r.get("msg"), "data_type": str(type(r.get("data"))), "sample": r.get("data")}
    except Exception as e:
        results["allFillOrders"] = {"error": str(e)}

    # 4. allOrders (history)
    try:
        r = bingx_get("/openApi/swap/v2/trade/allOrders", api_key, secret,
            {"symbol": sym, "startTime": start_ts, "endTime": end_ts, "limit": 3})
        results["allOrders"] = {"code": r.get("code"), "msg": r.get("msg"), "count": len((r.get("data") or {}).get("orders") or []), "sample": ((r.get("data") or {}).get("orders") or [])[:1]}
    except Exception as e:
        results["allOrders"] = {"error": str(e)}

    # 5. positionHistory (старый)
    try:
        r = bingx_get("/openApi/swap/v1/trade/positionHistory", api_key, secret,
            {"symbol": sym, "pageIndex": 1, "pageSize": 3})
        results["positionHistory"] = {"code": r.get("code"), "msg": r.get("msg"), "data_keys": list((r.get("data") or {}).keys()) if isinstance(r.get("data"), dict) else "not dict"}
    except Exception as e:
        results["positionHistory"] = {"error": str(e)}

    return results

# ---------- Вложения к сделке (скриншоты) ----------

class AttachmentIn(BaseModel):
    filename: str
    mime_type: str = "image/jpeg"
    data: str  # base64 dataURL, напр. "data:image/jpeg;base64,..."


@app.post("/trades/{trade_id}/attachments")
def add_attachment(
    trade_id: int,
    body: AttachmentIn,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Сохраняет скриншот к сделке."""
    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    # Лимит: не более 5 вложений на сделку
    count = db.query(TradeAttachment).filter(
        TradeAttachment.trade_id == trade_id,
        TradeAttachment.user_id == user_id,
    ).count()
    if count >= 5:
        raise HTTPException(status_code=400, detail="Максимум 5 скриншотов на сделку")

    att = TradeAttachment(
        trade_id=trade_id,
        user_id=user_id,
        filename=body.filename,
        mime_type=body.mime_type,
        data=body.data,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return {"id": att.id, "filename": att.filename, "created_at": att.created_at}


@app.get("/trades/{trade_id}/attachments")
def get_attachments(
    trade_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Возвращает все скриншоты сделки."""
    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Сделка не найдена")

    atts = db.query(TradeAttachment).filter(
        TradeAttachment.trade_id == trade_id,
        TradeAttachment.user_id == user_id,
    ).order_by(TradeAttachment.created_at).all()

    return [{"id": a.id, "filename": a.filename, "data": a.data} for a in atts]


@app.delete("/trades/{trade_id}/attachments/{att_id}")
def delete_attachment(
    trade_id: int,
    att_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Удаляет скриншот."""
    att = db.query(TradeAttachment).filter(
        TradeAttachment.id == att_id,
        TradeAttachment.trade_id == trade_id,
        TradeAttachment.user_id == user_id,
    ).first()
    if not att:
        raise HTTPException(status_code=404, detail="Вложение не найдено")
    db.delete(att)
    db.commit()
    return {"ok": True}
