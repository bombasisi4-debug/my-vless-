import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import game as G  # noqa: E402
import pyramid as P  # noqa: E402

CHAT = "-100"


def setup():
    clock = {"t": 1000.0}
    g = G.Game(":memory:", rng=random.Random(0), today=lambda: date(2026, 10, 15))
    pyr = P.Pyramids(g, clock=lambda: clock["t"])
    people = {}
    for uid, name, length in [(1, "Босс", 0), (2, "Аня", 1000), (3, "Боря", 1000), (4, "Вова", 1000), (5, "Нищий", 5)]:
        r = g.player(CHAT, uid, name, create=True)
        g.set_length(r["id"], length)
        people[name] = r["id"]
    return g, pyr, people, clock


def L(g, pid):
    return g.get(pid)["length"]


def test_draft_roundtrip():
    g, pyr, _, _ = setup()
    tok = pyr.draft(1, "  Супер   МММ  ")
    assert pyr.take_draft(tok) == {"uid": 1, "name": "Супер МММ"}
    assert pyr.draft(1, "Супер МММ") == tok


def test_ponzi_pays_first_investors_from_new_money():
    g, pyr, p, _ = setup()
    pid, err = pyr.create(CHAT, p["Босс"], "МММ", "IM")
    assert err is None
    assert pyr.create(CHAT, p["Босс"], "Ещё одна", None)[1]          # вторую нельзя
    r = pyr.invest(pid, g.get(p["Аня"]), 100)
    assert r.error is None and r.payouts == []
    assert L(g, p["Аня"]) == 900 and L(g, p["Босс"]) == 10           # 10% основателю
    assert pyr.get(pid)["fund"] == 90
    r = pyr.invest(pid, g.get(p["Боря"]), 100)                       # касса 180 ≥ 150 — Аня получает
    assert r.payouts == [("Аня", 150)]
    assert L(g, p["Аня"]) == 1050 and pyr.get(pid)["fund"] == 30
    n, unpaid, owed = pyr.stats(pid)
    assert (n, unpaid, owed) == (2, 1, 150)
    assert "Аня получил 150 см" in pyr.card(pid)


def test_rules_and_errors():
    g, pyr, p, _ = setup()
    pid, _ = pyr.create(CHAT, p["Босс"], "МММ", None)
    assert "Основатель" in pyr.invest(pid, g.get(p["Босс"]), 10).error
    assert "не хватает" in pyr.invest(pid, g.get(p["Нищий"]), 10).error
    assert "только основатель" in pyr.run_away(pid, p["Аня"])[1]


def test_founder_runs_away_with_fund():
    g, pyr, p, _ = setup()
    pid, _ = pyr.create(CHAT, p["Босс"], "МММ", None)
    pyr.invest(pid, g.get(p["Аня"]), 500)
    pyr.invest(pid, g.get(p["Боря"]), 100)
    fund = pyr.get(pid)["fund"]
    boss = L(g, p["Босс"])
    got, err = pyr.run_away(pid, p["Босс"])
    assert err is None and got == fund and L(g, p["Босс"]) == boss + fund
    assert pyr.get(pid)["status"] == "scam"
    assert "сбежал с кассой" in pyr.card(pid)
    assert pyr.invest(pid, g.get(p["Вова"]), 10).error                 # закрыта


def test_idle_collapse_splits_fund_between_unpaid():
    g, pyr, p, clock = setup()
    pid, _ = pyr.create(CHAT, p["Босс"], "МММ", None)
    pyr.invest(pid, g.get(p["Аня"]), 500)                               # обещано 750, касса 450
    pyr.invest(pid, g.get(p["Боря"]), 100)                              # касса 540 < 750
    clock["t"] += P.IDLE_COLLAPSE - 1
    assert pyr.collapse_idle() == []
    clock["t"] += 2
    a, b = L(g, p["Аня"]), L(g, p["Боря"])
    assert pyr.collapse_idle() == [pid]
    assert L(g, p["Аня"]) - a == 540 * 750 // 900 and L(g, p["Боря"]) - b == 540 * 150 // 900
    assert "рухнула" in pyr.card(pid)
