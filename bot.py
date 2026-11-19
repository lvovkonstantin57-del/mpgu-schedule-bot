from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    User,
    WebAppInfo,
)
from aiohttp import web
from dotenv import load_dotenv

from api import describe_import, import_excel, make_app
from db import DB, clean_spaces, norm_fio
from excel_io import ExcelError

load_dotenv()

HERE = Path(__file__).resolve().parent
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]  # страница мини-приложения (GitHub Pages), https://
API_URL = os.environ["API_URL"].rstrip("/")  # адрес этого сервера, https://
# Прокси для связи с Telegram (необязательно): socks5://логин:пароль@адрес:порт или http://…
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
# Код, которым один раз назначается администратор: /admin КОД
ADMIN_CODE = os.environ.get("ADMIN_CODE", "").strip()
# Telegram ID администраторов через запятую (необязательно, в дополнение к /admin КОД)
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
DB_FILE = os.environ.get("DB_FILE", str(HERE / "data.db"))
OLD_USERS_FILE = HERE / "users.json"  # регистрации из прошлой версии бота

# ФИО: 2–5 слов из букв, допускаются дефис и апостроф
FIO_RE = re.compile(r"^[A-Za-zА-Яа-яЁё'’-]+(?: [A-Za-zА-Яа-яЁё'’-]+){1,4}$")

db = DB(DB_FILE)
dp = Dispatcher()


class Reg(StatesGroup):
    fio = State()


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS or tg_id in db.admin_ids()


def all_admin_ids() -> set[int]:
    return ADMIN_IDS | db.admin_ids()


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def app_link() -> str:
    """Ссылка на мини-приложение; в ней адрес API, чтобы приложение знало, куда обращаться."""
    parts = urlsplit(WEBAPP_URL)
    query = dict(parse_qsl(parts.query))
    query["api"] = API_URL
    return urlunsplit(parts._replace(query=urlencode(query)))


def open_app_kb(text: str = "Открыть расписание") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=app_link()))]])


def who(user: User) -> str:
    name = " ".join(filter(None, [user.first_name, user.last_name]))
    return f"{name} (@{user.username}, id {user.id})" if user.username else f"{name} (id {user.id})"


_last_notice: dict[tuple[int, str], float] = {}


async def notify_admins(bot: Bot, about: User, kind: str, text: str) -> None:
    """Сообщение администраторам, не чаще раза в час по одному студенту и поводу."""
    key = (about.id, kind)
    if time.time() - _last_notice.get(key, 0) < 3600:
        return
    _last_notice[key] = time.time()
    for admin_id in all_admin_ids():
        if admin_id == about.id:
            continue
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logging.warning("Не удалось уведомить администратора %s: %s", admin_id, e)


# ---------- регистрация ----------
async def finish_registration(message: Message, state: FSMContext, student: dict) -> None:
    await state.clear()
    await message.answer(
        f"Готово! {student['fio']}, группа {student['group']}.\n\nОткройте расписание:",
        reply_markup=open_app_kb(),
    )


async def ask_fio(message: Message, state: FSMContext) -> None:
    await state.set_state(Reg.fio)
    await message.answer(
        "Здравствуйте! Это расписание занятий МПГУ.\n\n"
        "Напишите ваше ФИО полностью — так, как в списке группы. Например: Иванов Иван Иванович."
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    student = db.student_by_tg(uid)
    if student:
        await message.answer(
            f"Здравствуйте, {student['fio']}! Группа {student['group']}.", reply_markup=open_app_kb()
        )
    elif is_admin(uid):
        await message.answer(
            "Вы администратор. В приложении есть вкладка «Админ»: расписание, студенты, семестр.\n"
            "Если вы ещё и студент, отправьте /register.",
            reply_markup=open_app_kb("Открыть приложение"),
        )
    else:
        await ask_fio(message, state)


@dp.message(Command("register"))
async def register(message: Message, state: FSMContext) -> None:
    student = db.student_by_tg(message.from_user.id)
    if student:
        await message.answer(f"Вы уже зарегистрированы: {student['fio']}, группа {student['group']}.",
                             reply_markup=open_app_kb())
        return
    await ask_fio(message, state)


@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(Command("admin"))
async def admin_cmd(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    if is_admin(uid):
        await message.answer(
            "Вы администратор. Откройте приложение и перейдите на вкладку «Админ».\n"
            "Файл Excel с расписанием или списком студентов можно также просто прислать сюда.",
            reply_markup=open_app_kb("Открыть админ-панель"),
        )
        return
    code = (command.args or "").strip()
    if not ADMIN_CODE or not code:
        await message.answer("Эта команда — для администратора.")
        return
    if all_admin_ids():
        await message.answer("Администратор уже назначен.")
        return
    if not hmac.compare_digest(code.encode(), ADMIN_CODE.encode()):
        await message.answer("Неверный код.")
        return
    db.add_admin(uid)
    logging.info("Назначен администратор: %s", who(message.from_user))
    await message.answer(
        "Готово, вы администратор.\n\nВ приложении появилась вкладка «Админ»: там загружается расписание "
        "и список студентов. Начните со списка студентов — без него студенты не смогут зарегистрироваться.",
        reply_markup=open_app_kb("Открыть админ-панель"),
    )


@dp.message(StateFilter(Reg.fio), F.text, ~F.text.startswith("/"))
async def got_fio(message: Message, state: FSMContext) -> None:
    user = message.from_user
    text = clean_spaces(message.text)
    if not FIO_RE.match(text):
        await message.answer("Не похоже на ФИО. Напишите фамилию, имя и отчество через пробел, только буквы.")
        return
    if db.students_count() == 0:
        await message.answer("Список студентов ещё не загружен администратором. Попробуйте позже.")
        await notify_admins(message.bot, user, "empty",
                            f"Студент пытается зарегистрироваться, а список студентов пуст: «{text}», {who(user)}.\n"
                            "Загрузите список в админ-панели.")
        return

    matches = db.students_by_fio(text)
    mine = [s for s in matches if s["tg_id"] == user.id]
    if mine:
        await finish_registration(message, state, mine[0])
        return
    free = [s for s in matches if s["tg_id"] is None]
    if len(free) == 1:
        db.link_student(free[0]["id"], user.id, user.username)
        logging.info("Регистрация: %s → %s, %s", who(user), free[0]["fio"], free[0]["group"])
        await finish_registration(message, state, free[0])
    elif len(free) > 1:
        await state.update_data(fio_norm=norm_fio(text))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s["group"], callback_data=f"reg:{s['id']}")] for s in free
        ])
        await message.answer("В списке несколько студентов с таким ФИО. Выберите вашу группу:", reply_markup=kb)
    elif matches:
        await message.answer(
            "Этот студент уже зарегистрирован с другого аккаунта Telegram. "
            "Если это ошибка, напишите администратору."
        )
        groups = ", ".join(s["group"] for s in matches)
        await notify_admins(message.bot, user, "taken",
                            f"Попытка зарегистрироваться как «{matches[0]['fio']}» ({groups}) с другого аккаунта: "
                            f"{who(user)}.\nЕсли это тот же человек, сбросьте привязку к Telegram в админ-панели.")
    else:
        await message.answer(
            "Не нашёл вас в списке студентов. Проверьте написание ФИО — полностью, как в зачётке — и пришлите "
            "ещё раз. Если всё верно, напишите администратору."
        )
        await notify_admins(message.bot, user, "notfound",
                            f"Студента нет в списке: «{text}», {who(user)}.\n"
                            "Если это ваш студент, добавьте его в админ-панели (вкладка «Студенты»).")


@dp.callback_query(F.data.startswith("reg:"))
async def pick_group(cb: CallbackQuery, state: FSMContext) -> None:
    student = db.student(int(cb.data.split(":", 1)[1]))
    fio_norm = (await state.get_data()).get("fio_norm")
    if (not student or student["tg_id"] not in (None, cb.from_user.id)
            or norm_fio(student["fio"]) != fio_norm):
        await cb.answer("Не получилось. Начните заново: /start", show_alert=True)
        return
    db.link_student(student["id"], cb.from_user.id, cb.from_user.username)
    await state.clear()
    await cb.message.edit_text(f"Группа {student['group']}.")
    await cb.message.answer(
        f"Готово! {student['fio']}, группа {student['group']}.\n\nОткройте расписание:", reply_markup=open_app_kb()
    )
    await cb.answer()


# ---------- файлы Excel от администратора ----------
@dp.message(F.document)
async def got_document(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await fallback(message, state)
        return
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".xlsx"):
        await message.answer("Нужна таблица Excel в формате .xlsx — шаблоны есть в админ-панели.")
        return
    if (doc.file_size or 0) > 10 * 1024 * 1024:
        await message.answer("Файл слишком большой (больше 10 МБ).")
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    try:
        result = import_excel(db, buf.getvalue())
    except ExcelError as e:
        lines = e.errors[:15] + ([f"…и ещё {len(e.errors) - 15}"] if len(e.errors) > 15 else [])
        await message.answer("Файл не загружен, ничего не изменилось:\n" + "\n".join(lines))
        return
    await message.answer(describe_import(result))


@dp.message()
async def fallback(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    if db.student_by_tg(uid) or is_admin(uid):
        await message.answer("Чтобы открыть расписание, нажмите кнопку ниже.", reply_markup=open_app_kb())
    else:
        await start(message, state)


# ---------- переход со старой версии ----------
async def migrate_old_users(bot: Bot) -> None:
    """Регистрации из users.json прошлой версии переносятся в базу как студенты с привязкой к Telegram."""
    if not OLD_USERS_FILE.exists():
        return
    try:
        users = json.loads(OLD_USERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("Не удалось прочитать %s: %s", OLD_USERS_FILE, e)
        return
    for uid, u in users.items():
        u = u if isinstance(u, dict) else {"fio": u}
        if u.get("fio") and u.get("group"):
            try:
                sid, _ = db.add_student(u["fio"], u["group"])
                if db.student(sid)["tg_id"] is None and not db.student_by_tg(int(uid)):
                    db.link_student(sid, int(uid), None)
            except ValueError as e:
                logging.warning("Пропущен %s: %s", uid, e)
        try:  # убрать личную кнопку меню со старой ссылкой — останется общая
            await bot.set_chat_menu_button(chat_id=int(uid), menu_button=MenuButtonDefault())
        except Exception:
            pass
    OLD_USERS_FILE.rename(OLD_USERS_FILE.with_name("users.json.migrated"))
    logging.info("Перенесено регистраций из старой версии: %s", len(users))


# ---------- запуск ----------
def hide_password(url: str) -> str:
    return re.sub(r"//([^:@/]+):[^@/]*@", r"//\1:***@", url)


def fail(message: str) -> NoReturn:
    logging.error(message)
    raise SystemExit(1)


def make_bot() -> Bot:
    if not PROXY_URL:
        logging.info("Подключение к Telegram напрямую, без прокси")
        return Bot(BOT_TOKEN)
    proxy = PROXY_URL
    if proxy.lower().startswith("tg://") or "secret=" in proxy:
        fail(
            "PROXY_URL — это MTProto-прокси (ссылка tg://proxy или t.me/proxy). Он работает только "
            "в приложениях Telegram, а боту нужен SOCKS5- или HTTP-прокси."
        )
    # socks5h:// — то же, что socks5:// (адреса и так разрешаются на стороне прокси).
    # «HTTPS-прокси» у продавцов — это обычный HTTP-прокси, он записывается как http://
    proxy = re.sub(r"^socks5h://", "socks5://", proxy, flags=re.I)
    proxy = re.sub(r"^https://", "http://", proxy, flags=re.I)
    try:
        session = AiohttpSession(proxy=proxy)
    except ValueError as e:
        fail(
            f"Неверный формат PROXY_URL ({e}). Нужно так: socks5://логин:пароль@адрес:порт "
            "или http://логин:пароль@адрес:порт. Если прокси выдан строкой адрес:порт:логин:пароль, "
            "переставьте части: socks5://логин:пароль@адрес:порт"
        )
    logging.info("Подключение к Telegram через прокси %s", hide_password(proxy))
    return Bot(BOT_TOKEN, session=session)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = make_bot()
    try:
        me = await bot.get_me()
    except Exception as e:
        logging.error(
            "Не удалось подключиться к Telegram: %s. Проверьте BOT_TOKEN%s.",
            e, " и PROXY_URL" if PROXY_URL else " или укажите PROXY_URL",
        )
        await bot.session.close()
        raise SystemExit(1)
    logging.info("Бот @%s запущен", me.username)
    await migrate_old_users(bot)

    origins = {origin_of(WEBAPP_URL)} | {o.strip() for o in os.environ.get("EXTRA_ORIGINS", "").split(",") if o.strip()}
    runner = web.AppRunner(make_app(bot, db, BOT_TOKEN, origins, ADMIN_IDS), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    logging.info("API работает на %s:%s, снаружи — %s", WEB_HOST, WEB_PORT, API_URL)
    if not all_admin_ids():
        logging.info("Администратор ещё не назначен: отправьте боту /admin и код из ADMIN_CODE")

    # Кнопка «Расписание» в меню всех чатов; незарегистрированным приложение подскажет, что делать
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Расписание", web_app=WebAppInfo(url=app_link()))
    )
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
