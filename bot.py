import asyncio
import logging
import os
import json
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
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
            followup_sent INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            info TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def touch_user(user_id: int, username: str, lang: str = None):
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
        c.execute("INSERT INTO users (user_id, username, lang, started_at, last_message_at) VALUES (?,?,?,?,?)",
                  (user_id, username, lang, now, now))
    conn.commit()
    conn.close()

def mark_lead(user_id: int, username: str, info: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET lead_sent=1 WHERE user_id=?", (user_id,))
    c.execute("INSERT INTO leads (user_id, username, info, created_at) VALUES (?,?,?,?)",
              (user_id, username, info, datetime.now().isoformat()))
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
Ism va telefon olingach:
"Rahmat! Ma'lumotlaringizni mutaxassisimizga yubordim. Tez orada siz bilan bog'lanishadi. 📞 Shoshilinch bo'lsa: +998 97 308-09-99"

Agar foydalanuvchi ism va telefon raqamini bergan bo'lsa, javobingning OXIRIGA aniq shu formatda maxsus qator qo'sh (foydalanuvchiga ko'rinmasin, faqat tizim uchun):
[LEAD_CAPTURED: ism=<ism>, telefon=<raqam>]
"""

VISION_PROMPT = """Bu odam boshining rasmi. Sochsizlik holatini umumiy tarzda tasvirlab ber (masalan: "peshona chizig'i orqaga tortilgan", "tepa qismda yupqalashish bor", "keng maydonda sochsizlik" kabi). Aniq tibbiy diagnoz qo'yma, faqat vizual tavsif ber, 2-3 gap."""

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
    touch_user(message.from_user.id, message.from_user.username or "")

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
        await notify_manager(message, lead_info)

    history.append({"role": "assistant", "content": reply_text})
    await state.update_data(history=history)

    await message.answer(reply_text)

# ============ OPERATORGA SIGNAL + GOOGLE SHEETS ============
async def notify_manager(message: Message, lead_info: str):
    user = message.from_user
    text = (
        f"🆕 YANGI LEAD\n"
        f"Foydalanuvchi: @{user.username or '-'} (ID: {user.id})\n"
        f"{lead_info}\n"
        f"Vaqt: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    if MANAGER_CHAT_ID:
        await bot.send_message(MANAGER_CHAT_ID, text)

    mark_lead(user.id, user.username or "", lead_info)

    if GOOGLE_SHEET_ID:
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
            gc = gspread.authorize(creds)
            sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
            sheet.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                str(user.id),
                user.username or "-",
                lead_info,
            ])
        except Exception as e:
            logging.error(f"Google Sheets xatosi: {e}")

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
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

async def handle_health(request):
    return web.Response(text="OK")

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
        lead_info = lead_part.replace("]", "").strip()
        reply_text = visible_part.strip()

        if MANAGER_CHAT_ID:
            try:
                await bot.send_message(
                    MANAGER_CHAT_ID,
                    f"🆕 YANGI LEAD (Mini App)\n{lead_info}\nVaqt: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                )
            except Exception as e:
                logging.error(f"Mini App lead xabarini yuborishda xato: {e}")

        mark_lead(int(webapp_user_id) if str(webapp_user_id).isdigit() else 0, webapp_username, lead_info)

        if GOOGLE_SHEET_ID:
            try:
                scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
                gc = gspread.authorize(creds)
                sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
                sheet.append_row([
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "webapp", webapp_username, lead_info,
                ])
            except Exception as e:
                logging.error(f"Google Sheets xatosi (webapp): {e}")

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
                f"📅 YANGI BRON (Mini App)\n{info}\nYuborilgan vaqt: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
        except Exception as e:
            logging.error(f"Bron xabarini yuborishda xato: {e}")

    mark_lead(0, name, info)

    if GOOGLE_SHEET_ID:
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
            gc = gspread.authorize(creds)
            sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
            sheet.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                "webapp-booking", name, info,
            ])
        except Exception as e:
            logging.error(f"Google Sheets xatosi (booking): {e}")

    return web.json_response({"success": True})

async def handle_register_api(request):
    """Mini App'ning 'Ro'yxat' tabidan kelgan to'liq mijoz ma'lumoti."""
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

    if not name or not phone or not address:
        return web.json_response({"error": "missing_fields"}, status=400)

    info = (
        f"F.I.Sh={name}, telefon={phone}, tug'ilgan sana={birth or '-'}, "
        f"manzil={address}, pasport={passport or '-'}, paket={package or '-'}, "
        f"eslatma={note or '-'}"
    )

    if MANAGER_CHAT_ID:
        try:
            await bot.send_message(
                MANAGER_CHAT_ID,
                f"🧾 YANGI MIJOZ RO'YXATGA OLINDI (Mini App)\n{info}\n"
                f"Vaqt: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            )
        except Exception as e:
            logging.error(f"Ro'yxat xabarini yuborishda xato: {e}")

    mark_lead(0, name, info)

    if GOOGLE_SHEET_ID:
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
            gc = gspread.authorize(creds)
            sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
            sheet.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                "webapp-register", name, info,
            ])
        except Exception as e:
            logging.error(f"Google Sheets xatosi (register): {e}")

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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
