"""Извлечение сетки расписания из PDF (формат ИМИ МПГУ).

Каждая страница содержит таблицу: день | время | неделя (1/2) | группы...
Объединённые по горизонтали ячейки = поток (несколько групп),
по вертикали = занятие на обе недели (числитель и знаменатель).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pdfplumber

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
SLOTS = ["0900-1030", "1040-1210", "1240-1410", "1420-1550",
         "1600-1730", "1740-1910", "1920-2050"]
SLOT_LABELS = ["09:00–10:30", "10:40–12:10", "12:40–14:10", "14:20–15:50",
               "16:00–17:30", "17:40–19:10", "19:20–20:50"]

_DAY_SIG = {d: Counter(d.lower()) for d in DAYS}
_SLOT_SIG = {i: Counter(s.replace("-", "")) for i, s in enumerate(SLOTS)}


def detect_day(text: str) -> int | None:
    sig = Counter(c for c in text.lower() if c.isalpha())
    for i, d in enumerate(DAYS):
        if sig == _DAY_SIG[d]:
            return i
    return None


def detect_slot(text: str) -> int | None:
    sig = Counter(c for c in text if c.isdigit())
    for i, s in _SLOT_SIG.items():
        if sig == s:
            return i
    return None


@dataclass
class Cell:
    """Ячейка с занятием, уже разложенная по сетке."""
    day: int
    slot: int
    weeks: tuple[int, ...]       # (1,), (2,) или (1, 2)
    groups: list[str]
    text: str
    source: str = ""


@dataclass
class Section:
    direction: str = ""
    profiles: list[str] = field(default_factory=list)
    groups: list[tuple[float, float, str]] = field(default_factory=list)  # x0, x1, имя
    group_profile: dict = field(default_factory=dict)
    group_direction: dict = field(default_factory=dict)


def _cell_text(page, bbox) -> str:
    x0, y0, x1, y1 = bbox
    chars = [o for o in page.chars
             if x0 <= (o["x0"] + o["x1"]) / 2 <= x1 and y0 <= (o["top"] + o["bottom"]) / 2 <= y1]
    if not chars:
        return ""
    return pdfplumber.utils.extract_text(chars).strip()


def _near(a, b, tol=2.0):
    return abs(a - b) <= tol


def parse_pdf(path: str) -> tuple[list[Cell], list[Section], dict]:
    """Возвращает ячейки занятий, секции (направления с группами) и метаданные файла."""
    pdf = pdfplumber.open(path)
    cells: list[Cell] = []
    sections: list[Section] = []
    meta = {"file": path.split("/")[-1], "header": ""}
    sec: Section | None = None
    day = slot = None
    first_text = pdf.pages[0].extract_text() or ""
    meta["header"] = first_text.split("Направление")[0].strip()

    for page in pdf.pages:
        for table in page.find_tables():
            raw = [(tuple(c), _cell_text(page, c)) for c in table.cells]
            raw.sort(key=lambda t: (round(t[0][1], 1), t[0][0]))
            if not raw:
                continue
            tx0 = min(c[0] for c, _ in raw)
            # --- заголовок секции
            header_rows = [r for r in raw if _near(r[0][0], tx0) and r[1] in ("Направление", "Профиль", "Группа")]
            if header_rows:
                sec = Section()
                sections.append(sec)
                label_x1 = header_rows[0][0][2]
                rows = {}
                for name in ("Направление", "Профиль", "Группа"):
                    hr = [r for r in header_rows if r[1] == name]
                    if not hr:
                        continue
                    y0, y1 = hr[0][0][1], hr[0][0][3]
                    rows[name] = [(c[0], c[2], t.replace("\n", " ")) for c, t in raw
                                  if _near(c[1], y0) and _near(c[3], y1) and c[0] >= label_x1 - 1]
                sec.groups = sorted(rows.get("Группа", []))
                for x0, x1, g in sec.groups:
                    for key, store in (("Профиль", sec.group_profile), ("Направление", sec.group_direction)):
                        for a0, a1, v in rows.get(key, []):
                            if a0 - 1 <= x0 and x1 <= a1 + 1:
                                store[g] = v
                header_bottom = max(r[0][3] for r in header_rows)
                raw = [r for r in raw if r[0][1] >= header_bottom - 1]
            if sec is None:
                continue
            # --- колонки: день | время | неделя
            xs = sorted({round(c[0], 1) for c, _ in raw})
            if len(xs) < 4:
                continue
            x_day, x_time, x_week = xs[0], xs[1], xs[2]
            week_cells = []
            day_cells, time_cells, lesson_cells = [], [], []
            for c, t in raw:
                x0 = round(c[0], 1)
                if _near(x0, x_day):
                    day_cells.append((c, t))
                elif _near(x0, x_time):
                    time_cells.append((c, t))
                elif _near(x0, x_week):
                    week_cells.append((c, t))
                else:
                    lesson_cells.append((c, t))
            # для каждой строки недели определяем (день, пара, неделя)
            units = []  # (y0, y1, day, slot, week)
            table_top = min(c[1] for c, _ in raw)
            seen_day, seen_time = set(), set()
            for c, t in sorted(week_cells, key=lambda r: r[0][1]):
                ymid = (c[1] + c[3]) / 2
                dc = [d for d in day_cells if d[0][1] - 1 <= ymid <= d[0][3] + 1]
                if dc and dc[0][0] not in seen_day:
                    seen_day.add(dc[0][0])
                    d = detect_day(dc[0][1])
                    continuation = _near(dc[0][0][1], table_top) and not header_rows
                    if d is not None:
                        if d != day:
                            slot = None
                        day = d
                    elif not continuation:
                        # подпись дня обрезана переносом страницы — значит, следующий день
                        day = 0 if day is None else day + 1
                        slot = None
                tc = [d for d in time_cells if d[0][1] - 1 <= ymid <= d[0][3] + 1]
                if tc and tc[0][0] not in seen_time:
                    seen_time.add(tc[0][0])
                    s_ = detect_slot(tc[0][1])
                    continuation = _near(tc[0][0][1], table_top) and not header_rows
                    if s_ is not None:
                        slot = s_
                    elif not continuation:
                        slot = 0 if slot is None else slot + 1
                week = 1 if t.strip().startswith("1") else 2
                if tc:
                    sibling = sorted((w for w in week_cells if w[0][1] >= tc[0][0][1] - 1
                                      and w[0][3] <= tc[0][0][3] + 1), key=lambda w: w[0][1])
                    if len(sibling) == 2 and (c, t) in sibling:
                        week = sibling.index((c, t)) + 1
                if t.strip() in ("1", "2"):
                    week = int(t.strip())
                units.append((c[1], c[3], day, slot, week))
            for c, t in lesson_cells:
                if not t.strip():
                    continue
                cov = [u for u in units if c[1] - 1 <= (u[0] + u[1]) / 2 <= c[3] + 1]
                if not cov:
                    continue
                groups = [g for gx0, gx1, g in sec.groups if c[0] - 2 <= (gx0 + gx1) / 2 <= c[2] + 2]
                by_slot: dict = {}
                for u in cov:
                    by_slot.setdefault((u[2], u[3]), set()).add(u[4])
                for (d, s), weeks in by_slot.items():
                    if d is None or s is None:
                        continue
                    cells.append(Cell(d, s, tuple(sorted(weeks)), groups, t, meta["file"]))
    return _merge_fragments(cells), sections, meta


def _merge_fragments(cells: list[Cell]) -> list[Cell]:
    """Ячейка, разрезанная переносом страницы/строки: «ст. пр. Харламов П.В., ауд. 207»
    без названия дисциплины приклеиваем к предыдущей ячейке тех же групп без преподавателя."""
    from .lesson_text import TEACHER_RE, is_noise, is_teacher_only
    out: list[Cell] = []
    for c in cells:
        if is_noise(c.text):
            continue
        if is_teacher_only(c.text):
            for prev in reversed(out):
                if prev.groups == c.groups and not TEACHER_RE.search(prev.text.split("\n")[-1]):
                    prev.text += "\n" + c.text
                    break
            continue
        out.append(c)
    return out
