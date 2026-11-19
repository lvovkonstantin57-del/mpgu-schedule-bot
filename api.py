"""HTTP API для мини-приложения. Каждый запрос подписан Telegram (initData) — так сервер знает, кто его прислал."""
from __future__ import annotations

import json
import logging
import time
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import excel_io
from db import BELLS, DB, check_group

INIT_DATA_MAX_AGE = 24 * 3600  # подпись Telegram действует сутки, потом приложение нужно открыть заново
MAX_UPLOAD = 10 * 1024 * 1024


def _error(status: int, message: str) -> web.HTTPException:
    cls = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
           404: web.HTTPNotFound, 413: web.HTTPRequestEntityTooLarge}.get(status, web.HTTPBadRequest)
    kwargs = {"max_size": MAX_UPLOAD, "actual_size": 0} if status == 413 else {}
    return cls(text=json.dumps({"error": message}, ensure_ascii=False), content_type="application/json", **kwargs)


def make_app(bot: Bot, db: DB, token: str, allowed_origins: set[str], static_admins: set[int]) -> web.Application:
    routes = web.RouteTableDef()

    def is_admin(tg_id: int) -> bool:
        return tg_id in static_admins or tg_id in db.admin_ids()

    def auth(request: web.Request) -> dict:
        header = request.headers.get("Authorization", "")
        if not header.startswith("tma "):
            raise _error(401, "Откройте приложение из Telegram")
        try:
            data = safe_parse_webapp_init_data(token, header[4:])
        except ValueError:
            raise _error(401, "Подпись Telegram не прошла проверку. Откройте приложение заново") from None
        if data.user is None:
            raise _error(401, "Telegram не передал пользователя")
        if time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE:
            raise _error(401, "Сессия устарела. Закройте и снова откройте приложение")
        uid = data.user.id
        return {"id": uid, "first_name": data.user.first_name, "username": data.user.username,
                "student": db.student_by_tg(uid), "is_admin": is_admin(uid)}

    def admin(request: web.Request) -> dict:
        user = auth(request)
        if not user["is_admin"]:
            raise _error(403, "Только для администратора")
        return user

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise _error(400, "Неверный запрос") from None
        if not isinstance(data, dict):
            raise _error(400, "Неверный запрос")
        return data

    def guard(fn: Callable[[], object]):
        """Ошибки проверки данных (ValueError) превращаются в понятный ответ 400."""
        try:
            return fn()
        except ValueError as e:
            msg = str(e)
            raise _error(400, msg[:1].upper() + msg[1:]) from None

    # ---------- общее ----------
    @routes.get("/health")
    async def health(_: web.Request):
        return web.Response(text="ok")

    @routes.get("/api/me")
    async def me(request: web.Request):
        user = auth(request)
        s = user["student"]
        return web.json_response({
            "user": {"id": user["id"], "first_name": user["first_name"]},
            "student": {"fio": s["fio"], "group": s["group"]} if s else None,
            "is_admin": user["is_admin"],
            "settings": db.settings(),
            "bells": BELLS,
            "groups": db.groups() if user["is_admin"] else [],
        })

    @routes.get("/api/schedule")
    async def schedule(request: web.Request):
        user = auth(request)
        if user["is_admin"] and request.query.get("group"):
            grp = request.query["group"]
        elif user["student"]:
            grp = user["student"]["group"]
        else:
            raise _error(403, "Сначала зарегистрируйтесь в боте")
        return web.json_response({"group": grp, "lessons": db.lessons(grp)})

    # ---------- админ: студенты ----------
    @routes.get("/api/admin/students")
    async def students(request: web.Request):
        admin(request)
        return web.json_response({"students": db.students(), "groups": db.groups()})

    @routes.post("/api/admin/students")
    async def add_student(request: web.Request):
        admin(request)
        d = await body(request)
        sid, new = guard(lambda: db.add_student(d.get("fio", ""), d.get("group", "")))
        if not new:
            raise _error(400, "Такой студент в этой группе уже есть")
        return web.json_response({"student": db.student(sid)})

    @routes.put("/api/admin/students/{sid:\\d+}")
    async def edit_student(request: web.Request):
        admin(request)
        sid = int(request.match_info["sid"])
        if not db.student(sid):
            raise _error(404, "Студент не найден")
        d = await body(request)
        guard(lambda: db.update_student(sid, d.get("fio", ""), d.get("group", "")))
        return web.json_response({"student": db.student(sid)})

    @routes.delete("/api/admin/students/{sid:\\d+}")
    async def delete_student(request: web.Request):
        admin(request)
        db.delete_student(int(request.match_info["sid"]))
        return web.json_response({"ok": True})

    @routes.post("/api/admin/students/{sid:\\d+}/unlink")
    async def unlink_student(request: web.Request):
        admin(request)
        db.unlink_student(int(request.match_info["sid"]))
        return web.json_response({"student": db.student(int(request.match_info["sid"]))})

    # ---------- админ: расписание ----------
    @routes.get("/api/admin/lessons")
    async def lessons(request: web.Request):
        admin(request)
        grp = guard(lambda: check_group(request.query.get("group", "")))
        return web.json_response({"group": grp, "lessons": db.lessons(grp), "groups": db.groups()})

    @routes.post("/api/admin/lessons")
    async def add_lesson(request: web.Request):
        admin(request)
        d = await body(request)
        lid = guard(lambda: db.add_lesson(d))
        return web.json_response({"lesson": db.lesson(lid)})

    @routes.put("/api/admin/lessons/{lid:\\d+}")
    async def edit_lesson(request: web.Request):
        admin(request)
        lid = int(request.match_info["lid"])
        if not db.lesson(lid):
            raise _error(404, "Пара не найдена")
        d = await body(request)
        guard(lambda: db.update_lesson(lid, d))
        return web.json_response({"lesson": db.lesson(lid)})

    @routes.delete("/api/admin/lessons/{lid:\\d+}")
    async def delete_lesson(request: web.Request):
        admin(request)
        db.delete_lesson(int(request.match_info["lid"]))
        return web.json_response({"ok": True})

    # ---------- админ: загрузка Excel ----------
    @routes.post("/api/admin/import")
    async def import_file(request: web.Request):
        admin(request)
        try:
            reader = await request.multipart()
            part = await reader.next()
            while part is not None and part.name != "file":
                part = await reader.next()
            data = b""
            while part is not None:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_UPLOAD:
                    raise _error(413, "Файл слишком большой (больше 10 МБ)")
        except web.HTTPException:
            raise
        except Exception:
            raise _error(400, "Файл не получен") from None
        if not data:
            raise _error(400, "Файл не получен")
        try:
            return web.json_response(import_excel(db, data))
        except excel_io.ExcelError as e:
            raise _error(400, "\n".join(e.errors[:15]) + (f"\n…и ещё {len(e.errors) - 15}" if len(e.errors) > 15 else "")) from None

    # ---------- админ: файлы в чат ----------
    @routes.post("/api/admin/send")
    async def send_file(request: web.Request):
        user = admin(request)
        what = (await body(request)).get("what")
        files = {
            "schedule_template": lambda: (excel_io.schedule_workbook(), "Шаблон расписания.xlsx",
                                          "Шаблон расписания. Заполните и загрузите в админ-панели или пришлите этот файл боту."),
            "schedule_export": lambda: (excel_io.schedule_workbook(db.lessons()), "Расписание.xlsx",
                                        "Текущее расписание всех групп. Можно поправить и загрузить обратно."),
            "roster_template": lambda: (excel_io.roster_workbook(), "Шаблон списка студентов.xlsx",
                                        "Шаблон списка студентов. Заполните и загрузите в админ-панели или пришлите боту."),
            "roster_export": lambda: (excel_io.roster_workbook(db.students()), "Студенты.xlsx",
                                      "Текущий список студентов."),
        }
        if what not in files:
            raise _error(400, "Неизвестный файл")
        content, name, caption = files[what]()
        await bot.send_document(user["id"], BufferedInputFile(content, filename=name), caption=caption)
        return web.json_response({"ok": True})

    # ---------- админ: семестр ----------
    @routes.put("/api/admin/settings")
    async def save_settings(request: web.Request):
        admin(request)
        d = await body(request)
        guard(lambda: db.save_settings(d.get("semester_start") or None, bool(d.get("first_week_odd", True))))
        return web.json_response({"settings": db.settings()})

    @web.middleware
    async def cors(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        origin = request.headers.get("Origin", "")
        if request.method == "OPTIONS":
            resp: web.StreamResponse = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = web.Response(status=e.status, text=e.text, content_type=e.content_type or "text/plain")
            except Exception:
                logging.exception("Ошибка API %s %s", request.method, request.path)
                resp = web.json_response({"error": "Ошибка на сервере. Попробуйте ещё раз"}, status=500)
        if origin in allowed_origins:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            resp.headers["Access-Control-Max-Age"] = "86400"
            resp.headers["Vary"] = "Origin"
        return resp

    app = web.Application(client_max_size=MAX_UPLOAD, middlewares=[cors])
    app.add_routes(routes)
    return app


def import_excel(db: DB, data: bytes) -> dict:
    """Общий импорт для админ-панели и файлов, присланных боту. Ошибки — excel_io.ExcelError."""
    kind = excel_io.detect_kind(data)
    if kind == "schedule":
        by_group = excel_io.parse_schedule(data)
        db.replace_schedule(by_group)
        return {"kind": "schedule", "groups": {g: len(items) for g, items in sorted(by_group.items())}}
    rows = excel_io.parse_roster(data)
    added, skipped = db.import_roster(rows)
    return {"kind": "roster", "added": added, "skipped": skipped}


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def describe_import(result: dict) -> str:
    if result["kind"] == "schedule":
        lines = [f"• {g}: {n} {plural(n, 'пара', 'пары', 'пар')}" for g, n in result["groups"].items()]
        return "Расписание загружено. Для этих групп старое расписание заменено:\n" + "\n".join(lines)
    text = f"Список студентов загружен: добавлено {result['added']}"
    if result["skipped"]:
        text += f", уже были в списке {result['skipped']}"
    return text + "."
