import asyncio, os, re, secrets, sqlite3, time
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()
E = os.environ
bot, dp = Bot(E["NOTIFY_TOKEN"]), Dispatcher()
ADMIN, CHANNEL = int(E["ADMIN_ID"]), E.get("CHANNEL_ID")
SBP = {"phone": E.get("SBP_PHONE", "+7 900 000-00-00"), "bank": E.get("SBP_BANK", "Т-Банк"),
       "name": E.get("SBP_NAME", "Иван И.")}

# Пакеты UC: id -> (UC, цена ₽). Цены на 21.11.2026: ориентир — розничные сервисы
# (660 UC ≈ 970 ₽, 1800 ≈ 2350 ₽, 8100 ≈ 9410 ₽), чуть ниже них. Меняй под свою закупку.
UC = {"uc60": (60, 89), "uc325": (325, 449), "uc660": (660, 890),
      "uc1800": (1800, 2190), "uc3850": (3850, 4490), "uc8100": (8100, 8990)}

db = sqlite3.connect("shop.db"); db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, kind TEXT, item TEXT, title TEXT,
 price INT, pubg_id TEXT, contact TEXT, token TEXT, status TEXT, ts INT);
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, title TEXT, descr TEXT,
 price INT, secret TEXT, sold INT DEFAULT 0);
""")
q = lambda s, *a: (db.execute(s, a), db.commit())[0]

# ---------- API ----------
async def catalog(_):
    accs = [dict(id=a["id"], title=a["title"], descr=a["descr"], price=a["price"])
            for a in db.execute("SELECT * FROM accounts WHERE sold=0")]
    return web.json_response({"uc": [{"id": k, "uc": v[0], "price": v[1]} for k, v in UC.items()], "accounts": accs})

async def create_order(r):
    d = await r.json()
    contact = str(d.get("contact", "")).strip()[:100]
    if len(contact) < 3: return web.json_response({"error": "Укажите контакт (Telegram или email)"}, status=400)
    if d.get("type") == "uc" and d.get("pack") in UC:
        pid = str(d.get("pubg_id", "")).strip()
        if not re.fullmatch(r"\d{5,14}", pid): return web.json_response({"error": "Неверный PUBG ID"}, status=400)
        n, price = UC[d["pack"]]; kind, item, title = "uc", d["pack"], f"{n} UC"
    elif d.get("type") == "acc":
        a = db.execute("SELECT * FROM accounts WHERE id=? AND sold=0", (d.get("id"),)).fetchone()
        if not a: return web.json_response({"error": "Аккаунт уже продан"}, status=409)
        q("UPDATE accounts SET sold=1 WHERE id=?", a["id"])  # резерв
        kind, item, title, price, pid = "acc", str(a["id"]), f"Аккаунт: {a['title']}", a["price"], None
    else: return web.json_response({"error": "Неверный заказ"}, status=400)
    tok = secrets.token_urlsafe(16)
    cur = q("INSERT INTO orders(kind,item,title,price,pubg_id,contact,token,status,ts) VALUES(?,?,?,?,?,?,?,?,?)",
            kind, item, title, price, pid, contact, tok, "new", int(time.time()))
    return web.json_response({"id": cur.lastrowid, "token": tok, "title": title, "price": price, "sbp": SBP})

def get_order(oid, tok):
    return db.execute("SELECT * FROM orders WHERE id=? AND token=?", (oid, tok)).fetchone()

async def mark_paid(r):
    d = await r.json(); o = get_order(d.get("id"), d.get("token"))
    if not o or o["status"] != "new": return web.json_response({"error": "Заказ не найден"}, status=404)
    q("UPDATE orders SET status='paid' WHERE id=?", o["id"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ok:{o['id']}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"no:{o['id']}")]])
    extra = f"\nPUBG ID: {o['pubg_id']}" if o["pubg_id"] else ""
    await bot.send_message(ADMIN, f"💰 Новая оплата, заказ #{o['id']}\n{o['title']} — {o['price']} ₽\n"
                                  f"Контакт: {o['contact']}{extra}", reply_markup=kb)
    return web.json_response({"ok": True})

async def order_status(r):
    o = get_order(r.query.get("id"), r.query.get("token"))
    if not o: return web.json_response({"error": "not found"}, status=404)
    res = {"id": o["id"], "title": o["title"], "price": o["price"], "status": o["status"]}
    if o["status"] == "done" and o["kind"] == "acc":
        res["secret"] = db.execute("SELECT secret FROM accounts WHERE id=?", (o["item"],)).fetchone()["secret"]
    return web.json_response(res)

# ---------- БОТ УВЕДОМЛЕНИЙ ----------
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(f"Ваш ID: {m.from_user.id}. Сюда приходят уведомления о покупках.")

@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def decide(c: CallbackQuery):
    if c.from_user.id != ADMIN: return await c.answer("Нет доступа", show_alert=True)
    act, oid = c.data.split(":")
    o = db.execute("SELECT * FROM orders WHERE id=? AND status='paid'", (int(oid),)).fetchone()
    if not o: return await c.answer("Уже обработан", show_alert=True)
    if act == "ok":
        q("UPDATE orders SET status='done' WHERE id=?", o["id"])
        if CHANNEL:
            await bot.send_message(CHANNEL, f"🔥 Кто-то только что купил: {o['title']}")
        await c.message.edit_text(c.message.text + "\n\n✅ Подтверждено")
    else:
        q("UPDATE orders SET status='rejected' WHERE id=?", o["id"])
        if o["kind"] == "acc": q("UPDATE accounts SET sold=0 WHERE id=?", o["item"])
        await c.message.edit_text(c.message.text + "\n\n❌ Отклонено")
    await c.answer()

@dp.message(Command("addacc"))
async def addacc(m: Message):
    # /addacc Название | Описание | Цена | логин:пароль
    if m.from_user.id != ADMIN: return
    try:
        t, d, p, s = [x.strip() for x in m.text.split(" ", 1)[1].split("|")]
        q("INSERT INTO accounts(title,descr,price,secret) VALUES(?,?,?,?)", t, d, int(p), s)
        await m.answer("Аккаунт добавлен ✅")
    except Exception:
        await m.answer("Формат: /addacc Название | Описание | Цена | логин:пароль")

# ---------- ЗАПУСК ----------
async def main():
    app = web.Application()
    app.router.add_get("/api/catalog", catalog)
    app.router.add_post("/api/order", create_order)
    app.router.add_post("/api/paid", mark_paid)
    app.router.add_get("/api/order", order_status)
    app.router.add_get("/", lambda r: web.FileResponse("site/index.html"))
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(E.get("PORT", 8080))).start()
    await dp.start_polling(bot, handle_signals=False)

asyncio.run(main())
