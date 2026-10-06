"""Оптимизация расписания имитацией отжига (как в «СуперЗавуче», но для вуза).

Переменные: для каждого подвижного события — день, пара, неделя (если занятие
раз в две недели) и аудитория. Жёсткие ограничения (накладки преподавателей,
групп, аудиторий, неподходящая аудитория) штрафуются большим весом, мягкие —
окна, перегруз дня, поздние пары, суббота, переезд на физкультуру и т.д.
"""
from __future__ import annotations

import math
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
DAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб"]

DEFAULT_WEIGHTS = {
    "conflict": 1000.0,        # накладка преподавателя / группы / аудитории
    "room_unsuitable": 300.0,  # лабораторная не в компьютерном классе, поток не помещается
    "teacher_unavailable": 500.0,
    "group_gap": 12.0,         # окно у группы (за каждую пустую пару)
    "teacher_gap": 5.0,        # окно у преподавателя
    "group_overload": 15.0,    # пар в день сверх лимита (квадратично)
    "group_single": 8.0,       # день ради одной пары
    "late_pair": 4.0,          # пара после 16:00 у очников (растёт с номером пары)
    "saturday": 3.0,           # пара в субботу у очников
    "pe_travel": 40.0,         # физкультура на Кибальчича вплотную к паре на Краснопрудной
    "teacher_single": 8.0,     # преподаватель едет ради одной пары
    "teacher_day": 6.0,        # каждый рабочий день преподавателя (чем компактнее неделя, тем лучше)
    "teacher_new_day": 10.0,   # преподаватель приезжает в день, когда раньше не работал
    "stability": 0.0,          # штраф за перенос занятия относительно исходного расписания
    "room_change": 0.5,        # штраф за смену аудитории (чтобы не менять без нужды)
    "stream_bonus": 3.0,       # поощрение: одну лекцию читают сразу нескольким группам (поток)
}

# «бережный» режим: двигаем только то, что действительно мешает
GENTLE = {"stability": 25.0, "room_change": 8.0}


def parse_ru_date(s: str) -> date:
    d, m, y = s.split()
    return date(int(y), MONTHS[m], int(d))


@dataclass
class Config:
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    max_pairs_per_day: int = 4
    days: int = 6
    slots: int = 7
    teacher_unavailable: dict = field(default_factory=dict)  # имя -> set((day, slot|None))
    group_scope: list[str] | None = None                       # оптимизировать только эти группы
    fixed_teachers: list[str] = field(default_factory=list)    # занятия этих преподавателей не двигать

    @classmethod
    def from_toml(cls, path: str) -> "Config":
        import tomllib
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        cfg = cls()
        cfg.weights.update(raw.get("weights", {}))
        cfg.max_pairs_per_day = raw.get("max_pairs_per_day", cfg.max_pairs_per_day)
        cfg.group_scope = raw.get("groups") or None
        cfg.fixed_teachers = raw.get("fixed_teachers", [])
        for name, t in raw.get("teachers", {}).items():
            un = parse_unavailable(t.get("unavailable", []))
            if t.get("available"):
                # «может только во вт и чт» = все остальные дни и пары недоступны
                ok = parse_unavailable(t["available"])
                ok_days = {d for d, s_ in ok if s_ is None}
                un |= {(d, s_) for d in range(cfg.days) for s_ in range(cfg.slots)
                       if d not in ok_days and (d, s_) not in ok}
            cfg.teacher_unavailable[name] = un
        return cfg


def parse_unavailable(items: list[str]) -> set:
    """«пн», «сб», «ср 1-2», «пт 5» → {(день, пара|None)} (пары с 1)."""
    out = set()
    for it in items:
        m = re.match(r"^\s*([а-я]{2})\s*(?:(\d)(?:\s*-\s*(\d))?)?\s*$", it.lower())
        if not m or m.group(1) not in DAY_SHORT:
            raise ValueError(f"не понимаю ограничение «{it}» (пример: «пн», «ср 1-2»)")
        d = DAY_SHORT.index(m.group(1))
        if m.group(2):
            a, b = int(m.group(2)), int(m.group(3) or m.group(2))
            out |= {(d, s - 1) for s in range(a, b + 1)}
        else:
            out.add((d, None))
    return out


class Problem:
    def __init__(self, data: dict, cfg: Config | None = None):
        self.data = data
        self.cfg = cfg or Config()
        self.w = self.cfg.weights
        ev = data["events"]
        self.n = len(ev)
        self.groups = data["groups"]
        # --- периоды обучения групп (занятия разных периодов не конфликтуют)
        self.period = []
        for e in ev:
            ps = [self.groups[g].get("period") for g in e["groups"] if self.groups.get(g, {}).get("period")]
            if ps:
                a = min(parse_ru_date(p[0]) for p in ps).toordinal()
                b = max(parse_ru_date(p[1]) for p in ps).toordinal()
            else:
                a, b = 0, 10 ** 7
            self.period.append((a, b))
        self.ozfo = [any(self.groups.get(g, {}).get("form") == "очно-заочная" for g in e["groups"]) for e in ev]
        self.dated = [bool(e["lessons"]) and all(l["dates"] for l in e["lessons"]) for e in ev]
        self.subgroups = [frozenset(e["subgroups"]) for e in ev]
        self.offsite = [bool(e["offsite"]) for e in ev]
        self.online = [bool(e["online"]) for e in ev]
        # «подпись» лекции: одну и ту же лекцию преподаватель может читать потоку из нескольких групп
        self.lecture_sig = []
        for e in ev:
            ls = e["lessons"]
            if ls and all(l["kind"] == "ЛК" and l["teachers"] for l in ls) and not e["subgroups"]:
                # поток собираем только внутри одного курса: «Матанализ» 1 и 2 курса — разные лекции
                course = frozenset(self.groups.get(g, {}).get("course") for g in e["groups"])
                self.lecture_sig.append((course, frozenset(
                    (re.sub(r"\s+", " ", l["subject"].lower()).strip(), l["dates"],
                     tuple(sorted(t["name"] for t in l["teachers"]))) for l in ls)))
            else:
                self.lecture_sig.append(None)
        self.n_groups = [len(e["groups"]) for e in ev]
        # --- аудитории: «вместимость» = максимум групп, которые там сидели; компьютерные классы
        cap, lab_cnt, all_cnt = defaultdict(int), defaultdict(int), defaultdict(int)
        for e in ev:
            for r in e["rooms"]:
                cap[r] = max(cap[r], len(e["groups"]))
                all_cnt[r] += 1
                lab_cnt[r] += e["kind"] == "lab"
        self.room_cap = dict(cap)
        self.comp_rooms = {r for r in all_cnt if lab_cnt[r] * 2 >= all_cnt[r]}
        self.rooms = sorted(cap, key=lambda r: (len(r), r))
        self.needs_comp = [e["kind"] == "lab" and bool(e["rooms"]) and set(e["rooms"]) <= self.comp_rooms for e in ev]
        # --- что двигаем
        scope = set(self.cfg.group_scope) if self.cfg.group_scope else None
        fixed_t = set(self.cfg.fixed_teachers)
        self.movable = []
        for i, e in enumerate(ev):
            mov = not e["frozen"] and not self.dated[i]
            if scope is not None and not set(e["groups"]) <= scope:
                mov = False
            if fixed_t & set(e["teachers"]):
                mov = False
            if mov:
                self.movable.append(i)
        self.movable_set = set(self.movable)
        # аудитория меняется только у занятий в одной аудитории
        self.room_choices = []
        for i, e in enumerate(ev):
            if i in self.movable_set and len(e["rooms"]) == 1 and not e["online"]:
                need = len(e["groups"])
                cands = [r for r in self.rooms if self.room_cap[r] >= need
                         and (r in self.comp_rooms) == self.needs_comp[i]]
                self.room_choices.append(cands or [e["rooms"][0]])
            else:
                self.room_choices.append(None)
        # --- допустимые позиции
        self.positions = []
        for i, e in enumerate(ev):
            if self.ozfo[i]:
                pos = [(d, s) for d in range(5) for s in (5, 6)] + [(5, s) for s in range(4)]
            else:
                pos = [(d, s) for d in range(self.cfg.days) for s in range(self.cfg.slots)]
            self.positions.append(pos)
        self.by_group = defaultdict(list)
        for i in self.movable:
            self.by_group[tuple(sorted(ev[i]["groups"]))].append(i)
        self.orig = [(e["day"], e["slot"], tuple(e["weeks"]), tuple(e["rooms"])) for e in ev]
        self.ents = [[("g", g) for g in e["groups"]] + [("t", t) for t in e["teachers"]] for e in ev]
        self.unavail = self.cfg.teacher_unavailable
        self.teacher_days = defaultdict(set)
        for e in ev:
            for t in e["teachers"]:
                self.teacher_days[t].add(e["day"])

    # ------------------------------------------------------------------ состояние
    def initial_state(self, randomize: bool = False, seed: int = 0) -> "State":
        st = State(self, [list(o) for o in self.orig])
        if randomize:
            rnd = random.Random(seed)
            for i in self.movable:
                d, s = rnd.choice(self.positions[i])
                w = st.pos[i][2] if len(st.pos[i][2]) == 2 else (rnd.choice((1, 2)),)
                st.pos[i] = [d, s, w, st.pos[i][3]]
        st.rebuild()
        return st


class State:
    def __init__(self, pb: Problem, pos: list):
        self.pb = pb
        self.pos = pos                          # [day, slot, weeks, rooms]
        self.occ: dict = defaultdict(list)      # (тип, имя, день, пара, неделя) -> [события]

    # ключи занятости события
    def keys(self, i, p=None):
        d, s, ws, rooms = p or self.pos[i]
        ents = self.pb.ents[i] + [("r", r) for r in rooms]
        return [(k, n, d, s, w) for (k, n) in ents for w in ws]

    def rebuild(self):
        self.occ = defaultdict(list)
        for i in range(self.pb.n):
            for key in self.keys(i):
                self.occ[key].append(i)

    # ------------------------------------------------------------------ штрафы
    def same_stream(self, a, b) -> bool:
        """Одна и та же лекция одного преподавателя в одной аудитории (или онлайн) — это поток."""
        pb = self.pb
        sa = pb.lecture_sig[a]
        if sa is None or sa != pb.lecture_sig[b]:
            return False
        ra, rb = self.pos[a][3], self.pos[b][3]
        online = pb.data["events"][a]["online"] and pb.data["events"][b]["online"]
        return online or (tuple(ra) == tuple(rb) and bool(ra))

    def stream_lectures(self) -> int:
        """Сколько раз за две недели лекцию читают сразу нескольким группам."""
        pb = self.pb
        groups = defaultdict(set)
        for i in range(pb.n):
            if pb.lecture_sig[i] is None:
                continue
            d, s, ws, rooms = self.pos[i]
            for w in ws:
                groups[(pb.lecture_sig[i], d, s, w, tuple(rooms))] |= set(pb.data["events"][i]["groups"])
        return sum(len(g) > 1 for g in groups.values())

    def pair_conflict(self, a, b, kind) -> bool:
        pb = self.pb
        if kind in ("t", "r") and self.same_stream(a, b):
            if kind == "t":
                return False
            # аудитория: поток должен поместиться
            cap = max(pb.room_cap.get(r, 0) for r in self.pos[a][3])
            return pb.n_groups[a] + pb.n_groups[b] > cap
        if pb.dated[a] and pb.dated[b]:
            return False
        pa, pb_ = pb.period[a], pb.period[b]
        if pa[1] < pb_[0] or pb_[1] < pa[0]:
            return False
        if kind == "g":
            sa, sb = pb.subgroups[a], pb.subgroups[b]
            if sa and sb and not (sa & sb):
                return False
        return True

    def key_cost(self, key, only_movable=False) -> float:
        lst = self.occ.get(key)
        if not lst or len(lst) < 2:
            return 0.0
        c = bonus = 0
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                a, b = lst[x], lst[y]
                if only_movable and a not in self.pb.movable_set and b not in self.pb.movable_set:
                    continue
                if self.pair_conflict(a, b, key[0]):
                    c += 1
                elif key[0] == "t" and self.same_stream(a, b):
                    bonus += 1
        return c * self.pb.w["conflict"] - bonus * self.pb.w.get("stream_bonus", 0.0)

    def day_cost(self, ent, d, w) -> float:
        kind, name = ent
        pb, W = self.pb, self.pb.w
        occ = self.occ
        busy = []
        for s in range(pb.cfg.slots):
            lst = occ.get((kind, name, d, s, w))
            if lst:
                busy.append((s, lst))
        if not busy:
            return 0.0
        n = len(busy)
        gaps = busy[-1][0] - busy[0][0] + 1 - n
        if kind == "t":
            cost = gaps * W["teacher_gap"]
            # день целиком дистанционный — ехать не надо
            if all(pb.online[i] for _, lst in busy for i in lst):
                return cost
            cost += W["teacher_day"]
            if n == 1:
                cost += W["teacher_single"]
            if W["teacher_new_day"] and d not in pb.teacher_days[name]:
                cost += W["teacher_new_day"]
            return cost
        cost = gaps * W["group_gap"]
        if n > pb.cfg.max_pairs_per_day:
            cost += (n - pb.cfg.max_pairs_per_day) ** 2 * W["group_overload"]
        if n == 1:
            cost += W["group_single"]
        # переезд Краснопрудная <-> Кибальчича
        if W["pe_travel"]:
            off = {s: any(pb.offsite[i] for i in lst) for s, lst in busy}
            for s, _ in busy:
                if off[s]:
                    for nb in (s - 1, s + 1):
                        if nb in off and not off[nb]:
                            cost += W["pe_travel"]
        return cost

    def event_cost(self, i) -> float:
        pb, W = self.pb, self.pb.w
        d, s, ws, rooms = self.pos[i]
        c = 0.0
        if not pb.ozfo[i]:
            if s >= 4:
                c += W["late_pair"] * (s - 3) ** 2 * len(ws) / 2
            if d == 5:
                c += W["saturday"] * len(ws) / 2
        for r in rooms:
            if pb.room_cap.get(r, 0) < len(pb.data["events"][i]["groups"]):
                c += W["room_unsuitable"]
            if pb.needs_comp[i] and r not in pb.comp_rooms:
                c += W["room_unsuitable"]
        for t in pb.data["events"][i]["teachers"]:
            un = pb.unavail.get(t)
            if un and ((d, None) in un or (d, s) in un):
                c += W["teacher_unavailable"]
        if i in pb.movable_set:
            o = pb.orig[i]
            if W["stability"] and ((d, s) != (o[0], o[1]) or tuple(ws) != o[2]):
                c += W["stability"]
            if W["room_change"] and tuple(rooms) != o[3]:
                c += W["room_change"]
        return c

    def affected(self, events, positions_list):
        keys, days = set(), set()
        for i, p in zip(events, positions_list):
            for key in self.keys(i, p):
                keys.add(key)
                if key[0] != "r":
                    days.add((key[0], key[1], key[2], key[4]))
        return keys, days

    def local_cost(self, keys, days, events) -> float:
        c = sum(self.key_cost(k) for k in keys)
        c += sum(self.day_cost((k, n), d, w) for (k, n, d, w) in days)
        c += sum(self.event_cost(i) for i in events)
        return c

    def total_cost(self) -> float:
        c = sum(self.key_cost(k) for k in self.occ)
        days = {(k[0], k[1], k[2], k[4]) for k in self.occ if k[0] != "r"}
        c += sum(self.day_cost((k, n), d, w) for (k, n, d, w) in days)
        c += sum(self.event_cost(i) for i in range(self.pb.n))
        return c

    def apply(self, events, new_positions):
        for i in events:
            for key in self.keys(i):
                self.occ[key].remove(i)
        for i, p in zip(events, new_positions):
            self.pos[i] = p
        for i in events:
            for key in self.keys(i):
                self.occ[key].append(i)

    def try_move(self, events, new_positions) -> float:
        """Применяет ход и возвращает изменение стоимости."""
        old = [self.pos[i] for i in events]
        k1, d1 = self.affected(events, old)
        k2, d2 = self.affected(events, new_positions)
        keys, days = k1 | k2, d1 | d2
        before = self.local_cost(keys, days, events)
        self.apply(events, new_positions)
        after = self.local_cost(keys, days, events)
        return after - before

    # ------------------------------------------------------------------ отчёт
    def report(self) -> dict:
        pb, W = self.pb, self.pb.w
        conflicts = []
        for key, lst in self.occ.items():
            for x in range(len(lst)):
                for y in range(x + 1, len(lst)):
                    a, b = lst[x], lst[y]
                    if self.pair_conflict(a, b, key[0]):
                        conflicts.append({"type": key[0], "name": key[1], "day": key[2], "slot": key[3],
                                          "week": key[4], "events": [a, b]})
        # одна накладка «каждую неделю» = две записи по неделям; схлопываем
        merged = {}
        for c in conflicts:
            k = (c["type"], c["name"], c["day"], c["slot"], tuple(c["events"]))
            if k in merged:
                merged[k]["weeks"].append(c["week"])
            else:
                a, b = c["events"]
                merged[k] = {**c, "weeks": [c["week"]], "possible": pb.dated[a] or pb.dated[b]
                             or any(l["dates"] for x in (a, b) for l in pb.data["events"][x]["lessons"])}
                del merged[k]["week"]
        streams = set()
        for key, lst in self.occ.items():
            if key[0] == "t":
                for x in range(len(lst)):
                    for y in range(x + 1, len(lst)):
                        if self.same_stream(lst[x], lst[y]):
                            streams.add((key[1], key[2], key[3], lst[x], lst[y]))
        self.streams = sorted({(t, d, s_) for t, d, s_, _, _ in streams})
        g_gaps = t_gaps = overload = singles = late = sat = pe = 0
        t_singles = t_days = unavail = 0
        days = {(k[0], k[1], k[2], k[4]) for k in self.occ if k[0] != "r" and self.occ[k]}
        for kind, name, d, w in days:
            busy = [s for s in range(pb.cfg.slots) if self.occ.get((kind, name, d, s, w))]
            gaps = busy[-1] - busy[0] + 1 - len(busy)
            if kind == "t":
                t_gaps += gaps
                if not all(pb.online[i] for s_ in busy for i in self.occ[(kind, name, d, s_, w)]):
                    t_days += 1
                    t_singles += len(busy) == 1
            else:
                g_gaps += gaps
                overload += max(0, len(busy) - pb.cfg.max_pairs_per_day)
                singles += len(busy) == 1
                offs = {s for s in busy if any(pb.offsite[i] for i in self.occ[(kind, name, d, s, w)])}
                pe += sum(1 for s in offs for nb in (s - 1, s + 1) if nb in busy and nb not in offs)
        for i in range(pb.n):
            d, s, ws, _ = self.pos[i]
            for t in pb.data["events"][i]["teachers"]:
                un = pb.unavail.get(t)
                if un and ((d, None) in un or (d, s) in un):
                    unavail += len(ws)
            if not pb.ozfo[i]:
                late += (s >= 4) * len(ws)
                sat += (d == 5) * len(ws)
        moved = sum(1 for i in pb.movable if tuple(self.pos[i][:2]) != pb.orig[i][:2]
                    or tuple(self.pos[i][2]) != pb.orig[i][2])
        room_changed = sum(1 for i in pb.movable if tuple(self.pos[i][3]) != pb.orig[i][3])
        return {
            "cost": round(self.total_cost(), 1),
            "conflicts": list(merged.values()),
            "streams": [{"teacher": t, "day": d, "slot": s_, "events": [a, b]}
                        for t, d, s_, a, b in sorted(streams)],
            "metrics": {
                "Накладки (преподаватель/группа/аудитория)": sum(not c["possible"] for c in merged.values()),
                "Возможные накладки (занятия по датам)": sum(c["possible"] for c in merged.values()),
                "Окна у групп (пар за 2 недели)": g_gaps,
                "Окна у преподавателей (пар за 2 недели)": t_gaps,
                "Приезды преподавателей ради одной пары": t_singles,
                "Рабочие дни преподавателей в корпусе (за 2 недели)": t_days,
                "Пары в недоступное для преподавателя время": unavail,
                f"Пары сверх {pb.cfg.max_pairs_per_day} в день у групп": overload,
                "Дни ради одной пары (группа·день)": singles,
                "Пары после 16:00 у очников": late,
                "Пары в субботу у очников": sat,
                "Физкультура вплотную к паре на Краснопрудной": pe,
                "Лекции потоком (сразу для нескольких групп)": self.stream_lectures(),
                "Перенесено занятий": moved,
                "Сменено аудиторий": room_changed,
            },
        }


def anneal(pb: Problem, st: State, iters: int = 300_000, seed: int = 0, t_end: float = 0.05,
           log=print, log_every: int = 50_000, t_start: float | None = None) -> State:
    rnd = random.Random(seed)
    if not pb.movable:
        return st
    cost = st.total_cost()
    best_cost, best_pos = cost, [list(p) for p in st.pos]

    def propose():
        i = rnd.choice(pb.movable)
        d, s, ws, rooms = st.pos[i]
        r = rnd.random()
        if r < 0.5:
            nd, ns = rnd.choice(pb.positions[i])
            nws = ws if len(ws) == 2 else (rnd.choice((1, 2)),)
            return [i], [[nd, ns, nws, rooms]]
        if r < 0.8:
            peers = pb.by_group[tuple(sorted(pb.data["events"][i]["groups"]))]
            j = rnd.choice(peers)
            if j != i and len(st.pos[j][2]) == len(ws):
                pj = st.pos[j]
                if (pj[0], pj[1]) in pb.positions[i] and (d, s) in pb.positions[j]:
                    return [i, j], [[pj[0], pj[1], pj[2], rooms], [d, s, ws, pj[3]]]
        if pb.room_choices[i] and len(pb.room_choices[i]) > 1:
            return [i], [[d, s, ws, (rnd.choice(pb.room_choices[i]),)]]
        if len(ws) == 1:
            return [i], [[d, s, (3 - ws[0],), rooms]]
        nd, ns = rnd.choice(pb.positions[i])
        return [i], [[nd, ns, ws, rooms]]

    # стартовая температура: средний положительный прирост на случайных ходах
    ups = []
    for _ in range(300):
        evs, new = propose()
        old = [st.pos[i] for i in evs]
        dlt = st.try_move(evs, new)
        st.apply(evs, old)
        if dlt > 0:
            ups.append(dlt)
    t0 = (sum(ups) / len(ups)) if ups else 1.0
    t0 = min(t0, 200.0) if t_start is None else t_start
    alpha = (t_end / t0) ** (1.0 / max(1, iters))
    t = t0
    started = time.time()
    accepted = 0
    for it in range(1, iters + 1):
        evs, new = propose()
        old = [st.pos[i] for i in evs]
        dlt = st.try_move(evs, new)
        if dlt <= 0 or rnd.random() < math.exp(-dlt / t):
            cost += dlt
            accepted += 1
            if cost < best_cost - 1e-9:
                best_cost, best_pos = cost, [list(p) for p in st.pos]
        else:
            st.apply(evs, old)
        t *= alpha
        if log and it % log_every == 0:
            log(f"  итерация {it:>8}  T={t:8.3f}  стоимость={cost:10.1f}  лучшая={best_cost:10.1f}  "
                f"({time.time() - started:.0f} с)")
    st.pos = best_pos
    st.rebuild()
    return st
