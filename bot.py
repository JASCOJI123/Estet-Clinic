import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import anthropic
import gspread
from google.oauth2.service_account import Credentials

# ============ SOZLAMALAR (Environment Variables orqali olinadi) ============
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID")  # klinika operatorining Telegram chat ID'si
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")  # ixtiyoriy, lead yozib borish uchun
GOOGLE_CREDS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "google_creds.json")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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

# ============ HOLAT (FSM) ============
class Chat(StatesGroup):
    talking = State()

# ============ TIL TANLASH TUGMALARI ============
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

WELCOME = {
    "uz": "Assalomu alaykum! Estet Clinic soch ekish konsultatsiya botiga xush kelibsiz 🌿\nSizga qanday yordam bera olaman?",
    "ru": "Здравствуйте! Добро пожаловать в консультационного бота Estet Clinic по пересадке волос 🌿\nЧем могу помочь?",
    "en": "Hello! Welcome to Estet Clinic's hair transplant consultation bot 🌿\nHow can I help you today?",
}

# ============ /start ============
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
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
    await message.answer(WELCOME[lang], reply_markup=ReplyKeyboardRemove())

# ============ ASOSIY SUHBAT (LLM orqali) ============
@dp.message(Chat.talking, F.text)
async def chat_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("history", [])

    history.append({"role": "user", "content": message.text})

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    reply_text = response.content[0].text

    # Lead aniqlangan bo'lsa, ajratib olish va operatorga yuborish
    if "[LEAD_CAPTURED:" in reply_text:
        visible_part, lead_part = reply_text.split("[LEAD_CAPTURED:", 1)
        lead_info = lead_part.replace("]", "").strip()
        reply_text = visible_part.strip()
        await notify_manager(message, lead_info)

    history.append({"role": "assistant", "content": reply_text})
    await state.update_data(history=history)

    await message.answer(reply_text)

# ============ RASM QABUL QILISH ============
@dp.message(Chat.talking, F.photo)
async def photo_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "uz")
    caption = {
        "uz": "✅ Rasmingiz qabul qilindi, mutaxassisimizga yuborildi.",
        "ru": "✅ Ваше фото получено и отправлено специалисту.",
        "en": "✅ Your photo has been received and sent to our specialist.",
    }[lang]
    if MANAGER_CHAT_ID:
        await bot.send_photo(
            MANAGER_CHAT_ID,
            message.photo[-1].file_id,
            caption=f"📸 Yangi rasm — mijoz: @{message.from_user.username or message.from_user.id}",
        )
    await message.answer(caption)

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

# ============ ISHGA TUSHIRISH ============
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
