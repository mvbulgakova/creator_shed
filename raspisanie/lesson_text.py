"""Разбор текста ячейки: «Алгебра (ПЗ) / доц. Цыбуля Л.М., ауд. 206»."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TITLE = r"(?:проф\.|доц\.|ст\.\s*пр\.|ст\.\s*преп\.|ассист\.|асс\.|преп\.|зав\.\s*каф\.)"
TEACHER_RE = re.compile(TITLE + r"\s*([А-ЯЁ][а-яё\-]+)\s*([А-ЯЁ]\.\s*(?:[А-ЯЁ]\.?)?)?")
TITLE_RE = re.compile(r"^\s*" + TITLE)
ROOM_RE = re.compile(r"ауд\.?\s*((?:\d+[а-яА-Я]?)(?:\s*[,/]\s*\d+[а-яА-Я]?)*)")
SUBGROUP_RE = re.compile(r"(\d(?:\s*,\s*\d)*)\s*п/гр")
_K = r"(?:ЛК|ПЗ|ПР|лаб|ЛР|СЗ|сем)"
TYPE_RE = re.compile(r"\((" + _K + r"(?:/" + _K + r")?)\b([^)]*)\)\s*(?:(\d(?:\s*,\s*\d)*)\s*п/гр)?\s*([\d.;,\s]*)$")
DATES_LINE_RE = re.compile(r"^\s*\d{1,2}\.\d{2}(?:\s*[;,]\s*\d{1,2}\.\d{2})*\s*$")
LANG_RE = re.compile(r"\b(англ|немец|франц|испан|китай)", re.I)
ATTR_RE = re.compile(r"^(?:[\s,.;]|ауд\.?\s*[\d,\s а-я]*|\d(?:\s*,\s*\d)*\s*п/гр|el\.mpgu\.su|"
                     r"\d{1,2}\.\d{2}|\d{3}(?:/\d{3})*|улица [^,]+, дом \d+|англ\.?|немец\.?|франц\.?|испан\.?|"
                     r"[Фф]ранцузский|немецкий|английский)+$")
ADDRESS_RE = re.compile(r"(улица [^,]+, дом \d+)")

TYPE_NAMES = {"ЛК": "лекция", "ПЗ": "практическое занятие", "ПР": "практикум", "лаб": "лабораторная",
              "СЗ": "семинар", "сем": "семинар"}


@dataclass
class Teacher:
    name: str
    title: str = ""
    subgroups: list[int] = field(default_factory=list)
    language: str = ""


@dataclass
class Lesson:
    subject: str
    kind: str = ""               # ЛК / ПЗ / лаб / "" (практика, физкультура...)
    dates: str = ""              # «12.09, 19.09», «по 09.12», «кроме ...»
    teachers: list[Teacher] = field(default_factory=list)
    rooms: list[str] = field(default_factory=list)
    online: bool = False
    address: str = ""
    elective: str = ""           # «Д/В 1.1»
    subgroups: list[int] = field(default_factory=list)
    raw: str = ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,.;")


def _is_teacherish(line: str) -> bool:
    return bool(TITLE_RE.search(line)) or bool(ATTR_RE.match(line.strip()))


def split_lessons(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    cur: list[str] = []
    seen_attr = False
    for line in [l.strip() for l in text.split("\n") if l.strip()]:
        teacherish = _is_teacherish(line) or bool(TEACHER_RE.search(line))
        if not teacherish and seen_attr and cur:
            blocks.append(cur)
            cur, seen_attr = [], False
        cur.append(line)
        if teacherish:
            seen_attr = True
    if cur:
        blocks.append(cur)
    return blocks


def parse_block(lines: list[str]) -> Lesson:
    subj_lines, rest = [], []
    for i, line in enumerate(lines):
        m = TEACHER_RE.search(line)
        if m or (ATTR_RE.match(line) and subj_lines):
            # строка с преподавателем; хвост до титула может быть частью названия
            if m and m.start() > 0 and not rest:
                head = line[:m.start()]
                if head.strip(" ,"):
                    subj_lines.append(head)
                rest.append(line[m.start():])
            else:
                rest.append(line)
            rest.extend(lines[i + 1:])
            break
        subj_lines.append(line)
    subject = _clean(" ".join(subj_lines).replace("- ", "-"))
    subject = re.sub(r"(?<=\s)\d(?:\s\d)+(?=\s)", "", subject).replace("  ", " ")  # обрывки «0 1» от соседних ячеек
    lesson = Lesson(subject=subject, raw="\n".join(lines))
    m = re.match(r"^(Д/В\s*\d+\.\d+)\s*(.*)$", subject)
    if m:
        lesson.elective, subject = m.group(1).replace(" ", ""), m.group(2)
    m = TYPE_RE.search(subject)
    if m:
        lesson.kind = m.group(1).replace("ЛР", "лаб")
        lesson.dates = _clean((m.group(2) or "") + " " + (m.group(4) or ""))
        if m.group(3):
            lesson.subgroups = [int(x) for x in re.findall(r"\d", m.group(3))]
        subject = subject[:m.start()]
    lesson.subject = _clean(subject)
    tail = " ".join(rest)
    lesson.online = "el.mpgu.su" in tail
    if not lesson.dates:
        lesson.dates = ", ".join(l.strip() for l in rest if DATES_LINE_RE.match(l)).replace(";", ",")
    am = ADDRESS_RE.search(lesson.raw)
    if am:
        lesson.address = am.group(1)
    for rm in ROOM_RE.finditer(tail):
        lesson.rooms += [r.strip() for r in re.split(r"[,/]", rm.group(1)) if r.strip()]
    matches = list(TEACHER_RE.finditer(tail))
    for j, tm in enumerate(matches):
        seg_end = matches[j + 1].start() if j + 1 < len(matches) else len(tail)
        seg = tail[tm.end():seg_end]
        title = re.sub(r"\s+", " ", tail[tm.start():tm.start(1)]).strip()
        initials = re.sub(r"\s+", "", tm.group(2) or "")
        if initials and not initials.endswith("."):
            initials += "."
        t = Teacher(name=f"{tm.group(1)} {initials}".strip(), title=title)
        sg = SUBGROUP_RE.search(seg)
        if sg:
            t.subgroups = [int(x) for x in re.findall(r"\d", sg.group(1))]
        lg = LANG_RE.search(seg)
        if lg:
            t.language = lg.group(1).lower()
        lesson.teachers.append(t)
    if not lesson.subgroups:
        sg = SUBGROUP_RE.search(" ".join(subj_lines))
        if sg:
            lesson.subgroups = [int(x) for x in re.findall(r"\d", sg.group(1))]
        elif lesson.teachers and all(t.subgroups for t in lesson.teachers):
            lesson.subgroups = sorted({s for t in lesson.teachers for s in t.subgroups})
    return lesson


def is_noise(text: str) -> bool:
    return not re.search(r"[А-Яа-яЁё]", text)


def is_teacher_only(text: str) -> bool:
    lines = [l for l in text.split("\n") if l.strip()]
    return bool(lines) and all(_is_teacherish(l) or TEACHER_RE.search(l) for l in lines)


def parse_cell_text(text: str) -> list[Lesson]:
    if is_noise(text):
        return []
    return [parse_block(b) for b in split_lessons(text)]
