"""
Скрипт формирования отчёта по отгрузкам на WB.

Что делает:
1. Берёт остатки и продажи по всем артикулам/складам из WB API.
2. Читает файл shipments.json — товары "дома" и "в пути".
3. Обновляет статусы отправок (если дата прибытия прошла -> "прибыло").
4. Считает: доступно = остаток_ФБО + дома + в пути (не просроченные).
5. Считает скорость продаж и "хватит на сколько дней".
6. Формирует рекомендации (сколько отправить, чтобы хватило на TARGET_DAYS дней).
7. Отправляет отчёт в Telegram.
8. Сохраняет shipments.json обратно (с обновлёнными статусами).

Переменные окружения (задаются в GitHub Secrets):
  WB_API_TOKEN
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import json
import datetime
import requests

# ---------- НАСТРОЙКИ ----------
WB_API_TOKEN = os.environ["WB_API_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DAYS_BACK = 14          # за сколько дней считать скорость продаж
TRANSIT_DAYS = 15       # сколько дней едет товар до ФБО
TARGET_DAYS = 25        # до какого запаса (в днях продаж) пополняем

SHIPMENTS_FILE = "shipments.json"

HEADERS = {
    "Authorization": WB_API_TOKEN,
    "Content-Type": "application/json",
}


# ---------- ШАГ 1: ОСТАТКИ ----------
def get_stocks():
    """
    Возвращает список {nmId, supplierArticle, warehouseName, quantity}
    """
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"
    params = {"dateFrom": "2020-01-01"}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


# ---------- ШАГ 2: ПРОДАЖИ ----------
def get_sales(days_back):
    """
    Возвращает список продаж за последние days_back дней.
    Используем для подсчёта скорости продаж по артикулу.
    """
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/sales"
    date_from = (datetime.datetime.now() - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = {"dateFrom": date_from}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


# ---------- ШАГ 3: ОТПРАВКИ (ДОМА / В ПУТИ) ----------
def load_shipments():
    if not os.path.exists(SHIPMENTS_FILE):
        return []
    with open(SHIPMENTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_shipments(shipments):
    with open(SHIPMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(shipments, f, ensure_ascii=False, indent=2)


def update_shipment_statuses(shipments):
    """Если дата прибытия прошла -> статус 'прибыло'."""
    today = datetime.date.today()
    for s in shipments:
        if s["status"] == "в пути":
            arrival = datetime.date.fromisoformat(s["arrival_date"])
            if today >= arrival:
                s["status"] = "прибыло"
    return shipments


# ---------- ШАГ 4: АГРЕГАЦИЯ ----------
def build_report():
    stocks = get_stocks()
    sales = get_sales(DAYS_BACK)
    shipments = update_shipment_statuses(load_shipments())

    # остатки ФБО: ключ (артикул, склад) -> кол-во
    fbo = {}
    names = {}
    for item in stocks:
        key = (str(item.get("supplierArticle")), item.get("warehouseName"))
        fbo[key] = fbo.get(key, 0) + item.get("quantity", 0)
        names[str(item.get("supplierArticle"))] = item.get("subject") or item.get("supplierArticle")

    # продажи по артикулу (суммарно по всем складам — для расчёта скорости)
    sales_count = {}
    for item in sales:
        art = str(item.get("supplierArticle"))
        sales_count[art] = sales_count.get(art, 0) + 1
        if art not in names:
            names[art] = item.get("subject") or art

    # "дома" и "в пути" по (артикул, склад)
    home = {}
    transit = {}
    for s in shipments:
        key = (str(s["article"]), s["warehouse"])
        if s["status"] == "дома":
            home[key] = home.get(key, 0) + s["qty"]
        elif s["status"] == "в пути":
            transit[key] = transit.get(key, 0) + s["qty"]

    # все ключи (артикул, склад), которые встречаются хоть где-то
    all_keys = set(fbo) | set(home) | set(transit)

    rows = []
    for key in all_keys:
        art, wh = key
        fbo_qty = fbo.get(key, 0)
        home_qty = home.get(key, 0)
        transit_qty = transit.get(key, 0)
        available = fbo_qty + home_qty + transit_qty

        per_day = sales_count.get(art, 0) / DAYS_BACK
        per_day = max(per_day, 0.01)  # защита от деления на 0

        days_left = round(available / per_day, 1)
        need = max(0, round(TARGET_DAYS * per_day - available))

        if days_left < TRANSIT_DAYS:
            status = "critical"
        elif days_left < TRANSIT_DAYS + 10:
            status = "warning"
        else:
            status = "ok"

        rows.append({
            "art": art,
            "name": names.get(art, art),
            "warehouse": wh,
            "fbo": fbo_qty,
            "home": home_qty,
            "transit": transit_qty,
            "available": available,
            "per_day": round(per_day, 2),
            "days_left": days_left,
            "need": need,
            "status": status,
        })

    save_shipments(shipments)
    return rows, shipments


# ---------- ШАГ 5: ФОРМИРОВАНИЕ ТЕКСТА ----------
def format_message(rows, shipments):
    today = datetime.date.today().strftime("%d.%m.%Y")
    next_report = (datetime.date.today() + datetime.timedelta(days=5)).strftime("%d.%m.%Y")

    critical = [r for r in rows if r["status"] == "critical"]
    warning = [r for r in rows if r["status"] == "warning"]
    ok = [r for r in rows if r["status"] == "ok"]

    lines = [f"📦 ОТЧЁТ ПО ОТГРУЗКАМ — {today}", f"(доставка: {TRANSIT_DAYS} дн, целевой запас: {TARGET_DAYS} дн)", ""]

    if critical:
        lines.append("🔴 КРИТИЧНО — отправить срочно:")
        for r in critical:
            lines.append(f"• {r['art']} \"{r['name']}\" → {r['warehouse']}")
            lines.append(f"  ФБО:{r['fbo']} Дома:{r['home']} В пути:{r['transit']} → хватит {r['days_left']} дн")
            lines.append(f"  ➤ Отправить: {r['need']} шт")
        lines.append("")

    if warning:
        lines.append("🟠 ВНИМАНИЕ — готовить отгрузку:")
        for r in warning:
            lines.append(f"• {r['art']} \"{r['name']}\" → {r['warehouse']} — хватит {r['days_left']} дн")
            lines.append(f"  ➤ Отправить: {r['need']} шт")
        lines.append("")

    if ok:
        lines.append("🟢 НОРМА:")
        for r in ok:
            lines.append(f"• {r['art']} → {r['warehouse']} — хватит {r['days_left']} дн")
        lines.append("")

    in_transit = [s for s in shipments if s["status"] == "в пути"]
    if in_transit:
        lines.append("🚚 В ПУТИ:")
        for s in in_transit:
            lines.append(f"• {s['article']} → {s['warehouse']}: {s['qty']} шт, прибудет {s['arrival_date']}")
        lines.append("")

    at_home = [s for s in shipments if s["status"] == "дома"]
    if at_home:
        lines.append("🏠 ЛЕЖИТ ДОМА:")
        for s in at_home:
            lines.append(f"• {s['article']} → {s['warehouse']}: {s['qty']} шт")
        lines.append("")

    lines.append(f"✅ Проверено артикулов: {len(set(r['art'] for r in rows))}")
    lines.append(f"Следующий отчёт: {next_report}")

    return "\n".join(lines)


# ---------- ШАГ 6: ОТПРАВКА В TELEGRAM ----------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram ограничивает длину сообщения ~4096 символов — режем при необходимости
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
        resp.raise_for_status()


if __name__ == "__main__":
    rows, shipments = build_report()
    message = format_message(rows, shipments)
    print(message)  # для логов GitHub Actions
    send_telegram(message)
