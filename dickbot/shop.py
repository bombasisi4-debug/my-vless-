"""Готовые префиксы перед ником, которые владелец выдаёт через админку
(«префикс я 1», «префикс Оболдуй 5»)."""

from __future__ import annotations

PREFIXES = [
    "👑 Король", "💎 Элита", "💸 Мажор", "🐐 GOAT", "🚀", "🍆 Гровер",
    "🔥", "☠️", "👽", "🐍", "🌈", "🤡",
]


def by_number(n: int) -> str | None:
    return PREFIXES[n - 1] if 1 <= n <= len(PREFIXES) else None


def catalog_text() -> str:
    lines = ["<b>Префиксы</b> (номер для команды <code>префикс &lt;кто&gt; &lt;номер&gt;</code>):"]
    lines += [f"{n}. {p}" for n, p in enumerate(PREFIXES, start=1)]
    lines.append('\nСвой текст — в кавычках: <code>префикс 2 "🦄 Единорог"</code>')
    return "\n".join(lines)
