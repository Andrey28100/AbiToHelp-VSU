import asyncio
import aiosqlite
import os
import qrcode
from io import BytesIO
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

WELCOME_GIF_PATH = "bot.mp4"

def load_welcome_gif() -> bytes:
    if not os.path.exists(WELCOME_GIF_PATH):
        raise FileNotFoundError(f"Файл {WELCOME_GIF_PATH} не найден!")
    with open(WELCOME_GIF_PATH, "rb") as f:
        return f.read()

WELCOME_GIF_BYTES = None        

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODERATOR_TG_ID = os.getenv("MODER_ID")
BOT_USERNAME = "abitohelp_bot"

try:
    MODERATOR_TG_ID = int(MODERATOR_TG_ID)
except ValueError:
    raise ValueError("MODER_ID должен быть целым числом")

DB_PATH = "bot.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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

        await db.commit()


def generate_qr(data: str) -> BytesIO:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


async def has_admin_access(tg_id: int) -> bool:
    # Доступ по .env (для первоначальной настройки)
    if tg_id == MODERATOR_TG_ID:
        return True
    # Или по роли в БД
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT role FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return bool(row and row[0] == "moderator")


def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 О боте", callback_data="about_bot")
    builder.button(text="👤 Мой профиль", callback_data="my_profile")
    builder.button(text="🎫 Мой QR-код", callback_data="my_qr_card")
    builder.button(text="🔔 Настройки уведомлений", callback_data="notif_settings")
    builder.adjust(1)
    return builder.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_main")
    return builder.as_markup()


def event_register_kb(event_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Зарегистрироваться", callback_data=f"reg_{event_id}")
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

    if payload and payload.isdigit():
        target_id = int(payload)
        if target_id == user.id:
            # Это своя визитка — НЕ показываем GIF
            await message.answer("✅ Вы перешли по своей QR-визитке!", reply_markup=back_kb())
        else:
            # Просмотр чужого профиля — НЕ показываем GIF
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
        # Основной старт — ПОКАЗЫВАЕМ GIF
        gif_file = BufferedInputFile(WELCOME_GIF_BYTES, filename="bot.mp4")
        await message.answer_animation(
            animation=gif_file,
            caption=(
                "🎓 Добро пожаловать в бот поддержки абитуриентов!\n\n"
                "Здесь вы можете:\n"
                "• Получить персональный QR-код\n"
                "• Зарегистрироваться на мероприятия\n"
                "• Настроить уведомления"
            ),
            reply_markup=main_menu_kb()
        )

@dp.message(Command("add_event"))
async def cmd_add_event(message: types.Message):
    if not has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор может добавлять мероприятия.")
        return

    # Извлекаем аргументы после команды
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❗ Неверный формат.\n"
            "Используйте:\n"
            "/add_event Название | Описание | Дата (ГГГГ-ММ-ДД ЧЧ:ММ) | Место"
        )
        return

    payload = args[1].strip()
    parts = payload.split(" | ")
    if len(parts) != 4:
        await message.answer(
            "❗ Неверное количество параметров.\n"
            "Нужно ровно 4, разделённых ` | `:\n"
            "Название | Описание | Дата | Место"
        )
        return

    title, description, event_datetime, location = [p.strip() for p in parts]

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO events (title, description, event_datetime, location, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (title, description, event_datetime, location, message.from_user.id))
        event_id = cursor.lastrowid
        await db.commit()

    event_tag = f"#event_{event_id}"
    post_text = (
        f"🎉 <b>{title}</b>\n\n"
        f"{description}\n\n"
        f"📅 {event_datetime}\n"
        f"📍 {location}\n\n"
        f"{event_tag}"
    )
    sent_msg = await message.answer(post_text, parse_mode="HTML")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET post_message_id = ? WHERE id = ?", (sent_msg.message_id, event_id))
        await db.commit()

    await sent_msg.edit_reply_markup(reply_markup=event_register_kb(event_id))
    await message.answer(f"✅ Мероприятие создано! ID: {event_id}")

    # Рассылка (опционально)
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
        except:
            pass


@dp.message(Command("moder"))
async def cmd_moder(message: types.Message):
    if not has_admin_access(message.from_user.id):
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="mod_stats")
    builder.button(text="➕ Создать мероприятие", callback_data="mod_create_event")
    builder.button(text="👤 Назначить роль", callback_data="mod_set_role")
    builder.button(text="📨 Рассылка", callback_data="mod_broadcast")
    builder.button(text="🔍 Найти пользователя", callback_data="mod_search_user")
    builder.adjust(1)
    await message.answer("🛠 <b>Панель модератора</b>", reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.message(Command("set_role"))
async def cmd_set_role(message: types.Message):
    if not has_admin_access(message.from_user.id):
        await message.answer("⚠️ Только модератор может менять роли.")
        return

    args = message.text.split()
    if len(args) != 3:
        await message.answer("Используйте: /set_role <tg_id> <applicant|student|curator|moderator>")
        return

    try:
        tg_id = int(args[1])
        new_role = args[2]
        if new_role not in ("applicant", "student", "curator", "moderator"):
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ Некорректный ID или роль.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Убедимся, что пользователь существует
        cursor = await db.execute("SELECT 1 FROM users WHERE tg_id = ?", (tg_id,))
        if not await cursor.fetchone():
            await message.answer("❌ Пользователь не найден. Он должен написать боту /start.")
            return

        await db.execute("UPDATE users SET role = ? WHERE tg_id = ?", (new_role, tg_id))
        await db.commit()

    role_name = {"applicant": "Абитуриент", "student": "Студент", "curator": "Куратор", "moderator": "Модератор"}[new_role]
    await message.answer(f"✅ Роль пользователя {tg_id} изменена на: {role_name}")


@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
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
        await callback.answer("✅ Регистрация подтверждена!", show_alert=True)
        return

    if data == "noop":
        await callback.answer()
        return

    if data == "about_bot":
        gif_file = BufferedInputFile(WELCOME_GIF_BYTES, filename="bot.mp4")
        await callback.message.answer_animation(
            animation=gif_file,
            caption="🤖 <b>Бот абитуриента</b>\n\nПомогает ориентироваться в университете и регистрироваться на мероприятия.",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "my_profile":
        user_id = user.id
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT full_name, username, role FROM users WHERE tg_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if not row:
                await callback.message.edit_text("❌ Профиль не найден. Напишите /start.")
                await callback.answer()
                return

            full_name, username, role = row
            role_name = {"applicant": "Абитуриент", "curator": "Куратор", "moderator": "Модератор"}.get(role, role)

            # Получаем мероприятия
            cursor = await db.execute("""
                SELECT e.title, e.event_datetime FROM events e
                JOIN registrations r ON e.id = r.event_id
                WHERE r.user_id = ?
            """, (user_id,))
            events = await cursor.fetchall()

        text = f"👤 <b>Ваш профиль</b>\n\nИмя: {full_name}\nРоль: {role_name}"
        if events:
            text += "\n\n✅ Зарегистрирован на:\n" + "\n".join(f"• {title} ({dt})" for title, dt in events)
        else:
            text += "\n\n📭 Не зарегистрирован ни на одно мероприятие."

        gif_file = BufferedInputFile(WELCOME_GIF_BYTES, filename="bot.mp4")
        caption = text  # тот самый text с профилем
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="back_to_main")
        await callback.message.answer_animation(
            animation=gif_file,
            caption=caption,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if data == "my_qr_card":
        deeplink_url = f"https://t.me/{BOT_USERNAME}?start={user.id}"
        qr_img = generate_qr(deeplink_url)
        photo_file = BufferedInputFile(qr_img.getvalue(), filename="qr_vizitka.png")
        caption = (
            "🎫 <b>Ваш персональный QR-код</b>\n\n"
            "При сканировании другие увидят ваш профиль и список мероприятий, на которые вы записаны.\n\n"
            f"🔗 <code>{deeplink_url}</code>"
        )
        await callback.message.answer_photo(photo=photo_file, caption=caption, parse_mode="HTML")
        await callback.answer()
        return

    if data == "notif_settings":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT events_enabled FROM notification_prefs WHERE user_id = ?", (user.id,))
            row = await cursor.fetchone()
        events_on = bool(row[0]) if row else True

        gif_file = BufferedInputFile(WELCOME_GIF_BYTES, filename="bot.mp4")
        await callback.message.answer_animation(
            animation=gif_file,
            caption="🔔 <b>Настройки уведомлений</b>",
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
        await callback.message.edit_text("🔔 <b>Настройки уведомлений</b>", reply_markup=notif_toggle_kb(events_on), parse_mode="HTML")
        await callback.answer()
        return

    if data == "mod_stats":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            users = await cursor.fetchone()

            cursor = await db.execute("SELECT COUNT(*) FROM events")
            events = await cursor.fetchone()

            cursor = await db.execute("SELECT COUNT(*) FROM registrations")
            regs = await cursor.fetchone()
        text = f"📊 <b>Статистика</b>\n\nПользователей: {users[0]}\nМероприятий: {events[0]}\nРегистраций: {regs[0]}"
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="back_to_moder")
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
        return

    if data == "mod_broadcast_demo":
        await callback.message.edit_text("📨 Рассылка запущена (демо).")
        await callback.answer()
        return

    if data == "mod_create_event":
        await callback.message.edit_text(
            "✏️ <b>Создание мероприятия</b>\n\n"
            "Отправьте данные в формате:\n"
            "<code>Название | Описание | Дата (ГГГГ-ММ-ДД ЧЧ:ММ) | Место</code>\n\n"
            "Пример:\n"
            "<code>День открытых дверей | Приёмная комиссия ответит на вопросы | 2025-12-10 12:00 | Главный корпус</code>",
            parse_mode="HTML"
        )
        # Сохрани состояние: ждём ввод от модератора
        # Для простоты на хакатоне — просто установим флаг в памяти (в реальном проекте — FSM)
        context = dp.get("mod_context", {})
        context[callback.from_user.id] = "awaiting_event_data"
        dp["mod_context"] = context
        await callback.answer()
        return

    if data == "mod_set_role":
        await callback.message.edit_text(
            "👤 <b>Назначение роли</b>\n\n"
            "Отправьте в формате:\n"
            "<code>tg_id роль</code>\n\n"
            "Роли: <code>applicant</code>, <code>student</code>, <code>curator</code>, <code>moderator</code>\n\n"
            "Пример:\n"
            "<code>123456789 curator</code>",
            parse_mode="HTML"
        )
        context = dp.get("mod_context", {})
        context[callback.from_user.id] = "awaiting_role_data"
        dp["mod_context"] = context
        await callback.answer()
        return

    if data == "mod_search_user":
        await callback.message.edit_text(
            "🔍 <b>Поиск пользователя</b>\n\n"
            "Отправьте <b>Telegram ID</b> пользователя:",
            parse_mode="HTML"
        )
        context = dp.get("mod_context", {})
        context[callback.from_user.id] = "awaiting_user_id"
        dp["mod_context"] = context
        await callback.answer()
        return

    if data == "mod_broadcast":
        await callback.message.edit_text(
            "📨 <b>Рассылка</b>\n\n"
            "Отправьте текст сообщения для всех пользователей с включёнными уведомлениями:",
            parse_mode="HTML"
        )
        context = dp.get("mod_context", {})
        context[callback.from_user.id] = "awaiting_broadcast_text"
        dp["mod_context"] = context
        await callback.answer()
        return

    if data == "back_to_moder":
        builder = InlineKeyboardBuilder()
        builder.button(text="📊 Статистика", callback_data="mod_stats")
        builder.button(text="📨 Рассылка (демо)", callback_data="mod_broadcast_demo")
        builder.adjust(1)
        await callback.message.edit_text("🛠 Панель модератора:", reply_markup=builder.as_markup())
        await callback.answer()
        return

    if data == "back_to_main":
        gif_file = BufferedInputFile(WELCOME_GIF_BYTES, filename="bot.mp4")
        await callback.message.answer_animation(
            animation=gif_file,
            caption=(
                "🎓 Добро пожаловать в бот поддержки абитуриентов!\n\n"
                "Здесь вы можете:\n"
                "• Получить персональный QR-код\n"
                "• Зарегистрироваться на мероприятия\n"
                "• Настроить уведомления"
            ),
            reply_markup=main_menu_kb()
        )
        await callback.answer()
        return

    await callback.answer()


async def main():
    global WELCOME_GIF_BYTES
    WELCOME_GIF_BYTES = load_welcome_gif()
    await init_db()
    me = await bot.get_me()
    print(f"✅ Бот запущен как @{me.username}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())