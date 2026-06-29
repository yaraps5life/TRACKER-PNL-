"""
Бэкенд журнала сделок v3 — с авторизацией через Telegram и PostgreSQL.

Каждый запрос должен содержать заголовок
"Authorization: tma <init_data>" — это подпись Telegram,
из которой сервер достаёт user_id и работает ТОЛЬКО с данными этого юзера.

Новое в v3 — эндпоинты под полноценный UI (дашборд/журнал/деталь/аналитика):
  GET  /stats/summary       — сводка для дашборда (PnL, винрейт, equity curve)
  GET  /trades              — список сделок с фильтрами (журнал)
  GET  /trades/{trade_id}   — одна сделка (деталь)
  PATCH /trades/{trade_id}  — обновить заметку/теги
  DELETE /trades/{trade_id} — удалить сделку
  POST /trades              — добавить сделку вручную
  GET  /stats/by-tag        — PnL по тегам/сетапам (аналитика)
  GET  /tags                — список всех тегов пользователя

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
from models import Base, Trade
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
    risk_percent: float
    result_r: float
    symbol: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    size: Optional[float] = None
    leverage: Optional[float] = None
    pnl_usd: Optional[float] = None
    source: Literal["manual", "auto"] = "manual"
    note: Optional[str] = None
    tags: Optional[list[str]] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class UpdateTrade(BaseModel):
    """Для PATCH — обычно меняют только заметку и теги уже импортированной сделки."""
    note: Optional[str] = None
    tags: Optional[list[str]] = None


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


# ---------- Расчёт статистики (R-based, как в v1/v2) ----------

def calculate_stats(trades: list[Trade]) -> dict:
    """Винрейт/profit factor/streak/equity curve по R-мультипликаторам —
    это и есть твоя основная метрика по ICT/SMT методологии."""
    if not trades:
        return {
            "total_trades": 0, "winrate": 0, "profit_factor": 0,
            "total_r": 0, "current_streak": 0, "equity_curve": [],
            "pnl_usd_total": 0,
        }

    wins = [t.result_r for t in trades if t.result_r > 0]
    losses = [t.result_r for t in trades if t.result_r < 0]

    winrate = round(len(wins) / len(trades) * 100, 1)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float(gross_profit > 0)
    total_r = round(sum(t.result_r for t in trades), 2)

    streak = 0
    if trades:
        last_was_win = trades[-1].result_r > 0
        for t in reversed(trades):
            if (t.result_r > 0) == last_was_win:
                streak += 1 if last_was_win else -1
            else:
                break

    equity_curve = []
    running_total = 0
    for t in trades:
        running_total += t.result_r
        equity_curve.append(round(running_total, 2))

    pnl_usd_total = round(sum(t.pnl_usd or 0 for t in trades), 2)

    return {
        "total_trades": len(trades), "winrate": winrate,
        "profit_factor": profit_factor, "total_r": total_r,
        "current_streak": streak, "equity_curve": equity_curve,
        "pnl_usd_total": pnl_usd_total,
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
        "opened_at": t.opened_at.strftime("%d.%m.%Y %H:%M") if t.opened_at else None,
        "closed_at": t.closed_at.strftime("%d.%m.%Y %H:%M") if t.closed_at else None,
        "created_at": t.created_at.strftime("%d.%m.%Y %H:%M") if t.created_at else None,
    }


# ---------- Дашборд ----------

@app.get("/stats/summary")
def get_summary(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Сводка для главного экрана: PnL, винрейт, profit factor, equity curve."""
    trades = db.query(Trade).filter(Trade.user_id == user_id).order_by(Trade.created_at).all()
    stats = calculate_stats(trades)

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
        query = query.filter(Trade.result_r > 0)
    elif result == "loss":
        query = query.filter(Trade.result_r < 0)
    if source:
        query = query.filter(Trade.source == source)
    if symbol:
        query = query.filter(Trade.symbol == symbol)

    trades = query.order_by(Trade.created_at).all()

    # Фильтр по тегу — отдельно, потому что tags хранится как JSON-массив
    if tag:
        trades = [t for t in trades if t.tags and tag in t.tags]

    stats = calculate_stats(trades)

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
        risk_percent=new_trade.risk_percent,
        result_r=new_trade.result_r,
        symbol=new_trade.symbol or new_trade.asset,
        entry_price=new_trade.entry_price,
        exit_price=new_trade.exit_price,
        size=new_trade.size,
        leverage=new_trade.leverage,
        pnl_usd=new_trade.pnl_usd,
        source=new_trade.source,
        note=new_trade.note,
        tags=new_trade.tags or [],
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
