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
from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel

from database import engine, get_db
from models import Base, Trade, UserSettings, FavoriteSymbol
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
    trade_date: Optional[datetime] = None  # если не передано — ставим "сегодня" на сервере
    result_r: Optional[float] = None       # Risk Reward — двигает суммарный R на дашборде
    outcome: Optional[Literal["win", "loss", "breakeven"]] = None  # статус без суммы $
    note: Optional[str] = None
    tags: Optional[list[str]] = None
    source: Literal["manual", "auto"] = "manual"

    # поля под будущую авто-синхронизацию — не из формы, но поддерживаются API
    pnl_usd: Optional[float] = None
    symbol: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    size: Optional[float] = None
    leverage: Optional[float] = None
    risk_percent: Optional[float] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class UpdateTrade(BaseModel):
    """Для PATCH — обычно меняют только заметку и теги уже импортированной сделки."""
    note: Optional[str] = None
    tags: Optional[list[str]] = None


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
    r_curve = [0]
    running = 0
    for t in trades:
        running += t.result_r or 0
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
        "result_r": t.result_r,
        "outcome": t.outcome,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "size": t.size,
        "leverage": t.leverage,
        "pnl_usd": t.pnl_usd,
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
    db: Session = Depends(get_db),
):
    """Сводка для главного экрана: суммарный R (баланс в R), график R по сделкам."""
    trades = db.query(Trade).filter(Trade.user_id == user_id).all()
    trades = sorted(trades, key=sort_key)

    stats = calculate_stats(trades)

    recent = [trade_to_dict(t) for t in reversed(trades[-5:])]

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
        note=new_trade.note,
        trade_date=new_trade.trade_date or datetime.utcnow(),  # дата обязательна — если не передана, берём "сегодня"
        tags=new_trade.tags or [],
        source=new_trade.source,
        symbol=new_trade.symbol or new_trade.asset,
        entry_price=new_trade.entry_price,
        exit_price=new_trade.exit_price,
        size=new_trade.size,
        leverage=new_trade.leverage,
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

