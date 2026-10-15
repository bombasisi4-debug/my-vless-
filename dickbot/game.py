"""Игровая логика и хранилище (SQLite). Без зависимостей от Telegram,
чтобы всё можно было проверить тестами."""

from __future__ import annotations

import random
import re
import sqlite3
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
"""

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
        self.db.executescript(SCHEMA)
        self.rng = rng or random.SystemRandom()
        self.today = today

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
        if not active:
            return "none", None, None
        excluded = int(len(active) * DOD_RICH_EXCLUSION_RATIO)
        winner = self.rng.choice(active[excluded:] or active)
        base = self.rng.randint(1, DOD_BONUS_MAX)
        total, perks, updates = self._apply_perks(winner, base, growth=False)
        self._update(winner["id"], length=winner["length"] + total, **updates)
        self.db.execute("INSERT INTO dod VALUES (?, ?, ?)", (chat, day, winner["id"]))
        res = GrowResult(True, total, winner["length"] + total, self.position(chat, winner["id"]), perks)
        return "chosen", self.get(winner["id"]), res

    # --- батлы --------------------------------------------------------------

    def has_enough(self, chat: str, user_id: int, bet: int) -> bool:
        row = self.find(chat, user_id)
        return bool(row) and row["length"] >= bet

    def battle(self, chat: str, battle_id: str, initiator: sqlite3.Row, acceptor: sqlite3.Row,
               bet: int) -> BattleResult | None:
        """None — бой с таким id уже был."""
        try:
            self.db.execute("INSERT INTO battles VALUES (?)", (battle_id,))
        except sqlite3.IntegrityError:
            return None
        if self.rng.random() < 0.5:
            winner, loser = initiator, acceptor
        else:
            winner, loser = acceptor, initiator
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
