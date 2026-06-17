import sqlite3
import logging
import html
import aiohttp
import asyncio
import os
from pathlib import Path
from typing import Dict, Tuple, Optional, List
from datetime import datetime

# FastAPI импорты
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from fastapi.exceptions import RequestValidationError
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)

# ==================== НАСТРОЙКИ ====================

TOKEN = os.getenv("BOT_TOKEN")  # токен бота
RANDOM_ORG_API_KEY = os.getenv("RANDOM_API_KEY")  # API ключ от random.org
# Путь к папке data
DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Путь к базе данных
DB_NAME = str(DATA_DIR / "bot.db")

# Webhook настройки
WEBHOOK_HOST = f"https://{os.getenv('DOMAIN')}" # Ваш домен (обязательно HTTPS)
WEBHOOK_PORT = int(os.getenv("PORT", "3000")) # Порт для webhook
WEBHOOK_LISTEN = "0.0.0.0"  # Адрес для прослушивания
WEBHOOK_URL_PATH = "/webhook"  # Путь для webhook

# Состояния для ConversationHandler
WAITING_FOR_CATEGORY_NAME = 1
WAITING_FOR_PRIVACY = 2
WAITING_FOR_IMAGES = 3

# Временное хранилище данных пользователя в памяти
user_sessions: Dict[int, Dict] = {}

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== API RANDOM.ORG ====================
class RandomOrgAPI:
    """Класс для работы с API random.org"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.random.org/json-rpc/4/invoke"
    
    async def generate_integers(self, min_val: int, max_val: int, count: int = 1) -> Optional[List[int]]:
        """Генерирует случайные целые числа через API random.org"""
        if not self.api_key or self.api_key == "YOUR_RANDOM_ORG_API_KEY":
            logger.error("API ключ random.org не настроен")
            return None
        
        payload = {
            "jsonrpc": "2.0",
            "method": "generateIntegers",
            "params": {
                "apiKey": self.api_key,
                "n": count,
                "min": min_val,
                "max": max_val,
                "replacement": True
            },
            "id": int(datetime.now().timestamp())
        }
        
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.base_url, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        if "result" in data and "random" in data["result"]:
                            random_numbers = data["result"]["random"]["data"]
                            logger.info(f"Сгенерировано {len(random_numbers)} случайных чисел")
                            return random_numbers
                        elif "error" in data:
                            logger.error(f"Ошибка API random.org: {data['error']['message']}")
                            return None
                    else:
                        logger.error(f"HTTP ошибка: {response.status}")
                        return None
        except Exception as e:
            logger.error(f"Исключение при запросе к random.org: {e}")
            return None
    
    async def generate_random_index(self, max_index: int) -> Optional[int]:
        """Генерирует один случайный индекс от 0 до max_index-1"""
        if max_index <= 0:
            return None
        
        result = await self.generate_integers(0, max_index - 1, 1)
        if result and len(result) > 0:
            return result[0]
        return None

# Инициализация API клиента
random_api = RandomOrgAPI(RANDOM_ORG_API_KEY)

# ==================== БАЗА ДАННЫХ ====================
def get_db_connection():
    """Создает подключение к БД и обязательно включает поддержку Foreign Keys"""
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    """Создаёт таблицы, если их нет"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                creator_id INTEGER NOT NULL DEFAULT 0,
                is_private INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE CASCADE
            )
        ''')
    logger.info("База данных инициализирована")

def get_or_create_category(category_name: str, creator_id: int, is_private: int) -> int:
    """Возвращает ID колоды. Если не существует — создаёт."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        row = cursor.fetchone()
        if row:
            return row[0]
        
        cursor.execute(
            "INSERT INTO categories (name, creator_id, is_private) VALUES (?, ?, ?)", 
            (category_name, creator_id, is_private)
        )
        return cursor.lastrowid

def add_image_to_db(category_name: str, creator_id: int, is_private: int, file_id: str, file_unique_id: str, description: str) -> bool:
    """Добавляет запись об изображении карты в БД используя file_id"""
    try:
        cat_id = get_or_create_category(category_name, creator_id, is_private)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO images (category_id, file_id, file_unique_id, description) 
                   VALUES (?, ?, ?, ?)""",
                (cat_id, file_id, file_unique_id, description)
            )
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления в БД: {e}")
        return False

def get_all_images_from_category(category_name: str) -> List[Tuple[str, str, str]]:
    """Возвращает список всех изображений в колоде"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT images.file_id, images.file_unique_id, images.description 
            FROM images 
            JOIN categories ON images.category_id = categories.id
            WHERE categories.name = ?
            ORDER BY images.id
        ''', (category_name,))
        return cursor.fetchall()

def delete_category_by_id(category_id: int, user_id: int) -> Tuple[bool, str, str]:
    """Удаляет колоду по ID с каскадным удалением карт"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, creator_id FROM categories WHERE id = ?", (category_id,))
        row = cursor.fetchone()
        if not row:
            return False, "Колода не найдена.", ""
        
        category_name, creator_id = row
        if creator_id != user_id:
            return False, "У вас нет прав на удаление этой колоды. Только создатель может удалить её.", ""

        cursor.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        return True, "Успешно удалено.", category_name

def delete_category_by_name(category_name: str, user_id: int) -> bool:
    """Удаляет колоду по имени (используется как fallback при пустой отмене)"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM categories WHERE name = ? AND creator_id = ?", (category_name, user_id))
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления пустой колоды: {e}")
        return False

def get_visible_categories(user_id: int) -> List[Tuple[int, str, int, int]]:
    """Возвращает список колод, видимых пользователю (id, name, creator_id, is_private)"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, creator_id, is_private FROM categories WHERE is_private = 0 OR creator_id = ? ORDER BY name", 
            (user_id,)
        )
        return cursor.fetchall()

def get_category_statistics(category_name: str) -> int:
    """Возвращает количество карт в колоде"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) 
            FROM images 
            JOIN categories ON images.category_id = categories.id
            WHERE categories.name = ?
        ''', (category_name,))
        row = cursor.fetchone()
        return row[0] if row else 0

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "👋 <b>Привет! Я бот для хранения карточных колод и вытаскивания карт.</b>\n\n"
        "📌 /new_category — создать новую колоду и настроить приватность\n"
        "📌 /random — получить случайное изображение из доступной колоды\n"
        "📌 /delete_category — удалить созданную вами колоду\n"
        "📌 /stats — показать статистику по доступным колодам\n"
        "📌 /test_random — тест работы генератора случайных чисел от random.org\n"
        "📌 /cancel — отменить текущее действие",
        parse_mode="HTML"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего диалога"""
    user_id = update.effective_user.id
    if user_id in user_sessions:
        del user_sessions[user_id]
    
    message = update.message if update.message else update.callback_query.message
    await message.reply_text("❌ Действие отменено.", parse_mode="HTML")
    return ConversationHandler.END

async def test_random_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестирование API random.org"""
    await update.message.reply_text("🎲 Тестирую подключение к random.org...", parse_mode="HTML")
    
    if not RANDOM_ORG_API_KEY or RANDOM_ORG_API_KEY == "YOUR_RANDOM_ORG_API_KEY":
        await update.message.reply_text("❌ API ключ не настроен!\n\n", parse_mode="HTML")
        return
    
    result = await random_api.generate_integers(1, 100, 5)
    
    if result:
        numbers_str = ", ".join(map(str, result))
        await update.message.reply_text(
            f"✅ <b>API random.org работает!</b>\n\n"
            f"🎲 Случайные числа (1-100):\n{numbers_str}\n\n"
            f"🔗 Использовано true random (аппаратная энтропия)",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "❌ Ошибка при обращении к API random.org\n\nВозможно превышен дневной лимит запросов",
            parse_mode="HTML"
        )

async def new_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания категории: запрос имени"""
    await update.message.reply_text("📝 <b>Введите название новой колоды:</b>", parse_mode="HTML")
    return WAITING_FOR_CATEGORY_NAME

async def receive_category_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем имя колоды, запрашиваем режим приватности"""
    user_id = update.effective_user.id
    category_name = update.message.text.strip()
    
    if not category_name:
        await update.message.reply_text("❌ Название не может быть пустым. Попробуйте ещё раз:", parse_mode="HTML")
        return WAITING_FOR_CATEGORY_NAME
    
    if len(category_name) > 64:
        await update.message.reply_text("❌ Название слишком длинное (макс. 64 символа):", parse_mode="HTML")
        return WAITING_FOR_CATEGORY_NAME
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        exists = cursor.fetchone()
    
    if exists:
        await update.message.reply_text("❌ Колода с таким названием уже существует. Придумайте другое название:", parse_mode="HTML")
        return WAITING_FOR_CATEGORY_NAME

    user_sessions[user_id] = {
        "category_name": category_name,
        "is_private": 0,
        "images_count": 0
    }
    
    keyboard = [
        [
            InlineKeyboardButton("🌐 Публичная", callback_data="privacy_public"),
            InlineKeyboardButton("🔒 Приватная", callback_data="privacy_private")
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="privacy_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    escaped_cat_name = html.escape(category_name)
    await update.message.reply_text(
        f"Укажите режим доступа для колоды <b>'{escaped_cat_name}'</b>:\n\n"
        f"🌐 <b>Публичная</b> — все пользователи бота смогут вытягивать из неё карты.\n"
        f"🔒 <b>Приватная</b> — колоду будете видеть только вы.",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
    return WAITING_FOR_PRIVACY

async def handle_privacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем выбор приватности колоды"""
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()
    
    if query.data == "privacy_cancel":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await query.edit_message_text("❌ Создание колоды отменено.", parse_mode="HTML")
        return ConversationHandler.END
        
    if user_id not in user_sessions:
        await query.edit_message_text("❌ Ошибка сессии. Начните заново с /new_category", parse_mode="HTML")
        return ConversationHandler.END
        
    is_private = 1 if query.data == "privacy_private" else 0
    user_sessions[user_id]["is_private"] = is_private
    category_name = user_sessions[user_id]["category_name"]
    
    privacy_status = "🔒 <b>Приватная</b>" if is_private else "🌐 <b>Публичная</b>"
    escaped_cat_name = html.escape(category_name)
    
    await query.edit_message_text(
        f"✅ Режим доступа установлен: {privacy_status}\n\n"
        f"📸 Теперь отправляйте мне <b>изображения</b> с <b>описанием</b> в подписи для колоды <b>'{escaped_cat_name}'</b>.\n"
        f"Когда закончите, отправьте команду /done",
        parse_mode="HTML"
    )
    return WAITING_FOR_IMAGES

async def handle_image_with_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото + описания (в caption) с сохранением file_id"""
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Сначала начните создание колоды командой /new_category", parse_mode="HTML")
        return ConversationHandler.END
    
    session = user_sessions[user_id]
    category_name = session["category_name"]
    is_private = session["is_private"]
    
    if not update.message.photo:
        await update.message.reply_text("❌ Пожалуйста, отправьте именно изображение с подписью.", parse_mode="HTML")
        return WAITING_FOR_IMAGES
    
    description = update.message.caption if update.message.caption else ""
    
    if not description:
        await update.message.reply_text("⚠️ Добавьте описание в подпись к изображению.\nПопробуйте ещё раз:", parse_mode="HTML")
        return WAITING_FOR_IMAGES
    
    description = description.strip()[:1024]
    photo = update.message.photo[-1]
    file_id = photo.file_id
    file_unique_id = photo.file_unique_id
    
    success = add_image_to_db(category_name, user_id, is_private, file_id, file_unique_id, description)
    
    if success:
        session["images_count"] += 1
        escaped_desc = html.escape(description)
        await update.message.reply_photo(
            photo=file_id,
            caption=f"✅ <b>Карта #{session['images_count']} сохранена!</b>\n\n"
                   f"📝 <b>Описание:</b> {escaped_desc}",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Ошибка сохранения в базе данных.", parse_mode="HTML")
    
    return WAITING_FOR_IMAGES

async def finish_adding_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение добавления изображений"""
    user_id = update.effective_user.id
    
    if user_id in user_sessions:
        session = user_sessions[user_id]
        category_name = session["category_name"]
        count = session["images_count"]
        
        if count == 0:
            delete_category_by_name(category_name, user_id)
            await update.message.reply_text("⚠️ Вы не добавили ни одной карты. Колода не создана.", parse_mode="HTML")
        else:
            privacy_str = "приватная" if session["is_private"] else "публичная"
            escaped_cat_name = html.escape(category_name)
            await update.message.reply_text(
                f"🎉 <b>Поздравляю!</b>\n\n"
                f"✅ Колода <b>'{escaped_cat_name}'</b> ({privacy_str}) успешно создана!\n"
                f"📸 Добавлено карт: <b>{count}</b>",
                parse_mode="HTML"
            )
        del user_sessions[user_id]
    else:
        await update.message.reply_text("❌ Нет активного процесса добавления.", parse_mode="HTML")
    
    return ConversationHandler.END

async def random_image_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем кнопки с доступными категориями для выбора"""
    user_id = update.effective_user.id
    categories = get_visible_categories(user_id)
    
    if not categories:
        await update.message.reply_text(
            "😔 Нет ни одной доступной колоды.\n\nСначала добавьте колоду через команду /new_category",
            parse_mode="HTML"
        )
        return
    
    keyboard = []
    for cat_id, cat_name, creator_id, is_private in categories:
        count = get_category_statistics(cat_name)
        prefix = "🔒" if is_private else "🌐"
        keyboard.append([InlineKeyboardButton(f"{prefix} {cat_name} ({count})", callback_data=f"rand_{cat_id}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="rand_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎲 <b>Выберите колоду</b> для получения случайной карты\n(используется генератор от random.org):",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_random_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора категории с получением случайного индекса через API"""
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()
    
    if query.data == "rand_cancel":
        await query.edit_message_text("❌ Выбор колоды отменён.", parse_mode="HTML")
        return
    
    cat_id_str = query.data.replace("rand_", "")
    if not cat_id_str.isdigit():
        return
    cat_id = int(cat_id_str)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, is_private, creator_id FROM categories WHERE id = ?", (cat_id,))
        row = cursor.fetchone()
    
    if not row:
        await query.edit_message_text("❌ Колода не найдена.", parse_mode="HTML")
        return
        
    category_name, is_private, creator_id = row
    
    if is_private == 1 and creator_id != user_id:
        await query.edit_message_text("❌ Эта колода приватная, у вас нет к ней доступа.", parse_mode="HTML")
        return

    images = get_all_images_from_category(category_name)
    escaped_cat_name = html.escape(category_name)
    
    if not images:
        await query.edit_message_text(f"😞 В колоде <b>{escaped_cat_name}</b> нет карт.", parse_mode="HTML")
        return
    
    await query.edit_message_text(
        f"🎲 <b>Генерация случайного числа через API random.org...</b>\n\n"
        f"📁 Колода: {escaped_cat_name}\n"
        f"📸 Всего карт: {len(images)}\n\n"
        f"⏳ Пожалуйста, подождите...",
        parse_mode="HTML"
    )
    
    random_index = await random_api.generate_random_index(len(images))
    
    if random_index is None:
        import random as local_random
        random_index = local_random.randint(0, len(images) - 1)
        await query.message.reply_text(
            "⚠️ <b>Внимание!</b>\nAPI random.org недоступен. Использован локальный генератор.\n\n",
            parse_mode="HTML"
        )
    else:
        await query.message.reply_text(
            f"✅ <b>Случайное число получено от random.org!</b>\n🎲 Номер карты: {random_index + 1} / {len(images)}",
            parse_mode="HTML"
        )
    
    file_id, file_unique_id, description = images[random_index]
    escaped_desc = html.escape(description)
    
    try:
        await query.message.reply_photo(
            photo=file_id,
            caption=f"🎲 <b>Случайная карта</b>\n\n"
                   f"📁 <b>Колода:</b> {escaped_cat_name}\n"
                   f"📝 <b>Описание:</b> {escaped_desc}\n\n"
                   f"🔢 <b>Номер:</b> {random_index + 1} из {len(images)}",
            parse_mode="HTML"
        )
        
        keyboard = [
            [InlineKeyboardButton("🎲 Ещё раз", callback_data=f"rand_{cat_id}")],
            [InlineKeyboardButton("🔙 Другая колода", callback_data="back_to_categories")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text("✨ <b>Что дальше?</b>", reply_markup=reply_markup, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка отправки карты: {e}")
        await query.message.reply_text("❌ Не удалось отправить изображение.", parse_mode="HTML")

async def back_to_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к списку категорий"""
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()
    
    categories = get_visible_categories(user_id)
    if not categories:
        await query.edit_message_text("😔 Нет доступных колод.", parse_mode="HTML")
        return
    
    keyboard = []
    for cat_id, cat_name, creator_id, is_private in categories:
        count = get_category_statistics(cat_name)
        prefix = "🔒" if is_private else "🌐"
        keyboard.append([InlineKeyboardButton(f"{prefix} {cat_name} ({count})", callback_data=f"rand_{cat_id}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="rand_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("🎲 <b>Выберите колоду:</b>", reply_markup=reply_markup, parse_mode="HTML")

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику по категориям, доступным текущему пользователю"""
    user_id = update.effective_user.id
    categories = get_visible_categories(user_id)
    
    if not categories:
        await update.message.reply_text("📊 Нет данных. Список доступных колод пуст.", parse_mode="HTML")
        return
    
    stats_text = "<b>📊 Доступная статистика:</b>\n\n"
    total_images = 0
    
    for cat_id, cat_name, creator_id, is_private in categories:
        count = get_category_statistics(cat_name)
        total_images += count
        privacy_label = "🔒 Приватная" if is_private else "🌐 Публичная"
        stats_text += f"📁 <b>{html.escape(cat_name)}</b> ({privacy_label}): {count} карт\n"
    
    stats_text += f"\n✨ <b>Всего доступно карт:</b> {total_images}"
    stats_text += f"\n📂 <b>Всего доступно колод:</b> {len(categories)}"
    
    if RANDOM_ORG_API_KEY and RANDOM_ORG_API_KEY != "YOUR_RANDOM_ORG_API_KEY":
        stats_text += f"\n\n🎲 <b>Генератор:</b> random.org"
    else:
        stats_text += f"\n\n⚠️ <b>Генератор:</b> локальный"
    
    await update.message.reply_text(stats_text, parse_mode="HTML")

async def delete_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем кнопки с категориями для удаления (только те, которые созданы пользователем)"""
    user_id = update.effective_user.id
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM categories WHERE creator_id = ? ORDER BY name", (user_id,))
        rows = cursor.fetchall()
    
    if not rows:
        await update.message.reply_text("❌ У вас нет созданных колод, которые вы могли бы удалить.", parse_mode="HTML")
        return
    
    keyboard = []
    for cat_id, cat_name in rows:
        count = get_category_statistics(cat_name)
        keyboard.append([InlineKeyboardButton(f"🗑 {cat_name} ({count})", callback_data=f"del_{cat_id}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚠️ <b>ВНИМАНИЕ!</b> ⚠️\n\n"
        "Вы собираетесь <b>безвозвратно удалить</b> созданную вами колоду\n"
        "и <b>ВСЕ</b> карты в ней.\n\n"
        "Выберите колоду для удаления:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка удаления колоды с проверкой авторства по ID"""
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()
    
    if query.data == "del_cancel":
        await query.edit_message_text("✅ Удаление отменено.", parse_mode="HTML")
        return
    
    cat_id_str = query.data.replace("del_", "")
    if not cat_id_str.isdigit():
        await query.edit_message_text("❌ Некорректный идентификатор колоды.", parse_mode="HTML")
        return
    
    category_id = int(cat_id_str)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
        row = cursor.fetchone()
    
    if not row:
        await query.edit_message_text("❌ Колода уже была удалена или не существует.", parse_mode="HTML")
        return
        
    category_name = row[0]
    count = get_category_statistics(category_name)
    
    success, message, deleted_name = delete_category_by_id(category_id, user_id)
    
    if success:
        await query.edit_message_text(
            f"✅ <b>Колода успешно удалена!</b>\n\n"
            f"📁 <b>Название:</b> {html.escape(deleted_name)}\n"
            f"📸 <b>Удалено карт:</b> {count}",
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text(f"❌ <b>Ошибка:</b> {html.escape(message)}", parse_mode="HTML")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует исключения, вызванные обновлениями."""
    logger.error(msg="Исключение при обработке обновления:", exc_info=context.error)
    
# ==================== FASTAPI WEBHOOK ОБРАБОТЧИК ====================
telegram_app = None

def setup_telegram_app() -> Application:
    """Настройка приложения Telegram"""
    global telegram_app
    
    telegram_app = Application.builder().token(TOKEN).build()
    
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("new_category", new_category_start)],
        states={
            WAITING_FOR_CATEGORY_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_category_name)
            ],
            WAITING_FOR_PRIVACY: [
                CallbackQueryHandler(handle_privacy_callback, pattern="^privacy_")
            ],
            WAITING_FOR_IMAGES: [
                MessageHandler(filters.PHOTO, handle_image_with_description),
                CommandHandler("done", finish_adding_images),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel), 
            CallbackQueryHandler(handle_privacy_callback, pattern="^privacy_cancel$")
        ],
        allow_reentry=True,
        per_message=False,
    )
    
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("cancel", cancel))
    telegram_app.add_handler(CommandHandler("stats", show_stats))
    telegram_app.add_handler(CommandHandler("test_random", test_random_api))
    telegram_app.add_handler(add_conv)
    telegram_app.add_handler(CommandHandler("random", random_image_start))
    telegram_app.add_handler(CommandHandler("delete_category", delete_category_start))
    
    telegram_app.add_handler(CallbackQueryHandler(handle_random_callback, pattern="^rand_"))
    telegram_app.add_handler(CallbackQueryHandler(back_to_categories, pattern="^back_to_categories$"))
    telegram_app.add_handler(CallbackQueryHandler(handle_delete_callback, pattern="^(del_|del_cancel)"))
    
    telegram_app.add_error_handler(error_handler)
	
    return telegram_app
    
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Управление жизненным циклом приложения (Lifespan)"""
    init_db()
    setup_telegram_app()
    
    await telegram_app.initialize()
    
    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_URL_PATH}"
    try:
        result = await telegram_app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True
        )
        if result:
            logger.info(f"Webhook успешно установлен: {webhook_url}")
        else:
            logger.error("Не удалось установить webhook")
    except Exception as e:
        logger.error(f"Ошибка при установке webhook: {e}")

    yield

    try:
        await telegram_app.bot.delete_webhook()
        logger.info("Webhook удалён")
        await telegram_app.shutdown()
        logger.info("Telegram-приложение успешно остановлено")
    except Exception as e:
        logger.error(f"Ошибка при остановке telegram app: {e}")

app = FastAPI(title="Telegram Bot Webhook", lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error("❌ Ошибка валидации входящего Webhook-запроса от Telegram!")
    logger.error(f"Тело запроса: {exc.body}")
    logger.error(f"Детали ошибки: {exc.errors()}")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"status": "error", "message": "Invalid data format", "details": exc.errors()}
    )

@app.exception_handler(Exception)
async def universal_exception_handler(request: Request, exc: Exception):
    logger.error(f"💥 Критическая ошибка при обработке запроса {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"status": "error", "message": "Internal server error"}
    )
	
@app.post(WEBHOOK_URL_PATH)
async def webhook(request: Request):
    """Обработчик webhook от Telegram"""
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, telegram_app.bot)
        await telegram_app.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Ошибка в webhook: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": str(e)}
        )

@app.get("/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/")
def index():
    return {
        "name": "Telegram Bot Webhook (FastAPI)",
        "status": "running"        
    }

if __name__ == "__main__":
    uvicorn.run(
        "modbot:app",  # имя файла изменено на modbot (соответствует переданному названию)
        host=WEBHOOK_LISTEN,
        port=WEBHOOK_PORT,
        reload=False
    )