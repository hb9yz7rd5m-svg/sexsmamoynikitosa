"""
Бот записи к барберу (aiogram 3, SQLite, инлайн-кнопки).
Запуск:  pip install "aiogram>=3.4"   →   python bot.py
"""
import asyncio
import re
import logging
import sqlite3
from datetime import datetime, timedelta, date, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message

# ============== НАСТРОЙКИ ==============
TOKEN = "8709799458:AAFwlV6lAwXs6I3ati4wqyimURlUrkb_EU0"
ADMIN_IDS = {7521801228}           # Telegram ID админа(ов)
SERVICES = [                        # (название, цена в BYN, показывать «от»)
    ("Мужская стрижка", 35, False),
    ("Оформление бороды и усов", 10, True),
    ("Тонирование бороды", 15, True),
    ("Удаление волос воском", 5, False),
    ("Мужская стрижка + коррекция бороды", 40, True),
]
MASTERS = [                         # (имя, описание) — имя сохраняется в записи
    ("Гранд", "Барбер, 5 лет опыта"),
    ("Тимофей", "Про барбер, 7 лет опыта"),
    ("Руслан", "Про барбер"),
]
WORK_START, WORK_END = 10, 20       # рабочие часы
SLOT_MIN = 30                       # длина слота в минутах
LUNCH = ("14:00", "15:00")          # общий обед (с, до). Поставьте None, если обеда нет
MASTER_LUNCH = {                    # свой обед у конкретного мастера (перекрывает общий)
    # "Руслан": ("15:00", "16:00"),
    # "Гранд": None,                # None = у этого мастера обеда нет
}
DAYS_AHEAD = 7                      # на сколько дней вперёд можно записаться
REMIND_HOURS = 2                    # за сколько часов напоминать
ADDRESS = "📍 Адрес: Богдановича 8\n💬 Telegram: @tvoy_barber1"
# =======================================

def now():
    """Текущее время по Минску (UTC+3), не зависит от часового пояса сервера"""
    return datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)


def today():
    return now().date()


WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
STATUS = {"pending": "⏳ ожидает", "confirmed": "✅ подтверждена", "cancelled": "❌ отменена"}

db = sqlite3.connect("barber.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS bookings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, name TEXT, phone TEXT,
    service TEXT, price INTEGER,
    master TEXT DEFAULT '',
    date TEXT, time TEXT,
    status TEXT DEFAULT 'pending',
    reminded INTEGER DEFAULT 0,
    created TEXT
);
""")
db.execute("CREATE TABLE IF NOT EXISTS blocks("
           "master TEXT, date TEXT, time TEXT, PRIMARY KEY(master, date, time))")
# миграция: если база создана старой версией бота (без мастеров)
if "master" not in [r["name"] for r in db.execute("PRAGMA table_info(bookings)")]:
    db.execute("ALTER TABLE bookings ADD COLUMN master TEXT DEFAULT ''")
# защита от двойной записи: один мастер — одно время
db.execute("DROP INDEX IF EXISTS uq_slot")
db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_master ON bookings(master, date, time) "
           "WHERE status != 'cancelled'")
db.commit()


# ---------- helpers ----------
def kb(rows):
    """rows: [[(text, callback_data), ...], ...]"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def chunk(buttons, n):
    return [buttons[i:i + n] for i in range(0, len(buttons), n)]


def fmt_date(iso):
    """Полный вид: «понедельник, 12 октября»"""
    d = date.fromisoformat(iso)
    return f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def fmt_btn(iso):
    """Короткий вид для кнопок: «Сегодня», «Завтра», «Ср, 14 окт»"""
    d = date.fromisoformat(iso)
    if d == today():
        return "Сегодня"
    if d == today() + timedelta(days=1):
        return "Завтра"
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS_SHORT[d.month - 1]}"


def all_slots():
    t = datetime.combine(today(), datetime.min.time()).replace(hour=WORK_START)
    end = t.replace(hour=WORK_END)
    out = []
    while t < end:
        out.append(t.strftime("%H:%M"))
        t += timedelta(minutes=SLOT_MIN)
    return out


def lunch_for(master):
    return MASTER_LUNCH.get(master, LUNCH)


def free_slots(iso, master):
    lunch = lunch_for(master)
    taken = {r["time"] for r in db.execute(
        "SELECT time FROM bookings WHERE date=? AND master=? AND status!='cancelled'", (iso, master))}
    taken |= {r["time"] for r in db.execute(
        "SELECT time FROM blocks WHERE date=? AND master=?", (iso, master))}
    cur = now()
    return [s for s in all_slots()
            if s not in taken
            and not (lunch and lunch[0] <= s < lunch[1])
            and datetime.fromisoformat(f"{iso} {s}") > cur]


def price_text(name, price):
    for n, p, frm in SERVICES:
        if n == name:
            return f"{'от ' if frm else ''}{p} BYN"
    return f"{price} BYN"


def get_booking(bid):
    return db.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()


def booking_text(b):
    return (f"📋 Запись №{b['id']}\n"
            f"Услуга: {b['service']} — {price_text(b['service'], b['price'])}\n"
            f"Мастер: {b['master'] or '—'}\n"
            f"Дата: {fmt_date(b['date'])}\n"
            f"Время: {b['time']}\n"
            f"Клиент: {b['name']}\n"
            f"Телефон: {b['phone']}\n"
            f"Статус: {STATUS[b['status']]}")


MAIN_MENU = kb([
    [("✂️ Записаться", "book")],
    [("📅 Мои записи", "my")],
    [("📍 Контакты", "contacts")],
])
GREETING = (
    "👋 Здравствуйте!\n\n"
    "Это бот для записи к барберу ✂️\n"
    "Свежая стрижка, чёткие линии и отличное настроение — всё это в пару кликов.\n\n"
    "Выберите действие ниже 👇"
)


async def show(cb: CallbackQuery, text, markup=None):
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


# ---------- клиентская часть ----------
client = Router()


class Book(StatesGroup):
    name = State()
    phone = State()
    confirm = State()


@client.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(GREETING, reply_markup=MAIN_MENU)


@client.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, GREETING, MAIN_MENU)


@client.callback_query(F.data == "contacts")
async def contacts(cb: CallbackQuery):
    await show(cb, ADDRESS, kb([[("⬅️ Назад", "menu")]]))


# 1. услуга
@client.callback_query(F.data == "book")
async def choose_service(cb: CallbackQuery):
    rows = [[(f"{n} — {'от ' if frm else ''}{p} BYN", f"s:{i}")] for i, (n, p, frm) in enumerate(SERVICES)]
    rows.append([("⬅️ Назад", "menu")])
    await show(cb, "Выберите услугу:", kb(rows))


# 2. мастер
@client.callback_query(F.data.startswith("s:"))
async def choose_master(cb: CallbackQuery):
    svc = cb.data.split(":")[1]
    rows = [[(f"{n} · {desc}", f"m:{svc}:{i}")] for i, (n, desc) in enumerate(MASTERS)]
    rows.append([("⬅️ Назад", "book")])
    await show(cb, "Выберите мастера:", kb(rows))


# 3. дата
@client.callback_query(F.data.startswith("m:"))
async def choose_date(cb: CallbackQuery):
    _, svc, mi = cb.data.split(":")
    master = MASTERS[int(mi)][0]
    buttons = []
    for i in range(DAYS_AHEAD):
        iso = (today() + timedelta(days=i)).isoformat()
        if free_slots(iso, master):
            buttons.append((fmt_btn(iso), f"d:{svc}:{mi}:{iso}"))
    if not buttons:
        return await show(cb, f"У мастера {master} пока нет свободных окон 😕\nВыберите другого мастера.",
                          kb([[("⬅️ Назад", f"s:{svc}")]]))
    rows = chunk(buttons, 3) + [[("⬅️ Назад", f"s:{svc}")]]
    await show(cb, f"Мастер: {master}\nВыберите дату:", kb(rows))


# 4. время
@client.callback_query(F.data.startswith("d:"))
async def choose_time(cb: CallbackQuery):
    _, svc, mi, iso = cb.data.split(":")
    master = MASTERS[int(mi)][0]
    slots = free_slots(iso, master)
    if not slots:
        return await cb.answer("На этот день всё занято", show_alert=True)
    buttons = [(s, f"t:{svc}:{mi}:{iso}:{s.replace(':', '')}") for s in slots]
    rows = chunk(buttons, 4) + [[("⬅️ Назад", f"m:{svc}:{mi}")]]
    await show(cb, f"Мастер: {master}\nСвободное время на {fmt_date(iso)}:", kb(rows))


# 5. имя → телефон → подтверждение
@client.callback_query(F.data.startswith("t:"))
async def got_time(cb: CallbackQuery, state: FSMContext):
    _, svc, mi, iso, hhmm = cb.data.split(":")
    await state.update_data(svc=int(svc), master=MASTERS[int(mi)][0], date=iso,
                            time=f"{hhmm[:2]}:{hhmm[2:]}")
    await state.set_state(Book.name)
    await show(cb, "Как вас зовут? (напишите имя)", kb([[("✖️ Отмена", "menu")]]))


@client.message(StateFilter(Book.name), F.text)
async def got_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:50])
    await state.set_state(Book.phone)
    await m.answer("Ваш номер телефона:", reply_markup=kb([[("✖️ Отмена", "menu")]]))


@client.message(StateFilter(Book.phone), F.text)
async def got_phone(m: Message, state: FSMContext):
    phone = "".join(c for c in m.text if c not in " -()").strip()
    if not re.fullmatch(r"\+375\d{9}|80\d{9}", phone):
        return await m.answer(
            "Введите номер в формате +375XXXXXXXXX (13 символов) или 80XXXXXXXXX (11 символов).\n"
            "Например: +375445889273\n\nВведите ещё раз:")
    await state.update_data(phone=phone)
    d = await state.get_data()
    svc_name, price, _ = SERVICES[d["svc"]]
    await state.set_state(Book.confirm)
    await m.answer(
        f"Проверьте запись:\n\n{svc_name} — {price_text(svc_name, price)}\nМастер: {d['master']}\n{fmt_date(d['date'])} в {d['time']}\n"
        f"{d['name']}, {d['phone']}",
        reply_markup=kb([[("✅ Подтвердить", "confirm")], [("✖️ Отмена", "menu")]]),
    )


@client.callback_query(StateFilter(Book.confirm), F.data == "confirm")
async def confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    svc_name, price, _ = SERVICES[d["svc"]]
    if db.execute("SELECT 1 FROM blocks WHERE master=? AND date=? AND time=?",
                  (d["master"], d["date"], d["time"])).fetchone():
        await state.clear()
        return await show(cb, "Это время стало недоступно 😕 Выберите другое.",
                          kb([[("✂️ Записаться снова", "book")]]))
    try:
        cur = db.execute(
            "INSERT INTO bookings(user_id,name,phone,service,price,master,date,time,created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (cb.from_user.id, d["name"], d["phone"], svc_name, price, d["master"],
             d["date"], d["time"], now().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        await state.clear()
        return await show(cb, "Это время у мастера только что заняли 😕 Выберите другое.",
                          kb([[("✂️ Записаться снова", "book")]]))
    await state.clear()
    b = get_booking(cur.lastrowid)
    await show(cb, "✅ Вы записаны! Мы подтвердим запись в ближайшее время.\n\n" + booking_text(b)
               + "\n\n📍 Ждём вас по адресу: Богдановича 8",
               kb([[("📅 Мои записи", "my")], [("⬅️ В меню", "menu")]]))
    for admin in ADMIN_IDS:  # заявка прилетает админу
        await bot.send_message(
            admin, "🔔 Новая заявка!\n\n" + booking_text(b),
            reply_markup=kb([[("✅ Подтвердить", f"a_ok:{b['id']}"),
                              ("❌ Отклонить", f"a_no:{b['id']}")]]))


# личный кабинет
@client.callback_query(F.data == "my")
async def my_bookings(cb: CallbackQuery):
    rows = db.execute(
        "SELECT * FROM bookings WHERE user_id=? AND status!='cancelled' AND date>=? "
        "ORDER BY date, time", (cb.from_user.id, today().isoformat())).fetchall()
    if not rows:
        return await show(cb, "У вас нет предстоящих записей.",
                          kb([[("✂️ Записаться", "book")], [("⬅️ Назад", "menu")]]))
    text = "📅 Ваши записи:\n\n" + "\n\n".join(
        f"№{b['id']} · {fmt_date(b['date'])} в {b['time']}\n{b['service']}, мастер {b['master'] or '—'} · {STATUS[b['status']]}"
        for b in rows)
    buttons = [[(f"Отменить №{b['id']} ({fmt_btn(b['date'])}, {b['time']})", f"c:{b['id']}")] for b in rows]
    await show(cb, text, kb(buttons + [[("⬅️ Назад", "menu")]]))


@client.callback_query(F.data.startswith("c:"))
async def cancel_own(cb: CallbackQuery, bot: Bot):
    b = get_booking(int(cb.data.split(":")[1]))
    if not b or b["user_id"] != cb.from_user.id or b["status"] == "cancelled":
        return await cb.answer("Запись не найдена", show_alert=True)
    db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
    db.commit()
    await cb.answer("Запись отменена")
    for admin in ADMIN_IDS:
        await bot.send_message(admin, f"⚠️ Клиент отменил запись\n\n{booking_text(get_booking(b['id']))}")
    await my_bookings(cb)


# ---------- админ-панель ----------
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

def admin_menu():  # пересчитываем даты каждый раз
    t = today()
    return kb([
        [("📆 Сегодня", f"a_day:{t.isoformat()}"),
         ("📆 Завтра", f"a_day:{(t + timedelta(days=1)).isoformat()}")],
        [("🗓 Другая дата", "a_dates")],
        [("⏸ Перерывы и закрытое время", "a_br")],
    ])


@admin.message(Command("admin"))
async def admin_panel(m: Message):
    await m.answer("Админ-панель:", reply_markup=admin_menu())


@admin.callback_query(F.data == "a_menu")
async def a_menu(cb: CallbackQuery):
    await show(cb, "Админ-панель:", admin_menu())


@admin.callback_query(F.data == "a_dates")
async def a_dates(cb: CallbackQuery):
    buttons = [(fmt_btn(iso), f"a_day:{iso}") for iso in
               [(today() + timedelta(days=i)).isoformat() for i in range(14)]]
    await show(cb, "Выберите дату:", kb(chunk(buttons, 3) + [[("⬅️ Назад", "a_menu")]]))


@admin.callback_query(F.data.startswith("a_day:"))
async def a_day(cb: CallbackQuery):
    iso = cb.data.split(":")[1]
    rows = db.execute("SELECT * FROM bookings WHERE date=? AND status!='cancelled' ORDER BY time",
                      (iso,)).fetchall()
    if not rows:
        return await show(cb, f"На {fmt_date(iso)} записей нет.", kb([[("⬅️ Назад", "a_menu")]]))
    buttons = [[(f"{b['time']} · {b['master'] or '—'} · {b['name']} {'✅' if b['status']=='confirmed' else '⏳'}",
                 f"a_view:{b['id']}")] for b in rows]
    await show(cb, f"Записи на {fmt_date(iso)} ({len(rows)}):",
               kb(buttons + [[("⬅️ Назад", "a_menu")]]))


@admin.callback_query(F.data.startswith("a_view:"))
async def a_view(cb: CallbackQuery):
    b = get_booking(int(cb.data.split(":")[1]))
    if not b:
        return await cb.answer("Не найдено", show_alert=True)
    rows = []
    if b["status"] == "pending":
        rows.append([("✅ Подтвердить", f"a_ok:{b['id']}")])
    if b["status"] != "cancelled":
        rows.append([("🗑 Удалить запись", f"a_del:{b['id']}")])
    rows.append([("⬅️ К списку", f"a_day:{b['date']}")])
    await show(cb, booking_text(b), kb(rows))


async def notify_client(bot: Bot, b, text):
    try:
        await bot.send_message(b["user_id"], text + "\n\n" + booking_text(get_booking(b["id"])))
    except Exception:
        logging.warning("Не удалось написать клиенту %s", b["user_id"])


@admin.callback_query(F.data.startswith("a_ok:"))
async def a_ok(cb: CallbackQuery, bot: Bot):
    b = get_booking(int(cb.data.split(":")[1]))
    if not b or b["status"] == "cancelled":
        return await cb.answer("Запись уже отменена", show_alert=True)
    db.execute("UPDATE bookings SET status='confirmed' WHERE id=?", (b["id"],))
    db.commit()
    await notify_client(bot, b, "✅ Ваша запись подтверждена!")
    await show(cb, booking_text(get_booking(b["id"])), kb([[("⬅️ К списку", f"a_day:{b['date']}")]]))


@admin.callback_query(F.data.startswith(("a_no:", "a_del:")))
async def a_cancel(cb: CallbackQuery, bot: Bot):
    action, bid = cb.data.split(":")
    b = get_booking(int(bid))
    if not b or b["status"] == "cancelled":
        return await cb.answer("Уже отменена", show_alert=True)
    db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
    db.commit()
    msg = "❌ К сожалению, заявка отклонена." if action == "a_no" else "❌ Ваша запись отменена администратором."
    await notify_client(bot, b, msg + " Вы можете выбрать другое время.")
    await show(cb, "Запись удалена, клиент уведомлён.\n\n" + booking_text(get_booking(b["id"])),
               kb([[("⬅️ К списку", f"a_day:{b['date']}")]]))


# ---------- перерывы через админку ----------
def block_view(mi, iso):
    """Сетка времени мастера на день: нажатие закрывает/открывает слот"""
    master = MASTERS[mi][0]
    booked = {r["time"] for r in db.execute(
        "SELECT time FROM bookings WHERE master=? AND date=? AND status!='cancelled'", (master, iso))}
    blocked = {r["time"] for r in db.execute(
        "SELECT time FROM blocks WHERE master=? AND date=?", (master, iso))}
    lunch = lunch_for(master)
    cur = now()
    buttons = []
    for t in all_slots():
        if datetime.fromisoformat(f"{iso} {t}") <= cur:
            continue
        if t in booked:
            label = f"📋 {t}"
        elif lunch and lunch[0] <= t < lunch[1]:
            label = f"🍽 {t}"
        elif t in blocked:
            label = f"⛔ {t}"
        else:
            label = t
        buttons.append((label, f"a_bt:{mi}:{iso}:{t.replace(':', '')}"))
    text = (f"⏸ Мастер: {master}\n{fmt_date(iso)}\n\n"
            "Нажмите на время, чтобы закрыть или открыть его.\n"
            "⛔ закрыто · 📋 есть запись · 🍽 обед по расписанию")
    if not buttons:
        text += "\n\nНа этот день свободного времени уже нет."
    rows = chunk(buttons, 3) + [
        [("🔒 Закрыть весь день", f"a_bx:{mi}:{iso}"), ("🔓 Открыть весь день", f"a_bo:{mi}:{iso}")],
        [("⬅️ Назад", f"a_bm:{mi}")],
    ]
    return text, kb(rows)


@admin.callback_query(F.data == "a_br")
async def a_br(cb: CallbackQuery):
    rows = [[(f"{n}", f"a_bm:{i}")] for i, (n, _) in enumerate(MASTERS)]
    rows.append([("⬅️ Назад", "a_menu")])
    await show(cb, "Для какого мастера закрыть время?", kb(rows))


@admin.callback_query(F.data.startswith("a_bm:"))
async def a_bm(cb: CallbackQuery):
    mi = int(cb.data.split(":")[1])
    buttons = [(fmt_btn(iso), f"a_bd:{mi}:{iso}") for iso in
               [(today() + timedelta(days=i)).isoformat() for i in range(14)]]
    await show(cb, f"Мастер: {MASTERS[mi][0]}\nВыберите дату:",
               kb(chunk(buttons, 3) + [[("⬅️ Назад", "a_br")]]))


@admin.callback_query(F.data.startswith("a_bd:"))
async def a_bd(cb: CallbackQuery):
    _, mi, iso = cb.data.split(":")
    text, markup = block_view(int(mi), iso)
    await show(cb, text, markup)


@admin.callback_query(F.data.startswith("a_bt:"))
async def a_bt(cb: CallbackQuery):
    _, mi, iso, hhmm = cb.data.split(":")
    mi, t = int(mi), f"{hhmm[:2]}:{hhmm[2:]}"
    master = MASTERS[mi][0]
    if db.execute("SELECT 1 FROM bookings WHERE master=? AND date=? AND time=? AND status!='cancelled'",
                  (master, iso, t)).fetchone():
        return await cb.answer("На это время есть запись. Сначала удалите её в списке записей.", show_alert=True)
    lunch = lunch_for(master)
    if lunch and lunch[0] <= t < lunch[1]:
        return await cb.answer("Это обед по расписанию (настройка LUNCH в файле бота).", show_alert=True)
    if db.execute("SELECT 1 FROM blocks WHERE master=? AND date=? AND time=?", (master, iso, t)).fetchone():
        db.execute("DELETE FROM blocks WHERE master=? AND date=? AND time=?", (master, iso, t))
    else:
        db.execute("INSERT OR IGNORE INTO blocks(master,date,time) VALUES(?,?,?)", (master, iso, t))
    db.commit()
    text, markup = block_view(mi, iso)
    await show(cb, text, markup)


@admin.callback_query(F.data.startswith("a_bx:"))
async def a_bx(cb: CallbackQuery):
    _, mi, iso = cb.data.split(":")
    master = MASTERS[int(mi)][0]
    booked = {r["time"] for r in db.execute(
        "SELECT time FROM bookings WHERE master=? AND date=? AND status!='cancelled'", (master, iso))}
    for t in all_slots():
        if t not in booked:
            db.execute("INSERT OR IGNORE INTO blocks(master,date,time) VALUES(?,?,?)", (master, iso, t))
    db.commit()
    await cb.answer("Весь день закрыт (кроме времени с записями)")
    text, markup = block_view(int(mi), iso)
    await show(cb, text, markup)


@admin.callback_query(F.data.startswith("a_bo:"))
async def a_bo(cb: CallbackQuery):
    _, mi, iso = cb.data.split(":")
    db.execute("DELETE FROM blocks WHERE master=? AND date=?", (MASTERS[int(mi)][0], iso))
    db.commit()
    await cb.answer("Всё время открыто")
    text, markup = block_view(int(mi), iso)
    await show(cb, text, markup)


# ---------- напоминания ----------
async def reminder_loop(bot: Bot):
    while True:
        try:
            cur = now()
            rows = db.execute("SELECT * FROM bookings WHERE status!='cancelled' AND reminded=0").fetchall()
            for b in rows:
                dt = datetime.fromisoformat(f"{b['date']} {b['time']}")
                if cur < dt <= cur + timedelta(hours=REMIND_HOURS):
                    await bot.send_message(
                        b["user_id"], f"⏰ Напоминание: сегодня в {b['time']} у вас запись — {b['service']}"
                        + (f", мастер {b['master']}." if b['master'] else "."))
                    db.execute("UPDATE bookings SET reminded=1 WHERE id=?", (b["id"],))
                    db.commit()
        except Exception:
            logging.exception("reminder error")
        await asyncio.sleep(60)


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(admin, client)   # админский роутер первым
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
