"""Командная строка.

    python -m raspisanie parse                 # PDF -> data/parsed.json
    python -m raspisanie check                 # проверить действующее расписание
    python -m raspisanie solve --mode gentle   # оптимизировать -> data/optimized.json
    python -m raspisanie export                # Excel + .ics
    python -m raspisanie site                  # site/index.html
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from datetime import date

from . import model
from .solver import GENTLE, Config, Problem, anneal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSED = os.path.join(ROOT, "data", "parsed.json")
OPTIMIZED = os.path.join(ROOT, "data", "optimized.json")


def _scope(data: dict, args) -> dict:
    if getattr(args, "all", False):
        return data
    day = date.fromisoformat(args.date) if args.date else date.today()
    out = model.active_on(data, day)
    print(f"Группы, у которых {day:%d.%m.%Y} идут занятия: {len(out['groups'])} из {len(data['groups'])}"
          f" (флаг --all — взять все)")
    return out


def _config(args) -> Config:
    cfg = Config.from_toml(args.config) if args.config and os.path.exists(args.config) else Config()
    if getattr(args, "mode", None) == "gentle":
        for k, v in GENTLE.items():
            cfg.weights[k] = max(cfg.weights.get(k, 0), v)
    return cfg


def print_report(rep: dict, data: dict, limit: int = 40) -> None:
    for k, v in rep["metrics"].items():
        print(f"  {k:<48} {v}")
    if rep["conflicts"]:
        print("\nНакладки:")
        names = {"t": "преподаватель", "g": "группа", "r": "аудитория"}
        for c in rep["conflicts"][:limit]:
            a, b = (data["events"][i] for i in c["events"])
            wk = "каждую неделю" if len(c["weeks"]) == 2 else f"{c['weeks'][0]}-я неделя"
            mark = " (по датам — проверить)" if c.get("possible") else ""
            print(f"  • {names[c['type']]} {c['name']}: {data['days'][c['day']]}, {data['slots'][c['slot']]}, {wk}{mark}")
            for e in (a, b):
                print(f"      {', '.join(e['groups'])} — {e['lessons'][0]['subject']}")
        if len(rep["conflicts"]) > limit:
            print(f"  … и ещё {len(rep['conflicts']) - limit}")


def cmd_parse(args):
    data = model.build_dataset(args.pdf)
    model.save(data, args.out)
    print(f"Разобрано: {len(data['events'])} занятий, {len(data['groups'])} групп, "
          f"{len(data['teachers'])} преподавателей, {len(data['rooms'])} аудиторий -> {args.out}")


def cmd_check(args):
    data = _scope(model.load(args.input), args)
    pb = Problem(data, _config(args))
    rep = pb.initial_state().report()
    print(f"\nДействующее расписание, штраф {rep['cost']}:")
    print_report(rep, data)


def cmd_solve(args):
    data = _scope(model.load(args.input), args)
    cfg = _config(args)
    pb = Problem(data, cfg)
    st0 = pb.initial_state()
    before = st0.report()
    print(f"Исходное расписание: штраф {before['cost']}, двигаем {len(pb.movable)} из {pb.n} занятий")
    best = None
    for run in range(args.restarts):
        st = pb.initial_state(randomize=args.from_scratch, seed=args.seed + run)
        # в бережном режиме «не плавим» расписание целиком: низкая стартовая температура
        t_start = 4.0 if args.mode == "gentle" and not args.from_scratch else None
        st = anneal(pb, st, iters=args.iters, seed=args.seed + run, log_every=max(1, args.iters // 6),
                    t_start=t_start)
        c = st.total_cost()
        print(f"Прогон {run + 1}: штраф {c:.1f}")
        if best is None or c < best[0]:
            best = (c, [list(p) for p in st.pos])
    st.pos = best[1]
    st.rebuild()
    after = st.report()
    out = copy.deepcopy(data)
    for i, e in enumerate(out["events"]):
        d, s, ws, rooms = st.pos[i]
        e["orig"] = {"day": e["day"], "slot": e["slot"], "weeks": e["weeks"], "rooms": e["rooms"]}
        e["day"], e["slot"], e["weeks"], e["rooms"] = d, s, list(ws), list(rooms)
    out["report_before"], out["report_after"] = before, after
    out["solver"] = {"mode": args.mode, "iters": args.iters, "weights": cfg.weights,
                     "from_scratch": args.from_scratch}
    model.save(out, args.out)
    print("\nПосле оптимизации:")
    print_report(after, out)
    print(f"\nСохранено в {args.out}")


def cmd_export(args):
    from .export import export_ics_dir, to_xlsx
    data = model.load(args.input)
    os.makedirs(args.out, exist_ok=True)
    xlsx = os.path.join(args.out, "raspisanie.xlsx")
    to_xlsx(data, xlsx)
    n = export_ics_dir(data, os.path.join(args.out, "ics"))
    print(f"Excel: {xlsx}\nКалендари (.ics): {n} файлов в {os.path.join(args.out, 'ics')}")


def cmd_site(args):
    from .site import build_site
    inputs = [p for p in args.input if os.path.exists(p)]
    variants = [model.load(p) for p in inputs]
    note = ""
    if os.path.exists(PARSED):
        allg = model.load(PARSED)["groups"]
        skipped = sorted({allg[g]["course"] for g in allg if g not in variants[0]["groups"]})
        if skipped:
            note = (f"Курсы {', '.join(map(str, skipped))} сейчас не учатся по этой сетке "
                    f"(практика или занятия закончились) и в расчёт не вошли.")
    path = build_site(variants, args.out, scope_note=note)
    print(f"Сайт: {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="raspisanie", description="Конструктор расписания ИМИ МПГУ")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="разобрать PDF-расписания")
    p.add_argument("--pdf", default=os.path.join(ROOT, "data", "pdf"))
    p.add_argument("--out", default=PARSED)
    p.set_defaults(func=cmd_parse)

    for name, fn, hlp in (("check", cmd_check, "найти накладки и посчитать метрики"),
                          ("solve", cmd_solve, "оптимизировать расписание")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--input", default=PARSED)
        p.add_argument("--date", help="брать группы, которые учатся в этот день (ГГГГ-ММ-ДД), по умолчанию сегодня")
        p.add_argument("--all", action="store_true", help="все группы из PDF, без фильтра по дате")
        p.add_argument("--config", default=os.path.join(ROOT, "config.toml"))
        p.set_defaults(func=fn)
        if name == "solve":
            p.add_argument("--mode", choices=["gentle", "full"], default="gentle",
                           help="gentle — минимум переносов; full — перестроить смело")
            p.add_argument("--from-scratch", action="store_true", help="начать со случайного расписания")
            p.add_argument("--iters", type=int, default=300_000)
            p.add_argument("--restarts", type=int, default=1)
            p.add_argument("--seed", type=int, default=1)
            p.add_argument("--out", default=OPTIMIZED)
        else:
            p.add_argument("--mode", default=None, help=argparse.SUPPRESS)

    p = sub.add_parser("export", help="выгрузить в Excel и .ics")
    p.add_argument("--input", default=OPTIMIZED)
    p.add_argument("--out", default=os.path.join(ROOT, "out"))
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("site", help="собрать веб-просмотрщик")
    p.add_argument("--input", nargs="+", default=[OPTIMIZED, OPTIMIZED.replace(".json", "_full.json")],
                   help="результаты solve (на одном наборе групп)")
    p.add_argument("--out", default=os.path.join(ROOT, "site", "index.html"))
    p.set_defaults(func=cmd_site)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
