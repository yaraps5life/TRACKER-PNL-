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
from models import Base, Trade, UserSettings, FavoriteSymbol, ShareLink
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
    db: Session = Depends(get_db),
    year: Optional[int] = None,
    month: Optional[int] = None,
    result: Optional[Literal["win", "loss", "breakeven"]] = None,
):
    """Сводка для главного экрана: суммарный R, график R.
    Поддерживает фильтры year, month, result для фильтрованного дашборда."""
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
