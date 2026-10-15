"""Админка владельца текстом: в личке боту или в инлайне после @бота.

parse() разбирает фразу и возвращает Action: что будет сделано (для превью),
публичный текст-объявление (если у действия он есть) и функцию, которая всё выполняет.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Callable

import shop
import texts as T
from game import Game, letters_only, normalize_name

NUM = re.compile(r"^[+-]?\d+$")


@dataclass
class Action:
    title: str                                  # что увидит владелец в превью
    run: Callable[[], str] | None = None        # выполнить; возвращает отчёт для лички
    announce: Callable[[], str] | None = None   # текст объявления в чат (после run)
    error: str | None = None
    info_only: bool = False                     # ничего не меняет (инфо, помощь)


def err(text: str) -> Action:
    return Action(title=text, error=text)


def pname(row: sqlite3.Row) -> str:
    return row["name"] or "???"


def place_of(game: Game, chat: str, row: sqlite3.Row) -> int:
    return game.position(chat, row["id"])


def describe(game: Game, chat: str, row: sqlite3.Row) -> str:
    return f"{pname(row)} (#{place_of(game, chat, row)}, {row['length']} см)"


# --- поиск игрока ------------------------------------------------------------

def find_target(game: Game, chat: str, spec: str, owner_id: int) -> tuple[sqlite3.Row | None, str | None]:
    spec = spec.strip()
    if not spec:
        return None, "Не указано, кому"
    low = spec.casefold()
    if low in ("я", "меня", "мне", "себе", "себя", "me"):
        row = game.find(chat, owner_id)
        return (row, None) if row else (None, "Тебя нет в топе этого чата")
    if spec.startswith("@"):
        uname = spec[1:].casefold()
        for r in game.all_players(chat):
            if r["username"] and r["username"].casefold() == uname:
                return r, None
        return None, f"Нет игрока с ником {spec} (бот узнаёт ник, когда человек жмёт его кнопки)"
    if spec.isdigit():
        rows = game.top(chat, int(spec) - 1, 1)
        return (rows[0], None) if rows and int(spec) > 0 else (None, f"В топе нет места #{spec}")
    if len(spec) >= 2 and spec[0] == spec[-1] and spec[0] in "\"'«":
        spec = spec[1:-1]
    elif spec.startswith("«") and spec.endswith("»"):
        spec = spec[1:-1]
    players = game.all_players(chat)
    for key in (normalize_name, letters_only):
        want = key(spec)
        if not want:
            continue
        exact = [r for r in players if key(r["name"]) == want]
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            return None, "Несколько с таким именем, укажи место: " + ", ".join(
                f"#{place_of(game, chat, r)}" for r in exact[:5])
    want = normalize_name(spec)
    part = [r for r in players if want and want in normalize_name(r["name"])]
    if len(part) == 1:
        return part[0], None
    if len(part) > 1:
        return None, "Подходят несколько: " + ", ".join(
            f"{pname(r)} #{place_of(game, chat, r)}" for r in part[:5])
    return None, f"Не нашёл «{spec}»"


# --- команды -------------------------------------------------------------------

CATEGORIES = {
    "notice": ("справки", "Справки и отказы"),
    "report": ("таблички", "Таблички (топ, статистика)"),
    "event": ("результаты", "Результаты (рост, батлы, писюн дня)"),
    "application": ("интерактив", "Интерактив (вызовы, кредит, «Кликни!»)"),
}
CATEGORY_ALIASES = {
    "справки": "notice", "отказы": "notice", "ошибки": "notice",
    "таблички": "report", "топ": "report", "статистика": "report", "стата": "report",
    "результаты": "event", "события": "event", "рост": "event",
    "интерактив": "application", "вызовы": "application", "кнопки": "application",
}
DEFAULT_DELAYS = {"notice": 2, "report": 5, "event": 0, "application": 60}


def cleanup_delay(game: Game, cat: str) -> int:
    v = game.setting(f"clean_{cat}")
    return int(v) if v is not None else DEFAULT_DELAYS[cat]


def cleanup_status(game: Game) -> str:
    lines = ["Самоочистка (через сколько минут сообщение превращается в 🧹):"]
    for cat, (alias, title) in CATEGORIES.items():
        d = cleanup_delay(game, cat)
        lines.append(f"— {title}: " + (f"{d} мин" if d else "навсегда") + f"  [{alias}]")
    lines.append("\nПоменять: <code>очистка топ 5</code>, 0 — не чистить.")
    return "\n".join(lines)


def split_number(rest: str, last: bool = True) -> tuple[str, int | None]:
    parts = rest.split()
    if not parts:
        return rest, None
    tok = parts[-1] if last else parts[0]
    if NUM.match(tok) and len(parts) > 1:
        body = parts[:-1] if last else parts[1:]
        return " ".join(body), int(tok)
    return rest, None


def parse(game: Game, chat: str | None, text: str, owner_id: int) -> Action | None:
    """None — это не админская фраза."""
    text = " ".join(text.split())
    if not text:
        return None
    first, _, rest = text.partition(" ")
    cmd = first.casefold()

    if cmd in ("админ", "admin", "помощь", "команды"):
        return Action(title="Справка по админке", info_only=True, run=lambda: T.ADMIN_HELP)

    if cmd in ("очистка", "чистка"):
        if not rest:
            return Action(title="Настройки самоочистки", info_only=True, run=lambda: cleanup_status(game))
        what, minutes = split_number(rest)
        cat = CATEGORY_ALIASES.get(what.casefold())
        if not cat or minutes is None or minutes < 0:
            return err("Формат: очистка топ 5 (категории: справки, топ, результаты, вызовы)")
        title = CATEGORIES[cat][1]

        def run_clean() -> str:
            game.set_setting(f"clean_{cat}", str(minutes))
            return cleanup_status(game)
        return Action(title=f"🧹 {title}: " + (f"{minutes} мин" if minutes else "не чистить"), run=run_clean)

    if cmd in ("префиксы", "скины"):
        return Action(title="Список префиксов", info_only=True, run=shop.catalog_text)

    if cmd in ("звезды", "звёзды", "донаты"):
        def run_stars() -> str:
            total, rows = game.payments()
            lines = [f"⭐ Всего задонатили: <b>{total}</b>"]
            for r in rows:
                item = shop.ITEMS.get(r["item"])
                mark = " (возвращено)" if r["refunded"] else ""
                lines.append(f"— {T.name(r['user_name'])}: {item.title if item else r['item']} за {r['stars']}⭐{mark}\n"
                             f"  <code>{r['charge_id']}</code>")
            return "\n".join(lines)
        return Action(title="Донаты", info_only=True, run=run_stars)

    if chat is None:
        return err("Пока нет ни одного чата с игроками")

    if cmd in ("префикс", "скин"):
        return parse_prefix(game, chat, rest, owner_id)

    if cmd in ("налог", "налоги", "tax"):
        return parse_tax(game, chat, rest)

    def target(spec: str) -> tuple[sqlite3.Row | None, Action | None]:
        row, e = find_target(game, chat, spec, owner_id)
        return row, (err(e) if e else None)

    # +100 Оболдуй / -50 3
    if NUM.match(first) and first[0] in "+-":
        delta = int(first)
        row, e = target(rest)
        if e:
            return e
        new = row["length"] + delta

        def run_add() -> str:
            game.add_length(row["id"], delta)
            return f"{T.name(pname(row))}: {row['length']} → {new} см"

        def ann_add() -> str:
            tmpl = T.ANN_ADD if delta >= 0 else T.ANN_SUB
            return tmpl.format(name=T.b(T.name(pname(row))), k=T.b(abs(delta)), length=T.b(f"{new} см"))
        return Action(title=f"{delta:+d} см → {describe(game, chat, row)} = {new} см", run=run_add, announce=ann_add)

    if cmd in ("см", "set", "сантиметры", "длина"):
        spec, value = split_number(rest)
        if value is None:
            return err("Формат: см <кто> <число>, например: см Оболдуй 100")
        row, e = target(spec)
        if e:
            return e

        def run_set() -> str:
            game.set_length(row["id"], value)
            return f"{T.name(pname(row))}: {row['length']} → {value} см"
        return Action(title=f"{describe(game, chat, row)} → {value} см", run=run_set,
                      announce=lambda: T.ANN_SET.format(name=T.b(T.name(pname(row))), length=T.b(f"{value} см")))

    if cmd in ("ноль", "обнулить", "обнуль"):
        row, e = target(rest)
        if e:
            return e

        def run_zero() -> str:
            game.set_length(row["id"], 0)
            return f"{T.name(pname(row))}: {row['length']} → 0 см"
        return Action(title=f"Обнулить {describe(game, chat, row)}", run=run_zero,
                      announce=lambda: T.ANN_ZERO.format(name=T.b(T.name(pname(row)))))

    if cmd in ("обмен", "поменять", "свап", "swap"):
        halves = re.split(r"\s+(?:и|<->|\|)\s+|\s*\|\s*", rest, maxsplit=1)
        if len(halves) != 2:
            return err("Формат: обмен <кто> и <кто>, например: обмен 2 и 5")
        a, e = target(halves[0])
        if e:
            return e
        b, e = target(halves[1])
        if e:
            return e
        if a["id"] == b["id"]:
            return err("Это один и тот же человек")

        def run_swap() -> str:
            game.swap(a["id"], b["id"])
            return f"{T.name(pname(a))}: {b['length']} см, {T.name(pname(b))}: {a['length']} см"
        return Action(title=f"Поменять {describe(game, chat, a)} ⇄ {describe(game, chat, b)}", run=run_swap,
                      announce=lambda: T.ANN_SWAP.format(a=T.b(T.name(pname(a))), b=T.b(T.name(pname(b)))))

    if cmd in ("фарт", "везунчик", "лох", "неудачник", "честно", "снять", "простить"):
        row, e = target(rest)
        if e:
            return e
        if cmd in ("фарт", "везунчик"):
            luck, what = 1, "всегда выигрывает батлы"
        elif cmd in ("лох", "неудачник"):
            luck, what = -1, "всегда проигрывает батлы"
        else:
            luck, what = 0, "снова честная игра: без фарта, проклятий, благословений и бана"

        def run_luck() -> str:
            game.set_luck(row["id"], luck)
            if luck == 0:
                game.set_mode(row["id"], None)
                game.unban(row["id"])
            return f"{T.name(pname(row))}: {what}"
        return Action(title=f"{pname(row)}: {what}", run=run_luck)

    if cmd in ("проклясть", "проклятие", "благословить", "благословение", "бан", "забанить"):
        spec, days = split_number(rest)
        days = days if days and days > 0 else (1 if cmd.startswith(("бан", "забан")) else 3)
        row, e = target(spec)
        if e:
            return e
        word = f"{days} {T.days_word(days)}"
        nm = T.b(T.name(pname(row)))
        if cmd.startswith("прокл"):
            def run_curse() -> str:
                game.set_mode(row["id"], "curse", days)
                return f"{T.name(pname(row))}: проклят на {word} (растёт только в минус)"
            return Action(title=f"🧿 Проклясть {describe(game, chat, row)} на {word}", run=run_curse,
                          announce=lambda: T.ANN_CURSE.format(name=nm, days=word))
        if cmd.startswith("благосл"):
            def run_bless() -> str:
                game.set_mode(row["id"], "bless", days)
                return f"{T.name(pname(row))}: благословлён на {word} (+7…+10 см каждый день)"
            return Action(title=f"😇 Благословить {describe(game, chat, row)} на {word}", run=run_bless,
                          announce=lambda: T.ANN_BLESS.format(name=nm, days=word))

        def run_ban() -> str:
            until = game.ban(row["id"], days)
            return f"{T.name(pname(row))}: бан до {until:%d.%m.%Y} включительно"
        return Action(title=f"🚫 Бан {describe(game, chat, row)} на {word}", run=run_ban,
                      announce=lambda: T.ANN_BAN.format(name=nm, days=word))

    if cmd in ("разбан", "разбанить"):
        row, e = target(rest)
        if e:
            return e

        def run_unban() -> str:
            game.unban(row["id"])
            return f"{T.name(pname(row))}: разбанен"
        return Action(title=f"Разбанить {describe(game, chat, row)}", run=run_unban)

    if cmd in ("откат", "сброс", "ещё", "еще"):
        row, e = target(rest)
        if e:
            return e

        def run_reset() -> str:
            game.reset_cooldown(row["id"])
            return f"{T.name(pname(row))}: может растить ещё раз сегодня"
        return Action(title=f"⏪ Ещё попытка сегодня: {describe(game, chat, row)}", run=run_reset,
                      announce=lambda: T.ANN_RESET.format(name=T.b(T.name(pname(row)))))

    if cmd in ("долг", "кредит"):
        spec, value = split_number(rest)
        if value is None:
            return err("Формат: долг <кто> <см>, 0 — списать долг")
        row, e = target(spec)
        if e:
            return e

        def run_debt() -> str:
            game.set_debt(row["id"], value)
            return f"{T.name(pname(row))}: долг {max(value, 0)} см"
        return Action(title=f"💸 Долг {describe(game, chat, row)} = {max(value, 0)} см", run=run_debt,
                      announce=lambda: T.ANN_DEBT.format(name=T.b(T.name(pname(row))), k=T.b(f"{max(value, 0)} см")))

    if cmd in ("дод", "пд", "писюн", "писюндня"):
        spec = rest
        if spec.casefold().startswith("дня "):
            spec = spec[4:]
        row, e = target(spec)
        if e:
            return e

        def run_dod() -> str:
            redo = game.force_dod(chat, row["id"])
            tail = " Сегодняшний выбор отменён — можно выбирать заново." if redo else ""
            return f"Следующим Писюном Дня будет {T.name(pname(row))}.{tail}"
        return Action(title=f"🔮 Подкрутить Писюна Дня: {describe(game, chat, row)}", run=run_dod)

    if cmd in ("инфо", "кто", "info"):
        row, e = target(rest)
        if e:
            return e
        return Action(title=f"ℹ️ {describe(game, chat, row)}", info_only=True,
                      run=lambda: player_card(game, chat, row))

    if cmd in ("топ", "top"):
        return Action(title="Топ чата", info_only=True, run=lambda: "")

    return None


def parse_prefix(game: Game, chat: str, rest: str, owner_id: int) -> Action:
    """префикс <кто> <номер | "свой текст" | эмодзи | ->"""
    rest = rest.strip()
    m = re.match(r'^(.+?)\s+["«](.+)["»]$', rest)
    if m:
        spec, prefix = m.group(1), m.group(2).strip()
    else:
        spec, _, last = rest.rpartition(" ")
        if not spec:
            return err('Формат: префикс <кто> <номер> — например: префикс я 1, или префикс 2 "🦄 Единорог"')
        if last in ("-", "снять", "нет", "убрать"):
            prefix = None
        elif last.isdigit():
            item = shop.by_number(int(last))
            if not item:
                return err(f"Нет префикса №{last}. Список: префиксы")
            prefix = item.prefix
        elif not any(ch.isalnum() for ch in last):
            prefix = last  # одиночный эмодзи
        else:
            return err('Свой текст — в кавычках: префикс 2 "🦄 Единорог"')
    row, e = find_target(game, chat, spec, owner_id)
    if e:
        return err(e)
    shown = prefix or "без префикса"

    def run_prefix() -> str:
        game.set_prefix(row["id"], prefix)
        return f"{T.name(pname(row))}: {T.name(shown)}"
    gift = row["user_id"] != owner_id and prefix
    return Action(
        title=f"🎁 {pname(row)} → {shown}" if gift else f"{pname(row)} → {shown}", run=run_prefix,
        announce=(lambda: T.ANN_PREFIX.format(name=T.b(T.name(pname(row))), prefix=T.b(T.name(prefix))))
        if gift else None,
    )


TAX_AMOUNT = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(%|см)?$")


def tax_amount(token: str) -> tuple[float | None, int | None] | None:
    """'10%' -> (10.0, None), '5' / '5см' -> (None, 5)."""
    m = TAX_AMOUNT.match(token.replace(" ", ""))
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    if m.group(2) == "%":
        return (value, None) if 0 < value <= 100 else None
    return (None, int(value)) if value >= 1 else None


def tax_what(amount: tuple[float | None, int | None]) -> str:
    pct, cm = amount
    return f"{pct:g}%" if pct is not None else f"{cm} см"


def parse_tax(game: Game, chat: str, rest: str) -> Action:
    words = rest.casefold().split()
    daily, grow = game.tax_status(chat)
    if not words:
        lines = ["Налоги сейчас:",
                 "— ежедневный: " + (daily or "нет"),
                 "— с роста: " + (f"{grow}%" if grow else "нет")]
        return Action(title=" | ".join(lines), info_only=True, run=lambda: "\n".join(lines))
    if words[0] in ("стоп", "отмена", "отменить", "выкл", "нет", "0"):
        def run_off() -> str:
            game.set_daily_tax(chat, None)
            game.set_growth_tax(chat, None)
            return "Налоги отменены: ежедневного и налога с роста больше нет."
        return Action(title="Отменить все налоги", run=run_off, announce=lambda: T.ANN_TAX_OFF)

    burn = words[-1] in ("сжечь", "сжигать", "сгорает", "burn")
    if burn:
        words = words[:-1]
    joined = " ".join(words)
    m = re.match(r"^(каждый день|ежедневно|ежедневный|в день|daily)\s+(.+)$", joined)
    if m:
        amount = tax_amount(m.group(2))
        if not amount:
            return err("Формат: налог каждый день 2% или налог каждый день 3")
        what = tax_what(amount)

        def run_daily() -> str:
            game.set_daily_tax(chat, what.replace(" см", ""))
            return f"Ежедневный налог {what}: первый сбор завтра, всё уходит в казну (тебе)."
        return Action(title=f"🏛 Ежедневный налог {what} со всех", run=run_daily,
                      announce=lambda: T.ANN_TAX_DAILY.format(what=T.b(what)))
    m = re.match(r"^(с роста|на рост|рост|с прироста)\s+(.+)$", joined)
    if m:
        amount = tax_amount(m.group(2))
        if not amount or amount[0] is None:
            return err("Налог с роста — только в процентах: налог с роста 20%")
        pct = amount[0]

        def run_grow() -> str:
            game.set_growth_tax(chat, pct)
            return f"Налог с роста {pct:g}%: забирается с каждого прироста и уходит в казну (тебе)."
        return Action(title=f"🏛 Налог с роста {pct:g}%", run=run_grow,
                      announce=lambda: T.ANN_TAX_GROW.format(what=T.b(f"{pct:g}%")))

    amount = tax_amount(joined)
    if not amount:
        return err("Формат: налог 10% / налог 5 / налог каждый день 2% / налог с роста 20% / налог стоп")
    pct, cm = amount
    what = tax_what(amount)
    # прикидка для превью: сколько соберём
    treasury_owner = game.owner_lookup()
    estimate = sum(
        (round(r["length"] * pct / 100) if pct is not None else min(cm, r["length"]))
        for r in game.all_players(chat) if r["length"] > 0 and r["user_id"] != treasury_owner
    )
    dest = "сгорит" if burn else "в казну"
    result: dict[str, int] = {}

    def run_once() -> str:
        payers, total = game.tax_once(chat, percent=pct, cm=cm, burn=burn)
        result["total"] = total
        return f"Налог {what}: заплатили {payers} чел., собрано {total} см ({dest})."

    def ann_once() -> str:
        tmpl = T.ANN_TAX_BURN if burn else T.ANN_TAX
        return tmpl.format(what=T.b(what), total=T.b(f"{result.get('total', estimate)} см"))
    return Action(title=f"🏛 Налог {what} со всех ≈ {estimate} см {dest}", run=run_once, announce=ann_once)


def player_card(game: Game, chat: str, row: sqlite3.Row) -> str:
    row = game.get(row["id"])
    lines = [
        f"<b>{T.name(pname(row))}</b> — #{place_of(game, chat, row)}, {row['length']} см",
        f"Telegram: " + (f"id {row['user_id']}" if row["user_id"] else "ещё не заходил (запись из старого топа)")
        + (f", @{row['username']}" if row["username"] else ""),
        f"Последний рост: {row['grown_at'] or '—'}, серия {row['streak']} (макс {row['streak_max']})",
        f"Батлы: {row['wins']}/{row['battles']}, долг: {row['debt']} см",
    ]
    if row["luck"]:
        lines.append("🤫 " + ("фарт: всегда выигрывает" if row["luck"] > 0 else "лох: всегда проигрывает"))
    mode = game.active_mode(row)
    if mode:
        lines.append(("🧿 проклят" if mode == "curse" else "😇 благословлён") + f" до {row['mode_until']}")
    until = game.banned_until(row)
    if until:
        lines.append(f"🚫 бан до {until:%d.%m.%Y}")
    return "\n".join(lines)
