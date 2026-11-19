"""Хранилище данных бота и админ-панели: один файл SQLite рядом с ботом."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

# Звонки по умолчанию: время подставляется по номеру пары, если начало/конец не указаны
BELLS = [
    ("08:30", "10:00"), ("10:10", "11:40"), ("11:50", "13:20"), ("13:50", "15:20"),
    ("15:30", "17:00"), ("17:10", "18:40"), ("18:50", "20:20"), ("20:30", "22:00"),
]
LESSON_TYPES = {"lec", "pr", "lab", "ex", "zach", "other"}
WEEKS = {"all", "odd", "even"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id            INTEGER PRIMARY KEY,
    fio           TEXT NOT NULL,
    fio_norm      TEXT NOT NULL,
    grp           TEXT NOT NULL,
    tg_id         INTEGER UNIQUE,
    tg_username   TEXT,
    registered_at TEXT
);
CREATE INDEX IF NOT EXISTS students_fio ON students(fio_norm);

CREATE TABLE IF NOT EXISTS lessons (
    id         INTEGER PRIMARY KEY,
    grp        TEXT NOT NULL,
    day        INTEGER NOT NULL,
    weeks      TEXT NOT NULL DEFAULT 'all',
    num        INTEGER,
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'other',
    room       TEXT NOT NULL DEFAULT '',
    teacher    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS lessons_grp ON lessons(grp);

CREATE TABLE IF NOT EXISTS admins (
    tg_id    INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def clean_spaces(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def norm_fio(s: str) -> str:
    """Для сравнения ФИО: регистр, ё/е и лишние пробелы не важны."""
    return clean_spaces(s).lower().replace("ё", "е")


def norm_group(s: Any) -> str:
    g = clean_spaces(s).upper()
    if re.fullmatch(r"\d+\.0", g):  # Excel превращает 101 в 101.0
        g = g[:-2]
    return g


def check_fio(fio: str) -> str:
    fio = clean_spaces(fio)
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё'’-]+(?: [A-Za-zА-Яа-яЁё'’-]+){1,4}", fio):
        raise ValueError(f"«{fio}» не похоже на ФИО")
    return fio


def check_group(grp: Any) -> str:
    g = norm_group(grp)
    if not g:
        raise ValueError("не указана группа")
    if len(g) > 40:
        raise ValueError("слишком длинное название группы")
    return g


def parse_time(v: Any) -> str:
    """'8:30', '08.30', '8-30', '830' → '08:30'."""
    s = clean_spaces(v)
    m = re.fullmatch(r"(\d{1,2})\s*[:.\-]?\s*(\d{2})(?::\d{2})?", s)
    if not m:
        raise ValueError(f"время «{s}» не распознано")
    h, mi = int(m[1]), int(m[2])
    if h > 23 or mi > 59:
        raise ValueError(f"время «{s}» не распознано")
    return f"{h:02d}:{mi:02d}"


def check_lesson(d: dict) -> dict:
    """Проверяет и нормализует пару. Ошибки — ValueError с понятным текстом."""
    grp = check_group(d.get("group"))
    try:
        day = int(d.get("day"))
    except (TypeError, ValueError):
        raise ValueError("не указан день недели") from None
    if not 0 <= day <= 6:
        raise ValueError("день недели должен быть от понедельника до воскресенья")
    weeks = d.get("weeks") or "all"
    if weeks not in WEEKS:
        raise ValueError("неделя: каждая, нечётная или чётная")
    num = d.get("num")
    if str(num or "").strip() in ("", "None"):
        num = None
    else:
        try:
            num = int(float(str(num).strip()))
        except ValueError:
            raise ValueError(f"номер пары «{num}» должен быть числом") from None
    if num is not None and not 1 <= num <= len(BELLS):
        raise ValueError(f"номер пары должен быть от 1 до {len(BELLS)}")
    start, end = clean_spaces(d.get("start")), clean_spaces(d.get("end"))
    if (not start or not end) and num:
        start = start or BELLS[num - 1][0]
        end = end or BELLS[num - 1][1]
    if not start or not end:
        raise ValueError("укажите время начала и конца или номер пары")
    start, end = parse_time(start), parse_time(end)
    if end <= start:
        raise ValueError("конец пары раньше начала")
    subject = clean_spaces(d.get("subject"))
    if not subject:
        raise ValueError("не указан предмет")
    typ = d.get("type") or "other"
    if typ not in LESSON_TYPES:
        typ = "other"
    return {
        "group": grp, "day": day, "weeks": weeks, "num": num, "start": start, "end": end,
        "subject": subject[:200], "type": typ,
        "room": clean_spaces(d.get("room"))[:100], "teacher": clean_spaces(d.get("teacher"))[:150],
    }


class DB:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)

    def tx(self):
        """with db.tx(): … — несколько изменений одной транзакцией."""
        return _Tx(self.con)

    # ---------- студенты ----------
    @staticmethod
    def _student(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        return {"id": r["id"], "fio": r["fio"], "group": r["grp"], "tg_id": r["tg_id"],
                "tg_username": r["tg_username"], "registered_at": r["registered_at"]}

    def students(self) -> list[dict]:
        rows = self.con.execute("SELECT * FROM students ORDER BY grp, fio_norm").fetchall()
        return [self._student(r) for r in rows]

    def student(self, sid: int) -> dict | None:
        return self._student(self.con.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone())

    def student_by_tg(self, tg_id: int) -> dict | None:
        return self._student(self.con.execute("SELECT * FROM students WHERE tg_id=?", (tg_id,)).fetchone())

    def students_by_fio(self, fio: str) -> list[dict]:
        rows = self.con.execute("SELECT * FROM students WHERE fio_norm=? ORDER BY grp", (norm_fio(fio),)).fetchall()
        return [self._student(r) for r in rows]

    def students_count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM students").fetchone()[0]

    def add_student(self, fio: str, grp: str) -> tuple[int, bool]:
        """Возвращает (id, добавлен ли новый). Такой же студент в той же группе не дублируется."""
        fio, grp = check_fio(fio), check_group(grp)
        r = self.con.execute("SELECT id FROM students WHERE fio_norm=? AND grp=?", (norm_fio(fio), grp)).fetchone()
        if r:
            return r["id"], False
        cur = self.con.execute("INSERT INTO students (fio, fio_norm, grp) VALUES (?, ?, ?)", (fio, norm_fio(fio), grp))
        return cur.lastrowid, True

    def update_student(self, sid: int, fio: str, grp: str) -> None:
        fio, grp = check_fio(fio), check_group(grp)
        dup = self.con.execute("SELECT id FROM students WHERE fio_norm=? AND grp=? AND id<>?",
                               (norm_fio(fio), grp, sid)).fetchone()
        if dup:
            raise ValueError("такой студент в этой группе уже есть")
        self.con.execute("UPDATE students SET fio=?, fio_norm=?, grp=? WHERE id=?", (fio, norm_fio(fio), grp, sid))

    def delete_student(self, sid: int) -> None:
        self.con.execute("DELETE FROM students WHERE id=?", (sid,))

    def link_student(self, sid: int, tg_id: int, username: str | None) -> None:
        with self.tx():
            self.con.execute("UPDATE students SET tg_id=NULL, tg_username=NULL, registered_at=NULL WHERE tg_id=?", (tg_id,))
            self.con.execute("UPDATE students SET tg_id=?, tg_username=?, registered_at=? WHERE id=?",
                             (tg_id, username, now_iso(), sid))

    def unlink_student(self, sid: int) -> None:
        self.con.execute("UPDATE students SET tg_id=NULL, tg_username=NULL, registered_at=NULL WHERE id=?", (sid,))

    def import_roster(self, rows: Iterable[tuple[str, str]]) -> tuple[int, int]:
        added = skipped = 0
        with self.tx():
            for fio, grp in rows:
                _, new = self.add_student(fio, grp)
                added += new
                skipped += not new
        return added, skipped

    # ---------- расписание ----------
    @staticmethod
    def _lesson(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "group": r["grp"], "day": r["day"], "weeks": r["weeks"], "num": r["num"],
                "start": r["start_time"], "end": r["end_time"], "subject": r["subject"], "type": r["type"],
                "room": r["room"], "teacher": r["teacher"]}

    def lessons(self, grp: str | None = None) -> list[dict]:
        if grp is None:
            rows = self.con.execute("SELECT * FROM lessons ORDER BY grp, day, start_time").fetchall()
        else:
            rows = self.con.execute("SELECT * FROM lessons WHERE grp=? ORDER BY day, start_time",
                                    (norm_group(grp),)).fetchall()
        return [self._lesson(r) for r in rows]

    def lesson(self, lid: int) -> dict | None:
        r = self.con.execute("SELECT * FROM lessons WHERE id=?", (lid,)).fetchone()
        return self._lesson(r) if r else None

    def _lesson_values(self, d: dict) -> tuple:
        return (d["group"], d["day"], d["weeks"], d["num"], d["start"], d["end"], d["subject"], d["type"],
                d["room"], d["teacher"])

    def add_lesson(self, data: dict) -> int:
        d = check_lesson(data)
        cur = self.con.execute(
            "INSERT INTO lessons (grp, day, weeks, num, start_time, end_time, subject, type, room, teacher)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", self._lesson_values(d))
        return cur.lastrowid

    def update_lesson(self, lid: int, data: dict) -> None:
        d = check_lesson(data)
        self.con.execute(
            "UPDATE lessons SET grp=?, day=?, weeks=?, num=?, start_time=?, end_time=?, subject=?, type=?,"
            " room=?, teacher=? WHERE id=?", self._lesson_values(d) + (lid,))

    def delete_lesson(self, lid: int) -> None:
        self.con.execute("DELETE FROM lessons WHERE id=?", (lid,))

    def replace_schedule(self, by_group: dict[str, list[dict]]) -> None:
        """Заменяет расписание целиком для каждой группы из файла. Остальные группы не трогает."""
        with self.tx():
            for grp, items in by_group.items():
                self.con.execute("DELETE FROM lessons WHERE grp=?", (grp,))
                for item in items:
                    self.con.execute(
                        "INSERT INTO lessons (grp, day, weeks, num, start_time, end_time, subject, type, room, teacher)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)", self._lesson_values(item))

    def groups(self) -> list[str]:
        rows = self.con.execute("SELECT grp FROM students UNION SELECT grp FROM lessons ORDER BY 1").fetchall()
        return [r[0] for r in rows]

    def lessons_count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]

    # ---------- администраторы ----------
    def admin_ids(self) -> set[int]:
        return {r[0] for r in self.con.execute("SELECT tg_id FROM admins")}

    def add_admin(self, tg_id: int) -> None:
        self.con.execute("INSERT OR IGNORE INTO admins (tg_id, added_at) VALUES (?, ?)", (tg_id, now_iso()))

    # ---------- настройки семестра ----------
    def settings(self) -> dict:
        s = {r["key"]: r["value"] for r in self.con.execute("SELECT key, value FROM settings")}
        return {
            "semester_start": s.get("semester_start") or None,
            "first_week_odd": s.get("first_week_odd", "1") == "1",
        }

    def save_settings(self, semester_start: str | None, first_week_odd: bool) -> None:
        if semester_start:
            try:
                datetime.strptime(semester_start, "%Y-%m-%d")
            except ValueError:
                raise ValueError("дата начала семестра не распознана") from None
        with self.tx():
            self.con.execute("INSERT OR REPLACE INTO settings VALUES ('semester_start', ?)", (semester_start or "",))
            self.con.execute("INSERT OR REPLACE INTO settings VALUES ('first_week_odd', ?)",
                             ("1" if first_week_odd else "0",))


class _Tx:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.outer = False

    def __enter__(self):
        self.outer = not self.con.in_transaction
        if self.outer:
            self.con.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.outer:
            self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
