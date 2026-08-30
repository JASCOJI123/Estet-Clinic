import asyncio
import logging
import os
import json
import re
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, ForceReply,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from groq import Groq
from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials

# ============ SOZLAMALAR (Environment Variables orqali olinadi) ============
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "google_creds.json")
ADMIN_CHAT_IDS = [x.strip() for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if x.strip()]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # Mini App'ning HTTPS manzili (GitHub Pages va h.k.)
ADMIN_PANEL_PASSWORD = os.environ.get("ADMIN_PANEL_PASSWORD", "")  # Mini App'dagi Ro'yxat bo'limi uchun parol

GROQ_TEXT_MODEL = "openai/gpt-oss-120b"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  # rasm tahlili uchun
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"  # ovozni matnga aylantirish uchun

FOLLOWUP_DELAY_HOURS = 20  # javobsiz qolgan mijozga necha soatdan keyin eslatma yuborish

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
groq_client = Groq(api_key=GROQ_API_KEY)

# ============ MA'LUMOTLAR BAZASI (statistika + follow-up uchun) ============
DB_PATH = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            lang TEXT,
            started_at TEXT,
            last_message_at TEXT,
            lead_sent INTEGER DEFAULT 0,
            followup_sent INTEGER DEFAULT 0,
            referred_by INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            info TEXT,
            source TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, phone TEXT, birth TEXT, address TEXT,
            passport TEXT, package TEXT, note TEXT,
            operation_date TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS package_interest (
            package TEXT, date TEXT, count INTEGER DEFAULT 0,
            PRIMARY KEY (package, date)
        )
    """)
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS handoff_messages (
            message_id INTEGER PRIMARY KEY,
            user_id INTEGER
        )
    """)
    # Eski bazalarda yo'q ustunlarni xavfsiz qo'shish (migratsiya)
    for alter_sql in [
        "ALTER TABLE users ADD COLUMN referred_by INTEGER",
        "ALTER TABLE leads ADD COLUMN source TEXT",
        "ALTER TABLE users ADD COLUMN handoff_active INTEGER DEFAULT 0",
    ]:
        try:
            c.execute(alter_sql)
        except sqlite3.OperationalError:
            pass  # ustun allaqachon mavjud
    conn.commit()
    conn.close()

def get_meta(key: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_meta(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()

def get_referrer(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def set_handoff(user_id: int, active: bool):
    """Mijozni 'operator bilan jonli suhbat' rejimiga qo'yadi yoki chiqaradi."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET handoff_active=? WHERE user_id=?", (1 if active else 0, user_id))
    if c.rowcount == 0:
        # Foydalanuvchi hali users jadvalida yo'q bo'lsa (kamdan-kam holat)
        now = datetime.now().isoformat()
        c.execute(
            "INSERT INTO users (user_id, username, started_at, last_message_at, handoff_active) VALUES (?,?,?,?,?)",
            (user_id, "", now, now, 1 if active else 0),
        )
    conn.commit()
    conn.close()

def is_handoff_active(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT handoff_active FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])

def save_handoff_message(message_id: int, user_id: int):
    """Guruhga yuborilgan xabar ID'sini mijoz ID'siga bog'laydi — shunda operator
    shu xabarga 'Reply' qilsa, tizim kimga javob berilayotganini biladi."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO handoff_messages (message_id, user_id) VALUES (?,?)", (message_id, user_id))
    conn.commit()
    conn.close()

def get_handoff_user(message_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM handoff_messages WHERE message_id=?", (message_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def handoff_done_keyboard(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Suhbatni yakunlash", callback_data=f"donehandoff_{user_id}")
    ]])

async def send_handoff_notice(chat_id, info_text: str, user_id: int):
    """Guruhga ikkita xabar yuboradi:
    1) to'liq ma'lumot + 'Suhbatni yakunlash' tugmasi
    2) 'shu yerga yozing' — bosilganda yozish oynasi avtomatik shu xabarga
       javob berish (reply) rejimiga o'tadi, qo'lda Reply bosish shart emas."""
    sent_info = await bot.send_message(chat_id, info_text, reply_markup=handoff_done_keyboard(user_id))
    save_handoff_message(sent_info.message_id, user_id)

    sent_prompt = await bot.send_message(
        chat_id,
        "✍️ Javob yozish uchun shu xabarni bosing:",
        reply_markup=ForceReply(input_field_placeholder="Javobingizni shu yerga yozing..."),
    )
    save_handoff_message(sent_prompt.message_id, user_id)

def track_package_interest(package: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO package_interest (package, date, count) VALUES (?,?,1)
        ON CONFLICT(package, date) DO UPDATE SET count = count + 1
    """, (package, today))
    conn.commit()
    conn.close()

def save_registration(name, phone, birth, address, passport, package, note, operation_date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO registrations (name, phone, birth, address, passport, package, note, operation_date, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (name, phone, birth, address, passport, package, note, operation_date, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_operations_due_today():
    """Amaliyotdan 3, 7 yoki 30 kun o'tgan mijozlarni topadi (kuzatuv qo'ng'irog'i uchun)."""
    now_tashkent = datetime.utcnow() + timedelta(hours=5)
    today = now_tashkent.date()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, phone, operation_date FROM registrations WHERE operation_date IS NOT NULL AND operation_date != ''")
    rows = c.fetchall()
    conn.close()
    due = []
    for name, phone, op_date_str in rows:
        try:
            op_date = datetime.strptime(op_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        days_passed = (today - op_date).days
        if days_passed in (3, 7, 30):
            due.append((name, phone, days_passed))
    return due

def get_daily_report_data(date_str: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM leads WHERE created_at LIKE ?", (date_str + "%",))
    total_leads = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leads WHERE created_at LIKE ? AND source LIKE '%booking%'", (date_str + "%",))
    total_bookings = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leads WHERE created_at LIKE ? AND source LIKE '%register%'", (date_str + "%",))
    total_registrations = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE started_at LIKE ?", (date_str + "%",))
    new_users = c.fetchone()[0]
    c.execute("SELECT package, SUM(count) as total FROM package_interest WHERE date=? GROUP BY package ORDER BY total DESC LIMIT 1", (date_str,))
    top_pkg_row = c.fetchone()
    conn.close()
    top_package = top_pkg_row[0] if top_pkg_row else None
    return {
        "total_leads": total_leads, "total_bookings": total_bookings,
        "total_registrations": total_registrations, "new_users": new_users,
        "top_package": top_package,
    }

def touch_user(user_id: int, username: str, lang: str = None, referred_by: int = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if c.fetchone():
        if lang:
            c.execute("UPDATE users SET last_message_at=?, username=?, lang=?, followup_sent=0 WHERE user_id=?",
                      (now, username, lang, user_id))
        else:
            c.execute("UPDATE users SET last_message_at=?, username=?, followup_sent=0 WHERE user_id=?",
                      (now, username, user_id))
    else:
        c.execute("INSERT INTO users (user_id, username, lang, started_at, last_message_at, referred_by) VALUES (?,?,?,?,?,?)",
                  (user_id, username, lang, now, now, referred_by))
    conn.commit()
    conn.close()

def mark_lead(user_id: int, username: str, info: str, source: str = "chat"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET lead_sent=1 WHERE user_id=?", (user_id,))
    c.execute("INSERT INTO leads (user_id, username, info, source, created_at) VALUES (?,?,?,?,?)",
              (user_id, username, info, source, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leads")
    total_leads = c.fetchone()[0]
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    c.execute("SELECT COUNT(*) FROM users WHERE started_at >= ?", (week_ago,))
    new_this_week = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leads WHERE created_at >= ?", (week_ago,))
    leads_this_week = c.fetchone()[0]
    conn.close()
    return total_users, total_leads, new_this_week, leads_this_week

def get_stale_users(hours: int):
    """So'nggi xabaridan beri X soat o'tgan, lead bermagan va oldin eslatma olmagan foydalanuvchilar"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute("""
        SELECT user_id, lang FROM users
        WHERE last_message_at <= ? AND lead_sent=0 AND followup_sent=0
    """, (cutoff,))
    rows = c.fetchall()
    conn.close()
    return rows

def mark_followup_sent(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET followup_sent=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ============ SYSTEM PROMPT ============
SYSTEM_PROMPT = """
Sen "Estet Clinic" markazining virtual konsultanti bo'lib ishlaysan. Bu — plastik va estetik jarrohlik markazi bo'lib, sen faqat SOCH EKISH (hair transplant) yo'nalishi bo'yicha maslahat berasan.

TIL QOIDASI:
Foydalanuvchi qaysi tilda yozsa (o'zbek, rus yoki ingliz), shu tilda javob ber.

SENING VAZIFANG:
1. Mijozning sochsizlik holatini tabiiy suhbat orqali aniqlash (davomiyligi, joylashuvi, jiddiyligi)
2. Shu asosida taxminiy mos paketni aytish
3. Klinika haqidagi savollarga aniq va ishonchli javob berish
4. Suhbat oxirida ism va telefon raqamini so'rab yakunlash

MUHIM CHEKLOVLAR:
- Sen shifokor emassan — aniq tibbiy diagnoz qo'yma.
- Narxni "taxminiy, klinikada bepul konsultatsiyada aniqlashtiriladi" deb ayt.
- Og'ir tibbiy holat/asorat savollarida — "Buni klinikadagi shifokor bilan muhokama qiling" deb yo'naltir.
- Bosim o'tkazma, sotuvga majburlama.

KLINIKA MA'LUMOTLARI:
Nomi: Estet Clinic
Aloqa: +998 97 308-09-99, +998 97 300-90-19
Instagram/Taplink: taplink.cc/estetclinic

PAKETLAR (Soch ekish):
- Minimum: 2.000-2.500 graft, 700$ — 2 seans plazmoterapiya, 1 yillik bepul konsultatsiya, umrlik kafolat, operatsiyadan keyingi preparatlar
- Medium: 2.600-3.500 graft, 900$ — xuddi shu xizmatlar, ko'proq graft
- Maximum: 3.600-4.500 graft, 1100$ — + 1 kunlik bepul mehmonxona, 3 seans plazmoterapiya, laboratoriya analizlari
- Maximum+: 4.600-6.000 graft, 1300$ — Maximum bilan bir xil, eng ko'p graft

TAVSIYA MANTIG'I (taxminiy):
- Faqat peshona chizig'i → Minimum/Medium
- Tepa qism ham ochilgan → Medium/Maximum
- Umumiy keng maydonda sochsizlik → Maximum/Maximum+
- Aniq son faqat klinikada bepul konsultatsiyada belgilanadi

Agar mijoz rasm yuborgan bo'lsa va rasm tavsifi senga "[RASM TAHLILI: ...]" formatida berilsa, shu tahlildan foydalanib tavsiyani aniqroq ber, lekin baribir "yakuniy xulosa faqat klinikadagi ko'rikda aniqlanadi" deb ta'kidla.

SUHBAT USLUBI:
- Iliq, ishonchli, professional
- Qisqa xabarlar (mobil ekran uchun)
- Emoji me'yorida (✅ 📍 📞)

SUHBATNI YAKUNLASH:
Faqat foydalanuvchi o'zining HAQIQIY ismini VA HAQIQIY telefon raqamini aniq yozganidan keyingina:
"Rahmat! Ma'lumotlaringizni mutaxassisimizga yubordim. Tez orada siz bilan bog'lanishadi. 📞 Shoshilinch bo'lsa: +998 97 308-09-99"

Javobingning OXIRIGA aniq shu formatda maxsus qator qo'sh (foydalanuvchiga ko'rinmasin, faqat tizim uchun):
[LEAD_CAPTURED: ism=<ism>, telefon=<raqam>]

QATTIQ QOIDALAR (buzilishi mumkin emas):
- Bu qatorni HECH QACHON bo'sh yoki taxminiy qiymatlar bilan yozma (masalan "ism=, telefon=" yoki "ism=mijoz, telefon=noma'lum" — bularning barchasi QATʼIYAN TAQIQLANGAN).
- Bu qatorni faqat ISM VA TELEFON RAQAMI ikkalasi ham foydalanuvchi tomonidan matnda aniq, to'liq yozilgan bo'lsagina qo'sh.
- Agar foydalanuvchi hali ism yoki telefon bermagan bo'lsa (masalan faqat "Bepul konsultatsiya olmoqchiman" yoki "Narxlar qanday?" degan bo'lsa) — bu qatorni QO'SHMA, buning o'rniga avval ism va telefonni so'ra.
- Bitta suhbatda bu qatorni faqat BIR MARTA, ma'lumot birinchi marta to'liq berilganda yoz.
"""

VISION_PROMPT = """Bu odam boshining rasmi. Sochsizlik holatini umumiy tarzda tasvirlab ber (masalan: "peshona chizig'i orqaga tortilgan", "tepa qismda yupqalashish bor", "keng maydonda sochsizlik" kabi). Aniq tibbiy diagnoz qo'yma, faqat vizual tavsif ber, 2-3 gap."""

HANDOFF_WAITING_MSG = {
    "uz": "✅ Xabaringiz operatorga yuborildi. Operator botning asosiy chatiga javob yozadi — iltimos, shu yerni kuzatib turing.",
    "ru": "✅ Ваше сообщение отправлено оператору. Оператор ответит в основном чате бота — пожалуйста, проверьте его.",
    "tr": "✅ Mesajınız operatöre iletildi. Operatör botun ana sohbetinden yanıt verecek — lütfen orayı kontrol edin.",
    "tg": "✅ Паёми шумо ба оператор фиристода шуд. Оператор дар чати асосии бот ҷавоб медиҳад — лутфан онро назорат кунед.",
}

# ============ HOLAT (FSM) ============
class Chat(StatesGroup):
    talking = State()

# ============ TUGMALAR ============
def lang_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="🇺🇿 O'zbek"),
            KeyboardButton(text="🇷🇺 Русский"),
            KeyboardButton(text="🇬🇧 English"),
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

QUICK_REPLIES = {
    "uz": [("💰 Narxlar", "narxlar"), ("📍 Manzil", "manzil"), ("📅 Bepul konsultatsiya", "konsultatsiya")],
    "ru": [("💰 Цены", "narxlar"), ("📍 Адрес", "manzil"), ("📅 Бесплатная консультация", "konsultatsiya")],
    "en": [("💰 Prices", "narxlar"), ("📍 Address", "manzil"), ("📅 Free consultation", "konsultatsiya")],
}

def quick_reply_keyboard(lang: str):
    buttons = [[InlineKeyboardButton(text=label, callback_data=cb)] for label, cb in QUICK_REPLIES.get(lang, QUICK_REPLIES["uz"])]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

QUICK_REPLY_TEXT = {
    "narxlar": {
        "uz": "💰 Paketlar:\nMinimum — 700$ (2000-2500 graft)\nMedium — 900$ (2600-3500 graft)\nMaximum — 1100$ (3600-4500 graft)\nMaximum+ — 1300$ (4600-6000 graft)\n\nAniq narx klinikadagi bepul konsultatsiyada belgilanadi.",
        "ru": "💰 Пакеты:\nMinimum — 700$ (2000-2500 графтов)\nMedium — 900$ (2600-3500 графтов)\nMaximum — 1100$ (3600-4500 графтов)\nMaximum+ — 1300$ (4600-6000 графтов)\n\nТочная цена определяется на бесплатной консультации в клинике.",
        "en": "💰 Packages:\nMinimum — $700 (2000-2500 grafts)\nMedium — $900 (2600-3500 grafts)\nMaximum — $1100 (3600-4500 grafts)\nMaximum+ — $1300 (4600-6000 grafts)\n\nExact price is determined during a free consultation at the clinic.",
    },
    "manzil": {
        "uz": "📍 Bog'lanish uchun:\n+998 97 308-09-99\n+998 97 300-90-19\n\nBatafsil: taplink.cc/estetclinic",
        "ru": "📍 Контакты:\n+998 97 308-09-99\n+998 97 300-90-19\n\nПодробнее: taplink.cc/estetclinic",
        "en": "📍 Contact:\n+998 97 308-09-99\n+998 97 300-90-19\n\nMore info: taplink.cc/estetclinic",
    },
    "konsultatsiya": {
        "uz": "📅 Bepul konsultatsiya uchun ismingiz va telefon raqamingizni yozib qoldiring, mutaxassisimiz siz bilan bog'lanadi.",
        "ru": "📅 Чтобы записаться на бесплатную консультацию, оставьте, пожалуйста, ваше имя и номер телефона — наш специалист свяжется с вами.",
        "en": "📅 To book a free consultation, please share your name and phone number — our specialist will reach out to you.",
    },
}

WELCOME = {
    "uz": "Assalomu alaykum! Estet Clinic soch ekish konsultatsiya botiga xush kelibsiz 🌿\nSizga qanday yordam bera olaman?",
    "ru": "Здравствуйте! Добро пожаловать в консультационного бота Estet Clinic по пересадке волос 🌿\nЧем могу помочь?",
    "en": "Hello! Welcome to Estet Clinic's hair transplant consultation bot 🌿\nHow can I help you today?",
}

FOLLOWUP_TEXT = {
    "uz": "Salom! 👋 Sizga hali savolingiz bo'yicha yordam kerakmi? Bemalol yozing, yordam beraman.",
    "ru": "Здравствуйте! 👋 Вам всё ещё нужна помощь по вашему вопросу? Напишите, я на связи.",
    "en": "Hi there! 👋 Do you still need help with your question? Feel free to write anytime.",
}

# ============ /start ============
def webapp_open_keyboard():
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌿 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    # Referral (do'stni taklif qilish) parametrini o'qish: /start ref_12345
    referred_by = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            candidate = int(parts[1][4:])
            if candidate != message.from_user.id:
                referred_by = candidate
        except ValueError:
            pass

    is_new_user = get_referrer(message.from_user.id) is None and referred_by is not None
    touch_user(message.from_user.id, message.from_user.username or "", referred_by=referred_by)

    if referred_by and is_new_user and MANAGER_CHAT_ID:
        try:
            await bot.send_message(
                MANAGER_CHAT_ID,
                f"🎁 Referral: @{message.from_user.username or message.from_user.id} "
                f"foydalanuvchi {referred_by} tomonidan taklif qilingan holda qo'shildi.",
            )
        except Exception as e:
            logging.error(f"Referral signalini yuborishda xato: {e}")

    kb = webapp_open_keyboard()
    if kb:
        await message.answer(
            "Assalomu alaykum! 🌿 Estet Clinic ilovasiga xush kelibsiz.\n\n"
            "Paketlar, AI yordamchi va manzil — barchasi bitta ilovada 👇",
            reply_markup=kb,
        )
        return

    await message.answer(
        "Qaysi tilda gaplashishni xohlaysiz?\nНа каком языке вам удобно общаться?\nWhich language would you prefer?",
        reply_markup=lang_keyboard(),
    )

@dp.message(F.text.in_(["🇺🇿 O'zbek", "🇷🇺 Русский", "🇬🇧 English"]))
async def lang_chosen(message: Message, state: FSMContext):
    lang_map = {"🇺🇿 O'zbek": "uz", "🇷🇺 Русский": "ru", "🇬🇧 English": "en"}
    lang = lang_map[message.text]
    await state.update_data(lang=lang, history=[])
    await state.set_state(Chat.talking)
    touch_user(message.from_user.id, message.from_user.username or "", lang)
    await message.answer(WELCOME[lang], reply_markup=ReplyKeyboardRemove())
    await message.answer("👇", reply_markup=quick_reply_keyboard(lang))

# ============ TEZ JAVOB TUGMALARI ============
@dp.callback_query(F.data.in_(["narxlar", "manzil", "konsultatsiya"]))
async def quick_reply_handler(callback):
    state: FSMContext = dp.fsm.get_context(bot, callback.from_user.id, callback.from_user.id)
    data = await state.get_data()
    lang = data.get("lang", "uz")
    text = QUICK_REPLY_TEXT[callback.data][lang]
    await callback.message.answer(text)
    await callback.answer()

# ============ GURUHDA "REPLY" QILINSA — AVTOMATIK MIJOZGA YUBORISH ============
@dp.message(F.reply_to_message)
async def group_reply_handler(message: Message):
    """Operator guruhda mijoz xabariga oddiy 'Reply' qilsa, bu avtomatik o'sha
    mijozga yuboriladi — /reply buyrug'ini yozish shart emas."""
    is_admin_user = bool(ADMIN_CHAT_IDS) and str(message.from_user.id) in ADMIN_CHAT_IDS
    is_manager_chat = bool(MANAGER_CHAT_ID) and str(message.chat.id) == str(MANAGER_CHAT_ID)
    if not (is_admin_user or is_manager_chat):
        raise SkipHandler
    if not message.text:
        raise SkipHandler

    target_user_id = get_handoff_user(message.reply_to_message.message_id)
    if not target_user_id:
        raise SkipHandler  # bu oddiy reply, handoff bilan bog'liq emas

    try:
        await bot.send_message(target_user_id, f"👤 Operator:\n{message.text}")
        set_handoff(target_user_id, True)
        await message.reply("✅ Yuborildi.")
    except Exception as e:
        await message.reply(f"❌ Xabar yuborilmadi: {e}")

# ============ OPERATOR REJIMI: MIJOZ XABARINI OPERATORGA UZATISH ============
@dp.message(F.text, ~F.text.startswith("/"))
async def handoff_relay_handler(message: Message, state: FSMContext):
    """Agar mijoz 'Odam bilan gaplashish' rejimida bo'lsa, xabarini AI o'rniga
    to'g'ridan-to'g'ri operator guruhiga yuboradi. Aks holda oddiy AI oqimiga o'tkazadi."""
    if not is_handoff_active(message.from_user.id):
        raise SkipHandler

    touch_user(message.from_user.id, message.from_user.username or "")

    if MANAGER_CHAT_ID:
        try:
            await send_handoff_notice(
                MANAGER_CHAT_ID,
                f"💬 MIJOZDAN XABAR (operator rejimi)\n"
                f"👤 @{message.from_user.username or '-'} (ID: {message.from_user.id})\n"
                f"✉️ {message.text}",
                message.from_user.id,
            )
        except Exception as e:
            logging.error(f"Handoff relay xatosi: {e}")

# ============ ASOSIY SUHBAT (LLM orqali) ============
@dp.message(Chat.talking, F.text)
async def chat_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("history", [])
    lang = data.get("lang", "uz")

    touch_user(message.from_user.id, message.from_user.username or "")

    history.append({"role": "user", "content": message.text})
    await run_llm_and_reply(message, state, history, lang)

# ============ RASM QABUL QILISH + AI TAHLIL ============
@dp.message(Chat.talking, F.photo)
async def photo_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "uz")
    history = data.get("history", [])

    touch_user(message.from_user.id, message.from_user.username or "")

    file = await bot.get_file(message.photo[-1].file_id)
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"

    if MANAGER_CHAT_ID:
        await bot.send_photo(
            MANAGER_CHAT_ID,
            message.photo[-1].file_id,
            caption=f"📸 Yangi rasm — mijoz: @{message.from_user.username or message.from_user.id}",
        )

    try:
        vision_response = groq_client.chat.completions.create(
            model=GROQ_VISION_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": file_url}},
                ],
            }],
        )
        vision_result = vision_response.choices[0].message.content
    except Exception as e:
        logging.error(f"Vision tahlil xatosi: {e}")
        vision_result = None

    if vision_result:
        history.append({"role": "user", "content": f"[RASM TAHLILI: {vision_result}]"})
    else:
        ack = {
            "uz": "✅ Rasmingiz qabul qilindi, mutaxassisimizga yuborildi.",
            "ru": "✅ Ваше фото получено и отправлено специалисту.",
            "en": "✅ Your photo has been received and sent to our specialist.",
        }[lang]
        await message.answer(ack)
        await state.update_data(history=history)
        return

    await run_llm_and_reply(message, state, history, lang)

# ============ OVOZLI XABAR QABUL QILISH ============
@dp.message(Chat.talking, F.voice)
async def voice_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "uz")
    history = data.get("history", [])

    touch_user(message.from_user.id, message.from_user.username or "")

    file = await bot.get_file(message.voice.file_id)
    local_path = f"voice_{message.from_user.id}.ogg"
    await bot.download_file(file.file_path, local_path)

    try:
        with open(local_path, "rb") as f:
            transcript = groq_client.audio.transcriptions.create(
                file=(local_path, f.read()),
                model=GROQ_WHISPER_MODEL,
            )
        text = transcript.text
    except Exception as e:
        logging.error(f"Ovozni matnga aylantirish xatosi: {e}")
        text = None
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)

    if not text:
        error_msg = {
            "uz": "Kechirasiz, ovozli xabaringizni tushunolmadim. Iltimos, yozib yuboring.",
            "ru": "Извините, не удалось распознать голосовое сообщение. Пожалуйста, напишите текстом.",
            "en": "Sorry, I couldn't understand the voice message. Please type it instead.",
        }[lang]
        await message.answer(error_msg)
        return

    history.append({"role": "user", "content": text})
    await run_llm_and_reply(message, state, history, lang)

def is_valid_lead_info(info: str) -> bool:
    """LEAD_CAPTURED formatidagi ism va telefon haqiqatan ham to'ldirilganini tekshiradi.
    Bo'sh yoki taxminiy (masalan 'noma'lum') qiymatlarni rad etadi."""
    name_match = re.search(r"ism=([^,]*)", info)
    phone_match = re.search(r"telefon=([^,]*)", info)
    name = name_match.group(1).strip() if name_match else ""
    phone = phone_match.group(1).strip() if phone_match else ""
    phone_digits = re.sub(r"\D", "", phone)
    return bool(name) and len(phone_digits) >= 7

# ============ LLM CHAQIRUV VA JAVOB YUBORISH (umumiy funksiya) ============
async def run_llm_and_reply(message: Message, state: FSMContext, history: list, lang: str):
    groq_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    response = groq_client.chat.completions.create(
        model=GROQ_TEXT_MODEL,
        max_tokens=600,
        messages=groq_messages,
    )
    reply_text = response.choices[0].message.content

    if "[LEAD_CAPTURED:" in reply_text:
        visible_part, lead_part = reply_text.split("[LEAD_CAPTURED:", 1)
        lead_info = lead_part.replace("]", "").strip()
        reply_text = visible_part.strip()
        if is_valid_lead_info(lead_info):
            await notify_manager(message, lead_info)
        else:
            logging.warning(f"Bo'sh/noto'g'ri LEAD_CAPTURED e'tiborga olinmadi: {lead_info}")

    history.append({"role": "assistant", "content": reply_text})
    await state.update_data(history=history)

    await message.answer(reply_text)

# ============ GOOGLE SHEETS YORDAMCHISI ============
def append_lead_to_sheet(row: list):
    """Har qanday lead manbaidan (chat/bron/ro'yxat) Google jadvalga qator qo'shadi.
    GOOGLE_CREDS_JSON (Render uchun, xavfsizroq) yoki GOOGLE_CREDS_FILE (lokal fayl) ishlatiladi."""
    if not GOOGLE_SHEET_ID:
        return
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds_json = os.environ.get("GOOGLE_CREDS_JSON")
        if creds_json:
            creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
        sheet.append_row(row)
    except Exception as e:
        logging.error(f"Google Sheets xatosi: {e}")

# ============ OPERATORGA SIGNAL + GOOGLE SHEETS ============
async def notify_referrer_if_any(user_id: int):
    """Agar bu mijoz kimningdir taklifi orqali kelgan bo'lsa, taklif qiluvchiga va adminga xabar beradi."""
    referrer_id = get_referrer(user_id)
    if not referrer_id:
        return
    try:
        await bot.send_message(
            referrer_id,
            "🎉 Sizning taklifingiz orqali yangi mijoz Estet Clinic bilan bog'landi! "
            "Chegirmangizni bilish uchun klinikaga qo'ng'iroq qiling: +998 97 308-09-99",
        )
    except Exception as e:
        logging.error(f"Referrerga xabar yuborishda xato: {e}")
    if MANAGER_CHAT_ID:
        try:
            await bot.send_message(
                MANAGER_CHAT_ID,
                f"🎁 REFERRAL LEAD: mijoz (ID: {user_id}) taklif qiluvchi (ID: {referrer_id}) orqali keldi. "
                f"Chegirma qo'llash uchun eslatma.",
            )
        except Exception as e:
            logging.error(f"Referral admin xabarida xato: {e}")

FIELD_EMOJIS = {
    "ism": "👤", "F.I.Sh": "👤", "telefon": "📞", "tug'ilgan sana": "🎂",
    "manzil": "📍", "pasport": "🪪", "paket": "📦", "amaliyot sanasi": "🗓",
    "eslatma": "📝", "sana": "📆", "vaqt": "⏰",
}
FIELD_LABELS = {
    "ism": "Ism", "F.I.Sh": "F.I.Sh", "telefon": "Telefon", "tug'ilgan sana": "Tug'ilgan sana",
    "manzil": "Manzil", "pasport": "Pasport", "paket": "Paket", "amaliyot sanasi": "Amaliyot sanasi",
    "eslatma": "Eslatma", "sana": "Sana", "vaqt": "Vaqt",
}

def format_lead_fields(info: str) -> str:
    """'ism=X, telefon=Y' kabi qatorni chiroyli, har biri alohida qatorda, emoji bilan ko'rsatadi."""
    lines = []
    for part in info.split(", "):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip() or "—"
        emoji = FIELD_EMOJIS.get(key, "▪️")
        label = FIELD_LABELS.get(key, key.capitalize())
        lines.append(f"{emoji} {label}: {value}")
    return "\n".join(lines)

async def notify_manager(message: Message, lead_info: str):
    user = message.from_user
    text = (
        f"🆕 YANGI LEAD (Telegram chat)\n\n"
        f"{format_lead_fields(lead_info)}\n"
        f"💬 Foydalanuvchi: @{user.username or '-'} (ID: {user.id})\n\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    if MANAGER_CHAT_ID:
        await bot.send_message(MANAGER_CHAT_ID, text)

    mark_lead(user.id, user.username or "", lead_info, source="telegram-chat")
    await notify_referrer_if_any(user.id)

    append_lead_to_sheet([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        str(user.id),
        user.username or "-",
        lead_info,
    ])

# ============ ADMIN: STATISTIKA ============
@dp.message(Command("stats"))
async def stats_handler(message: Message):
    if ADMIN_CHAT_IDS and str(message.from_user.id) not in ADMIN_CHAT_IDS:
        return
    total_users, total_leads, new_week, leads_week = get_stats()
    text = (
        f"📊 STATISTIKA\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"🆕 Shu hafta yangi: {new_week}\n\n"
        f"📝 Jami lead'lar: {total_leads}\n"
        f"📝 Shu hafta lead'lar: {leads_week}\n\n"
        f"📈 Konversiya: {round(total_leads/total_users*100, 1) if total_users else 0}%"
    )
    await message.answer(text)

# ============ ADMIN: MIJOZGA JAVOB YOZISH (/reply) ============
@dp.message(Command("reply"))
async def reply_handler(message: Message):
    # Ruxsat: ADMIN_CHAT_IDS ro'yxatidagi shaxsiy foydalanuvchi YOKI belgilangan guruh (MANAGER_CHAT_ID) ichida
    is_admin_user = bool(ADMIN_CHAT_IDS) and str(message.from_user.id) in ADMIN_CHAT_IDS
    is_manager_chat = bool(MANAGER_CHAT_ID) and str(message.chat.id) == str(MANAGER_CHAT_ID)
    if not (is_admin_user or is_manager_chat):
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Foydalanish: /reply <user_id> <xabar matni>\nMasalan: /reply 123456789 Salom, sizga qanday yordam bera olaman?")
        return

    target_id_str, reply_text = parts[1], parts[2]
    if not target_id_str.isdigit():
        await message.reply("❌ user_id raqam bo'lishi kerak.")
        return

    try:
        await bot.send_message(int(target_id_str), f"👤 Operator:\n{reply_text}")
        set_handoff(int(target_id_str), True)  # javob berilgan mijoz avtomatik operator rejimida qoladi
        await message.reply("✅ Xabar mijozga yuborildi.")
    except Exception as e:
        await message.reply(f"❌ Xabar yuborilmadi: {e}\n(Mijoz botni bloklagan yoki hech qachon /start bosmagan bo'lishi mumkin.)")

# ============ ADMIN: SUHBATNI AI'GA QAYTARISH (/done) ============
@dp.message(Command("done"))
async def done_handler(message: Message):
    is_admin_user = bool(ADMIN_CHAT_IDS) and str(message.from_user.id) in ADMIN_CHAT_IDS
    is_manager_chat = bool(MANAGER_CHAT_ID) and str(message.chat.id) == str(MANAGER_CHAT_ID)
    if not (is_admin_user or is_manager_chat):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Foydalanish: /done <user_id>")
        return

    target_id = int(parts[1])
    set_handoff(target_id, False)
    await message.reply(f"✅ {target_id} uchun operator rejimi yopildi — endi AI yordamchi ishlaydi.")
    try:
        await bot.send_message(target_id, "✅ Suhbat AI yordamchiga qaytarildi. Savolingiz bo'lsa yozing.")
    except Exception:
        pass

# ============ TUGMA: "✅ Suhbatni yakunlash" ============
@dp.callback_query(F.data.startswith("donehandoff_"))
async def done_button_handler(callback):
    is_admin_user = bool(ADMIN_CHAT_IDS) and str(callback.from_user.id) in ADMIN_CHAT_IDS
    is_manager_chat = bool(MANAGER_CHAT_ID) and str(callback.message.chat.id) == str(MANAGER_CHAT_ID)
    if not (is_admin_user or is_manager_chat):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return

    try:
        target_id = int(callback.data.split("_", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Xato")
        return

    set_handoff(target_id, False)
    try:
        await bot.send_message(target_id, "✅ Suhbat AI yordamchiga qaytarildi. Savolingiz bo'lsa yozing.")
    except Exception:
        pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("✅ Yakunlandi")

# ============ FOLLOW-UP: JAVOBSIZ MIJOZLARGA ESLATMA ============
async def followup_checker():
    while True:
        await asyncio.sleep(3600)
        try:
            stale_users = get_stale_users(FOLLOWUP_DELAY_HOURS)
            for user_id, lang in stale_users:
                lang = lang or "uz"
                try:
                    await bot.send_message(user_id, FOLLOWUP_TEXT.get(lang, FOLLOWUP_TEXT["uz"]))
                    mark_followup_sent(user_id)
                except Exception as e:
                    logging.error(f"Follow-up yuborishda xato (user {user_id}): {e}")
        except Exception as e:
            logging.error(f"Follow-up checker xatosi: {e}")

# ============ KUNLIK HISOBOT + OPERATSIYADAN KEYINGI ESLATMALAR ============
def admin_targets():
    """Xabar yuboriladigan adminlar ro'yxati (ADMIN_CHAT_IDS yoki fallback MANAGER_CHAT_ID)."""
    if ADMIN_CHAT_IDS:
        return ADMIN_CHAT_IDS
    return [MANAGER_CHAT_ID] if MANAGER_CHAT_ID else []

async def send_daily_report():
    yesterday = (datetime.utcnow() + timedelta(hours=5) - timedelta(days=1)).strftime("%Y-%m-%d")
    data = get_daily_report_data(yesterday)
    top_pkg_text = data["top_package"] or "—"
    text = (
        f"📊 KUNLIK HISOBOT ({yesterday})\n\n"
        f"👥 Yangi foydalanuvchilar: {data['new_users']}\n"
        f"📝 Jami lead'lar: {data['total_leads']}\n"
        f"📅 Bron qilinganlar: {data['total_bookings']}\n"
        f"🧾 Ro'yxatga olinganlar: {data['total_registrations']}\n"
        f"🔥 Eng ko'p so'ralgan paket: {top_pkg_text}"
    )
    for target in admin_targets():
        try:
            await bot.send_message(target, text)
        except Exception as e:
            logging.error(f"Kunlik hisobot yuborishda xato ({target}): {e}")

async def send_operation_reminders():
    due = get_operations_due_today()
    if not due:
        return
    lines = ["🩹 BUGUNGI KUZATUV QO'NG'IROQLARI:\n"]
    for name, phone, days_passed in due:
        lines.append(f"— {name} ({phone}) — amaliyotdan {days_passed}-kun")
    text = "\n".join(lines)
    for target in admin_targets():
        try:
            await bot.send_message(target, text)
        except Exception as e:
            logging.error(f"Kuzatuv eslatmasini yuborishda xato ({target}): {e}")

async def daily_tasks_checker():
    while True:
        await asyncio.sleep(1800)  # har 30 daqiqada tekshiradi
        try:
            now_tashkent = datetime.utcnow() + timedelta(hours=5)
            today_str = now_tashkent.strftime("%Y-%m-%d")
            if now_tashkent.hour == 9 and get_meta("last_report_date") != today_str:
                await send_daily_report()
                await send_operation_reminders()
                set_meta("last_report_date", today_str)
        except Exception as e:
            logging.error(f"Kunlik vazifalar xatosi: {e}")

# ============ MINI APP UCHUN BACKEND API (aiohttp) ============
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Password"
    return resp

async def handle_health(request):
    return web.Response(text="OK")

def check_admin_password(request) -> bool:
    """Mini App'dan kelgan X-Admin-Password headerini tekshiradi."""
    if not ADMIN_PANEL_PASSWORD:
        logging.warning("ADMIN_PANEL_PASSWORD sozlanmagan — Ro'yxat API himoyasiz qolmoqda!")
        return True
    provided = request.headers.get("X-Admin-Password", "")
    return provided == ADMIN_PANEL_PASSWORD

async def handle_admin_login_api(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False}, status=400)
    password = body.get("password", "")
    if ADMIN_PANEL_PASSWORD and password == ADMIN_PANEL_PASSWORD:
        return web.json_response({"success": True})
    return web.json_response({"success": False}, status=401)

async def handle_chat_api(request):
    """Mini App'ning 'AI Yordamchi' tabi shu endpointga so'rov yuboradi."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_text = (body.get("message") or "").strip()
    history = body.get("history") or []
    lang = body.get("lang", "uz")
    webapp_user_id = body.get("user_id") or 0
    webapp_username = body.get("username") or "webapp-mijoz"

    if not user_text:
        return web.json_response({"error": "message_required"}, status=400)

    if str(webapp_user_id).isdigit() and int(webapp_user_id) != 0 and is_handoff_active(int(webapp_user_id)):
        if MANAGER_CHAT_ID:
            try:
                await send_handoff_notice(
                    MANAGER_CHAT_ID,
                    f"💬 MIJOZDAN XABAR (Mini App, operator rejimi)\n"
                    f"👤 @{webapp_username} (ID: {webapp_user_id})\n"
                    f"✉️ {user_text}",
                    int(webapp_user_id),
                )
            except Exception as e:
                logging.error(f"Handoff forward xatosi (webapp): {e}")
        waiting_msg = HANDOFF_WAITING_MSG.get(lang, HANDOFF_WAITING_MSG["uz"])
        return web.json_response({"reply": waiting_msg, "lead": None})

    history = list(history) + [{"role": "user", "content": user_text}]
    groq_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_TEXT_MODEL,
            max_tokens=600,
            messages=groq_messages,
        )
        reply_text = response.choices[0].message.content
    except Exception as e:
        logging.error(f"Mini App chat xatosi: {e}")
        return web.json_response({"error": "llm_error"}, status=500)

    lead_info = None
    if "[LEAD_CAPTURED:" in reply_text:
        visible_part, lead_part = reply_text.split("[LEAD_CAPTURED:", 1)
        candidate_lead_info = lead_part.replace("]", "").strip()
        reply_text = visible_part.strip()

        if is_valid_lead_info(candidate_lead_info):
            lead_info = candidate_lead_info

            if MANAGER_CHAT_ID:
                try:
                    await bot.send_message(
                        MANAGER_CHAT_ID,
                        f"🆕 YANGI LEAD (Mini App — AI chat)\n\n"
                        f"{format_lead_fields(lead_info)}\n"
                        f"💬 Foydalanuvchi: @{webapp_username}\n\n"
                        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    )
                except Exception as e:
                    logging.error(f"Mini App lead xabarini yuborishda xato: {e}")

            mark_lead(int(webapp_user_id) if str(webapp_user_id).isdigit() else 0, webapp_username, lead_info, source="webapp-chat")

            append_lead_to_sheet([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                "webapp", webapp_username, lead_info,
            ])
        else:
            logging.warning(f"Bo'sh/noto'g'ri LEAD_CAPTURED e'tiborga olinmadi (webapp): {candidate_lead_info}")

    return web.json_response({"reply": reply_text, "lead": lead_info})

async def handle_book_api(request):
    """Mini App'ning 'Bron' tabidan kelgan konsultatsiya bron so'rovi."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    day = (body.get("day") or "").strip()
    time_slot = (body.get("time") or "").strip()

    if not name or not phone or not day or not time_slot:
        return web.json_response({"error": "missing_fields"}, status=400)

    info = f"ism={name}, telefon={phone}, sana={day}, vaqt={time_slot}"

    if MANAGER_CHAT_ID:
        try:
            await bot.send_message(
                MANAGER_CHAT_ID,
                f"📅 YANGI BRON (Mini App)\n\n"
                f"{format_lead_fields(info)}\n\n"
                f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
        except Exception as e:
            logging.error(f"Bron xabarini yuborishda xato: {e}")

    mark_lead(0, name, info, source="webapp-booking")

    append_lead_to_sheet([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "webapp-booking", name, info,
    ])

    return web.json_response({"success": True})

async def handle_register_api(request):
    """Mini App'ning 'Ro'yxat' tabidan kelgan to'liq mijoz ma'lumoti."""
    if not check_admin_password(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    address = (body.get("address") or "").strip()
    birth = (body.get("birth") or "").strip()
    passport = (body.get("passport") or "").strip()
    package = (body.get("package") or "").strip()
    note = (body.get("note") or "").strip()
    operation_date = (body.get("operation_date") or "").strip()

    if not name or not phone or not address:
        return web.json_response({"error": "missing_fields"}, status=400)

    info = (
        f"F.I.Sh={name}, telefon={phone}, tug'ilgan sana={birth or '-'}, "
        f"manzil={address}, pasport={passport or '-'}, paket={package or '-'}, "
        f"amaliyot sanasi={operation_date or '-'}, eslatma={note or '-'}"
    )

    if MANAGER_CHAT_ID:
        try:
            await bot.send_message(
                MANAGER_CHAT_ID,
                f"🧾 YANGI MIJOZ RO'YXATGA OLINDI (Mini App)\n\n"
                f"{format_lead_fields(info)}\n\n"
                f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
        except Exception as e:
            logging.error(f"Ro'yxat xabarini yuborishda xato: {e}")

    mark_lead(0, name, info, source="webapp-register")
    save_registration(name, phone, birth, address, passport, package, note, operation_date)

    append_lead_to_sheet([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "webapp-register", name, info,
    ])

    return web.json_response({"success": True})

async def handle_track_package_api(request):
    """Mini App'da 'Shu paket haqida so'rash' bosilganda chaqiriladi — statistikaga qo'shiladi."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    package = (body.get("package") or "").strip()
    if not package:
        return web.json_response({"error": "package_required"}, status=400)
    track_package_interest(package)
    return web.json_response({"success": True})

async def handle_handoff_api(request):
    """Mijoz 'Odam bilan gaplashish' tugmasini bosganda operatorga signal beradi."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id = body.get("user_id") or "-"
    username = body.get("username") or ""
    last_message = (body.get("last_message") or "").strip()

    if str(user_id).isdigit():
        set_handoff(int(user_id), True)

    contact_line = f"@{username}" if username else "username yo'q"
    text = (
        f"🙋 MIJOZ OPERATOR BILAN GAPLASHISHNI SO'RADI\n"
        f"Foydalanuvchi: {contact_line} (ID: {user_id})\n"
        f"So'nggi xabari: {last_message or '-'}\n"
        f"Vaqt: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    if MANAGER_CHAT_ID and str(user_id).isdigit():
        try:
            await send_handoff_notice(MANAGER_CHAT_ID, text, int(user_id))
        except Exception as e:
            logging.error(f"Handoff xabarini yuborishda xato: {e}")

    return web.json_response({"success": True})

async def start_web_server():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_health)
    app.router.add_post("/api/chat", handle_chat_api)
    app.router.add_route("OPTIONS", "/api/chat", handle_health)
    app.router.add_post("/api/book", handle_book_api)
    app.router.add_route("OPTIONS", "/api/book", handle_health)
    app.router.add_post("/api/register", handle_register_api)
    app.router.add_route("OPTIONS", "/api/register", handle_health)
    app.router.add_post("/api/admin-login", handle_admin_login_api)
    app.router.add_route("OPTIONS", "/api/admin-login", handle_health)
    app.router.add_post("/api/track-package", handle_track_package_api)
    app.router.add_route("OPTIONS", "/api/track-package", handle_health)
    app.router.add_post("/api/handoff", handle_handoff_api)
    app.router.add_route("OPTIONS", "/api/handoff", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Mini App API {port}-portda ishga tushdi")

# ============ ISHGA TUSHIRISH ============
async def main():
    init_db()
    await start_web_server()
    asyncio.create_task(followup_checker())
    asyncio.create_task(daily_tasks_checker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
