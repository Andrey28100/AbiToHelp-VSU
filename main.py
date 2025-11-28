import asyncio
import aiosqlite
import os
import qrcode
from PIL import Image, ImageDraw
from io import BytesIO
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, BufferedInputFile, InputMediaAnimation
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

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

try:
    MODERATOR_TG_ID = int(MODERATOR_TG_ID)
except (ValueError, TypeError):
    raise ValueError("MODER_ID должен быть целым числом")

DB_PATH = "bot.db"
WELCOME_GIF_BYTES = None
MODER_GIF_BYTES = None

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# === Вспомогательные функции ===

class EventCreation(StatesGroup):
    title = State()
    description = State()
    event_datetime = State()  # формат: ГГГГ-ММ-ДД ЧЧ:ММ
    location = State()

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
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            event_datetime TEXT,
            location TEXT,
            created_by INTEGER,
            post_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(tg_id)
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            user_id INTEGER,
            event_id INTEGER,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'confirmed',
            FOREIGN KEY(user_id) REFERENCES users(tg_id),
            FOREIGN KEY(event_id) REFERENCES events(id),
            PRIMARY KEY(user_id, event_id)
        )""")

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
    builder.button(text="👤 Мой профиль", callback_data="my_profile")
    builder.button(text="🎫 Мой QR-код", callback_data="my_qr_card")
    builder.button(text="🔔 Настройки уведомлений", callback_data="notif_settings")
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
    builder.button(text="✌️ QR для отметки", callback_data="qr_for_checkin")
    builder.button(text="⬅️ Назад", callback_data="back_to_main")
    return builder.as_markup()


def qr_code_checkin_kb( ) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=f"qr_for_checkin")
    return builder.as_markup()


def event_registered_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Зарегистрировано", callback_data="noop")
    return builder.as_markup()


def notif_toggle_kb(events_on: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    status = "✅ Включены" if events_on else "❌ Выключены"
    builder.button(text=f"Мероприятия: {status}", callback_data="toggle_events")
    builder.button(text="⬅️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


# === Обработчики ===

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
                SET status = "attended"
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
            "🎓 Добро пожаловать в бот поддержки абитуриентов!\n\n"
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

    # Получаем все данные
    data = await state.get_data()
    title = data["title"]
    description = data["description"]
    event_datetime = data["event_datetime"]
    location = data["location"]
    creator_id = message.from_user.id

    # Сохраняем в БД
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO events (title, description, event_datetime, location, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (title, description, event_datetime, location, creator_id))
        event_id = cursor.lastrowid
        await db.commit()

    # Отправляем пост
    event_tag = f"#event_{event_id}"
    post_text = (
        f"🎉 <b>{title}</b>\n\n"
        f"{description}\n\n"
        f"📅 {event_datetime}\n"
        f"📍 {location}\n\n"
        f"{event_tag}"
    )
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
            await bot.send_message(
                tg_id,
                f"📬 <b>Новое мероприятие!</b>\n\n{post_text}",
                parse_mode="HTML",
                reply_markup=event_register_kb(event_id)
            )
        except Exception:
            pass  # игнорируем заблокировавших

    await message.answer(f"✅ Мероприятие создано! ID: {event_id}")
    await state.clear()  # выходим из FSM


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


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активной операции для отмены.")
        return

    await state.clear()
    await message.answer("❌ Операция отменена. Вы можете начать заново.")


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
        await callback.answer("✅ Регистрация подтверждена! Вы можете найти QR-код для входа на мероприятие в своём профиле.", show_alert=True)
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
        # Получаем видео, если есть
        profile_video_id = await get_media_asset("profile")
        
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT full_name, username, role FROM users WHERE tg_id = ?",
                (user.id,)
            )
            row = await cursor.fetchone()
            if not row:
                text = "❌ Профиль не найден. Напишите /start."
            else:
                full_name, username, role = row
                role_name = {"applicant": "Абитуриент", "student": "Студент", "curator": "Куратор", "moderator": "Модератор"}.get(role, role)

                cursor = await db.execute("""
                    SELECT e.title, e.event_datetime FROM events e
                    JOIN registrations r ON e.id = r.event_id
                    WHERE r.user_id = ?
                """, (user.id,))
                events = await cursor.fetchall()


                text = f"👤 <b>Ваш профиль</b>\n\nИмя: {full_name}\nРоль: {role_name}"
                if events:
                    text += "\n\n✅ Зарегистрирован на:\n" + "\n".join(f"• {title} ({dt})" for title, dt in events)
                else:
                    text += "\n\n📭 Не зарегистрирован ни на одно мероприятие."

        # Отправляем ВИДЕО + текст, если видео есть, иначе только текст
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
            await callback.message.edit_caption(
                caption=text,
                reply_markup=profile_kb(),
                parse_mode="HTML"
            )
        await callback.answer()
        return

    if data == "my_qr_card":
        # QR — отдельное сообщение (не редактируем текущее)
        deeplink_url = f"https://t.me/{BOT_USERNAME}?start={user.id}"
        qr_gif = generate_qr_gif(deeplink_url)
        gif_file = BufferedInputFile(qr_gif.getvalue(), filename="qr_vizitka.gif")
        caption = (
            "🎫 <b>Ваш персональный QR-код</b>\n\n"
            "При сканировании другие увидят ваш профиль и список мероприятий, на которые вы записаны.\n\n"
            f"🔗 <code>{deeplink_url}</code>"
        )
        media = InputMediaAnimation(
            media=gif_file,
            caption=caption,
            parse_mode="HTML"
        )
        await callback.message.edit_media(media=media, reply_markup=back_kb(), parse_mode="HTML")
        await callback.answer()
        return

    if data == "notif_settings":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT events_enabled FROM notification_prefs WHERE user_id = ?", (user.id,))
            row = await cursor.fetchone()
        events_on = bool(row[0]) if row else True

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
                reply_markup=notif_toggle_kb(events_on),
                parse_mode="HTML"
            )
        else:
            await callback.message.answer(
                text,
                reply_markup=notif_toggle_kb(events_on),
                parse_mode="HTML"
            )
        await callback.answer()
        return

    if data == "toggle_events":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE notification_prefs SET events_enabled = 1 - events_enabled WHERE user_id = ?", (user.id,))
            await db.commit()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT events_enabled FROM notification_prefs WHERE user_id = ?", (user.id,))
            row = await cursor.fetchone()
        events_on = bool(row[0]) if row else True
        caption = "🔔 <b>Настройки уведомлений</b>"
        await callback.message.edit_caption(caption=caption, reply_markup=notif_toggle_kb(events_on), parse_mode="HTML")
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
            await callback.message.edit_media(media=fallback_media, reply_markup=back_kb())
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
        builder.button(text="⬅️ Назад", callback_data="my_profile")
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
        qr_gif = generate_qr_gif(deeplink)

        media = InputMediaAnimation(
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

    # === Модераторка (редактируем caption) ===

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
            "Поддерживается HTML-разметка и медиа."
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
            "🎓 Добро пожаловать в бот поддержки абитуриентов!\n\n"
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())