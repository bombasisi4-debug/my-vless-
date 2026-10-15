import base64
import json
import random
import struct
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import game as G  # noqa: E402

GROUP = -1001100294568


def inline_id(chat_id: int, msg: int = 42) -> str:
    """Как в тестах оригинального бота: dc_id, peer, message_id, access_hash."""
    peer = chat_id - G.SUPERGROUP_ID_SHIFT
    return base64.urlsafe_b64encode(struct.pack("<iqiq", 2, peer, msg, 7)).decode().rstrip("=")


def players():
    return json.loads((Path(__file__).resolve().parent.parent / "seed.json").read_text())["players"]


def make():
    day = {"d": date(2026, 10, 15)}
    return G.Game(":memory:", rng=random.Random(1), today=lambda: day["d"]), day


def test_decode_supergroup_id():
    iid = inline_id(GROUP)
    assert len(iid) == 32
    assert G.decode_inline_chat(iid) == str(GROUP)
    assert G.decode_inline_chat(inline_id(-1000001234567)) == "-1000001234567"
    assert G.decode_inline_chat("garbage") is None
    assert G.decode_inline_chat(None) is None
    # обычная группа: в id нет чата (положительное число) — не доверяем
    legacy = base64.urlsafe_b64encode(struct.pack("<iiiq", 2, 5, 68761694, 7)).decode().rstrip("=")
    assert len(legacy) == 27 and G.decode_inline_chat(legacy) is None


def test_split_tops_from_different_chat_instances_merge_into_one():
    """Баг: у каждого участника был свой chat_instance — и свой топ."""
    g, day = make()
    g.seed("ci:OWNER", players(), 1, "pussy", 15000)
    g.grow("ci:FRIEND", 555, "Оболдуй")          # раньше это был отдельный топ
    obol_progress = g.find("ci:FRIEND", 555)["length"]
    g.grow("ci:OTHER", 777, "Новенький")
    # первое нажатие после обновления — из супергруппы
    chat = g.resolve_chat(str(GROUP), "ci-any")
    assert chat == str(GROUP)
    assert [k for k, _ in g.chats()] == [str(GROUP)]
    assert g.find(chat, 1)["length"] == 15000                      # владелец на месте
    assert g.find(chat, 555)["length"] == 12754 + obol_progress    # прогресс прибавился к старому топу
    assert g.find(chat, 777) is not None
    assert len(g.all_players(chat)) == 86
    # дальше все — в один топ, кто бы ни нажимал
    for ci in ("a", "b", "c"):
        assert g.resolve_chat(str(GROUP), ci) == chat
        assert g.resolve_chat(None, ci) == chat   # без id — тоже в главный


def test_legacy_group_without_ids_pins_to_biggest_top():
    g, _ = make()
    g.seed("ci:OWNER", players(), 1, "pussy", 15000)
    g.grow("ci:X", 9, "x")
    main = g.resolve_chat(None, "Y")
    assert main == "ci:OWNER"
    assert [k for k, _ in g.chats()] == ["ci:OWNER"]
    assert g.resolve_chat(None, "Z") == main
    # потом узнали настоящий id — главный топ переезжает на него целиком
    assert g.resolve_chat(str(GROUP), "Z") == str(GROUP)
    assert [k for k, _ in g.chats()] == [str(GROUP)]
    assert g.admin_chat() == str(GROUP)


def test_another_supergroup_keeps_own_top():
    g, _ = make()
    g.seed("ci:OWNER", players(), 1, "pussy", 15000)
    main = g.resolve_chat(str(GROUP), "a")
    other = g.resolve_chat("-1009999999999", "b")
    assert other != main
    g.grow(other, 5, "кто-то")
    assert g.find(main, 5) is None


def test_merge_is_idempotent_for_same_user_twice():
    g, _ = make()
    g.grow("ci:A", 5, "Петя")
    g.grow("ci:B", 5, "Петя")
    a, b = g.find("ci:A", 5)["length"], g.find("ci:B", 5)["length"]
    g.resolve_chat(None, "B")
    rows = g.all_players(g.admin_chat())
    assert len(rows) == 1 and rows[0]["length"] == a + b
