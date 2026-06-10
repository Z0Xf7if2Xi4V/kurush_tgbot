import sqlite3
import logging
import aiohttp
import asyncio
import json
from typing import Dict, Tuple, Optional, List
from datetime import datetime
from flask import Flask, request, jsonify
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
    TypeHandler
)

# ==================== НАСТРОЙКИ ====================
TOKEN = "YOUR_BOT_TOKEN_HERE"  # токен бота
RANDOM_ORG_API_KEY = "YOUR_RANDOM_ORG_API_KEY"  # API ключ от random.org
DB_NAME = "memory_cards.db"

# Webhook настройки
WEBHOOK_HOST = "https://your-domain.com"  # Ваш домен (обязательно HTTPS)
WEBHOOK_PORT = 8443  # Порт для webhook (обычно 443, 8443, 80)
WEBHOOK_LISTEN = "0.0.0.0"  # Адрес для прослушивания
WEBHOOK_URL_PATH = f"/webhook/{TOKEN}"  # Путь для webhook

# Flask приложение
flask_app = Flask(__name__)

# Состояния для ConversationHandler
WAITING_FOR_CATEGORY_NAME = 1
WAITING_FOR_IMAGES = 2

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
            async with aiohttp.ClientSession() as session:
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
def init_db():
    """Создаёт таблицы, если их нет"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
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
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

def get_or_create_category(category_name: str) -> int:
    """Возвращает ID колоды. Если не существует — создаёт."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
    row = cursor.fetchone()
    if row:
        cat_id = row[0]
    else:
        cursor.execute("INSERT INTO categories (name) VALUES (?)", (category_name,))
        cat_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return cat_id

def add_image_to_db(category_name: str, file_id: str, file_unique_id: str, description: str) -> bool:
    """Добавляет запись об изображении карты в БД используя file_id"""
    try:
        cat_id = get_or_create_category(category_name)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO images (category_id, file_id, file_unique_id, description) 
               VALUES (?, ?, ?, ?)""",
            (cat_id, file_id, file_unique_id, description)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления в БД: {e}")
        return False

def get_all_images_from_category(category_name: str) -> List[Tuple[str, str, str]]:
    """Возвращает список всех изображений в колоде"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT images.file_id, images.file_unique_id, images.description 
        FROM images 
        JOIN categories ON images.category_id = categories.id
        WHERE categories.name = ?
        ORDER BY images.id
    ''', (category_name,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_category(category_name: str) -> bool:
    """Удаляет колоду и все записи о картах в ней"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM categories WHERE name = ?", (category_name,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления колоды: {e}")
        conn.rollback()
        conn.close()
        return False

def get_all_categories() -> List[str]:
    """Возвращает список всех колод"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM categories ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def get_category_statistics(category_name: str) -> int:
    """Возвращает количество карт в колоде"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) 
        FROM images 
        JOIN categories ON images.category_id = categories.id
        WHERE categories.name = ?
    ''', (category_name,))
    count = cursor.fetchone()[0]
    conn.close()
    return count

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "👋 Привет! Я бот для хранения карточных колод и вытаскивания карт.\n\n"
        "📌 /new_category — создать новую колоду и добавить в неё изображения карт\n"
        "📌 /random — получить случайное изображение из выбранной колоды\n"
        "📌 /delete_category — удалить колоду со всеми картами в ней\n"
        "📌 /stats — показать статистику по колодам\n"
        "📌 /test_random — тест работы генератора случайных чисел от random.org\n"
        "📌 /cancel — отменить текущее действие"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего диалога"""
    user_id = update.effective_user.id
    if user_id in user_sessions:
        del user_sessions[user_id]
    await update.message.reply_text("❌ Действие отменено.")
    return ConversationHandler.END

async def test_random_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестирование API random.org"""
    await update.message.reply_text("🎲 Тестирую подключение к random.org...")
    
    if not RANDOM_ORG_API_KEY or RANDOM_ORG_API_KEY == "YOUR_RANDOM_ORG_API_KEY":
        await update.message.reply_text(
            "❌ API ключ не настроен!\n\n"
        )
        return
    
    result = await random_api.generate_integers(1, 100, 5)
    
    if result:
        numbers_str = ", ".join(map(str, result))
        await update.message.reply_text(
            f"✅ API random.org работает!\n\n"
            f"🎲 Случайные числа (1-100):\n{numbers_str}\n\n"
            f"🔗 Использовано true random (аппаратная энтропия)"
        )
    else:
        await update.message.reply_text(
            "❌ Ошибка при обращении к API random.org\n\n"      
            "Возможно превышен дневной лимит запросов"
        )

async def new_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания категории: запрос имени"""
    await update.message.reply_text(
        "📝 Введите название новой колоды:\n"
    )
    return WAITING_FOR_CATEGORY_NAME

async def receive_category_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем имя колоды, инициализируем сессию"""
    user_id = update.effective_user.id
    category_name = update.message.text.strip()
    
    if not category_name:
        await update.message.reply_text("❌ Название не может быть пустым. Попробуйте ещё раз:")
        return WAITING_FOR_CATEGORY_NAME
    
    if len(category_name) > 50:
        await update.message.reply_text("❌ Название слишком длинное (макс. 50 символов):")
        return WAITING_FOR_CATEGORY_NAME
    
    user_sessions[user_id] = {
        "category_name": category_name,
        "images_count": 0
    }
    
    await update.message.reply_text(
        f"✅ Колода *'{category_name}'* готова к наполнению!\n\n"
        f"📸 Теперь отправляйте мне *изображения* с *описанием* в подписи.\n"
        f"Когда закончите, отправьте команду /done",
        parse_mode="Markdown"
    )
    return WAITING_FOR_IMAGES

async def handle_image_with_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото + описания (в caption) с сохранением file_id"""
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        await update.message.reply_text(
            "❌ Сначала начните создание колоды командой /new_category"
        )
        return ConversationHandler.END
    
    session = user_sessions[user_id]
    category_name = session["category_name"]
    
    if not update.message.photo:
        await update.message.reply_text(
            "❌ Пожалуйста, отправьте именно изображение с подписью."
        )
        return WAITING_FOR_IMAGES
    
    description = update.message.caption if update.message.caption else ""
    
    if not description:
        await update.message.reply_text(
            "⚠️ Добавьте описание в подпись к изображению.\n"
            "Попробуйте ещё раз:"
        )
        return WAITING_FOR_IMAGES
    
    photo = update.message.photo[-1]
    file_id = photo.file_id
    file_unique_id = photo.file_unique_id
    
    success = add_image_to_db(category_name, file_id, file_unique_id, description)
    
    if success:
        session["images_count"] += 1
        await update.message.reply_photo(
            photo=file_id,
            caption=f"✅ *Карта #{session['images_count']} сохранена!*\n\n"
                   f"📝 *Описание:* {description[:200]}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Ошибка сохранения в базе данных.")
    
    return WAITING_FOR_IMAGES

async def finish_adding_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение добавления изображений"""
    user_id = update.effective_user.id
    
    if user_id in user_sessions:
        session = user_sessions[user_id]
        category_name = session["category_name"]
        count = session["images_count"]
        
        if count == 0:
            await update.message.reply_text(
                "⚠️ Вы не добавили ни одной карты. Колода не создана."
            )
        else:
            await update.message.reply_text(
                f"🎉 *Поздравляю!*\n\n"
                f"✅ Колода *'{category_name}'* успешно создана!\n"
                f"📸 Добавлено карт: *{count}*",
                parse_mode="Markdown"
            )
        
        del user_sessions[user_id]
    else:
        await update.message.reply_text("❌ Нет активного процесса добавления.")
    
    return ConversationHandler.END

async def random_image_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем кнопки с категориями для выбора"""
    categories = get_all_categories()
    
    if not categories:
        await update.message.reply_text(
            "😔 Нет ни одной колоды.\n\n"
            "Сначала добавьте колоду через команду /new_category"
        )
        return
    
    keyboard = []
    for cat in categories:
        count = get_category_statistics(cat)
        keyboard.append([InlineKeyboardButton(f"📁 {cat} ({count})", callback_data=f"rand_{cat}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="rand_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎲 *Выберите колоду* для получения случайной карты\n"
        "(используется генератор от random.org):",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def handle_random_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора категории с получением случайного индекса через API"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "rand_cancel":
        await query.edit_message_text("❌ Выбор колоды отменён.")
        return
    
    category_name = query.data.replace("rand_", "")
    
    images = get_all_images_from_category(category_name)
    
    if not images:
        await query.edit_message_text(
            f"😞 В колоде *'{category_name}'* нету карт.",
            parse_mode="Markdown"
        )
        return
    
    await query.edit_message_text(
        f"🎲 *Генерация случайного числа через API random.org...*\n\n"
        f"📁 Колода: {category_name}\n"
        f"📸 Всего карт: {len(images)}\n\n"
        f"⏳ Пожалуйста, подождите...",
        parse_mode="Markdown"
    )
    
    random_index = await random_api.generate_random_index(len(images))
    
    if random_index is None:
        import random as local_random
        random_index = local_random.randint(0, len(images) - 1)
        await query.message.reply_text(
            "⚠️ *Внимание!*\n"
            "API random.org недоступен. Использован локальный генератор.\n\n",
            parse_mode="Markdown"
        )
    else:
        await query.message.reply_text(
            f"✅ *Случайное число получено от random.org!*\n"
            f"🎲 Номер карты: {random_index + 1} / {len(images)}",
            parse_mode="Markdown"
        )
    
    file_id, file_unique_id, description = images[random_index]
    
    try:
        await query.message.reply_photo(
            photo=file_id,
            caption=f"🎲 *Случайная карта*\n\n"
                   f"📁 *Колода:* {category_name}\n"
                   f"📝 *Описание:* {description}\n\n"
                   f"🔢 *Номер:* {random_index + 1} из {len(images)}",
            parse_mode="Markdown"
        )
        
        keyboard = [
            [InlineKeyboardButton("🎲 Ещё раз", callback_data=f"rand_{category_name}")],
            [InlineKeyboardButton("🔙 Другая колода", callback_data="back_to_categories")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(
            "✨ *Что дальше?*",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отправки карты: {e}")
        await query.message.reply_text(f"❌ Не удалось отправить изображение.")

async def back_to_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к списку категорий"""
    query = update.callback_query
    await query.answer()
    
    categories = get_all_categories()
    if not categories:
        await query.edit_message_text("😔 Нет доступных колод.")
        return
    
    keyboard = []
    for cat in categories:
        count = get_category_statistics(cat)
        keyboard.append([InlineKeyboardButton(f"📁 {cat} ({count})", callback_data=f"rand_{cat}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="rand_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🎲 *Выберите колоду:*",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику по категориям"""
    categories = get_all_categories()
    
    if not categories:
        await update.message.reply_text("📊 Нет данных. Список пуст.")
        return
    
    stats_text = "*📊 Статистика:*\n\n"
    total_images = 0
    
    for cat in categories:
        count = get_category_statistics(cat)
        total_images += count
        stats_text += f"📁 *{cat}*: {count} изображений карт\n"
    
    stats_text += f"\n✨ *Всего:* {total_images} изображений карт"
    stats_text += f"\n📂 *Всего колод:* {len(categories)}"
    
    if RANDOM_ORG_API_KEY and RANDOM_ORG_API_KEY != "YOUR_RANDOM_ORG_API_KEY":
        stats_text += f"\n\n🎲 *Генератор:* random.org"
    else:
        stats_text += f"\n\n⚠️ *Генератор:* локальный"
    
    await update.message.reply_text(stats_text, parse_mode="Markdown")

async def delete_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем кнопки с категориями для удаления"""
    categories = get_all_categories()
    
    if not categories:
        await update.message.reply_text("❌ Нет колод для удаления.")
        return
    
    keyboard = []
    for cat in categories:
        count = get_category_statistics(cat)
        keyboard.append([InlineKeyboardButton(f"🗑 {cat} ({count})", callback_data=f"del_{cat}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚠️ *ВНИМАНИЕ!* ⚠️\n\n"
        "Вы собираетесь *безвозвратно удалить* колоду\n"
        "и *ВСЕ* карты в ней.\n\n"
        "Выберите колоду для удаления:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка удаления колоды"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "del_cancel":
        await query.edit_message_text("✅ Удаление отменено.")
        return
    
    category_name = query.data.replace("del_", "")
    count = get_category_statistics(category_name)
    
    success = delete_category(category_name)
    
    if success:
        await query.edit_message_text(
            f"✅ *Колода успешно удалена!*\n\n"
            f"📁 *Название:* {category_name}\n"
            f"📸 *Удалено карт:* {count}",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text(
            f"❌ Ошибка при удалении колоды *'{category_name}'*.",
            parse_mode="Markdown"
        )

# ==================== WEBHOOK ОБРАБОТЧИК ====================
# Глобальные переменные для приложения Telegram
telegram_app = None
telegram_bot = None

async def setup_telegram_app():
    """Настройка приложения Telegram"""
    global telegram_app, telegram_bot
    
    telegram_app = Application.builder().token(TOKEN).build()
    telegram_bot = telegram_app.bot
    
    # Регистрация обработчиков
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("new_category", new_category_start)],
        states={
            WAITING_FOR_CATEGORY_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_category_name)
            ],
            WAITING_FOR_IMAGES: [
                MessageHandler(filters.PHOTO, handle_image_with_description),
                CommandHandler("done", finish_adding_images),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
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
    
    # Инициализация
    await telegram_app.initialize()
    
    return telegram_app

@flask_app.route(WEBHOOK_URL_PATH, methods=['POST'])
async def webhook():
    """Обработчик webhook от Telegram"""
    if request.method == 'POST':
        try:
            # Получаем обновление от Telegram
            update_data = request.get_json(force=True)
            
            # Конвертируем в объект Update
            update = Update.de_json(update_data, telegram_app.bot)
            
            # Обрабатываем обновление асинхронно
            await telegram_app.process_update(update)
            
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            logger.error(f"Ошибка в webhook: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    
    return jsonify({"status": "method not allowed"}), 405

@flask_app.route('/health', methods=['GET'])
def health_check():
    """Эндпоинт для проверки здоровья сервера"""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()}), 200

@flask_app.route('/', methods=['GET'])
def index():
    """Главная страница"""
    return jsonify({
        "name": "Telegram Bot Webhook",
        "status": "running",
        "webhook_url": f"{WEBHOOK_HOST}{WEBHOOK_URL_PATH}"
    }), 200

def run_flask():
    """Запуск Flask сервера в отдельном потоке"""
    flask_app.run(
        host=WEBHOOK_LISTEN,
        port=WEBHOOK_PORT,
        debug=False,
        threaded=True
    )

async def setup_webhook():
    """Настройка webhook в Telegram API"""
    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_URL_PATH}"
    
    try:
        # Устанавливаем webhook
        result = await telegram_app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
        if result:
            logger.info(f"Webhook успешно установлен: {webhook_url}")
            
            # Получаем информацию о webhook
            webhook_info = await telegram_app.bot.get_webhook_info()
            logger.info(f"Webhook info: {webhook_info}")
        else:
            logger.error("Не удалось установить webhook")
            
    except Exception as e:
        logger.error(f"Ошибка при установке webhook: {e}")

async def main():
    """Главная функция запуска"""
    global telegram_app
    
    # Инициализация базы данных
    init_db()
    
    # Настройка приложения Telegram
    telegram_app = await setup_telegram_app()
    
    # Настройка webhook
    await setup_webhook()
    
    # Запуск Flask сервера в отдельном потоке
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info(f"Бот запущен в режиме webhook на порту {WEBHOOK_PORT}")
    logger.info(f"Webhook URL: {WEBHOOK_HOST}{WEBHOOK_URL_PATH}")
    logger.info(f"Health check: {WEBHOOK_HOST}/health")
    
    # Держим основной поток активным
    try:
        while True:
            await asyncio.sleep(3600)  # Спим час, проверяем состояние
            # Периодическая проверка webhook
            webhook_info = await telegram_app.bot.get_webhook_info()
            logger.debug(f"Webhook status: {webhook_info}")
    except KeyboardInterrupt:
        logger.info("Остановка бота...")
        # Удаляем webhook при остановке
        await telegram_app.bot.delete_webhook()
        logger.info("Webhook удалён")

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    # Запуск асинхронного main
    asyncio.run(main())