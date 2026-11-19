"""Excel: разбор загруженных файлов (расписание, список студентов) и создание шаблонов/выгрузок."""
from __future__ import annotations

import io
import re
from datetime import datetime, time
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from db import check_fio, check_group, check_lesson, clean_spaces

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAY_ALIASES = {
    **{d.lower(): i for i, d in enumerate(DAYS)},
    **{a: i for i, a in enumerate(["пн", "вт", "ср", "чт", "пт", "сб", "вс"])},
    **{str(i + 1): i for i in range(7)},
}
WEEK_NAMES = {"all": "каждая", "odd": "нечётная", "even": "чётная"}
TYPE_NAMES = {"lec": "лекция", "pr": "практика", "lab": "лабораторная", "ex": "экзамен", "zach": "зачёт",
              "other": "другое"}

SCHEDULE_COLUMNS = ["Группа", "День", "Неделя", "№ пары", "Начало", "Конец", "Предмет", "Тип", "Аудитория",
                    "Преподаватель"]
ROSTER_COLUMNS = ["ФИО", "Группа"]

# Как могут называться столбцы в чужих таблицах
HEADER_ALIASES = {
    "group": ["группа", "группы", "номер группы", "group"],
    "day": ["день", "день недели", "дни"],
    "weeks": ["неделя", "недели", "чётность", "четность", "числитель/знаменатель"],
    "num": ["№ пары", "№", "пара", "номер пары", "n пары"],
    "start": ["начало", "время начала", "с"],
    "end": ["конец", "окончание", "время окончания", "по"],
    "subject": ["предмет", "дисциплина", "занятие", "название"],
    "type": ["тип", "вид", "тип занятия", "вид занятия"],
    "room": ["аудитория", "ауд", "ауд.", "кабинет", "место"],
    "teacher": ["преподаватель", "препод", "преподаватели", "фио преподавателя"],
    "fio": ["фио", "ф.и.о.", "ф. и. о.", "студент", "фамилия имя отчество"],
}
MAX_ROWS = 20000


class ExcelError(ValueError):
    """Ошибки в файле: список строк для показа админу."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:3]))
        self.errors = errors


def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return clean_spaces(v)


def _cell_time(v: Any) -> str:
    if isinstance(v, datetime):
        v = v.time()
    if isinstance(v, time):
        return f"{v.hour:02d}:{v.minute:02d}"
    if isinstance(v, float) and 0 < v < 1:  # доля суток
        minutes = round(v * 24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    return _cell_text(v)


def _parse_day(v: Any) -> int:
    s = _cell_text(v).lower().rstrip(".")
    if s in DAY_ALIASES:
        return DAY_ALIASES[s]
    if re.fullmatch(r"[а-я]{3,}", s):  # «понед», «вторн» и т. п.
        for name, i in DAY_ALIASES.items():
            if len(name) > 2 and (name.startswith(s) or s.startswith(name)):
                return i
    raise ValueError(f"день «{_cell_text(v)}» не распознан")


def _parse_weeks(v: Any) -> str:
    s = _cell_text(v).lower().replace("ё", "е").rstrip(".")
    if s in ("", "-", "—", "все", "всегда", "каждая", "каждую", "каждой", "еженедельно"):
        return "all"
    if s.startswith(("неч", "числ")) or s in ("н", "1"):
        return "odd"
    if s.startswith(("чет", "знам")) or s in ("ч", "2"):
        return "even"
    raise ValueError(f"неделя «{_cell_text(v)}» не распознана (каждая / нечётная / чётная)")


def _parse_type(v: Any) -> str:
    s = _cell_text(v).lower()
    if not s:
        return "other"
    for prefix, t in (("лек", "lec"), ("лк", "lec"), ("пр", "pr"), ("сем", "pr"), ("лаб", "lab"),
                      ("лр", "lab"), ("экз", "ex"), ("зач", "zach")):
        if s.startswith(prefix):
            return t
    return "other"


def _find_header(ws, wanted: set[str]) -> tuple[int, dict[str, int]]:
    """Ищет строку заголовков в первых 15 строках. Возвращает (номер строки, {поле: индекс столбца})."""
    for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
        found: dict[str, int] = {}
        for c_idx, v in enumerate(row):
            name = _cell_text(v).lower().rstrip(":")
            for field, aliases in HEADER_ALIASES.items():
                if name in aliases and field not in found:
                    found[field] = c_idx
        if wanted <= set(found):
            return r_idx, found
    return 0, {}


def _open(data: bytes):
    try:
        return load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ExcelError(["Не получилось открыть файл. Нужна таблица Excel в формате .xlsx"]) from None


def detect_kind(data: bytes) -> str:
    """'schedule' или 'roster' — по заголовкам первого листа, где они найдены."""
    wb = _open(data)
    try:
        for ws in wb.worksheets:
            if _find_header(ws, {"group", "subject"})[0]:
                return "schedule"
            if _find_header(ws, {"fio", "group"})[0]:
                return "roster"
    finally:
        wb.close()
    raise ExcelError([
        "Не нашёл в таблице нужных столбцов. Для расписания нужны «Группа», «День», «Предмет» "
        "и время или «№ пары»; для списка студентов — «ФИО» и «Группа». Возьмите шаблон из админ-панели."
    ])


def parse_schedule(data: bytes) -> dict[str, list[dict]]:
    wb = _open(data)
    try:
        ws, header_row, cols = None, 0, {}
        for sheet in wb.worksheets:
            header_row, cols = _find_header(sheet, {"group", "subject"})
            if header_row:
                ws = sheet
                break
        if ws is None:
            raise ExcelError(["Не нашёл столбцы «Группа» и «Предмет»"])
        if "day" not in cols:
            raise ExcelError(["Не нашёл столбец «День»"])
        if "num" not in cols and not {"start", "end"} <= set(cols):
            raise ExcelError(["Нужен столбец «№ пары» или столбцы «Начало» и «Конец»"])

        by_group: dict[str, list[dict]] = {}
        errors: list[str] = []
        get = lambda row, f: row[cols[f]] if f in cols and cols[f] < len(row) else None  # noqa: E731
        for r_idx, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
            if r_idx > MAX_ROWS:
                errors.append(f"Слишком много строк (больше {MAX_ROWS})")
                break
            if not any(_cell_text(v) for v in row):
                continue
            try:
                lesson = check_lesson({
                    "group": _cell_text(get(row, "group")),
                    "day": _parse_day(get(row, "day")),
                    "weeks": _parse_weeks(get(row, "weeks")),
                    "num": _cell_text(get(row, "num")) or None,
                    "start": _cell_time(get(row, "start")),
                    "end": _cell_time(get(row, "end")),
                    "subject": _cell_text(get(row, "subject")),
                    "type": _parse_type(get(row, "type")),
                    "room": _cell_text(get(row, "room")),
                    "teacher": _cell_text(get(row, "teacher")),
                })
            except ValueError as e:
                errors.append(f"Строка {r_idx}: {e}")
                continue
            by_group.setdefault(lesson["group"], []).append(lesson)
        if errors:
            raise ExcelError(errors)
        if not by_group:
            raise ExcelError(["В таблице нет ни одной пары"])
        return by_group
    finally:
        wb.close()


def parse_roster(data: bytes) -> list[tuple[str, str]]:
    wb = _open(data)
    try:
        ws, header_row, cols = None, 0, {}
        for sheet in wb.worksheets:
            header_row, cols = _find_header(sheet, {"fio", "group"})
            if header_row:
                ws = sheet
                break
        if ws is None:
            raise ExcelError(["Не нашёл столбцы «ФИО» и «Группа»"])
        rows, errors = [], []
        for r_idx, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
            if r_idx > MAX_ROWS:
                errors.append(f"Слишком много строк (больше {MAX_ROWS})")
                break
            fio = _cell_text(row[cols["fio"]] if cols["fio"] < len(row) else None)
            grp = _cell_text(row[cols["group"]] if cols["group"] < len(row) else None)
            if not fio and not grp:
                continue
            try:
                rows.append((check_fio(fio), check_group(grp)))
            except ValueError as e:
                errors.append(f"Строка {r_idx}: {e}")
        if errors:
            raise ExcelError(errors)
        if not rows:
            raise ExcelError(["В таблице нет ни одного студента"])
        return rows
    finally:
        wb.close()


# ---------- шаблоны и выгрузки ----------
HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1F6FEB")


def _sheet_header(ws, columns: list[str], widths: list[int]) -> None:
    ws.append(columns)
    for i, w in enumerate(widths, start=1):
        cell = ws.cell(row=1, column=i)
        cell.font, cell.fill = HEAD_FONT, HEAD_FILL
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[cell.column_letter].width = w
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 22


def _list_validation(ws, values: list[str], col: str) -> None:
    dv = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True)
    dv.error, dv.errorTitle = "Выберите значение из списка", "Неверное значение"
    ws.add_data_validation(dv)
    dv.add(f"{col}2:{col}5000")


def schedule_workbook(lessons: list[dict] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Расписание"
    _sheet_header(ws, SCHEDULE_COLUMNS, [14, 14, 12, 8, 9, 9, 36, 14, 12, 26])
    if lessons is None:
        lessons = [
            {"group": "ИВТ-21-1", "day": 0, "weeks": "all", "num": 1, "start": "08:30", "end": "10:00",
             "subject": "Математический анализ", "type": "lec", "room": "305", "teacher": "Иванов И. И."},
            {"group": "ИВТ-21-1", "day": 0, "weeks": "odd", "num": 2, "start": "10:10", "end": "11:40",
             "subject": "Физика", "type": "lab", "room": "Б-12", "teacher": "Петрова А. С."},
            {"group": "ИВТ-21-1", "day": 0, "weeks": "even", "num": 2, "start": "10:10", "end": "11:40",
             "subject": "История", "type": "pr", "room": "110", "teacher": ""},
        ]
    for l in lessons:
        ws.append([l["group"], DAYS[l["day"]], WEEK_NAMES[l["weeks"]], l["num"] or "", l["start"], l["end"],
                   l["subject"], TYPE_NAMES.get(l["type"], "другое"), l["room"], l["teacher"]])
    _list_validation(ws, DAYS, "B")
    _list_validation(ws, list(WEEK_NAMES.values()), "C")
    _list_validation(ws, list(TYPE_NAMES.values()), "H")

    help_ws = wb.create_sheet("Как заполнять")
    help_ws.column_dimensions["A"].width = 110
    for line in [
        "Одна строка — одна пара. Можно перечислить сразу несколько групп.",
        "При загрузке расписание каждой группы из файла заменяется целиком; другие группы не меняются.",
        "",
        "Группа — как в списке студентов, например ИВТ-21-1.",
        "День — понедельник…воскресенье (можно Пн, Вт…).",
        "Неделя — каждая, нечётная или чётная (можно «числитель» / «знаменатель»). Пусто = каждая.",
        "№ пары — если указан, а «Начало» и «Конец» пустые, время подставится по звонкам:",
        "    1) 08:30–10:00   2) 10:10–11:40   3) 11:50–13:20   4) 13:50–15:20",
        "    5) 15:30–17:00   6) 17:10–18:40   7) 18:50–20:20   8) 20:30–22:00",
        "Начало, Конец — время в виде 08:30.",
        "Тип — лекция, практика, лабораторная, экзамен, зачёт или другое.",
        "Аудитория и Преподаватель — необязательно.",
    ]:
        help_ws.append([line])
    help_ws["A1"].font = Font(bold=True)
    return _save(wb)


def roster_workbook(students: list[dict] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Студенты"
    _sheet_header(ws, ROSTER_COLUMNS, [40, 16])
    if students is None:
        students = [{"fio": "Иванов Иван Иванович", "group": "ИВТ-21-1"},
                    {"fio": "Петрова Анна Сергеевна", "group": "ИВТ-21-1"}]
    for s in students:
        ws.append([s["fio"], s["group"]])
    return _save(wb)


def _save(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
