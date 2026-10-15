import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin as A  # noqa: E402
import game as G  # noqa: E402

CHAT = "-100"
OWNER = 1


def seeded(seed=0):
    day = {"d": date(2026, 10, 15)}
    g = G.Game(":memory:", rng=random.Random(seed), today=lambda: day["d"])
    players = json.loads((Path(__file__).resolve().parent.parent / "seed.json").read_text())["players"]
    g.seed(CHAT, players, OWNER, "pussy", 15000)
    return g, day


def run(g, text):
    act = A.parse(g, CHAT, text, OWNER)
    assert act is not None and act.error is None, act and act.error
    return act.run()


def length(g, name):
    return [r for r in g.all_players(CHAT) if r["name"] == name][0]["length"]


def test_not_admin_phrases():
    g, _ = seeded()
    for q in ("", "10", "seed", "привет всем"):
        assert A.parse(g, CHAT, q, OWNER) is None


def test_set_add_sub_zero_by_place_name_and_me():
    g, _ = seeded()
    run(g, "см Оболдуй 100")
    assert length(g, "Оболдуй") == 100
    run(g, "+500 2")  # 2-е место теперь NoSock
    assert length(g, "NoSock") == 6042
    run(g, "-42 стас")  # часть имени
    assert length(g, "стасян") == 2948
    run(g, "см я 20000")
    assert g.find(CHAT, OWNER)["length"] == 20000
    run(g, "ноль NoSock")
    assert length(g, "NoSock") == 0


def test_ambiguous_and_missing_targets_are_errors():
    g, _ = seeded()
    assert "Несколько" in A.parse(g, CHAT, "ноль Ronili Hex", OWNER).error
    assert A.parse(g, CHAT, "ноль Назар", OWNER).error is None  # точное совпадение важнее части имени
    assert "Подходят несколько" in A.parse(g, CHAT, "ноль Наз", OWNER).error
    assert A.parse(g, CHAT, "ноль Несуществующий", OWNER).error
    assert A.parse(g, CHAT, 'см "67" 5', OWNER).error is None


def test_swap():
    g, _ = seeded()
    run(g, "обмен Оболдуй и стасян")
    assert length(g, "Оболдуй") == 2990 and length(g, "стасян") == 12754


def test_luck_rigs_battles():
    g, _ = seeded()
    run(g, "фарт я")
    a = g.find(CHAT, OWNER)
    b = g.player(CHAT, 555, "Оболдуй", create=False)
    for i in range(20):
        res = g.battle(CHAT, f"b{i}", g.get(a["id"]), g.get(b["id"]), 1)
        assert res.winner["id"] == a["id"]
    run(g, "лох я")
    res = g.battle(CHAT, "x", g.get(a["id"]), g.get(b["id"]), 1)
    assert res.loser["id"] == a["id"]
    run(g, "честно я")
    assert g.get(a["id"])["luck"] == 0


def test_curse_and_bless():
    g, day = seeded()
    g.player(CHAT, 555, "Оболдуй", create=False)
    run(g, "проклясть Оболдуй 3")
    for _ in range(3):
        day["d"] += timedelta(days=1)
        res = g.grow(CHAT, 555, "Оболдуй")
        assert res.ok and not any(p.key == "help-pussies" for p in res.perks)
        assert res.total - sum(p.change for p in res.perks) < 0
    day["d"] += timedelta(days=1)  # проклятие кончилось
    run(g, "благословить Оболдуй 2")
    day["d"] += timedelta(days=1)
    res = g.grow(CHAT, 555, "Оболдуй")
    assert res.total - sum(p.change for p in res.perks) >= 7


def test_ban_and_reset_and_debt():
    g, day = seeded()
    row = g.player(CHAT, 555, "Оболдуй", create=False)
    run(g, "бан Оболдуй 2")
    assert g.banned_until(g.get(row["id"])) == day["d"] + timedelta(days=1)
    run(g, "разбан Оболдуй")
    assert g.banned_until(g.get(row["id"])) is None
    assert not g.can_grow(g.get(row["id"]))
    run(g, "откат Оболдуй")
    assert g.can_grow(g.get(row["id"]))
    run(g, "долг Оболдуй 300")
    assert g.get(row["id"])["debt"] == 300


def test_forced_dod_even_after_chosen():
    g, _ = seeded()
    assert g.dod(CHAT)[0] == "chosen"
    out = run(g, "пд Назар Базар")
    assert "отменён" in out
    status, row, _ = g.dod(CHAT)
    assert status == "chosen" and row["name"] == "Назар Базар"
    assert g.dod(CHAT)[0] == "already"


def test_announcements_render():
    g, _ = seeded()
    act = A.parse(g, CHAT, "+100 Оболдуй", OWNER)
    assert "12854" in act.announce()
    assert A.parse(g, CHAT, "фарт я", OWNER).announce is None  # подкрутки — только тихо


def test_cleanup_settings_and_queue():
    g, _ = seeded()
    assert A.cleanup_delay(g, "report") == 5
    run(g, "очистка топ 1")
    assert A.cleanup_delay(g, "report") == 1
    g.schedule_cleanup("i:abc", time.time() - 1, inline_id="abc")
    g.schedule_cleanup("i:later", time.time() + 999, inline_id="later")
    assert [r["key"] for r in g.due_cleanups(time.time())] == ["i:abc"]
    g.schedule_cleanup("i:abc", None)
    assert g.due_cleanups(time.time()) == []


def test_migration_adds_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(G.SCHEMA.split("CREATE TABLE IF NOT EXISTS cleanup")[0])
    con.execute("INSERT INTO players (chat, user_id, name, created_at) VALUES ('c', 1, 'x', '2026-01-01')")
    con.commit()
    con.close()
    g = G.Game(str(db))
    row = g.find("c", 1)
    assert row["luck"] == 0 and row["banned_until"] is None
