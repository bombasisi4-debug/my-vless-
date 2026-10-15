"""Игровая логика и хранилище (SQLite). Без зависимостей от Telegram,
чтобы всё можно было проверить тестами."""

from __future__ import annotations

import base64
import random
import re
import sqlite3
import struct
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from texts import days_word

# Параметры по умолчанию оригинального бота (.env.example DickGrowerBot).
GROWTH_MIN = -5
GROWTH_MAX = 10
GROW_SHRINK_RATIO = 0.5
NEWCOMERS_GRACE_DAYS = 7
DOD_BONUS_MAX = 5
INACTIVITY_DAYS = 7
HELP_PUSSIES_COEF = 0.01
LOAN_PAYOUT_COEF = 0.1
STREAK_GRADES = (2, 4, 8, 16, 32)
DOD_RICH_EXCLUSION_RATIO = 0.1
PVP_DEFAULT_BET = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat TEXT NOT NULL,
    user_id INTEGER,
    name TEXT NOT NULL,
    length INTEGER NOT NULL DEFAULT 0,
    grown_at TEXT,
    created_at TEXT NOT NULL,
    streak INTEGER NOT NULL DEFAULT 0,
    streak_max INTEGER NOT NULL DEFAULT 0,
    debt INTEGER NOT NULL DEFAULT 0,
    battles INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    win_streak INTEGER NOT NULL DEFAULT 0,
    win_streak_max INTEGER NOT NULL DEFAULT 0,
    acquired INTEGER NOT NULL DEFAULT 0,
    lost INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS players_chat_user ON players(chat, user_id);
CREATE TABLE IF NOT EXISTS dod (
    chat TEXT NOT NULL,
    day TEXT NOT NULL,
    player_id INTEGER NOT NULL,
    PRIMARY KEY (chat, day)
);
CREATE TABLE IF NOT EXISTS chat_instances (
    chat_instance TEXT PRIMARY KEY,
    chat TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS battles (
    id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cleanup (
    key TEXT PRIMARY KEY,
    inline_id TEXT,
    chat_id INTEGER,
    message_id INTEGER,
    due REAL NOT NULL
);
"""

# Колонки, добавленные после первой версии: старые базы догоняются ALTER TABLE.
NEW_PLAYER_COLUMNS = {
    "username": "TEXT",
    "luck": "INTEGER NOT NULL DEFAULT 0",       # 1 — всегда выигрывает батлы, -1 — всегда проигрывает
    "mode": "TEXT",                             # 'curse' | 'bless'
    "mode_until": "TEXT",
    "banned_until": "TEXT",
    "prefix": "TEXT",                           # донатный префикс перед ником
}

# Латиница и кириллица, которые выглядят одинаково: «aaa» в топе может быть
# набрано любыми из них.
_HOMOGLYPHS = str.maketrans("аеорсухкмтвнАЕОРСУХКМТВН", "aeopcyxkmtbhaeopcyxkmtbh")


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold().translate(_HOMOGLYPHS)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")  # невидимые символы
    return " ".join(s.split())


def letters_only(s: str) -> str:
    """Имя без украшений: только латиница, кириллица и цифры."""
    return "".join(
        ch for ch in normalize_name(s)
        if ch.isdigit() or unicodedata.name(ch, "").startswith(("LATIN", "CYRILLIC"))
    )


SUPERGROUP_ID_SHIFT = -1_000_000_000_000


def decode_inline_chat(inline_message_id: str | None) -> str | None:
    """Настоящий id супергруппы, зашитый в inline_message_id (так делает и оригинальный бот).
    Для обычных групп и личек Telegram его туда не кладёт — тогда None."""
    if not inline_message_id:
        return None
    try:
        raw = base64.urlsafe_b64decode(inline_message_id + "=" * (-len(inline_message_id) % 4))
        if len(raw) == 24:
            _dc, peer, _msg, _hash = struct.unpack("<iqiq", raw)
        elif len(raw) == 20:
            _dc, _msg, peer, _hash = struct.unpack("<iiiq", raw)
        else:
            return None
    except (ValueError, struct.error):
        return None
    return str(SUPERGROUP_ID_SHIFT + peer) if peer < 0 else None


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def time_till_next_day(now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    mins = int((nxt - now).total_seconds() // 60)
    return mins // 60, mins % 60


def base_increment(rng: random.Random, newcomer: bool) -> int:
    positive = newcomer or rng.random() < GROW_SHRINK_RATIO
    return rng.randint(1, GROWTH_MAX) if positive else rng.randint(GROWTH_MIN, -1)


def streak_bonus(streak: int) -> tuple[int, int | None]:
    """Бонус перка «день за днём» и день, с которого он вырастет."""
    bonus = sum(1 for g in STREAK_GRADES if streak >= g)
    nxt = next((g for g in STREAK_GRADES if g > streak), None)
    return bonus, nxt


def help_pussies_bonus(length: int) -> int:
    return round(HELP_PUSSIES_COEF * abs(length)) if length < 0 else 0


def loan_payout(base: int, debt: int) -> int:
    return min(round(LOAN_PAYOUT_COEF * base), debt) if base > 0 and debt > 0 else 0


@dataclass
class Perk:
    key: str
    change: int
    note: str | None = None


@dataclass
class GrowResult:
    ok: bool
    total: int = 0
    length: int = 0
    pos: int = 0
    perks: list[Perk] = field(default_factory=list)


@dataclass
class BattleResult:
    winner: sqlite3.Row
    loser: sqlite3.Row
    bet: int
    withheld: int
    winner_pos: int
    loser_pos: int
    lost_win_streak: int


class Game:
    def __init__(self, path: str, rng: random.Random | None = None, today=utc_today):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # в WAL это безопасно и убирает fsync на каждую запись — на медленном диске хостинга
        # именно они подвешивали бота на секунды
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(players)")}
        for col, ddl in NEW_PLAYER_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE players ADD COLUMN {col} {ddl}")
        self.rng = rng or random.SystemRandom()
        self.today = today
        # кто владелец (он же казна для налогов); bot.py подменяет на свою функцию
        self.owner_lookup = lambda: int(self.setting("owner") or 0)

    # --- настройки ---------------------------------------------------------

    def setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO settings VALUES (?, ?) ON CONFLICT DO UPDATE SET value = excluded.value", (key, value)
        )

    # --- чаты ---------------------------------------------------------------

    def remember_chat_instance(self, chat_instance: str, chat: str) -> None:
        self.db.execute(
            "INSERT INTO chat_instances VALUES (?, ?) ON CONFLICT DO UPDATE SET chat = excluded.chat",
            (chat_instance, chat),
        )

    def chat_for_instance(self, chat_instance: str) -> str:
        row = self.db.execute(
            "SELECT chat FROM chat_instances WHERE chat_instance = ?", (chat_instance,)
        ).fetchone()
        return row["chat"] if row else f"ci:{chat_instance}"

    # --- игроки -------------------------------------------------------------

    def get(self, pid: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()

    def find(self, chat: str, user_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM players WHERE chat = ? AND user_id = ?", (chat, user_id)
        ).fetchone()

    def _unclaimed_by_name(self, chat: str, full_name: str) -> sqlite3.Row | None:
        rows = self.db.execute(
            "SELECT * FROM players WHERE chat = ? AND user_id IS NULL ORDER BY length DESC", (chat,)
        ).fetchall()
        for key in (normalize_name, letters_only):
            wanted = key(full_name)
            if not wanted:
                continue
            matches = [r for r in rows if key(r["name"]) == wanted]
            if len(matches) == 1:
                return matches[0]
        return None

    def player(self, chat: str, user_id: int, full_name: str, create: bool) -> sqlite3.Row | None:
        """Находит игрока; при первом заходе забирает его запись из импортированного
        топа по имени; если create — заводит нового."""
        row = self.find(chat, user_id)
        if row:
            if row["name"] != full_name:
                self.db.execute("UPDATE players SET name = ? WHERE id = ?", (full_name, row["id"]))
            return self.get(row["id"])
        seeded = self._unclaimed_by_name(chat, full_name)
        if seeded:
            self.db.execute(
                "UPDATE players SET user_id = ?, name = ? WHERE id = ?",
                (user_id, full_name, seeded["id"]),
            )
            return self.get(seeded["id"])
        if not create:
            return None
        cur = self.db.execute(
            "INSERT INTO players (chat, user_id, name, created_at) VALUES (?, ?, ?, ?)",
            (chat, user_id, full_name, self.today().isoformat()),
        )
        return self.get(cur.lastrowid)

    def position(self, chat: str, pid: int) -> int:
        row = self.get(pid)
        return 1 + self.db.execute(
            "SELECT COUNT(*) FROM players WHERE chat = ? AND (length > ? OR (length = ? AND id < ?))",
            (chat, row["length"], row["length"], pid),
        ).fetchone()[0]

    def top(self, chat: str, offset: int, limit: int) -> list[sqlite3.Row]:
        cutoff = (self.today() - timedelta(days=INACTIVITY_DAYS)).isoformat()
        return self.db.execute(
            "SELECT * FROM players WHERE chat = ? "
            "AND NOT (length = 0 AND (grown_at IS NULL OR grown_at < ?)) "
            "ORDER BY length DESC, id LIMIT ? OFFSET ?",
            (chat, cutoff, limit, offset),
        ).fetchall()

    def is_inactive(self, row: sqlite3.Row) -> bool:
        if not row["grown_at"]:
            return True
        return (self.today() - date.fromisoformat(row["grown_at"])).days > INACTIVITY_DAYS

    def can_grow(self, row: sqlite3.Row) -> bool:
        return not row["grown_at"] or date.fromisoformat(row["grown_at"]) < self.today()

    # --- рост ---------------------------------------------------------------

    def _apply_perks(self, row: sqlite3.Row, base: int, growth: bool) -> tuple[int, list[Perk], dict]:
        perks, updates = [], {}
        hp = help_pussies_bonus(row["length"])
        if hp:
            perks.append(Perk("help-pussies", hp))
        payout = loan_payout(base, row["debt"])
        if payout:
            perks.append(Perk("loan-payout", -payout))
            updates["debt"] = row["debt"] - payout
        if growth:
            last = date.fromisoformat(row["grown_at"]) if row["grown_at"] else None
            streak = row["streak"] + 1 if last == self.today() - timedelta(days=1) else 1
            updates["streak"] = streak
            updates["streak_max"] = max(row["streak_max"], streak)
            bonus, nxt = streak_bonus(streak)
            note = f"{streak} {days_word(streak)} подряд"
            if nxt:
                note += f"; +{bonus + 1} с {nxt}-го дня"
            if bonus:
                perks.append(Perk("streak", bonus, note))
            tax = self._growth_tax(row, base)
            if tax:
                pct = self.setting("tax_grow:" + row["chat"])
                perks.append(Perk("tax", -tax, f"{pct}% в казну"))
        total = base + sum(p.change for p in perks)
        return total, perks, updates

    def _update(self, pid: int, **fields) -> None:
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE players SET {cols} WHERE id = ?", (*fields.values(), pid))

    def grow(self, chat: str, user_id: int, full_name: str) -> GrowResult:
        row = self.player(chat, user_id, full_name, create=True)
        if not self.can_grow(row):
            return GrowResult(ok=False, length=row["length"])
        days = (self.today() - date.fromisoformat(row["created_at"])).days
        mode = self.active_mode(row)
        if mode == "curse":
            base = self.rng.randint(GROWTH_MIN, -1)
        elif mode == "bless":
            base = self.rng.randint(GROWTH_MAX - 3, GROWTH_MAX)
        else:
            base = base_increment(self.rng, newcomer=days <= NEWCOMERS_GRACE_DAYS)
        total, perks, updates = self._apply_perks(row, base, growth=True)
        self._update(row["id"], length=row["length"] + total, grown_at=self.today().isoformat(), **updates)
        return GrowResult(True, total, row["length"] + total, self.position(chat, row["id"]), perks)

    # --- писюн дня ----------------------------------------------------------

    def dod(self, chat: str) -> tuple[str, sqlite3.Row | None, GrowResult | None]:
        """('already', игрок, None) | ('none', None, None) | ('chosen', игрок, результат)"""
        day = self.today().isoformat()
        prev = self.db.execute("SELECT player_id FROM dod WHERE chat = ? AND day = ?", (chat, day)).fetchone()
        if prev:
            return "already", self.get(prev["player_id"]), None
        cutoff = (self.today() - timedelta(days=INACTIVITY_DAYS)).isoformat()
        active = self.db.execute(
            "SELECT * FROM players WHERE chat = ? AND user_id IS NOT NULL AND grown_at >= ? "
            "ORDER BY length DESC",
            (chat, cutoff),
        ).fetchall()
        if not active and not self.setting(f"force_dod:{chat}"):
            return "none", None, None
        excluded = int(len(active) * DOD_RICH_EXCLUSION_RATIO)
        winner = self.rng.choice(active[excluded:] or active) if active else None
        forced = self.setting(f"force_dod:{chat}")
        if forced:
            self.db.execute("DELETE FROM settings WHERE key = ?", (f"force_dod:{chat}",))
            winner = self.get(int(forced)) or winner
        if winner is None:
            return "none", None, None
        base = self.rng.randint(1, DOD_BONUS_MAX)
        total, perks, updates = self._apply_perks(winner, base, growth=False)
        self._update(winner["id"], length=winner["length"] + total, **updates)
        self.db.execute("INSERT INTO dod VALUES (?, ?, ?)", (chat, day, winner["id"]))
        res = GrowResult(True, total, winner["length"] + total, self.position(chat, winner["id"]), perks)
        return "chosen", self.get(winner["id"]), res

    # --- батлы --------------------------------------------------------------

    def has_enough(self, chat: str, user_id: int, bet: int) -> bool:
        """Ставка 0 — бой «на интерес», доступен всем, даже кто в минусе."""
        row = self.find(chat, user_id)
        return bool(row) and (bet == 0 or row["length"] >= bet)

    def battle(self, chat: str, battle_id: str, initiator: sqlite3.Row, acceptor: sqlite3.Row,
               bet: int) -> BattleResult | None:
        """None — бой с таким id уже был."""
        try:
            self.db.execute("INSERT INTO battles VALUES (?)", (battle_id,))
        except sqlite3.IntegrityError:
            return None
        li, la = initiator["luck"], acceptor["luck"]
        if li != la:
            initiator_wins = li > la
        else:
            initiator_wins = self.rng.random() < 0.5
        winner, loser = (initiator, acceptor) if initiator_wins else (acceptor, initiator)
        withheld = loan_payout(bet, winner["debt"])
        lost_streak = loser["win_streak"]
        self._update(
            winner["id"], length=winner["length"] + bet - withheld, debt=winner["debt"] - withheld,
            battles=winner["battles"] + 1, wins=winner["wins"] + 1,
            win_streak=winner["win_streak"] + 1,
            win_streak_max=max(winner["win_streak_max"], winner["win_streak"] + 1),
            acquired=winner["acquired"] + bet,
        )
        self._update(
            loser["id"], length=loser["length"] - bet, battles=loser["battles"] + 1,
            win_streak=0, lost=loser["lost"] + bet,
        )
        return BattleResult(
            self.get(winner["id"]), self.get(loser["id"]), bet, withheld,
            self.position(chat, winner["id"]), self.position(chat, loser["id"]), lost_streak,
        )

    # --- кредит -------------------------------------------------------------

    def take_loan(self, pid: int) -> bool:
        row = self.get(pid)
        if row["length"] >= 0:
            return False
        self._update(pid, debt=row["debt"] - row["length"], length=0)
        return True

    # --- админка ------------------------------------------------------------

    def seed(self, chat: str, players: list[dict], owner_id: int, owner_name: str,
             owner_length: int, force: bool = False) -> int:
        """Загружает топ, переписанный из оригинального бота. Возвращает число игроков."""
        exists = self.db.execute("SELECT COUNT(*) FROM players WHERE chat = ?", (chat,)).fetchone()[0]
        if exists and not force:
            raise ValueError("в этом чате уже есть игроки")
        today = self.today()
        marks = {"": today, "+": today - timedelta(days=1), "~": today - timedelta(days=INACTIVITY_DAYS + 3)}
        old = (today - timedelta(days=365)).isoformat()
        self.db.execute("BEGIN")
        try:
            self.db.execute("DELETE FROM players WHERE chat = ?", (chat,))
            self.db.execute("DELETE FROM dod WHERE chat = ?", (chat,))
            self.db.execute(
                "INSERT INTO players (chat, user_id, name, length, grown_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chat, owner_id, owner_name, owner_length, marks["+"].isoformat(), old),
            )
            n = 1
            for p in players:
                if p.get("owner") or normalize_name(p["name"]) == normalize_name(owner_name):
                    continue
                self.db.execute(
                    "INSERT INTO players (chat, user_id, name, length, grown_at, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
                    (chat, p["name"], int(p["length"]), marks[p.get("mark", "")].isoformat(), old),
                )
                n += 1
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return n

    def link(self, chat: str, target: str, user_id: int, full_name: str) -> sqlite3.Row | None:
        """Привязывает запись топа (по месту или имени) к пользователю.
        Его собственная новая запись, если была, удаляется."""
        if target.isdigit():
            rows = self.top(chat, int(target) - 1, 1)
            row = rows[0] if rows else None
        else:
            row = self.db.execute(
                "SELECT * FROM players WHERE chat = ? AND name = ?", (chat, target)
            ).fetchone()
            if not row:
                cands = [r for r in self.db.execute("SELECT * FROM players WHERE chat = ?", (chat,))
                         if normalize_name(r["name"]) == normalize_name(target)]
                row = cands[0] if len(cands) == 1 else None
        if not row:
            return None
        own = self.find(chat, user_id)
        if own and own["id"] != row["id"]:
            self.db.execute("DELETE FROM players WHERE id = ?", (own["id"],))
        self._update(row["id"], user_id=user_id, name=full_name)
        return self.get(row["id"])

    def set_length(self, pid: int, length: int) -> None:
        self._update(pid, length=length)

    # --- подкрутки владельца -------------------------------------------------

    def active_mode(self, row: sqlite3.Row) -> str | None:
        if row["mode"] and row["mode_until"] and date.fromisoformat(row["mode_until"]) >= self.today():
            return row["mode"]
        return None

    def banned_until(self, row: sqlite3.Row | None) -> date | None:
        if row is not None and row["banned_until"]:
            until = date.fromisoformat(row["banned_until"])
            if until >= self.today():
                return until
        return None

    def touch_username(self, chat: str, user_id: int, username: str | None) -> None:
        self.db.execute(
            "UPDATE players SET username = ? WHERE chat = ? AND user_id = ? AND username IS NOT ?",
            (username, chat, user_id, username),
        )

    def add_length(self, pid: int, delta: int) -> None:
        self.db.execute("UPDATE players SET length = length + ? WHERE id = ?", (delta, pid))

    def swap(self, a: int, b: int) -> None:
        la, lb = self.get(a)["length"], self.get(b)["length"]
        self._update(a, length=lb)
        self._update(b, length=la)

    def set_luck(self, pid: int, luck: int) -> None:
        self._update(pid, luck=luck)

    def set_mode(self, pid: int, mode: str | None, days: int = 0) -> None:
        until = (self.today() + timedelta(days=max(days, 1))).isoformat() if mode else None
        self._update(pid, mode=mode, mode_until=until)

    def reset_cooldown(self, pid: int) -> None:
        row = self.get(pid)
        if row["grown_at"] and date.fromisoformat(row["grown_at"]) >= self.today():
            self._update(pid, grown_at=(self.today() - timedelta(days=1)).isoformat())

    def ban(self, pid: int, days: int) -> date:
        until = self.today() + timedelta(days=max(days, 1) - 1)
        self._update(pid, banned_until=until.isoformat())
        return until

    def unban(self, pid: int) -> None:
        self._update(pid, banned_until=None)

    def set_debt(self, pid: int, debt: int) -> None:
        self._update(pid, debt=max(debt, 0))

    def force_dod(self, chat: str, pid: int) -> bool:
        """Следующий Писюн Дня будет этим игроком. True — если сегодняшний выбор
        пришлось отменить, чтобы его можно было запустить заново."""
        self.set_setting(f"force_dod:{chat}", str(pid))
        cur = self.db.execute("DELETE FROM dod WHERE chat = ? AND day = ?", (chat, self.today().isoformat()))
        return cur.rowcount > 0

    def chats(self) -> list[tuple[str, int]]:
        return [(r["chat"], r["n"]) for r in self.db.execute(
            "SELECT chat, COUNT(*) AS n FROM players GROUP BY chat ORDER BY n DESC"
        )]

    def admin_chat(self) -> str | None:
        """Чат, к которому применяются команды владельца: последний, где он нажимал
        кнопки бота, иначе самый большой."""
        chosen = self.setting("admin_chat") or self.setting("main_chat")
        if chosen:
            return chosen
        chats = self.chats()
        return chats[0][0] if chats else None

    def all_players(self, chat: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM players WHERE chat = ? ORDER BY length DESC, id", (chat,)).fetchall()

    # --- самоочистка сообщений ---------------------------------------------

    def schedule_cleanup(self, key: str, due: float | None, inline_id: str | None = None,
                         chat_id: int | None = None, message_id: int | None = None) -> None:
        """due=None — сообщение остаётся навсегда (и снимается с очереди)."""
        if due is None:
            self.db.execute("DELETE FROM cleanup WHERE key = ?", (key,))
            return
        self.db.execute(
            "INSERT INTO cleanup VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET due = excluded.due",
            (key, inline_id, chat_id, message_id, due),
        )

    def due_cleanups(self, now: float, limit: int = 30) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM cleanup WHERE due <= ? ORDER BY due LIMIT ?", (now, limit)
        ).fetchall()

    def cleanup_done(self, key: str) -> None:
        self.db.execute("DELETE FROM cleanup WHERE key = ?", (key,))

    # --- один топ на чат -----------------------------------------------------

    def resolve_chat(self, real: str | None, chat_instance: str | None) -> str:
        """Ключ чата для действия. real — настоящий id группы, если он известен
        (из сообщения или из inline_message_id); chat_instance — то, что Telegram
        прислал в нажатии кнопки.

        Всё, что нельзя привязать к настоящему id, идёт в главный чат, а при первой
        возможности все разъехавшиеся по chat_instance топы сливаются в один."""
        main = self.setting("main_chat")
        if main is None:
            biggest = next((k for k, _ in self.chats() if k.startswith("ci:")), None)
            base = real or biggest or f"ci:{chat_instance}"
            self.consolidate(base)
            self.set_setting("main_chat", base)
            main = base
        elif real and real != main and main.startswith("ci:"):
            # главный чат знали только по chat_instance — теперь известен его настоящий id
            self.consolidate(real)
            self.set_setting("main_chat", real)
            main = real
        if real:
            if chat_instance:
                self.remember_chat_instance(chat_instance, real)
            return real
        return main

    def consolidate(self, base: str) -> list[str]:
        """Сливает все топы, привязанные к chat_instance, в base. Самый большой из них
        становится основой, если в base ещё пусто. Возвращает слитые ключи."""
        keys = [k for k, _ in self.chats() if k.startswith("ci:") and k != base]
        has_base = self.db.execute("SELECT 1 FROM players WHERE chat = ? LIMIT 1", (base,)).fetchone()
        for k in keys:
            self.merge_chat(k, base) if has_base else self._rename_chat(k, base)
            has_base = True
        if self.setting("admin_chat") in keys:
            self.db.execute("DELETE FROM settings WHERE key = 'admin_chat'")
        return keys

    def _rename_chat(self, src: str, dst: str) -> None:
        self.db.execute("UPDATE players SET chat = ? WHERE chat = ?", (dst, src))
        self._move_chat_extras(src, dst)

    def _move_chat_extras(self, src: str, dst: str) -> None:
        self.db.execute("DELETE FROM dod WHERE chat = ?", (src,))
        self.db.execute("UPDATE chat_instances SET chat = ? WHERE chat = ?", (dst, src))
        self.db.execute(
            "UPDATE OR IGNORE settings SET key = ? WHERE key = ?", (f"force_dod:{dst}", f"force_dod:{src}")
        )

    def merge_chat(self, src: str, dst: str) -> None:
        """Переносит игроков src в dst. Если человек уже есть в dst (или там лежит его
        запись из старого топа), его сантиметры и статистика из src прибавляются."""
        if src == dst:
            return
        self.db.execute("BEGIN")
        try:
            for r in self.db.execute("SELECT * FROM players WHERE chat = ?", (src,)).fetchall():
                if r["user_id"] is not None:
                    t = self.find(dst, r["user_id"]) or self._unclaimed_by_name(dst, r["name"])
                    if t is None:
                        self._update(r["id"], chat=dst)
                        continue
                    self.db.execute("DELETE FROM players WHERE id = ?", (r["id"],))
                    self._update(
                        t["id"], user_id=r["user_id"], name=r["name"],
                        username=r["username"] or t["username"],
                        length=t["length"] + r["length"],
                        grown_at=max(filter(None, [t["grown_at"], r["grown_at"]]), default=None),
                        streak=max(t["streak"], r["streak"]), streak_max=max(t["streak_max"], r["streak_max"]),
                        debt=t["debt"] + r["debt"], battles=t["battles"] + r["battles"], wins=t["wins"] + r["wins"],
                        win_streak=max(t["win_streak"], r["win_streak"]),
                        win_streak_max=max(t["win_streak_max"], r["win_streak_max"]),
                        acquired=t["acquired"] + r["acquired"], lost=t["lost"] + r["lost"],
                        luck=t["luck"] or r["luck"], mode=t["mode"] or r["mode"],
                        mode_until=t["mode_until"] or r["mode_until"],
                        banned_until=t["banned_until"] or r["banned_until"],
                    )
                else:
                    same = any(normalize_name(x["name"]) == normalize_name(r["name"])
                               for x in self.db.execute("SELECT name FROM players WHERE chat = ?", (dst,)))
                    if same:
                        self.db.execute("DELETE FROM players WHERE id = ?", (r["id"],))
                    else:
                        self._update(r["id"], chat=dst)
            self._move_chat_extras(src, dst)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # --- налоги ------------------------------------------------------------------

    def _treasury(self, chat: str) -> sqlite3.Row | None:
        owner = self.owner_lookup()
        return self.find(chat, owner) if owner else None

    def _growth_tax(self, row: sqlite3.Row, base: int) -> int:
        """Сколько см забрать с прироста (и отдать в казну)."""
        pct = self.setting(f"tax_grow:{row['chat']}")
        if not pct or base <= 0 or row["user_id"] == self.owner_lookup():
            return 0
        tax = round(base * float(pct) / 100)
        treasury = self._treasury(row["chat"])
        if tax and treasury:
            self.add_length(treasury["id"], tax)
        return tax

    def tax_once(self, chat: str, percent: float | None = None, cm: int | None = None,
                 burn: bool = False) -> tuple[int, int]:
        """Списывает налог со всех, у кого длина больше нуля (кроме владельца).
        percent — процент от длины, cm — фиксированная сумма (но не больше длины).
        Возвращает (сколько человек заплатили, сколько см собрано)."""
        treasury = self._treasury(chat)
        payers, total = 0, 0
        for r in self.db.execute("SELECT id, length FROM players WHERE chat = ? AND length > 0", (chat,)).fetchall():
            if treasury and r["id"] == treasury["id"]:
                continue
            cut = round(r["length"] * percent / 100) if percent is not None else min(cm or 0, r["length"])
            if cut <= 0:
                continue
            self.add_length(r["id"], -cut)
            payers, total = payers + 1, total + cut
        if total and treasury and not burn:
            self.add_length(treasury["id"], total)
        return payers, total

    def set_daily_tax(self, chat: str, spec: str | None) -> None:
        """spec: '2%' или '3' (см); None — отменить. Первый сбор — завтра."""
        if spec is None:
            self.db.execute("DELETE FROM settings WHERE key = ?", (f"tax_daily:{chat}",))
            return
        self.set_setting(f"tax_daily:{chat}", spec)
        self.set_setting(f"tax_daily_last:{chat}", self.today().isoformat())

    def set_growth_tax(self, chat: str, percent: float | None) -> None:
        if percent is None:
            self.db.execute("DELETE FROM settings WHERE key = ?", (f"tax_grow:{chat}",))
        else:
            self.set_setting(f"tax_grow:{chat}", f"{percent:g}")

    def apply_daily_taxes(self) -> list[tuple[str, int, int]]:
        """Собирает ежедневные налоги, если сегодня ещё не собирали."""
        done = []
        today = self.today().isoformat()
        for r in self.db.execute("SELECT key, value FROM settings WHERE key LIKE 'tax_daily:%'").fetchall():
            chat = r["key"].split(":", 1)[1]
            if (self.setting(f"tax_daily_last:{chat}") or "") >= today:
                continue
            spec = r["value"]
            if spec.endswith("%"):
                payers, total = self.tax_once(chat, percent=float(spec[:-1]))
            else:
                payers, total = self.tax_once(chat, cm=int(spec))
            self.set_setting(f"tax_daily_last:{chat}", today)
            done.append((chat, payers, total))
        return done

    def tax_status(self, chat: str) -> tuple[str | None, str | None]:
        return self.setting(f"tax_daily:{chat}"), self.setting(f"tax_grow:{chat}")

    # --- префиксы ------------------------------------------------------------

    def set_prefix(self, pid: int, prefix: str | None) -> None:
        self._update(pid, prefix=(prefix or None) and prefix.strip()[:24])

