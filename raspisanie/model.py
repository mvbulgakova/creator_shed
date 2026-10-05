"""Модель данных: группы, преподаватели, аудитории и «события» (занятия в сетке).

Событие = одна ячейка исходного расписания: одна или несколько дисциплин
(например, параллельные Д/В 1.1 и 1.2 или подгруппы), которые идут одновременно
у одного набора групп. Именно события переставляет оптимизатор.
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import asdict, dataclass, field

from .lesson_text import Lesson, parse_cell_text
from .pdfgrid import DAYS, SLOT_LABELS, parse_pdf

LEVELS = {"bvo": "Базовое высшее", "spvo": "Специализированное высшее", "bak": "Бакалавриат"}


@dataclass
class Event:
    id: int
    groups: list[str]
    day: int
    slot: int
    weeks: list[int]                 # [1, 2] — каждую неделю, [1] / [2] — раз в две недели
    lessons: list[dict]
    teachers: list[str] = field(default_factory=list)
    rooms: list[str] = field(default_factory=list)
    subgroups: list[int] = field(default_factory=list)   # пусто = вся группа
    online: bool = False
    offsite: str = ""                # другой адрес (физкультура на Кибальчича)
    frozen: bool = False             # не двигать: занятия по датам, практики, дни самостоятельной работы
    kind: str = ""                   # lecture / practice / lab / mixed / pe / internship / selfstudy
    source: str = ""

    @property
    def title(self) -> str:
        return " + ".join(l["subject"] for l in self.lessons)


def classify(lessons: list[Lesson]) -> tuple[str, bool]:
    subj = " ".join(l.subject for l in lessons).lower()
    if "самостоятельн" in subj:
        return "selfstudy", True
    if "физическ" in subj and "культур" in subj:
        return "pe", False
    if "практика" in subj and not any(l.kind for l in lessons):
        return "internship", True
    kinds = {l.kind for l in lessons if l.kind}
    dated = any(l.dates for l in lessons)
    if kinds == {"ЛК"}:
        k = "lecture"
    elif kinds and kinds <= {"лаб", "ПР"}:
        k = "lab"
    elif kinds == {"ПЗ"} or kinds == {"СЗ"}:
        k = "practice"
    else:
        k = "mixed"
    return k, dated


def file_meta(path: str, header: str) -> dict:
    name = os.path.basename(path)
    m = re.search(r"(\d+)\s*семестр\s*(\d+)\s*курс", header)
    level = next((v for k, v in LEVELS.items() if re.search(rf"[-_]{k}[-_]", name)), "")
    if "bvo_bak" in name:
        level = "Базовое высшее / Бакалавриат"
    form = "очно-заочная" if "очно-заочная" in header else "очная"
    periods = {}
    for line in header.split("\n"):
        pm = re.match(r"^(.*?):?\s*с\s+(\d+\s+\S+\s+\d{4})\s*г\.\s*по\s+(\d+\s+\S+\s+\d{4})", line.strip())
        if pm:
            for g in re.split(r",\s*", pm.group(1).strip(": ")) or [""]:
                periods[g.strip()] = [pm.group(2), pm.group(3)]
    return {"file": name, "semester": int(m.group(1)) if m else None,
            "course": int(m.group(2)) if m else None, "level": level, "form": form,
            "periods": periods}


def build_dataset(pdf_dir: str) -> dict:
    events: list[Event] = []
    groups: dict[str, dict] = {}
    files = []
    for path in sorted(glob.glob(os.path.join(pdf_dir, "*.pdf"))):
        cells, sections, meta = parse_pdf(path)
        fm = file_meta(path, meta["header"])
        files.append(fm)
        for sec in sections:
            for _, _, g in sec.groups:
                period = fm["periods"].get(g) or fm["periods"].get("") or next(iter(fm["periods"].values()), None)
                groups[g] = {"name": g, "direction": sec.group_direction.get(g, ""),
                             "profile": sec.group_profile.get(g, ""), "course": fm["course"],
                             "semester": fm["semester"], "level": fm["level"], "form": fm["form"],
                             "period": period, "file": fm["file"]}
        for c in cells:
            lessons = parse_cell_text(c.text)
            if not lessons:
                continue
            kind, frozen = classify(lessons)
            teachers, rooms, subgroups = [], [], set()
            whole = False
            for l in lessons:
                for t in l.teachers:
                    if t.name not in teachers:
                        teachers.append(t.name)
                rooms += [r for r in l.rooms if r not in rooms]
                if l.subgroups:
                    subgroups |= set(l.subgroups)
                else:
                    whole = True
            ev = Event(
                id=len(events), groups=c.groups, day=c.day, slot=c.slot, weeks=list(c.weeks),
                lessons=[{"subject": l.subject, "kind": l.kind, "dates": l.dates, "elective": l.elective,
                          "online": l.online, "rooms": l.rooms, "subgroups": l.subgroups,
                          "teachers": [asdict(t) for t in l.teachers]} for l in lessons],
                teachers=teachers, rooms=rooms,
                subgroups=[] if whole else sorted(subgroups),
                online=all(l.online for l in lessons if l.teachers) and any(l.online for l in lessons),
                offsite=next((l.address for l in lessons if l.address), ""),
                frozen=frozen, kind=kind, source=fm["file"])
            events.append(ev)
    events = merge_streams(events)
    data = {
        "days": DAYS, "slots": SLOT_LABELS, "files": files,
        "groups": groups,
        "events": [asdict(e) for e in events],
    }
    data["teachers"] = sorted({t for e in events for t in e.teachers})
    data["rooms"] = sorted({r for e in events for r in e.rooms}, key=lambda r: (len(r), r))
    return data


def merge_streams(events: list[Event]) -> list[Event]:
    """Поток, разрезанный между таблицами разных направлений (одна лекция у групп
    из разных секций PDF), склеиваем в одно событие."""
    out: list[Event] = []
    index: dict = {}
    for e in events:
        key = (e.day, e.slot, tuple(e.weeks), tuple(sorted(e.teachers)),
               tuple((l["subject"], l["kind"], l["dates"]) for l in e.lessons), tuple(e.subgroups))
        if e.teachers and key in index:
            base = index[key]
            base.groups += [g for g in e.groups if g not in base.groups]
            base.rooms += [r for r in e.rooms if r not in base.rooms]
            continue
        index[key] = e
        out.append(e)
    for i, e in enumerate(out):
        e.id = i
    return out


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(data: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def active_on(data: dict, day) -> dict:
    """Оставляет только группы, у которых на дату `day` идут занятия (и их события)."""
    from .solver import parse_ru_date
    keep = {g for g, info in data["groups"].items()
            if info.get("period") and parse_ru_date(info["period"][0]) <= day <= parse_ru_date(info["period"][1])}
    out = dict(data)
    out["groups"] = {g: v for g, v in data["groups"].items() if g in keep}
    events = [dict(e) for e in data["events"] if set(e["groups"]) & keep]
    for e in events:
        e["groups"] = [g for g in e["groups"] if g in keep]
    for i, e in enumerate(events):
        e["id"] = i
    out["events"] = events
    out["teachers"] = sorted({t for e in events for t in e["teachers"]})
    out["rooms"] = sorted({r for e in events for r in e["rooms"]}, key=lambda r: (len(r), r))
    return out
