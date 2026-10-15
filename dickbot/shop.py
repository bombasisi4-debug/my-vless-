"""Магазин за Telegram Stars: префиксы перед ником и пакеты сантиметров."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    key: str
    title: str
    stars: int
    prefix: str | None = None
    cm: int = 0


PREFIXES = [
    Item("crown", "👑 Король", 50, prefix="👑 Король"),
    Item("diamond", "💎 Элита", 40, prefix="💎 Элита"),
    Item("money", "💸 Мажор", 35, prefix="💸 Мажор"),
    Item("goat", "🐐 GOAT", 30, prefix="🐐 GOAT"),
    Item("rocket", "🚀 Ракета", 25, prefix="🚀"),
    Item("grower", "🍆 Гровер", 20, prefix="🍆 Гровер"),
    Item("fire", "🔥 Огонь", 15, prefix="🔥"),
    Item("skull", "☠️ Череп", 15, prefix="☠️"),
    Item("alien", "👽 Пришелец", 10, prefix="👽"),
    Item("snake", "🐍 Змей", 10, prefix="🐍"),
    Item("rainbow", "🌈 Радуга", 10, prefix="🌈"),
    Item("clown", "🤡 Клоун", 5, prefix="🤡"),
]

PACKS = [
    Item("cm50", "+50 см", 5, cm=50),
    Item("cm300", "+300 см", 25, cm=300),
    Item("cm1500", "+1500 см", 100, cm=1500),
]

ITEMS = {i.key: i for i in PREFIXES + PACKS}


def by_number(n: int) -> Item | None:
    """Префикс по номеру из списка (1, 2, …) — для админки."""
    return PREFIXES[n - 1] if 1 <= n <= len(PREFIXES) else None


def catalog_text() -> str:
    lines = ["⭐ <b>Магазин</b>", "", "<b>Префиксы</b> — будут перед твоим ником в топе и в батлах:"]
    lines += [f"{n}. {i.title} — {i.stars}⭐" for n, i in enumerate(PREFIXES, start=1)]
    lines += ["", "<b>Сантиметры</b>:"]
    lines += [f"• {i.title} — {i.stars}⭐" for i in PACKS]
    lines += ["", "Новый префикс заменяет старый. Оплата — звёздами Telegram."]
    return "\n".join(lines)
