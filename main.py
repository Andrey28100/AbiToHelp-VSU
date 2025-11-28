import asyncio
import aiosqlite
import os
import qrcode
import feedparser
import asyncio
from datetime import datetime, timezone
from PIL import Image, ImageDraw
from io import BytesIO
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, BufferedInputFile, InputMediaAnimation, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODERATOR_TG_ID = os.getenv("MODER_ID")
BOT_USERNAME = "abitohelp_bot"

LAST_PROCESSED_LINK = None

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

try:
    MODERATOR_TG_ID = int(MODERATOR_TG_ID)
except (ValueError, TypeError):
    raise ValueError("MODER_ID должен быть целым числом")

DB_PATH = "bot.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# === Вспомогательные функции ===

async def rss_monitor():
    global LAST_PROCESSED_LINK
    rss_url = "https://www.vsu.ru/ru/news/rss"

    # Загружаем последнюю известную новость при старте
    feed = feedparser.parse(rss_url)
    if feed.entries:
        LAST_PROCESSED_LINK = feed.entries[0].link

    while True:
        try:
            feed = feedparser.parse(rss_url)
            new_news = []

            for entry in feed.entries:
                # Останавливаемся, когда дошли до уже обработанной новости
                if entry.link == LAST_PROCESSED_LINK:
                    break
                new_news.append(entry)

            # Обрабатываем в обратном порядке (от старых к новым), чтобы сохранить хронологию
            new_news.reverse()

            if new_news:
                # Сохраняем самую свежую ссылку
                LAST_PROCESSED_LINK = feed.entries[0].link

                # Получаем ID пользователей, подписанных на новости
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute("""
                        SELECT u.tg_id FROM users u
                        JOIN notification_prefs np ON u.tg_id = np.user_id
                        WHERE np.news_enabled = 1
                    """)
                    recipients = [row[0] for row in await cursor.fetchall()]

                # Рассылаем каждую новость
                for entry in new_news:
                    title = entry.title
                    description = entry.description or ""
                    link = entry.link
                    pub_date = entry.get('published', '')

                    # Форматируем дату (опционально)
                    try:
                        dt = datetime.strptime(pub_date, "%a, %d %b % %H:%M:%S %z")
                        date_str = dt.strftime("%d.%m.%Y")
                    except:
                        date_str = ""

                    text = f"🗞 <b>{title}</b>\n\n{description}\n\n<a href='{link}'>Читать далее</a>"
                    if date_str:
                        text = f"📅 {date_str}\n" + text

                    for tg_id in recipients:
                        try:
                            await bot.send_message(
                                tg_id,
                                text,
                                parse_mode="HTML",
                                disable_web_page_preview=False
                            )
                            await asyncio.sleep(0.05)  # избегаем лимитов Telegram
                        except Exception:
                            pass  # пользователь заблокировал бота

        except Exception as e:
            print(f"[RSS] Ошибка: {e}")

        # Проверяем каждые 10 минут
        await asyncio.sleep(600)


class EventCreation(StatesGroup):
    title = State()
    description = State()
    event_datetime = State()      # ГГГГ-ММ-ДД ЧЧ:ММ
    registration_deadline = State()
    location = State()
    photo = State()

class RoleAssignment(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_role = State()

class Broadcast(StatesGroup):
    waiting_for_message = State()

class UserSearch(StatesGroup):
    waiting_for_query = State()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            role TEXT DEFAULT 'applicant' CHECK(role IN ('applicant', 'student', 'curator', 'moderator')),
            status TEXT DEFAULT 'Не зачислен',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            event_datetime TEXT,
            location TEXT,
            registration_deadline TEXT,
            photo_file_id TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(tg_id)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS notification_prefs (
            user_id INTEGER PRIMARY KEY,
            events_enabled BOOLEAN DEFAULT 1,
            news_enabled BOOLEAN DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(tg_id)
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS media_assets (
            key TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            description TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            user_id INTEGER,
            event_id INTEGER,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'confirmed',
            attended BOOLEAN DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(tg_id),
            FOREIGN KEY(event_id) REFERENCES events(id),
            PRIMARY KEY(user_id, event_id)
        )
        """)

        await db.commit()


async def get_media_asset(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_id FROM media_assets WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


def generate_qr(data: str) -> BytesIO:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


def generate_qr_gif(data: str) -> BytesIO:
    # Генерируем QR-код как изображение
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # Создаём GIF (один кадр)
    gif_bio = BytesIO()
    img.save(gif_bio, format="GIF")
    gif_bio.seek(0)
    return gif_bio


async def show_event_by_index(message: types.Message, events: list, index: int, state: FSMContext):
    event_id, title, reg_deadline, photo_id = events[index]
    text = f"🎉 <b>{title}</b>\n⏳ Регистрация до: {reg_deadline}"

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Зарегистрироваться", callback_data=f"reg_{event_id}")
    
    if len(events) > 1:
        if index > 0:
            builder.button(text="⬅️", callback_data=f"nav_event_{index-1}")
        if index < len(events) - 1:
            builder.button(text="➡️", callback_data=f"nav_event_{index+1}")
    
    builder.button(text="⤴️ К списку", callback_data="events_hub")
    builder.adjust(1, 2 if (index > 0 or index < len(events) - 1) else 1)

    if photo_id:
        media = InputMediaPhoto(media=photo_id, caption=text, parse_mode="HTML")
        await message.edit_media(media=media, reply_markup=builder.as_markup())
    else:
        await message.edit_text(text=text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def has_admin_access(tg_id: int) -> bool:
    if tg_id == MODERATOR_TG_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT role FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return bool(row and row[0] == "moderator")


async def start_event_creation(message: types.Message, state: FSMContext):
    await message.answer("✏️ Введите <b>название</b> мероприятия:", parse_mode="HTML")
    await state.set_state(EventCreation.title)


async def start_role_assignment(message: types.Message, state: FSMContext):
    await message.answer("👤 Введите <b>Telegram ID</b> пользователя:", parse_mode="HTML")
    await state.set_state(RoleAssignment.waiting_for_user_id)


async def start_broadcast(message: types.Message, state: FSMContext):
    await message.answer(
        "📨 Отправьте текст (или текст + фото/видео) для рассылки.\n"
        "Поддерживается HTML-разметка и медиа."
    )
    await state.set_state(Broadcast.waiting_for_message)


async def start_user_search(message: types.Message, state: FSMContext):
    await message.answer(
        "🔍 Введите <b>Telegram ID</b> пользователя или часть имени:\n"
        "Пример: <code>123456789</code> или <code>Иван</code>",
        parse_mode="HTML"
    )
    await state.set_state(UserSearch.waiting_for_query)


# === Клавиатуры ===

def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ℹ️ О боте", callback_data="about_bot")
    builder.button(text="📰 Новости", callback_data="latest_news")
    builder.button(text="👤 Мой профиль", callback_data="my_profile")
    builder.button(text="📅 Мероприятия", callback_data="events_hub")
    builder.button(text="📩 Обратная связь", callback_data="feedback_menu")
    builder.button(text="🔔 Настройки уведомлений", callback_data="notif_settings")
    builder.adjust(2, 1, 1, 1, 1)
    return builder.as_markup()


def events_hub_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Ваши регистрации и QR-коды", callback_data="qr_for_checkin")
    builder.button(text="🔍 Активные мероприятия", callback_data="active_events")
    builder.button(text="⬅️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def moder_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="mod_stats")
    builder.button(text="➕ Создать мероприятие", callback_data="mod_create_event")
    builder.button(text="👤 Назначить роль", callback_data="mod_set_role")
    builder.button(text="📨 Рассылка", callback_data="mod_broadcast")
    builder.button(text="🔍 Найти пользователя", callback_data="mod_search_user")
    builder.adjust(1)
    return builder.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_main")
    return builder.as_markup()


def back_to_moder_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_moder")
    return builder.as_markup()


def event_register_kb(event_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Зарегистрироваться", callback_data=f"reg_{event_id}")
    return builder.as_markup()


def profile_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎫 Мой QR-код", callback_data="my_qr_card")
    builder.button(text="⬅️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def qr_code_checkin_kb( ) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=f"events_hub")
    return builder.as_markup()


def event_registered_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Зарегистрировано", callback_data="noop")
    builder.button(text="⤴️ К списку", callback_data="events_hub")
    return builder.as_markup()


def notif_toggle_kb(events_on: bool, news_on: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    events_status = "✅ Включены" if events_on else "❌ Выключены"
    news_status = "✅ Включены" if news_on else "❌ Выключены"
    
    builder.button(text=f"Мероприятия: {events_status}", callback_data="toggle_events")
    builder.button(text=f"Новости: {news_status}", callback_data="toggle_news")
    builder.button(text="⬅️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


# === Обработчики ===

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("ℹ️ Нет активной операции для отмены.")
        return

    await state.clear()
    await message.answer("❌ Операция отменена. Вы можете начать заново.")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (tg_id, full_name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                full_name = excluded.full_name,
                username = excluded.username
        """, (user.id, user.full_name, user.username))
        await db.execute("INSERT OR IGNORE INTO notification_prefs (user_id) VALUES (?)", (user.id,))
        await db.commit()

    payload = None
    if message.text and len(message.text) > 6:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    if payload and payload.startswith("checkin_"):
        try:
            _, event_id_str, attendee_id_str = payload.split("_")
            event_id = int(event_id_str)
            attendee_id = int(attendee_id_str)
        except (ValueError, IndexError):
            await message.answer("❌ Некорректная ссылка для отметки.")
            return

        # Проверяем: текущий пользователь — модератор?
        if not await has_admin_access(user.id):
            await message.answer("⚠️ Только модератор может ставить отметки о посещении.")
            return

        # Проверяем: зарегистрирован ли attendee на это мероприятие?
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT 1 FROM registrations
                WHERE user_id = ? AND event_id = ?
            """, (attendee_id, event_id))
            if not await cursor.fetchone():
                await message.answer("❌ Пользователь не зарегистрирован на это мероприятие.")
                return

            # Ставим attended = 1
            await db.execute("""
                UPDATE registrations
                SET attended = 1
                WHERE user_id = ? AND event_id = ?
            """, (attendee_id, event_id))
            await db.commit()

            # Получаем имена для отчёта
            cursor = await db.execute("SELECT full_name FROM users WHERE tg_id = ?", (attendee_id,))
            attendee_name = (await cursor.fetchone())[0] if cursor else f"ID{attendee_id}"
            cursor = await db.execute("SELECT title FROM events WHERE id = ?", (event_id,))
            event_title = (await cursor.fetchone())[0] if cursor else f"Мероприятие {event_id}"

        await message.answer(
            f"✅ Отметка о посещении проставлена!\n\n"
            f"👤 {attendee_name}\n"
            f"📅 {event_title}"
        )
        return

    if payload and payload.isdigit():
        target_id = int(payload)
        if target_id == user.id:
            await message.answer("✅ Вы перешли по своей QR-визитке!")
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("SELECT full_name, username, role FROM users WHERE tg_id = ?", (target_id,))
                row = await cursor.fetchone()
                if not row:
                    await message.answer("❌ Пользователь не найден.")
                else:
                    full_name, username, role = row
                    role_name = {"applicant": "Абитуриент", "moderator": "Модератор"}.get(role, role)
                    text = f"👤 <b>Профиль пользователя</b> (ID: {target_id})\n\nИмя: {full_name}\nРоль: {role_name}"

                    cursor = await db.execute("""
                        SELECT e.title, e.event_datetime FROM events e
                        JOIN registrations r ON e.id = r.event_id
                        WHERE r.user_id = ?
                    """, (target_id,))
                    events = await cursor.fetchall()

                    if events:
                        text += "\n\n✅ Зарегистрирован на:\n" + "\n".join(f"• {title} ({dt})" for title, dt in events)
                    else:
                        text += "\n\n📭 Не зарегистрирован ни на одно мероприятие."

                    await message.answer(text, parse_mode="HTML")
    else:
        welcome_file_id = await get_media_asset("welcome")
        caption = (
            "🎓 Добро пожаловать в бот поддержки абитуриентов и студентов ВГУ!\n\n"
            "Здесь вы можете:\n"
            "• Получить персональный QR-код\n"
            "• Зарегистрироваться на мероприятия\n"
            "• Настроить уведомления"
        )
        if welcome_file_id:
            await message.answer_animation(
                animation=welcome_file_id,
                caption=caption,
                reply_markup=main_menu_kb(),
                parse_mode="HTML"
            )
        else:
            # fallback: текст без видео
            await message.answer(
                caption,
                reply_markup=main_menu_kb()
            )

# === Команды модератора (без изменений) ===

@dp.message(Command("add_event"))
async def cmd_add_event_start(message: types.Message, state: FSMContext):
    if not await has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор может добавлять мероприятия.")
        return
    await start_event_creation(message, state)


@dp.message(EventCreation.title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("📝 Введите <b>описание</b> мероприятия:", parse_mode="HTML")
    await state.set_state(EventCreation.description)


@dp.message(EventCreation.description)
async def process_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer(
        "📅 Введите <b>дату и время</b> мероприятия в формате:\n"
        "<code>ГГГГ-ММ-ДД ЧЧ:ММ</code>\n\n"
        "Пример: <code>2025-12-10 15:30</code>",
        parse_mode="HTML"
    )
    await state.set_state(EventCreation.event_datetime)


@dp.message(EventCreation.event_datetime)
async def process_datetime(message: types.Message, state: FSMContext):
    user_input = message.text.strip()
    # Простая валидация формата
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", user_input):
        await message.answer(
            "❌ Неверный формат.\n"
            "Пожалуйста, используйте: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>",
            parse_mode="HTML"
        )
        return

    await state.update_data(event_datetime=user_input)
    await message.answer("📍 Введите <b>место проведения</b>:", parse_mode="HTML")
    await state.set_state(EventCreation.location)


@dp.message(EventCreation.location)
async def process_location(message: types.Message, state: FSMContext):
    await state.update_data(location=message.text.strip())
    await message.answer(
        "📸 (Опционально) Отправьте фото мероприятия или нажмите /skip, чтобы пропустить."
    )
    await state.set_state(EventCreation.photo)


@dp.message(Command("skip"))
@dp.message(EventCreation.photo)
async def process_photo(message: types.Message, state: FSMContext):
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    # Если отправлено не фото — сохраняем как None

    await state.update_data(photo_file_id=photo_file_id)

    # Собираем все данные
    data = await state.get_data()

    # Запрашиваем дедлайн регистрации
    await message.answer(
        "⏰ Введите <b>дату и время окончания регистрации</b> в формате:\n"
        "<code>ГГГГ-ММ-ДД ЧЧ:ММ</code>\n\n"
        "Пример: <code>2025-12-09 18:00</code>",
        parse_mode="HTML"
    )
    await state.set_state(EventCreation.registration_deadline)


@dp.message(EventCreation.registration_deadline)
async def process_reg_deadline(message: types.Message, state: FSMContext):
    user_input = message.text.strip()
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", user_input):
        await message.answer(
            "❌ Неверный формат.\n"
            "Пожалуйста, используйте: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>",
            parse_mode="HTML"
        )
        return

    await state.update_data(registration_deadline=user_input)

    # Сохраняем всё
    data = await state.get_data()
    title = data["title"]
    description = data["description"]
    event_datetime = data["event_datetime"]
    reg_deadline = data["registration_deadline"]
    location = data["location"]
    photo_file_id = data.get("photo_file_id")
    creator_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO events (
                title, description, event_datetime, registration_deadline,
                location, photo_file_id, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (title, description, event_datetime, reg_deadline, location, photo_file_id, creator_id))
        event_id = cursor.lastrowid
        await db.commit()

    # Отправляем пост
    event_tag = f"#event_{event_id}"
    post_text = (
        f"🎉 <b>{title}</b>\n\n"
        f"{description}\n\n"
        f"📅 Мероприятие: {event_datetime}\n"
        f"⏳ Регистрация до: {reg_deadline}\n"
        f"📍 {location}\n\n"
        f"{event_tag}"
    )

    # Отправляем с фото (если есть)
    if photo_file_id:
        sent_msg = await message.answer_photo(
            photo=photo_file_id,
            caption=post_text,
            parse_mode="HTML"
        )
    else:
        sent_msg = await message.answer(post_text, parse_mode="HTML")

    await sent_msg.edit_reply_markup(reply_markup=event_register_kb(event_id))

    # Рассылка
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT u.tg_id FROM users u
            JOIN notification_prefs np ON u.tg_id = np.user_id
            WHERE np.events_enabled = 1
        """)
        users = await cursor.fetchall()

    for (tg_id,) in users:
        try:
            if photo_file_id:
                await bot.send_photo(
                    tg_id,
                    photo=photo_file_id,
                    caption=f"📬 <b>Новое мероприятие!</b>\n\n{post_text}",
                    parse_mode="HTML",
                    reply_markup=event_register_kb(event_id)
                )
            else:
                await bot.send_message(
                    tg_id,
                    f"📬 <b>Новое мероприятие!</b>\n\n{post_text}",
                    parse_mode="HTML",
                    reply_markup=event_register_kb(event_id)
                )
        except Exception:
            pass

    await message.answer(f"✅ Мероприятие создано! ID: {event_id}")
    await state.clear()


@dp.message(Command("moder"))
async def cmd_moder(message: types.Message):
    if not await has_admin_access(message.from_user.id):
        return

    moder_file_id = await get_media_asset("moder")
    if moder_file_id:
        await message.answer_animation(
            animation=moder_file_id,
            caption="🛠 <b>Панель модератора</b>",
            reply_markup=moder_menu_kb(),
            parse_mode="HTML"
        )
    else:
        # fallback: текст без видео
        await message.answer(
            "Панель модератора (Видео не задано)",
            reply_markup=moder_menu_kb()
        )


@dp.message(Command("set_role"))
async def cmd_set_role_start(message: types.Message, state: FSMContext):
    if not await has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор может менять роли.")
        return
    await start_role_assignment(message, state)


@dp.message(RoleAssignment.waiting_for_user_id)
async def process_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID. Попробуйте снова:")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE tg_id = ?", (user_id,))
        if not await cursor.fetchone():
            await message.answer(
                "❌ Пользователь не найден. Убедитесь, что он писал боту /start.\n"
                "Попробуйте снова:"
            )
            return

    await state.update_data(target_user_id=user_id)
    await message.answer(
        "🔤 Введите новую роль:\n"
        "<code>applicant</code>, <code>student</code>, <code>curator</code> или <code>moderator</code>",
        parse_mode="HTML"
    )
    await state.set_state(RoleAssignment.waiting_for_role)


@dp.message(RoleAssignment.waiting_for_role)
async def process_role(message: types.Message, state: FSMContext):
    role = message.text.strip()
    if role not in ("applicant", "student", "curator", "moderator"):
        await message.answer(
            "❌ Недопустимая роль.\n"
            "Используйте: <code>applicant</code>, <code>student</code>, <code>curator</code>, <code>moderator</code>",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    target_id = data["target_user_id"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET role = ? WHERE tg_id = ?", (role, target_id))
        await db.commit()

    role_name = {
        "applicant": "Абитуриент",
        "student": "Студент",
        "curator": "Куратор",
        "moderator": "Модератор"
    }[role]

    await message.answer(f"✅ Роль пользователя {target_id} изменена на: {role_name}")
    await state.clear()


@dp.message(Command("broadcast"))
async def cmd_broadcast_start(message: types.Message, state: FSMContext):
    if not await has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор может делать рассылку.")
        return
    await start_broadcast(message, state)


@dp.message(Broadcast.waiting_for_message)
async def process_broadcast_message(message: types.Message, state: FSMContext):
    # Сохраняем исходное сообщение как шаблон
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT u.tg_id FROM users u
            JOIN notification_prefs np ON u.tg_id = np.user_id
            WHERE np.events_enabled = 1 OR np.news_enabled = 1
        """)
        recipients = await cursor.fetchall()

    success_count = 0
    total = len(recipients)

    for (tg_id,) in recipients:
        try:
            # Пересылаем точно такое же сообщение
            if message.text:
                await bot.send_message(
                    tg_id,
                    message.text,
                    parse_mode="HTML" if "<" in message.text else None
                )
            elif message.photo:
                await bot.send_photo(
                    tg_id,
                    photo=message.photo[-1].file_id,
                    caption=message.caption,
                    parse_mode="HTML" if message.caption and "<" in message.caption else None
                )
            elif message.video:
                await bot.send_video(
                    tg_id,
                    video=message.video.file_id,
                    caption=message.caption,
                    parse_mode="HTML" if message.caption and "<" in message.caption else None
                )
            elif message.animation:
                await bot.send_animation(
                    tg_id,
                    animation=message.animation.file_id,
                    caption=message.caption,
                    parse_mode="HTML" if message.caption and "<" in message.caption else None
                )
            else:
                await bot.send_message(tg_id, message.text or "Сообщение от модератора")
            success_count += 1
        except Exception:
            pass  # пользователь заблокировал или удалил чат

    await message.answer(f"📤 Рассылка отправлена {success_count} из {total} пользователей.")
    await state.clear()


@dp.message(Command("search_user"))
async def cmd_search_user_start(message: types.Message, state: FSMContext):
    if not await has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор может искать пользователей.")
        return
    await start_user_search(message, state)


@dp.message(UserSearch.waiting_for_query)
async def process_user_search(message: types.Message, state: FSMContext):
    query = message.text.strip()

    async with aiosqlite.connect(DB_PATH) as db:
        if query.isdigit():
            cursor = await db.execute(
                "SELECT tg_id, full_name, username, role FROM users WHERE tg_id = ?", (int(query),)
            )
        else:
            cursor = await db.execute(
                "SELECT tg_id, full_name, username, role FROM users WHERE full_name LIKE ?", (f"%{query}%",)
            )
        users = await cursor.fetchall()

    if not users:
        await message.answer("❌ Пользователи не найдены.")
    else:
        text = f"👥 Найдено {len(users)} пользователей:\n\n"
        for tg_id, full_name, username, role in users[:10]:  # максимум 10
            role_name = {"applicant": "Абитуриент", "student": "Студент", "curator": "Куратор", "moderator": "Модератор"}.get(role, role)
            uname = f" (@{username})" if username else ""
            text += f"• {full_name}{uname} | ID: <code>{tg_id}</code> | {role_name}\n"
        await message.answer(text, parse_mode="HTML")

    await state.clear()


@dp.message(Command("set_video"))
async def cmd_set_video(message: types.Message):
    if not await has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор.")
        return

    text = message.text or message.caption
    if not text:
        await message.answer("❗ Укажите ключ в подписи к видео. Пример: <code>/set_video welcome</code>", parse_mode="HTML")
        return
    args = text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Используйте: /set_video <ключ> (например, welcome, moder)")
        return

    key = args[1].strip()

    if message.video:
        file_id = message.video.file_id
    elif message.animation:  # для GIF/MP4 как animation
        file_id = message.animation.file_id
    else:
        await message.answer("Отправьте видео или анимацию вместе с командой (в подписи).")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO media_assets (key, file_id, description)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET file_id = excluded.file_id
        """, (key, file_id, f"Видео для {key}"))
        await db.commit()

    await message.answer(f"✅ Видео для '{key}' сохранено!")


class SetStatus(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_status = State()

@dp.message(Command("set_status"))
async def cmd_set_status_start(message: types.Message, state: FSMContext):
    if not await has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор.")
        return
    await message.answer("👤 Введите Telegram ID пользователя:")
    await state.set_state(SetStatus.waiting_for_user_id)

@dp.message(SetStatus.waiting_for_user_id)
async def process_status_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID. Попробуйте снова:")
        return
    await state.update_data(target_user_id=user_id)
    await message.answer("✏️ Введите новый статус (например: «Подал документы», «Зачислен»):")
    await state.set_state(SetStatus.waiting_for_status)

@dp.message(SetStatus.waiting_for_status)
async def process_status_text(message: types.Message, state: FSMContext):
    status = message.text.strip()
    data = await state.get_data()
    target_id = data["target_user_id"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET status = ? WHERE tg_id = ?", (status, target_id))
        await db.commit()

    await message.answer(f"✅ Статус пользователя {target_id} обновлён: {status}")
    await state.clear()


# === Обработчик кнопок — ТОЛЬКО edit_caption! ===

@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery, state: FSMContext):
    user = callback.from_user
    data = callback.data

    if data.startswith("reg_"):
        try:
            event_id = int(data.split("_", 1)[1])
        except ValueError:
            await callback.answer("❌ Некорректный ID мероприятия.", show_alert=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT title FROM events WHERE id = ?", (event_id,))
            event = await cursor.fetchone()
            if not event:
                await callback.answer("❌ Мероприятие не найдено.", show_alert=True)
                return

            cursor = await db.execute(
                "SELECT 1 FROM registrations WHERE user_id = ? AND event_id = ?",
                (user.id, event_id)
            )
            if await cursor.fetchone():
                await callback.answer("✅ Вы уже зарегистрированы!", show_alert=True)
                return

            await db.execute(
                "INSERT INTO registrations (user_id, event_id) VALUES (?, ?)",
                (user.id, event_id)
            )
            await db.commit()

        await callback.message.edit_reply_markup(reply_markup=event_registered_kb())
        await callback.answer("✅ Регистрация подтверждена! Вы можете найти QR-код для входа на мероприятие в своих регистрациях.", show_alert=True)
        return

    if data == "noop":
        await callback.answer()
        return

    if data == "about_bot":
        about_video_id = await get_media_asset("about")
        text = "ℹ️ <b>Бот абитуриента и студента ВГУ</b>\n\n• Помогает ориентироваться в университете и регистрироваться на мероприятия. \n• Бот центра адаптации абитуриентов Воронежского государственного университета"

        media = InputMediaAnimation(
            media=about_video_id,
            caption=text,
            parse_mode="HTML"
        )

        if about_video_id:
            await callback.message.edit_media(
                media=media,
                reply_markup=back_kb(),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_caption(
                text,
                reply_markup=back_kb(),
                parse_mode="HTML"
            )
        await callback.answer()
        return

    if data == "my_profile":
        profile_video_id = await get_media_asset("profile")
        
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT full_name, username, role, status FROM users WHERE tg_id = ?",
                (user.id,)
            )
            row = await cursor.fetchone()
            if not row:
                text = "❌ Профиль не найден. Напишите /start."
            else:
                full_name, username, role, status = row
                role_name = {
                    "applicant": "Абитуриент",
                    "student": "Студент",
                    "curator": "Куратор",
                    "moderator": "Модератор"
                }.get(role, role)

                # Метрики по мероприятиям
                cursor = await db.execute("""
                    SELECT 
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE attended = 1) AS visited
                    FROM registrations r
                    JOIN events e ON r.event_id = e.id
                    WHERE r.user_id = ?
                """, (user.id,))
                total, visited = await cursor.fetchone()

                # Формируем текст профиля
                text = f"👤 <b>{full_name}</b>\n\n"
                text += f"🆔 ID: <code>{user.id}</code>\n"
                text += f"🎭 Роль: {role_name}\n"
                if status:
                    text += f"🔖 Статус: {status}\n"
                text += f"📊 Мероприятия: {visited} из {total} посещено\n"

                # Дополнительно: если пользователь — абитуриент, даем совет
                if role == "applicant":
                    text += "\n💡 <i>Подайте документы заранее и посещайте дни открытых дверей!</i>"

        # Отправляем
        if profile_video_id:
            media = InputMediaAnimation(
                media=profile_video_id,
                caption=text,
                parse_mode="HTML"
            )
            await callback.message.edit_media(
                media=media,
                reply_markup=profile_kb(),
                parse_mode="HTML"
            )
        else:
            # ВАЖНО: используем edit_text, а не edit_caption, чтобы избежать ошибок!
            await callback.message.edit_text(
                text=text,
                reply_markup=profile_kb(),
                parse_mode="HTML"
            )
        await callback.answer()
        return

    if data == "my_qr_card":
        # QR — отдельное сообщение (не редактируем текущее)
        deeplink_url = f"https://t.me/{BOT_USERNAME}?start={user.id}"
        qr_gif = generate_qr(deeplink_url)
        gif_file = BufferedInputFile(qr_gif.getvalue(), filename="qr_vizitka.gif")
        caption = (
            "🎫 <b>Ваш персональный QR-код</b>\n\n"
            "При сканировании другие увидят ваш профиль и список мероприятий, на которые вы записаны.\n\n"
            f"🔗 <code>{deeplink_url}</code>"
        )
        media = InputMediaPhoto(
            media=gif_file,
            caption=caption,
            parse_mode="HTML"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="my_profile")

        await callback.message.edit_media(media=media, reply_markup=back_kb(), parse_mode="HTML")
        await callback.answer()
        return

    if data == "notif_settings":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT events_enabled, news_enabled FROM notification_prefs WHERE user_id = ?",
                (user.id,)
            )
            row = await cursor.fetchone()
        
        # Если настроек нет (маловероятно, но на всякий случай)
        if row:
            events_on, news_on = bool(row[0]), bool(row[1])
        else:
            events_on, news_on = True, True

        text = "🔔 <b>Настройки уведомлений</b>"
        notif_video_id = await get_media_asset("notifications")

        if notif_video_id:
            media = InputMediaAnimation(
                media=notif_video_id,
                caption=text,
                parse_mode="HTML"
            )
            await callback.message.edit_media(
                media=media,
                reply_markup=notif_toggle_kb(events_on, news_on),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                text=text,
                reply_markup=notif_toggle_kb(events_on, news_on),
                parse_mode="HTML"
            )
        await callback.answer()
        return

    if data == "toggle_events":
        # Переключаем events_enabled
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE notification_prefs SET events_enabled = 1 - events_enabled WHERE user_id = ?", (user.id,))
            await db.commit()

        # Получаем ОБА текущих значения — и для мероприятий, и для новостей
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT events_enabled, news_enabled FROM notification_prefs WHERE user_id = ?",
                (user.id,)
            )
            row = await cursor.fetchone()
            events_on = bool(row[0]) if row else True
            news_on = bool(row[1]) if row else True

        caption = "🔔 <b>Настройки уведомлений</b>"
        await callback.message.edit_caption(
            caption=caption,
            reply_markup=notif_toggle_kb(events_on, news_on),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "toggle_news":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE notification_prefs SET news_enabled = 1 - news_enabled WHERE user_id = ?", (user.id,))
            await db.commit()
        
        # Получаем актуальные значения
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT events_enabled, news_enabled FROM notification_prefs WHERE user_id = ?",
                (user.id,)
            )
            row = await cursor.fetchone()
            events_on = bool(row[0]) if row else True
            news_on = bool(row[1]) if row else True

        caption = "🔔 <b>Настройки уведомлений</b>"
        await callback.message.edit_caption(
            caption=caption,
            reply_markup=notif_toggle_kb(events_on, news_on),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "events_hub":
        text = "📅 <b>Мероприятия</b>\n\nВыберите раздел:"
        notif_video_id = await get_media_asset("hub")
        media = InputMediaAnimation(
                media=notif_video_id,
                caption=text,
                parse_mode="HTML"
            )
        await callback.message.edit_media(
            media=media,
            reply_markup=events_hub_kb(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "qr_for_checkin":
        # Получаем список мероприятий
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT e.id, e.title FROM events e
                JOIN registrations r ON e.id = r.event_id
                WHERE r.user_id = ?
            """, (user.id,))
            events = await cursor.fetchall()

        select_media_file_id = await get_media_asset("select")

        if not events:
            # Показываем ошибку через edit_media (не edit_text!)
            error_caption = "📭 Вы не записаны ни на одно мероприятие."
            fallback_media = InputMediaAnimation(
                media=select_media_file_id,
                caption=error_caption,
                parse_mode="HTML"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="⬅️ Назад", callback_data="events_hub")
            await callback.message.edit_media(media=fallback_media, reply_markup=builder.as_markup())
            await callback.answer()
            return

        # Формируем текст для caption
        event_list = "\n".join(
            f"• {title}" for _, title in events
        )
        caption = f"Выберите мероприятие для генерации QR:\n\n{event_list}"

        select_media = InputMediaAnimation(
            media=select_media_file_id,
            caption=caption,
            parse_mode="HTML"
        )

        # Клавиатура с кнопками на каждое мероприятие
        builder = InlineKeyboardBuilder()
        for event_id, title in events:
            builder.button(
                text=title[:20] + ("..." if len(title) > 20 else ""),
                callback_data=f"gen_qr_checkin_{event_id}"
            )
        builder.button(text="⬅️ Назад", callback_data="events_hub")
        builder.adjust(1)

        await callback.message.edit_media(
            media=select_media,
            reply_markup=builder.as_markup()
        )
        await callback.answer()
        return

    if data.startswith("gen_qr_checkin_"):
        event_id = int(data.split("_")[-1])
        deeplink = f"https://t.me/{BOT_USERNAME}?start=checkin_{event_id}_{user.id}"
        qr_gif = generate_qr(deeplink)

        media = InputMediaPhoto(
                media=BufferedInputFile(qr_gif.getvalue(), filename=f"qr_checkin_{event_id}.gif"),
                caption=f"🎫 QR для отметки на мероприятии\n\nПокажите его модератору при входе.",
                parse_mode="HTML"
            )

        await callback.message.edit_media(
            media=media,
            reply_markup=qr_code_checkin_kb(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "active_events":
        user_id = callback.from_user.id

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT e.id, e.title, e.registration_deadline, e.photo_file_id
                FROM events e
                WHERE datetime(e.registration_deadline) >= datetime('now')
                AND NOT EXISTS (
                    SELECT 1 FROM registrations r
                    WHERE r.user_id = ? AND r.event_id = e.id
                )
                ORDER BY e.registration_deadline
            """, (user_id,))
            events = await cursor.fetchall()

        if not events:
            builder = InlineKeyboardBuilder()
            builder.button(text="⬅️ Назад", callback_data="events_hub")
            active_video = await get_media_asset("actives")
            media = InputMediaAnimation(
                media=active_video,
                caption="📭 Нет мероприятий с открытой регистрацией.",
                parse_mode="HTML"
            )
            await callback.message.edit_media(
                media=media,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            await callback.answer()
            return

        # Сохраняем список мероприятий в state
        await state.update_data(active_events=events)
        
        # Показываем первое (индекс 0)
        await show_event_by_index(callback.message, events, 0, state)
        await callback.answer()
        return

    if data.startswith("nav_event_"):
        try:
            index = int(data.split("_", 2)[2])
        except (ValueError, IndexError):
            await callback.answer("❌ Ошибка навигации.")
            return

        user_data = await state.get_data()
        events = user_data.get("active_events")
        if not events or index < 0 or index >= len(events):
            await callback.answer("❌ Список устарел. Обновите.")
            return

        await show_event_by_index(callback.message, events, index, state)
        await callback.answer()
        return

    if data == "latest_news":
        try:
            import feedparser
            feed = feedparser.parse("https://www.vsu.ru/ru/news/rss")
            if not feed.entries:
                raise Exception("Нет новостей")

            # Берём 3 свежие новости
            entries = feed.entries[:3]
            text = "📰 <b>Последние новости ВГУ</b>\n\n"
            for entry in entries:
                title = entry.title.strip()
                link = entry.link
                # Обрезаем длинные заголовки
                if len(title) > 60:
                    title = title[:57] + "..."
                text += f"• <a href='{link}'>{title}</a>\n"

            text += "\n🔔 Новости приходят автоматически, если у вас включены уведомления."
        except Exception as e:
            text = "📭 Новости временно недоступны.\nПопробуйте позже."

        # Попробуем загрузить видео для фона (опционально)
        news_video_id = await get_media_asset("news")
        if news_video_id:
            media = InputMediaAnimation(
                media=news_video_id,
                caption=text,
                parse_mode="HTML"
            )
            await callback.message.edit_media(media=media, reply_markup=back_kb())
        else:
            await callback.message.edit_text(text=text, reply_markup=back_kb(), parse_mode="HTML")
        await callback.answer()
        return

    # === Модераторка ===

    if data == "mod_stats":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            users = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT COUNT(*) FROM events")
            events = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT COUNT(*) FROM registrations")
            regs = (await cursor.fetchone())[0]
        caption = f"📊 <b>Статистика</b>\n\nПользователей: {users}\nМероприятий: {events}\nРегистраций: {regs}"
        await callback.message.edit_caption(caption=caption, reply_markup=back_to_moder_kb(), parse_mode="HTML")
        await callback.answer()
        return

    if data == "mod_create_event":
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await state.set_state(EventCreation.title)
        await callback.message.answer("✏️ Введите <b>название</b> мероприятия:", parse_mode="HTML")
        await callback.answer()
        return

    if data == "mod_set_role":
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await state.set_state(RoleAssignment.waiting_for_user_id)
        await callback.message.answer("👤 Введите <b>Telegram ID</b> пользователя:", parse_mode="HTML")
        await callback.answer()
        return

    if data == "mod_broadcast":
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await state.set_state(Broadcast.waiting_for_message)
        await callback.message.answer(
            "📨 Отправьте текст (или текст + фото/видео) для рассылки.\n"
            "Поддерживается HTML-разметка и медиа.\nОформите полное, подробное содержание поста."
        )
        await callback.answer()
        return

    if data == "mod_search_user":
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await state.set_state(UserSearch.waiting_for_query)
        await callback.message.answer(
            "🔍 Введите <b>Telegram ID</b> пользователя или часть имени:\n"
            "Пример: <code>123456789</code> или <code>Иван</code>",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "back_to_moder":
        caption = "🛠 <b>Панель модератора</b>"
        await callback.message.edit_caption(
            caption=caption,
            reply_markup=moder_menu_kb(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "back_to_main":
        welcome_file_id = await get_media_asset("welcome")
        caption = (
            "🎓 Добро пожаловать в бот поддержки абитуриентов и студентов ВГУ!\n\n"
            "Здесь вы можете:\n"
            "• Получить персональный QR-код\n"
            "• Зарегистрироваться на мероприятия\n"
            "• Настроить уведомления"
        )
        media = InputMediaAnimation(
            media=welcome_file_id,
            caption=caption,
            parse_mode="HTML"
        )

        await callback.message.edit_media(media=media, reply_markup=main_menu_kb(), parse_mode="HTML")
        await callback.answer()
        return

    await callback.answer()


# === Запуск ===

async def main():
    await init_db()
    me = await bot.get_me()
    print(f"✅ Бот запущен как @{me.username}")
    asyncio.create_task(rss_monitor())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())