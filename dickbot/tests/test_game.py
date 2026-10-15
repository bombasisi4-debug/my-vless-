import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import game as G  # noqa: E402

CHAT = "-100"
OWNER = 1


class Clock:
    def __init__(self):
        self.day = date(2026, 10, 15)

    def __call__(self):
        return self.day


def make(seed=0):
    clock = Clock()
    return G.Game(":memory:", rng=random.Random(seed), today=clock), clock


def seeded():
    g, clock = make()
    players = json.loads((Path(__file__).resolve().parent.parent / "seed.json").read_text())["players"]
    g.seed(CHAT, players, OWNER, "Я", 15000)
    return g, clock


def test_base_increment_ranges():
    rng = random.Random(1)
    vals = [G.base_increment(rng, newcomer=False) for _ in range(2000)]
    assert min(vals) == -5 and max(vals) == 10 and 0 not in vals
    assert all(G.base_increment(rng, newcomer=True) > 0 for _ in range(500))


def test_streak_bonus_matches_original_screenshot():
    # «day after day (+1, 3 days in a row; +2 from day 4)»
    assert G.streak_bonus(1) == (0, 2)
    assert G.streak_bonus(3) == (1, 4)
    assert G.streak_bonus(32) == (5, None)


def test_seed_puts_owner_first_and_keeps_order():
    g, _ = seeded()
    top = g.top(CHAT, 0, 100)
    assert top[0]["user_id"] == OWNER and top[0]["length"] == 15000
    assert [r["name"] for r in top[1:4]] == ["Оболдуй", "NoSock", "стасян"]
    assert len(top) == 29


def test_seed_refuses_twice_without_force():
    g, _ = seeded()
    try:
        g.seed(CHAT, [], OWNER, "Я", 1)
        raise AssertionError("должно было упасть")
    except ValueError:
        pass
    assert g.seed(CHAT, [], OWNER, "Я", 1, force=True) == 1


def test_seeded_player_claims_record_by_name_and_keeps_cm():
    g, _ = seeded()
    row = g.player(CHAT, 555, "Оболдуй", create=True)
    assert row["length"] == 12754 and row["user_id"] == 555
    # homoglyphs: кириллическое «ааа» забирает латинское «aaa»
    assert g.player(CHAT, 556, "ааа", create=False)["length"] == 933
    # украшения в имени не мешают
    assert g.player(CHAT, 557, "SS Sasha SS", create=False)["length"] == 147


def test_grow_once_per_day_and_marks():
    g, clock = seeded()
    oboltus = g.player(CHAT, 555, "Оболдуй", create=True)
    assert not g.can_grow(oboltus)          # без метки = уже растил сегодня
    assert not g.grow(CHAT, 555, "Оболдуй").ok
    clock.day += timedelta(days=1)
    res = g.grow(CHAT, 555, "Оболдуй")
    assert res.ok and res.length == 12754 + res.total
    assert not g.grow(CHAT, 555, "Оболдуй").ok
    vasia = g.player(CHAT, 777, "Vasia", create=False)
    assert g.is_inactive(vasia)


def test_streak_perk_applies_from_day_two():
    g, clock = make(3)
    g.grow(CHAT, 9, "x")
    clock.day += timedelta(days=1)
    res = g.grow(CHAT, 9, "x")
    assert any(p.key == "streak" and p.change == 1 for p in res.perks)
    clock.day += timedelta(days=2)  # пропустил день — серия заново
    res = g.grow(CHAT, 9, "x")
    assert not any(p.key == "streak" for p in res.perks)


def test_battle_moves_bet_and_is_resolved_once():
    g, _ = seeded()
    a = g.find(CHAT, OWNER)
    b = g.player(CHAT, 555, "Оболдуй", create=False)
    total = a["length"] + b["length"]
    res = g.battle(CHAT, "id1", a, b, 100)
    assert res and res.winner["length"] - res.loser["length"] in (15000 - 12754 + 200, 12754 - 15000 + 200)
    assert g.find(CHAT, OWNER)["length"] + g.find(CHAT, 555)["length"] == total
    assert g.battle(CHAT, "id1", a, b, 100) is None
    assert res.winner["wins"] == 1 and res.loser["battles"] == 1


def test_loan_and_payout():
    g, clock = make(5)
    row = g.player(CHAT, 9, "x", create=True)
    g.set_length(row["id"], -50)
    assert g.take_loan(row["id"])
    row = g.get(row["id"])
    assert row["length"] == 0 and row["debt"] == 50
    assert not g.take_loan(row["id"])
    for _ in range(30):
        clock.day += timedelta(days=1)
        res = g.grow(CHAT, 9, "x")
        payout = [p for p in res.perks if p.key == "loan-payout"]
        assert all(p.change <= 0 for p in payout)
    assert g.get(row["id"])["debt"] < 50


def test_help_pussies():
    assert G.help_pussies_bonus(-500) == 5
    assert G.help_pussies_bonus(10) == 0


def test_dod_once_per_day_excludes_unclaimed():
    g, clock = seeded()
    status, row, res = g.dod(CHAT)
    assert status == "chosen" and row["user_id"] == OWNER  # единственный «живой» игрок
    assert g.dod(CHAT)[0] == "already"
    clock.day += timedelta(days=1)
    assert g.dod(CHAT)[0] == "chosen"


def test_dod_excludes_rich():
    g, _ = make(1)
    for uid in range(1, 21):
        r = g.player(CHAT, uid, f"u{uid}", create=True)
        g.set_length(r["id"], uid * 1000)
        g.db.execute("UPDATE players SET grown_at = ? WHERE id = ?", (g.today().isoformat(), r["id"]))
    winners = set()
    for i in range(40):
        g.today = (lambda d: (lambda: d))(date(2027, 1, 1) + timedelta(days=i))
        for r in g.db.execute("SELECT id FROM players").fetchall():
            g.db.execute("UPDATE players SET grown_at = ? WHERE id = ?", (g.today().isoformat(), r["id"]))
        _, row, _ = g.dod(CHAT)
        winners.add(row["user_id"])
    assert 20 not in winners and 19 not in winners


def test_link_moves_record_to_user():
    g, _ = seeded()
    g.player(CHAT, 999, "Совсем другое имя", create=True)
    row = g.link(CHAT, "5", 999, "Совсем другое имя")  # 5-е место = Чай Чаевич (1 — владелец)
    assert row["length"] == 1325 and g.find(CHAT, 999)["id"] == row["id"]
    assert g.db.execute("SELECT COUNT(*) FROM players WHERE user_id = 999").fetchone()[0] == 1


def test_chat_instance_mapping():
    g, _ = make()
    assert g.chat_for_instance("abc") == "ci:abc"
    g.remember_chat_instance("abc", CHAT)
    assert g.chat_for_instance("abc") == CHAT
