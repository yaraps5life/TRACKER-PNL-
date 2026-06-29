"""
Бэкенд журнала сделок v4 — с авторизацией через Telegram и PostgreSQL.

Каждый запрос должен содержать заголовок
"Authorization: tma <init_data>" — это подпись Telegram,
из которой сервер достаёт user_id и работает ТОЛЬКО с данными этого юзера.

Логика баланса: пользователь один раз задаёт стартовый баланс счёта
(GET/POST /settings), а текущий баланс на дашборде считается как
starting_balance + сумма pnl_usd всех сделок. % прибыли считается
от стартового баланса.

Эндпоинты:
  GET  /settings             — получить стартовый баланс
  POST /settings             — задать/изменить стартовый баланс
  GET  /stats/summary        — сводка для дашборда (баланс $, % прибыли, график баланса)
  GET  /trades               — список сделок с фильтрами (журнал)
  GET  /trades/{trade_id}    — одна сделка (деталь)
  PATCH /trades/{trade_id}   — обновить заметку/теги
  DELETE /trades/{trade_id}  — удалить сделку
  POST /trades               — добавить сделку вручную
  GET  /stats/by-tag         — PnL по тегам/сетапам (аналитика)
  GET  /tags                 — список всех тегов пользователя

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
from models import Base, Trade, UserSettings
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
    result_r: Optional[float] = None       # Risk Reward — только для статистики
    pnl_usd: Optional[float] = None        # двигает баланс счёта
    note: Optional[str] = None
    trade_date: Optional[datetime] = None  # дата сделки, не обязательна
    tags: Optional[list[str]] = None
    source: Literal["manual", "auto"] = "manual"

    # поля под будущую авто-синхронизацию — не из формы, но поддерживаются API
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

def calculate_stats(trades: list[Trade], starting_balance: float) -> dict:
    """
    Главная логика дашборда:
    - current_balance = starting_balance + сумма pnl_usd всех сделок
    - pnl_pct = процент прибыли от starting_balance
    - balance_curve = история баланса по каждой сделке (для графика) —
      считается в хронологическом порядке (trades должны быть отсортированы по дате)
    Winrate/profit factor считаются по pnl_usd, где он указан; если pnl_usd
    не указан у сделки — она не участвует в этих метриках (но участвует в R-статистике).
    """
    if not trades:
        return {
            "total_trades": 0, "winrate": 0, "profit_factor": 0,
            "total_r": 0, "current_streak": 0,
            "starting_balance": round(starting_balance, 2),
            "current_balance": round(starting_balance, 2),
            "pnl_total": 0, "pnl_pct": 0,
            "balance_curve": [round(starting_balance, 2)],
        }

    trades_with_pnl = [t for t in trades if t.pnl_usd is not None]
    wins = [t.pnl_usd for t in trades_with_pnl if t.pnl_usd > 0]
    losses = [t.pnl_usd for t in trades_with_pnl if t.pnl_usd < 0]

    winrate = round(len(wins) / len(trades_with_pnl) * 100, 1) if trades_with_pnl else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float(gross_profit > 0)

    trades_with_r = [t.result_r for t in trades if t.result_r is not None]
    total_r = round(sum(trades_with_r), 2) if trades_with_r else 0

    streak = 0
    if trades_with_pnl:
        last_was_win = trades_with_pnl[-1].pnl_usd > 0
        for t in reversed(trades_with_pnl):
            if (t.pnl_usd > 0) == last_was_win:
                streak += 1 if last_was_win else -1
            else:
                break

    pnl_total = round(sum(t.pnl_usd or 0 for t in trades), 2)
    current_balance = round(starting_balance + pnl_total, 2)
    pnl_pct = round((pnl_total / starting_balance) * 100, 2) if starting_balance > 0 else 0

    # График баланса — начинается со стартового баланса, дальше шаг за шагом
    # прибавляем pnl_usd каждой сделки по порядку
    balance_curve = [round(starting_balance, 2)]
    running = starting_balance
    for t in trades:
        running += t.pnl_usd or 0
        balance_curve.append(round(running, 2))

    return {
        "total_trades": len(trades), "winrate": winrate,
        "profit_factor": profit_factor, "total_r": total_r,
        "current_streak": streak,
        "starting_balance": round(starting_balance, 2),
        "current_balance": current_balance,
        "pnl_total": pnl_total, "pnl_pct": pnl_pct,
        "balance_curve": balance_curve,
    }


def trade_to_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "asset": t.asset,
        "symbol": t.symbol or t.asset,
        "direction": t.direction,
        "risk_percent": t.risk_percent,
        "result_r": t.result_r,
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
    """Сводка для главного экрана: текущий баланс $, % прибыли, график баланса."""
    settings = get_or_create_settings(user_id, db)
    trades = db.query(Trade).filter(Trade.user_id == user_id).all()
    trades = sorted(trades, key=sort_key)

    stats = calculate_stats(trades, settings.starting_balance)

    recent = [trade_to_dict(t) for t in reversed(trades[-5:])]

    return {"stats": stats, "recent_trades": recent}


# ---------- Журнал сделок (с фильтрами) ----------

@app.get("/trades")
def get_trades(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    result: Optional[Literal["win", "loss"]] = None,
    source: Optional[Literal["manual", "auto"]] = None,
    symbol: Optional[str] = None,
    tag: Optional[str] = None,
):
    """Список сделок с опциональными фильтрами — под экран Журнала."""
    query = db.query(Trade).filter(Trade.user_id == user_id)

    if result == "win":
        query = query.filter(Trade.pnl_usd > 0)
    elif result == "loss":
        query = query.filter(Trade.pnl_usd < 0)
    if source:
        query = query.filter(Trade.source == source)
    if symbol:
        query = query.filter(Trade.symbol == symbol)

    trades = query.all()
    trades = sorted(trades, key=sort_key)

    # Фильтр по тегу — отдельно, потому что tags хранится как JSON-массив
    if tag:
        trades = [t for t in trades if t.tags and tag in t.tags]

    settings = get_or_create_settings(user_id, db)
    stats = calculate_stats(trades, settings.starting_balance)

    return {
        "trades": [trade_to_dict(t) for t in reversed(trades)],
        "stats": stats,
    }


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
        pnl_usd=new_trade.pnl_usd,
        note=new_trade.note,
        trade_date=new_trade.trade_date,
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
    """PnL по тегам/сетапам — под экран Аналитики.
    Считаем в Python, а не в SQL, потому что tags — JSON-массив
    и одна сделка может относиться к нескольким тегам сразу."""
    trades = db.query(Trade).filter(Trade.user_id == user_id).all()

    by_tag: dict[str, dict] = {}
    for t in trades:
        for tag in (t.tags or []):
            if tag not in by_tag:
                by_tag[tag] = {"tag": tag, "pnl_usd": 0, "total_r": 0, "count": 0}
            by_tag[tag]["pnl_usd"] += t.pnl_usd or 0
            by_tag[tag]["total_r"] += t.result_r
            by_tag[tag]["count"] += 1

    result = sorted(by_tag.values(), key=lambda x: x["pnl_usd"], reverse=True)
    for r in result:
        r["pnl_usd"] = round(r["pnl_usd"], 2)
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
