import logging
import asyncio
import sqlite3
from typing import List, Tuple, Optional
from contextlib import contextmanager
from datetime import datetime
import re

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# ============================================================
# SOZLAMALAR
# ============================================================
API_TOKEN = '8243058925:AAF4ydzoKzlBoUs5RH_Iyd8Xd6B-sRWt_o8'
CREATOR_ID = 7710435255
DB_NAME = 'cinema_pro.db'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Proxy (agar kerak bo'lsa)
proxy_url = None  # "http://proxy:port" formatida

bot = Bot(token=API_TOKEN, proxy=proxy_url)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ============================================================
# KLAVIATURALAR
# ============================================================
cancel_kb = ReplyKeyboardMarkup(resize_keyboard=True).add(
    KeyboardButton("❌ Bekor qilish")
)

def get_main_admin_kb():
    """Admin panel asosiy klaviatura"""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🎬 Kino qo'shish", callback_data="add_movie"),
        InlineKeyboardButton("🗑 Kino o'chirish", callback_data="del_movie"),
        InlineKeyboardButton("📢 Kanal qo'shish", callback_data="add_ch"),
        InlineKeyboardButton("🗑 Kanal o'chirish", callback_data="del_ch"),
        InlineKeyboardButton("📣 Reklama", callback_data="send_ads"),
        InlineKeyboardButton("📊 Statistika", callback_data="show_stats"),
        InlineKeyboardButton("👤 Admin qo'shish", callback_data="add_admin"),
        InlineKeyboardButton("🔍 Kino qidirish", callback_data="search_movie")
    )
    return kb

# ============================================================
# STATES
# ============================================================
class AdminStates(StatesGroup):
    waiting_for_movie = State()
    waiting_for_code = State()
    waiting_for_channel_id = State()
    waiting_for_channel_link = State()
    waiting_for_ads = State()
    waiting_for_delete_code = State()
    waiting_for_delete_channel = State()
    waiting_for_new_admin = State()
    waiting_for_search = State()

# ============================================================
# DATABASE UTILITIES
# ============================================================
@contextmanager
def get_db():
    """Database connection context manager"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        conn.close()

def init_db():
    """Ma'lumotlar bazasini ishga tushirish"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Jadvallar
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS movies (
                code TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                caption TEXT,
                added_date TEXT DEFAULT CURRENT_TIMESTAMP,
                added_by INTEGER
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                link TEXT NOT NULL,
                name TEXT,
                added_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_requests (
                user_id INTEGER,
                channel_id TEXT,
                request_date TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, channel_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS search_log (
                user_id INTEGER,
                query TEXT,
                found INTEGER,
                search_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Yaratuvchini admin qilish
        cursor.execute("INSERT OR IGNORE INTO admins VALUES (?, ?)", 
                      (CREATOR_ID, datetime.now().isoformat()))

def is_admin(user_id: int) -> bool:
    """Foydalanuvchi admin ekanligini tekshirish"""
    with get_db() as conn:
        cursor = conn.cursor()
        result = cursor.execute(
            "SELECT 1 FROM admins WHERE user_id=?", (user_id,)
        ).fetchone()
        return result is not None

def add_user(user_id: int, username: str = None, first_name: str = None):
    """Yangi foydalanuvchi qo'shish"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username, first_name)
        )

def search_movies(query: str) -> List[Tuple[str, str]]:
    """Kinolarni qidirish (fuzzy)"""
    with get_db() as conn:
        cursor = conn.cursor()
        # LIKE orqali qidirish
        pattern = f"%{query}%"
        results = cursor.execute(
            "SELECT code, caption FROM movies WHERE code LIKE ? OR caption LIKE ? LIMIT 10",
            (pattern, pattern)
        ).fetchall()
        return [(row['code'], row['caption'] or 'Caption yo\'q') for row in results]

# ============================================================
# OBUNA TEKSHIRISH
# ============================================================
async def check_sub(user_id: int) -> List[Tuple[str, str]]:
    """Obunani tekshirish"""
    with get_db() as conn:
        cursor = conn.cursor()
        channels = cursor.execute("SELECT id, link FROM channels").fetchall()
        pending = cursor.execute(
            "SELECT channel_id FROM pending_requests WHERE user_id=?", (user_id,)
        ).fetchall()
        pending_ids = {row['channel_id'] for row in pending}
    
    unsub = []
    for ch in channels:
        ch_id, ch_link = ch['id'], ch['link']
        if ch_id in pending_ids:
            continue
        
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unsub.append((ch_id, ch_link))
        except Exception as e:
            logger.warning(f"Channel check error {ch_id}: {e}")
            unsub.append((ch_id, ch_link))
    
    return unsub

# ============================================================
# STATISTIKA
# ============================================================
def get_stats() -> dict:
    """Statistika olish"""
    with get_db() as conn:
        cursor = conn.cursor()
        users = cursor.execute("SELECT COUNT(*) as c FROM users").fetchone()['c']
        movies = cursor.execute("SELECT COUNT(*) as c FROM movies").fetchone()['c']
        channels = cursor.execute("SELECT COUNT(*) as c FROM channels").fetchone()['c']
        admins = cursor.execute("SELECT COUNT(*) as c FROM admins").fetchone()['c']
        
        # Oxirgi 24 soat ichidagi qidiruvlar
        recent_searches = cursor.execute(
            "SELECT COUNT(*) as c FROM search_log WHERE datetime(search_date) > datetime('now', '-1 day')"
        ).fetchone()['c']
        
        return {
            'users': users,
            'movies': movies,
            'channels': channels,
            'admins': admins,
            'searches_24h': recent_searches
        }

# ============================================================
# UMUMIY HANDLERLAR
# ============================================================
@dp.message_handler(state='*', text="❌ Bekor qilish")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer(
        "✅ Amaliyot bekor qilindi.",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.chat_join_request_handler()
async def join_requested(update: types.ChatJoinRequest):
    """Kanalga qo'shilish so'rovi"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO pending_requests (user_id, channel_id) VALUES (?, ?)",
            (update.from_user.id, str(update.chat.id))
        )
    logger.info(f"Join request: {update.from_user.id} -> {update.chat.id}")

@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message, state: FSMContext):
    await state.finish()
    
    # Foydalanuvchini saqlash
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    
    unsub = await check_sub(message.from_user.id)
    
    if unsub:
        btns = InlineKeyboardMarkup()
        for _, link in unsub:
            btns.add(InlineKeyboardButton("A'zo bo'lish 📢", url=link))
        btns.add(InlineKeyboardButton("Tasdiqlash ✅", callback_data="check"))
        
        await message.answer(
            "<b>🎬 CinemaPro Bot ga xush kelibsiz!</b>\n\n"
            "Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:",
            reply_markup=btns,
            parse_mode="HTML"
        )
    else:
        welcome_text = (
            "🎬 <b>CinemaPro Bot</b>\n\n"
            "Kino kodini yuboring yoki /search buyrug'i bilan qidiring!\n\n"
            "📌 <i>Misol: KINO123</i>"
        )
        await message.answer(welcome_text, parse_mode="HTML")

@dp.callback_query_handler(text="check")
async def check_callback(call: types.CallbackQuery):
    unsub = await check_sub(call.from_user.id)
    
    if not unsub:
        await call.message.delete()
        
        # Animatsiya
        try:
            gold_stars_id = "CAACAgIAAxkBAAEL6_lmA13-S2_S6_S1_S0"  # To'g'ri sticker ID kiriting
            await bot.send_sticker(call.message.chat.id, gold_stars_id)
        except:
            pass
        
        await bot.send_message(
            call.message.chat.id,
            "✅ <b>Tabriklaymiz!</b>\n\n"
            "Siz muvaffaqiyatli a'zo bo'ldingiz. Endi kino kodini yuboring!",
            parse_mode="HTML"
        )
    else:
        await call.answer(
            "❌ Hali barcha kanallarga a'zo emassiz!",
            show_alert=True
        )

# ============================================================
# ADMIN PANEL
# ============================================================
@dp.message_handler(commands=['admin'])
async def admin_menu(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Sizda admin huquqi yo'q!")
        return
    
    stats = get_stats()
    
    text = (
        "🛠 <b>Admin Panel</b>\n\n"
        f"👥 Foydalanuvchilar: <code>{stats['users']}</code>\n"
        f"🎬 Kinolar: <code>{stats['movies']}</code>\n"
        f"📢 Kanallar: <code>{stats['channels']}</code>\n"
        f"👤 Adminlar: <code>{stats['admins']}</code>\n"
        f"🔍 Qidiruvlar (24h): <code>{stats['searches_24h']}</code>"
    )
    
    await message.answer(text, reply_markup=get_main_admin_kb(), parse_mode="HTML")

@dp.callback_query_handler(text="show_stats")
async def show_stats_call(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("❌ Ruxsat yo'q!")
    
    stats = get_stats()
    text = (
        f"📊 Statistika:\n\n"
        f"👥 Foydalanuvchilar: {stats['users']}\n"
        f"🎬 Kinolar: {stats['movies']}\n"
        f"📢 Kanallar: {stats['channels']}\n"
        f"🔍 Qidiruvlar (24h): {stats['searches_24h']}"
    )
    await call.answer(text, show_alert=True)

# ============================================================
# KINO QO'SHISH
# ============================================================
@dp.callback_query_handler(text="add_movie")
async def add_movie_start(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("❌ Ruxsat yo'q!")
    
    await AdminStates.waiting_for_movie.set()
    await call.message.answer(
        "🎬 Kino videosini yuboring:",
        reply_markup=cancel_kb
    )

@dp.message_handler(state=AdminStates.waiting_for_movie, content_types=['video'])
async def get_movie_video(message: types.Message, state: FSMContext):
    await state.update_data(
        file_id=message.video.file_id,
        caption=message.caption or ""
    )
    await AdminStates.waiting_for_code.set()
    await message.answer(
        "🔢 Kino uchun kod yuboring:\n\n"
        "<i>Misol: AVATAR2023</i>",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

@dp.message_handler(state=AdminStates.waiting_for_code)
async def get_movie_code(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    
    # Kod validatsiyasi
    if not re.match(r'^[A-Z0-9_-]{3,30}$', code):
        await message.answer(
            "❌ Kod faqat lotin harflari, raqamlar va _ - belgisidan iborat bo'lishi kerak!\n"
            "Uzunligi: 3-30 ta belgi"
        )
        return
    
    data = await state.get_data()
    
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO movies (code, file_id, caption, added_by) VALUES (?, ?, ?, ?)",
                (code, data['file_id'], data['caption'], message.from_user.id)
            )
        
        await message.answer(
            f"✅ Kino muvaffaqiyatli saqlandi!\n\n"
            f"🔢 Kod: <code>{code}</code>",
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ Bu kod allaqachon band!")
    except Exception as e:
        logger.error(f"Movie save error: {e}")
        await message.answer("❌ Xatolik yuz berdi!")
    
    await state.finish()

# ============================================================
# KINO O'CHIRISH
# ============================================================
@dp.callback_query_handler(text="del_movie")
async def delete_movie_start(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("❌ Ruxsat yo'q!")
    
    await AdminStates.waiting_for_delete_code.set()
    await call.message.answer(
        "🗑 O'chirish uchun kino kodini yuboring:",
        reply_markup=cancel_kb
    )

@dp.message_handler(state=AdminStates.waiting_for_delete_code)
async def delete_movie_confirm(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    
    with get_db() as conn:
        cursor = conn.cursor()
        result = cursor.execute(
            "SELECT caption FROM movies WHERE code=?", (code,)
        ).fetchone()
        
        if result:
            cursor.execute("DELETE FROM movies WHERE code=?", (code,))
            await message.answer(
                f"✅ Kino o'chirildi!\n\n🔢 Kod: <code>{code}</code>",
                reply_markup=types.ReplyKeyboardRemove(),
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Bunday kod topilmadi!")
    
    await state.finish()

# ============================================================
# KANAL QO'SHISH
# ============================================================
@dp.callback_query_handler(text="add_ch")
async def add_ch_start(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("❌ Ruxsat yo'q!")
    
    await AdminStates.waiting_for_channel_id.set()
    await call.message.answer(
        "📢 Kanal ID sini yuboring:\n\n"
        "<i>Misol: -1001234567890</i>\n\n"
        "💡 ID ni olish uchun: @userinfobot",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

@dp.message_handler(state=AdminStates.waiting_for_channel_id)
async def get_ch_id(message: types.Message, state: FSMContext):
    ch_id = message.text.strip()
    
    # ID validatsiyasi
    if not re.match(r'^-100\d{10,}$', ch_id):
        await message.answer(
            "❌ Noto'g'ri format!\n"
            "Kanal ID -100 bilan boshlanishi va raqamlardan iborat bo'lishi kerak."
        )
        return
    
    await state.update_data(ch_id=ch_id)
    await AdminStates.waiting_for_channel_link.set()
    await message.answer(
        "🔗 Kanal linkini yuboring:\n\n"
        "<i>Misol: https://t.me/mychannel</i>",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

@dp.message_handler(state=AdminStates.waiting_for_channel_link)
async def get_ch_link(message: types.Message, state: FSMContext):
    link = message.text.strip()
    
    # Link validatsiyasi
    if not re.match(r'^https?://(t\.me|telegram\.me)/.+$', link):
        await message.answer("❌ Noto'g'ri link formati!")
        return
    
    data = await state.get_data()
    
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO channels (id, link) VALUES (?, ?)",
                (data['ch_id'], link)
            )
        
        await message.answer(
            "✅ Kanal muvaffaqiyatli qo'shildi!",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ Bu kanal allaqachon qo'shilgan!")
    except Exception as e:
        logger.error(f"Channel add error: {e}")
        await message.answer("❌ Xatolik yuz berdi!")
    
    await state.finish()

# ============================================================
# KANAL O'CHIRISH
# ============================================================
@dp.callback_query_handler(text="del_ch")
async def delete_channel_start(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("❌ Ruxsat yo'q!")
    
    with get_db() as conn:
        cursor = conn.cursor()
        channels = cursor.execute("SELECT id, link FROM channels").fetchall()
    
    if not channels:
        await call.answer("❌ Hech qanday kanal yo'q!", show_alert=True)
        return
    
    text = "📢 <b>Kanallar ro'yxati:</b>\n\n"
    for i, ch in enumerate(channels, 1):
        text += f"{i}. <code>{ch['id']}</code>\n   {ch['link']}\n\n"
    
    text += "\nO'chirish uchun kanal ID sini yuboring:"
    
    await AdminStates.waiting_for_delete_channel.set()
    await call.message.answer(text, reply_markup=cancel_kb, parse_mode="HTML")

@dp.message_handler(state=AdminStates.waiting_for_delete_channel)
async def delete_channel_confirm(message: types.Message, state: FSMContext):
    ch_id = message.text.strip()
    
    with get_db() as conn:
        cursor = conn.cursor()
        result = cursor.execute(
            "SELECT link FROM channels WHERE id=?", (ch_id,)
        ).fetchone()
        
        if result:
            cursor.execute("DELETE FROM channels WHERE id=?", (ch_id,))
            cursor.execute("DELETE FROM pending_requests WHERE channel_id=?", (ch_id,))
            await message.answer(
                f"✅ Kanal o'chirildi!\n\n<code>{ch_id}</code>",
                reply_markup=types.ReplyKeyboardRemove(),
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Bunday kanal topilmadi!")
    
    await state.finish()

# ============================================================
# REKLAMA YUBORISH
# ============================================================
@dp.callback_query_handler(text="send_ads")
async def ads_start(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("❌ Ruxsat yo'q!")
    
    await AdminStates.waiting_for_ads.set()
    await call.message.answer(
        "📣 Reklama xabarini yuboring:\n\n"
        "Rasm, video, matn yoki boshqa formatda bo'lishi mumkin.",
        reply_markup=cancel_kb
    )

@dp.message_handler(state=AdminStates.waiting_for_ads, content_types=types.ContentTypes.ANY)
async def start_ads(message: types.Message, state: FSMContext):
    with get_db() as conn:
        cursor = conn.cursor()
        users = cursor.execute("SELECT user_id FROM users").fetchall()
    
    total = len(users)
    msg = await message.answer(
        f"📣 Reklama {total} ta foydalanuvchiga yuborilmoqda...\n\n"
        "⏳ Kutib turing...",
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    success, blocked, error = 0, 0, 0
    
    for i, user in enumerate(users, 1):
        try:
            await bot.copy_message(user['user_id'], message.chat.id, message.message_id)
            success += 1
            await asyncio.sleep(0.05)
            
            # Har 50 tadan keyin progress yangilash
            if i % 50 == 0:
                await msg.edit_text(
                    f"📊 Progress: {i}/{total}\n\n"
                    f"✅ Yuborildi: {success}\n"
                    f"❌ Bloklagan: {blocked}\n"
                    f"⚠️ Xato: {error}"
                )
        except Exception as e:
            if "blocked" in str(e).lower():
                blocked += 1
            else:
                error += 1
                logger.error(f"Ad send error to {user['user_id']}: {e}")
    
    await msg.edit_text(
        "✅ <b>Reklama yuborish yakunlandi!</b>\n\n"
        f"📊 Jami: {total}\n"
        f"✅ Yuborildi: {success}\n"
        f"❌ Bloklagan: {blocked}\n"
        f"⚠️ Xato: {error}",
        parse_mode="HTML"
    )
    
    await state.finish()

# ============================================================
# ADMIN QO'SHISH
# ============================================================
@dp.callback_query_handler(text="add_admin")
async def add_admin_start(call: types.CallbackQuery):
    if call.from_user.id != CREATOR_ID:
        return await call.answer("❌ Faqat yaratuvchi admin qo'sha oladi!")
    
    await AdminStates.waiting_for_new_admin.set()
    await call.message.answer(
        "👤 Yangi admin ID sini yuboring:",
        reply_markup=cancel_kb
    )

@dp.message_handler(state=AdminStates.waiting_for_new_admin)
async def add_admin_confirm(message: types.Message, state: FSMContext):
    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Faqat raqam kiriting!")
        return
    
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO admins (user_id) VALUES (?)", (new_admin_id,)
            )
        
        await message.answer(
            f"✅ Yangi admin qo'shildi!\n\n"
            f"ID: <code>{new_admin_id}</code>",
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ Bu foydalanuvchi allaqachon admin!")
    except Exception as e:
        logger.error(f"Add admin error: {e}")
        await message.answer("❌ Xatolik yuz berdi!")
    
    await state.finish()

# ============================================================
# KINO QIDIRISH
# ============================================================
@dp.message_handler(commands=['search'])
async def search_cmd(message: types.Message):
    await AdminStates.waiting_for_search.set()
    await message.answer(
        "🔍 Qidirish uchun kino nomi yoki kodini kiriting:",
        reply_markup=cancel_kb
    )

@dp.callback_query_handler(text="search_movie")
async def search_movie_callback(call: types.CallbackQuery):
    await AdminStates.waiting_for_search.set()
    await call.message.answer(
        "🔍 Qidirish uchun kino nomi yoki kodini kiriting:",
        reply_markup=cancel_kb
    )

@dp.message_handler(state=AdminStates.waiting_for_search)
async def process_search(message: types.Message, state: FSMContext):
    query = message.text.strip()
    
    if len(query) < 2:
        await message.answer("❌ Kamida 2 ta belgi kiriting!")
        return
    
    results = search_movies(query)
    
    # Qidiruv logini saqlash
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO search_log (user_id, query, found) VALUES (?, ?, ?)",
            (message.from_user.id, query, len(results))
        )
    
    if results:
        text = f"🔍 <b>'{query}' bo'yicha natijalar:</b>\n\n"
        for code, caption in results:
            text += f"🎬 <code>{code}</code>\n"
            if caption and caption != "Caption yo'q":
                text += f"   <i>{caption[:100]}...</i>\n"
            text += "\n"
        
        text += "\n💡 Kino kodini yuboring yoki bosing"
        
        await message.answer(text, reply_markup=types.ReplyKeyboardRemove(), parse_mode="HTML")
    else:
        await message.answer(
            f"❌ '{query}' bo'yicha hech narsa topilmadi.",
            reply_markup=types.ReplyKey
