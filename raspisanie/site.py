"""Сборка одностраничного просмотрщика: site/index.html со встроенными данными."""
from __future__ import annotations

import json
import os
from datetime import date

from .solver import Problem

TEMPLATE = os.path.join(os.path.dirname(__file__), "site_template.html")

VARIANT_NAMES = {
    "gentle": ("Бережная правка", "Убраны накладки и переезды, перенесено минимум занятий"),
    "full": ("Полная перестройка", "Все подвижные занятия переставлены заново ради окон и поздних пар"),
}


def _compact_event(e: dict) -> dict:
    return {
        "g": e["groups"], "t": e["teachers"], "on": e["online"], "off": e["offsite"],
        "fr": e["frozen"], "k": e["kind"], "sg": e["subgroups"],
        "l": [{"s": l["subject"], "k": l["kind"], "d": l["dates"], "e": l["elective"],
               "tt": [[t["name"], t["subgroups"], t["language"]] for t in l["teachers"]]}
              for l in e["lessons"]],
    }


def build_site(variants: list[dict], out: str, today: date | None = None, scope_note: str = "") -> str:
    """variants — результаты `solve` (data/optimized*.json) на одном и том же наборе групп."""
    base = variants[0]
    pb = Problem({**base, "events": [{**e, **e.get("orig", {})} for e in base["events"]]})
    payload = {
        "generated": (today or date.today()).isoformat(),
        "scopeNote": scope_note,
        "days": base["days"], "slots": base["slots"],
        "groups": {g: {k: v.get(k) for k in ("direction", "profile", "course", "form", "level", "period")}
                   for g, v in base["groups"].items()},
        "teachers": base["teachers"],
        "rooms": pb.rooms, "roomCap": pb.room_cap, "compRooms": sorted(pb.comp_rooms),
        "events": [_compact_event(e) for e in base["events"]],
        "variants": [{
            "id": "orig", "name": "Действующее", "desc": "Как в опубликованных PDF",
            "pos": [[e["orig"]["day"], e["orig"]["slot"], e["orig"]["weeks"], e["orig"]["rooms"]] for e in base["events"]],
            "report": base["report_before"],
        }],
    }
    for v in variants:
        mode = v.get("solver", {}).get("mode", "full")
        name, desc = VARIANT_NAMES.get(mode, (mode, ""))
        payload["variants"].append({
            "id": mode, "name": name, "desc": desc,
            "pos": [[e["day"], e["slot"], e["weeks"], e["rooms"]] for e in v["events"]],
            "report": v["report_after"],
        })
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", blob)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out
