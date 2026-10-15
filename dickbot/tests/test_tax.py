import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin as A  # noqa: E402
import game as G  # noqa: E402

CHAT = "-100"
OWNER = 1


def seeded():
    day = {"d": date(2026, 10, 15)}
    g = G.Game(":memory:", rng=random.Random(0), today=lambda: day["d"])
    g.owner_lookup = lambda: OWNER
    players = json.loads((Path(__file__).resolve().parent.parent / "seed.json").read_text())["players"]
    g.seed(CHAT, players, OWNER, "pussy", 15000)
    return g, day


def run(g, text):
    act = A.parse(g, CHAT, text, OWNER)
    assert act is not None and act.error is None, act and act.error
    return act


def L(g, name):
    return [r for r in g.all_players(CHAT) if r["name"] == name][0]["length"]


def test_percent_tax_goes_to_owner_and_skips_negative():
    g, _ = seeded()
    act = run(g, "налог 10%")
    assert "≈" in act.title
    act.run()
    assert L(g, "Оболдуй") == 12754 - 1275
    assert L(g, "amurabs") == -10                       # минус не трогаем
    assert L(g, "Mrkwa") == 1                            # 10% от 1 см = 0
    collected = 15000 + sum(round(p["length"] * 0.1) for p in g.all_players(CHAT) if False)
    assert g.find(CHAT, OWNER)["length"] > 15000         # казна пополнилась
    assert "ушло в казну" in act.announce()


def test_fixed_tax_never_below_zero_and_burn():
    g, _ = seeded()
    before_owner = g.find(CHAT, OWNER)["length"]
    run(g, "налог 5 сжечь").run()
    assert L(g, "Mrkwa") == 0 and L(g, "Оболдуй") == 12749
    assert g.find(CHAT, OWNER)["length"] == before_owner  # сожгли, владельцу ничего


def test_daily_tax_once_per_day_from_tomorrow():
    g, day = seeded()
    run(g, "налог каждый день 1%").run()
    assert g.apply_daily_taxes() == []                   # сегодня не берём
    day["d"] += timedelta(days=1)
    first = g.apply_daily_taxes()
    assert first and first[0][2] > 0
    assert g.apply_daily_taxes() == []                   # второй раз за день — нет
    run(g, "налог стоп").run()
    day["d"] += timedelta(days=1)
    assert g.apply_daily_taxes() == []


def test_growth_tax_perk_and_owner_exempt():
    g, day = seeded()
    run(g, "налог с роста 50%").run()
    g.player(CHAT, 555, "Оболдуй", create=False)
    owner_before = g.find(CHAT, OWNER)["length"]
    taxed = False
    for _ in range(10):
        day["d"] += timedelta(days=1)
        res = g.grow(CHAT, 555, "Оболдуй")
        tax = [p for p in res.perks if p.key == "tax"]
        taxed |= bool(tax)
        assert all(p.change < 0 and "в казну" in p.note for p in tax)
    assert taxed and g.find(CHAT, OWNER)["length"] > owner_before
    res = g.grow(CHAT, OWNER, "pussy")                   # с владельца не берут
    assert not any(p.key == "tax" for p in res.perks)


def test_tax_status_and_bad_input():
    g, _ = seeded()
    assert "нет" in run(g, "налог").run()
    assert A.parse(g, CHAT, "налог много", OWNER).error
    assert A.parse(g, CHAT, "налог с роста 5", OWNER).error   # только проценты
