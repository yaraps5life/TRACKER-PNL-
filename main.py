"""
Бэкенд журнала сделок v2 — с авторизацией через Telegram и PostgreSQL.

Главное отличие от v1: КАЖДЫЙ запрос должен содержать заголовок
"Authorization: tma <init_data>" — это та самая подпись Telegram,
из которой сервер достаёт user_id и работает ТОЛЬКО с данными этого юзера.

Как запустить локально:
   pip install fastapi uvicorn sqlalchemy psycopg2-binary --break-system-packages
   (нужен установленный и запущенный PostgreSQL)
   uvicorn main:app --reload
"""

import os
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Literal

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


class NewTrade(BaseModel):
    asset: str
    direction: Literal["long", "short"]
    risk_percent: float
    result_r: float


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

    print(f"[DEBUG] Авторизован user_id={user_data['id']}, name={user_data.get('first_name')}")
    return user_data["id"]


def calculate_stats(trades: list[Trade]) -> dict:
    """Та же логика расчёта метрик, что в v1 — просто теперь работает со
    списком объектов из базы данных, а не со словарями из JSON-файла."""
    if not trades:
        return {
            "total_trades": 0, "winrate": 0, "profit_factor": 0,
            "total_r": 0, "current_streak": 0, "equity_curve": []
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

    return {
        "total_trades": len(trades), "winrate": winrate,
        "profit_factor": profit_factor, "total_r": total_r,
        "current_streak": streak, "equity_curve": equity_curve
    }


@app.get("/trades")
def get_trades(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    # КЛЮЧЕВАЯ СТРОКА всей многопользовательской логики —
    # фильтр .filter(Trade.user_id == user_id) гарантирует, что человек
    # увидит ТОЛЬКО свои сделки, никогда чужие
    trades = db.query(Trade).filter(Trade.user_id == user_id).order_by(Trade.created_at).all()

    stats = calculate_stats(trades)

    return {
        "trades": [
            {
                "asset": t.asset, "direction": t.direction,
                "risk_percent": t.risk_percent, "result_r": t.result_r,
                "created_at": t.created_at.strftime("%d.%m.%Y %H:%M")
            }
            for t in reversed(trades)
        ],
        "stats": stats
    }


@app.post("/trades")
def add_trade(
    new_trade: NewTrade,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    trade = Trade(
        user_id=user_id,  # привязываем сделку именно к этому пользователю
        asset=new_trade.asset,
        direction=new_trade.direction,
        risk_percent=new_trade.risk_percent,
        result_r=new_trade.result_r
    )
    db.add(trade)
    db.commit()
    return {"status": "ok"}
