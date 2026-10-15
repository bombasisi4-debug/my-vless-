"""Финансовые пирамиды на сантиметрах (в духе МММ).

Основатель открывает пирамиду, люди вкладывают см, каждому обещано +50%.
Выплаты идут строго по очереди и только из новых вкладов. Основатель берёт
комиссию с каждого вклада и может в любой момент сбежать с кассой. Если сутки
никто не вкладывает — пирамида рушится, остаток кассы делят между теми, кто
не дождался выплаты.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass

import texts as T
from game import Game

RETURN = 1.5            # обещанный возврат: вложил 100 — ждёшь 150
FOUNDER_CUT = 0.10      # комиссия основателя с каждого вклада
IDLE_COLLAPSE = 24 * 3600
AMOUNTS = (10, 50, 100, 500)
NAME_MAX = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS pyramids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat TEXT NOT NULL,
    founder INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | scam | collapsed
    fund INTEGER NOT NULL DEFAULT 0,
    paid_out INTEGER NOT NULL DEFAULT 0,
    cut_total INTEGER NOT NULL DEFAULT 0,
    final_amount INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    last_deposit REAL NOT NULL,
    inline_id TEXT,
    events TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS pyramid_deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pyramid INTEGER NOT NULL,
    player INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    promised INTEGER NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
"""


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())[:NAME_MAX]
    return name or "МММ"


@dataclass
class InvestResult:
    error: str | None = None
    toast: str = ""
    payouts: list[tuple[str, int]] | None = None


class Pyramids:
    def __init__(self, game: Game, clock=time.time):
        self.game = game
        self.db = game.db
        self.clock = clock
        self.db.executescript(SCHEMA)

    # --- черновик: инлайн-запрос ещё не знает чат, поэтому имя ждёт нажатия кнопки ---

    def draft(self, user_id: int, name: str) -> str:
        name = clean_name(name)
        token = hashlib.sha1(f"{user_id}:{name}".encode()).hexdigest()[:12]
        self.game.set_setting(f"pyrdraft:{token}", json.dumps({"uid": user_id, "name": name}))
        return token

    def take_draft(self, token: str) -> dict | None:
        raw = self.game.setting(f"pyrdraft:{token}")
        return json.loads(raw) if raw else None

    # --- жизнь пирамиды -----------------------------------------------------

    def get(self, pyr_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM pyramids WHERE id = ?", (pyr_id,)).fetchone()

    def active_of(self, chat: str, founder: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM pyramids WHERE chat = ? AND founder = ? AND status = 'active'", (chat, founder)
        ).fetchone()

    def create(self, chat: str, founder_pid: int, name: str, inline_id: str | None) -> tuple[int | None, str | None]:
        if self.active_of(chat, founder_pid):
            return None, "У тебя уже есть действующая пирамида. Сначала сбеги с кассой 😏"
        now = self.clock()
        cur = self.db.execute(
            "INSERT INTO pyramids (chat, founder, name, created, last_deposit, inline_id) VALUES (?, ?, ?, ?, ?, ?)",
            (chat, founder_pid, clean_name(name), now, now, inline_id),
        )
        return cur.lastrowid, None

    def _event(self, pyr: sqlite3.Row, line: str) -> None:
        events = json.loads(pyr["events"])[-2:] + [line]
        self.db.execute("UPDATE pyramids SET events = ? WHERE id = ?", (json.dumps(events, ensure_ascii=False), pyr["id"]))

    def invest(self, pyr_id: int, investor: sqlite3.Row, amount: int) -> InvestResult:
        pyr = self.get(pyr_id)
        if not pyr or pyr["status"] != "active":
            return InvestResult(error="Пирамида уже закрылась 🏚")
        if investor["id"] == pyr["founder"]:
            return InvestResult(error="Основатель не вкладывается в свою пирамиду — он с неё кормится 😏")
        if investor["length"] < amount:
            return InvestResult(error=f"У тебя всего {investor['length']} см — не хватает на вклад {amount} см")
        promised = int(amount * RETURN)
        cut = round(amount * FOUNDER_CUT)
        now = self.clock()
        self.game.add_length(investor["id"], -amount)
        self.game.add_length(pyr["founder"], cut)
        self.db.execute(
            "INSERT INTO pyramid_deposits (pyramid, player, amount, promised, created) VALUES (?, ?, ?, ?, ?)",
            (pyr_id, investor["id"], amount, promised, now),
        )
        self.db.execute(
            "UPDATE pyramids SET fund = fund + ?, cut_total = cut_total + ?, last_deposit = ? WHERE id = ?",
            (amount - cut, cut, now, pyr_id),
        )
        self._event(self.get(pyr_id), f"{investor['name']} вложил {amount} см")
        payouts = self._pay_queue(pyr_id)
        toast = f"Ты вложил {amount} см в «{pyr['name']}». Обещано {promised} см — жди своей очереди!"
        return InvestResult(toast=toast, payouts=payouts)

    def _pay_queue(self, pyr_id: int) -> list[tuple[str, int]]:
        """Платит самым первым вкладчикам, пока в кассе хватает денег."""
        paid = []
        while True:
            pyr = self.get(pyr_id)
            dep = self.db.execute(
                "SELECT d.*, p.name FROM pyramid_deposits d JOIN players p ON p.id = d.player "
                "WHERE d.pyramid = ? AND d.paid = 0 ORDER BY d.id LIMIT 1", (pyr_id,)
            ).fetchone()
            if not dep or dep["promised"] > pyr["fund"]:
                return paid
            self.game.add_length(dep["player"], dep["promised"])
            self.db.execute("UPDATE pyramid_deposits SET paid = 1 WHERE id = ?", (dep["id"],))
            self.db.execute(
                "UPDATE pyramids SET fund = fund - ?, paid_out = paid_out + ? WHERE id = ?",
                (dep["promised"], dep["promised"], pyr_id),
            )
            self._event(self.get(pyr_id), f"{dep['name']} получил {dep['promised']} см 🤑")
            paid.append((dep["name"], dep["promised"]))

    def run_away(self, pyr_id: int, founder_pid: int) -> tuple[int | None, str | None]:
        pyr = self.get(pyr_id)
        if not pyr or pyr["status"] != "active":
            return None, "Пирамида уже закрылась 🏚"
        if pyr["founder"] != founder_pid:
            return None, "Сбежать с кассой может только основатель 😏"
        self.game.add_length(founder_pid, pyr["fund"])
        self.db.execute(
            "UPDATE pyramids SET status = 'scam', final_amount = fund, fund = 0 WHERE id = ?", (pyr_id,)
        )
        return pyr["fund"], None

    def collapse_idle(self) -> list[int]:
        """Рушит пирамиды без вкладов дольше суток; касса делится между теми, кто
        не дождался выплаты, пропорционально обещанному."""
        cutoff = self.clock() - IDLE_COLLAPSE
        collapsed = []
        for pyr in self.db.execute(
            "SELECT * FROM pyramids WHERE status = 'active' AND last_deposit < ?", (cutoff,)
        ).fetchall():
            unpaid = self.db.execute(
                "SELECT * FROM pyramid_deposits WHERE pyramid = ? AND paid = 0", (pyr["id"],)
            ).fetchall()
            total_promised = sum(d["promised"] for d in unpaid)
            fund = pyr["fund"]
            if total_promised and fund:
                for d in unpaid:
                    self.game.add_length(d["player"], fund * d["promised"] // total_promised)
            self.db.execute(
                "UPDATE pyramids SET status = 'collapsed', final_amount = fund, fund = 0 WHERE id = ?", (pyr["id"],)
            )
            collapsed.append(pyr["id"])
        return collapsed

    # --- отображение -----------------------------------------------------------

    def stats(self, pyr_id: int) -> tuple[int, int, int]:
        """(всего вкладов, ждут выплаты, сколько им обещано)."""
        row = self.db.execute(
            "SELECT COUNT(*) AS n, SUM(paid = 0) AS u, COALESCE(SUM(CASE WHEN paid = 0 THEN promised END), 0) AS s "
            "FROM pyramid_deposits WHERE pyramid = ?", (pyr_id,)
        ).fetchone()
        return row["n"], row["u"] or 0, row["s"]

    def investors_text(self, pyr_id: int) -> str:
        rows = self.db.execute(
            "SELECT p.name, SUM(d.amount) AS a, SUM(CASE WHEN d.paid THEN d.promised ELSE 0 END) AS got "
            "FROM pyramid_deposits d JOIN players p ON p.id = d.player WHERE d.pyramid = ? "
            "GROUP BY d.player ORDER BY a DESC LIMIT 6", (pyr_id,)
        ).fetchall()
        if not rows:
            return "Пока никто не вложился. Будь первым — первым платят!"
        return "\n".join(f"{r['name'][:16]}: вложил {r['a']}, получил {r['got']}" for r in rows)[:190]

    def card(self, pyr_id: int) -> str:
        pyr = self.get(pyr_id)
        founder = self.game.get(pyr["founder"])
        fname = T.b(T.name(founder["name"] if founder else "???"))
        title = T.b(f"Пирамида «{T.name(pyr['name'])}»")
        n, unpaid, owed = self.stats(pyr_id)
        if pyr["status"] == "scam":
            return T.PYR_SCAM.format(title=title, founder=fname, fund=T.b(f"{pyr['final_amount']} см"),
                                     losers=T.b(unpaid), owed=T.b(f"{owed} см"))
        if pyr["status"] == "collapsed":
            return T.PYR_COLLAPSED.format(title=title, fund=T.b(f"{pyr['final_amount']} см"), losers=T.b(unpaid))
        events = "\n".join(f"— {T.name(e)}" for e in json.loads(pyr["events"])) or "— пока тихо…"
        return T.PYR_CARD.format(
            title=title, founder=fname, cut=f"{FOUNDER_CUT:.0%}", ret=f"+{RETURN - 1:.0%}",
            fund=T.b(f"{pyr['fund']} см"), n=T.b(n), unpaid=T.b(unpaid), owed=T.b(f"{owed} см"),
            paid=T.b(f"{pyr['paid_out']} см"), events=events,
        )

    def active_list(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM pyramids WHERE status = 'active' ORDER BY id").fetchall()
