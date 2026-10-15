"""Клон @DickGrowerBot на aiogram 3. Запуск: python bot.py (нужна переменная BOT_TOKEN)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, CallbackQuery, ChosenInlineResult, InlineKeyboardButton,
    InlineKeyboardMarkup, InlineQuery, InlineQueryResultArticle, InlineQueryResultsButton,
    InputTextMessageContent, Message, User,
)

import admin
import texts as T
from game import INACTIVITY_DAYS, LOAN_PAYOUT_COEF, Game, GrowResult, time_till_next_day

HERE = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """Подхватывает .env рядом с ботом, если он есть (переменные окружения важнее)."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


def find_token(raw: str) -> str | None:
    """Достаёт токен, даже если в панели его вставили с кавычками, пробелами
    или целой строкой вида BOT_TOKEN=..."""
    m = TOKEN_RE.search(raw or "")
    return m.group(0) if m else None


load_env_file(HERE / ".env")
OWNER_ID = int(os.environ.get("OWNER_ID") or 0)
OWNER_LENGTH = int(os.environ.get("OWNER_LENGTH") or 0)
TOP_LIMIT = int(os.environ.get("TOP_LIMIT") or 30)
DB_PATH = os.environ.get("DB_PATH") or str(HERE / "data" / "bot.db")
SEED_FILE = Path(os.environ.get("SEED_FILE") or HERE / "seed.json")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
game = Game(DB_PATH)
router = Router()
BOT_USERNAME = "bot"


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def is_group(msg: Message) -> bool:
    return msg.chat.type in ("group", "supergroup")


# --- ответы (общие для команд и инлайна) -------------------------------------

def perks_part(res: GrowResult) -> str:
    if not res.perks:
        return ""
    lines = []
    for p in res.perks:
        inner = f"{p.change:+d}" + (f", {p.note}" if p.note else "")
        lines.append(f"— {T.PERK_NAMES[p.key]} ({inner})")
    return f"\n\n{T.PERKS_TOP_LINE}:\n" + "\n".join(lines)


# Ответ: текст, кнопки и категория самоочистки (notice/report/event/application, как в оригинале).
Reply = "tuple[str, InlineKeyboardMarkup | None, str]"


def render_grow(chat: str, user: User) -> Reply:
    res = game.grow(chat, user.id, user.full_name)
    if not res.ok:
        return T.GROW_TOMORROW + T.time_till_next_day(*time_till_next_day()), None, "notice"
    event = T.GROWN if res.total >= 0 else T.SHRUNK
    text = T.GROW_RESULT.format(event=event, incr=T.b(abs(res.total)), length=T.b(f"{res.length} см"))
    text += "\n" + T.POSITION.format(pos=T.b(res.pos))
    return text + perks_part(res) + T.time_till_next_day(*time_till_next_day()), None, "event"


def render_top(chat: str, page: int) -> Reply:
    rows = game.top(chat, page * TOP_LIMIT, TOP_LIMIT + 1)
    more = len(rows) > TOP_LIMIT
    rows = rows[:TOP_LIMIT]
    if not rows:
        return T.TOP_EMPTY, None, "notice"
    lines, any_inactive = [], False
    for i, r in enumerate(rows, start=page * TOP_LIMIT + 1):
        line = T.TOP_LINE.format(n=i, name=T.name(r["name"]), length=r["length"])
        if game.is_inactive(r):
            any_inactive = True
            line += " [~]"
        elif game.can_grow(r):
            line += " [+]"
        lines.append(line)
    text = f"{T.TOP_TITLE}\n\n" + "\n".join(lines) + f"\n\n{T.TOP_ENDING}"
    if any_inactive:
        text += "\n" + T.TOP_ENDING_INACTIVE.format(days=INACTIVITY_DAYS, word=T.days_word(INACTIVITY_DAYS))
    buttons = []
    if page > 0:
        buttons.append(("⬅️", f"top:{page - 1}"))
    if more:
        buttons.append(("➡️", f"top:{page + 1}"))
    return text, (kb(buttons) if buttons else None), "report"


def render_dod(chat: str) -> Reply:
    status, row, res = game.dod(chat)
    if status == "none":
        return T.DOD_NO_CANDIDATES, None, "notice"
    if status == "already":
        return T.DOD_ALREADY.format(name=T.b(T.name(row["name"]))), None, "notice"
    who = T.mention(row["user_id"], row["name"]) if row["user_id"] else T.b(T.name(row["name"]))
    text = T.DOD_RESULT.format(mention=who, growth=T.b(f"{res.total} см"), length=T.b(res.length))
    return text + "\n\n" + T.DOD_POSITION.format(pos=T.b(res.pos)) + perks_part(res), None, "event"


def render_stats(chat: str, user: User) -> Reply:
    row = game.player(chat, user.id, user.full_name, create=False)
    if not row:
        return T.STATS_NO_DICK, None, "notice"
    parts = [T.STATS_LENGTH.format(length=T.b(f"{row['length']} см"), pos=T.b(game.position(chat, row["id"])))]
    if row["streak_max"]:
        parts.append(T.STATS_STREAK.format(cur=T.b(row["streak"]), max=T.b(row["streak_max"])))
    wr = f"{row['wins'] * 100 / row['battles']:.2f}%" if row["battles"] else "0.00%"
    parts.append(T.STATS_PVP.format(
        wr=T.b(wr), battles=T.b(row["battles"]), wins=T.b(row["wins"]), ws=T.b(row["win_streak_max"]),
        acq=T.b(row["acquired"]), lost=T.b(row["lost"]),
    ))
    if row["debt"]:
        parts.append(T.LOAN_DEBT.format(debt=T.b(f"{row['debt']} см")))
    return "\n\n".join(parts), None, "report"


def render_loan(chat: str, user: User) -> Reply:
    row = game.player(chat, user.id, user.full_name, create=False)
    if row and row["debt"]:
        return T.LOAN_DEBT.format(debt=T.b(f"{row['debt']} см")), None, "notice"
    if not row or row["length"] >= 0:
        return T.LOAN_POSITIVE, None, "notice"
    text = T.LOAN_CONFIRM.format(pct=T.b(f"{LOAN_PAYOUT_COEF:.0%}"), debt=T.b(f"{-row['length']} см"))
    markup = kb([(T.LOAN_AGREE, f"loan:{user.id}:y"), (T.LOAN_DISAGREE, f"loan:{user.id}:n")])
    return text, markup, "application"


def banned_text(chat: str, user: User) -> str | None:
    until = game.banned_until(game.player(chat, user.id, user.full_name, create=False))
    return T.BANNED.format(date=f"{until:%d.%m.%Y}") if until else None


def pvp_offer(user: User, bet: int) -> tuple[str, InlineKeyboardMarkup]:
    text = T.PVP_START.format(name=T.b(T.name(user.full_name)), bet=T.b(f"{bet} см"))
    return text, kb([(T.PVP_BUTTON, f"pvp:{user.id}:{bet}:{int(time.time())}")])


def render_battle(chat: str, data: str, acceptor: User) -> tuple[str | None, str | None]:
    """(новый текст, None) или (None, текст всплывашки с ошибкой)."""
    _, uid, bet, _ts = data.split(":")
    uid, bet = int(uid), int(bet)
    if uid == acceptor.id:
        return None, T.PVP_SAME_PERSON
    ban = banned_text(chat, acceptor)
    if ban:
        return None, ban
    initiator = game.find(chat, uid)
    if not initiator or initiator["length"] < bet:
        return None, T.PVP_NOT_ENOUGH_INITIATOR
    acc = game.player(chat, acceptor.id, acceptor.full_name, create=False)
    if not acc:
        return None, T.PVP_NO_DICK
    res = game.battle(chat, f"{chat}|{data}", initiator, acc, bet)
    if res is None:
        return None, T.PVP_ALREADY_DONE
    w, l = res.winner, res.loser
    text = T.PVP_FINISH.format(
        winner=T.b(T.name(w["name"])), wl=T.b(f"{w['length']} см"), ll=T.b(f"{l['length']} см"),
        bet=T.b(f"{bet} см"),
    )
    text += "\n\n" + T.PVP_POS.format(name=T.b(T.name(w["name"])), pos=T.b(res.winner_pos))
    text += "\n" + T.PVP_POS.format(name=T.b(T.name(l["name"])), pos=T.b(res.loser_pos))
    if res.withheld:
        text += "\n\n" + T.PVP_WITHHELD.format(payout=T.b(f"{res.withheld} см"))
    wr = lambda r: T.b(f"{r['wins'] * 100 / r['battles']:.2f}%")
    text += "\n\n" + T.PVP_STATS.format(
        wr=wr(w), ws=T.b(w["win_streak"]), wsm=T.b(w["win_streak_max"]), lr=wr(l)
    )
    if res.lost_win_streak > 1:
        text += "\n" + T.PVP_LOST_STREAK.format(n=T.b(res.lost_win_streak))
    return text, None


# --- самоочистка ---------------------------------------------------------------

def cleanup_due(cat: str) -> float | None:
    minutes = admin.cleanup_delay(game, cat)
    return time.time() + minutes * 60 if minutes else None


def schedule_inline(inline_id: str, cat: str) -> None:
    game.schedule_cleanup(f"i:{inline_id}", cleanup_due(cat), inline_id=inline_id)


def schedule_message(msg: Message | None, cat: str) -> None:
    if msg is not None:
        game.schedule_cleanup(f"m:{msg.chat.id}:{msg.message_id}", cleanup_due(cat),
                              chat_id=msg.chat.id, message_id=msg.message_id)


async def cleanup_worker(bot: Bot) -> None:
    """Раз в 5 секунд превращает просроченные сообщения в «🧹 вычищено»: инлайн-сообщения
    можно только отредактировать, обычные — удаляются (а если нельзя — тоже редактируются)."""
    while True:
        try:
            for row in game.due_cleanups(time.time()):
                try:
                    if row["inline_id"]:
                        await bot.edit_message_text(text=T.CLEANED, inline_message_id=row["inline_id"])
                    else:
                        try:
                            await bot.delete_message(row["chat_id"], row["message_id"])
                        except Exception:
                            await bot.edit_message_text(text=T.CLEANED, chat_id=row["chat_id"],
                                                        message_id=row["message_id"])
                except Exception as e:  # сообщение уже удалено/не изменилось — просто забываем
                    logging.debug("cleanup %s: %s", row["key"], e)
                game.cleanup_done(row["key"])
        except Exception:
            logging.exception("Ошибка в самоочистке")
        await asyncio.sleep(5)


# --- команды (если бота всё-таки добавят в чат) --------------------------------------

async def group_guard(msg: Message) -> bool:
    if not is_group(msg):
        await msg.reply(T.NOT_GROUP)
        return False
    if msg.sender_chat or not msg.from_user:
        await msg.reply(T.GROUP_ACCOUNT)
        return False
    ban = banned_text(str(msg.chat.id), msg.from_user)
    if ban:
        schedule_message(await msg.reply(ban), "notice")
        return False
    game.touch_username(str(msg.chat.id), msg.from_user.id, msg.from_user.username)
    return True


async def send_reply(msg: Message, reply: Reply) -> None:
    text, markup, cat = reply
    schedule_message(await msg.reply(text, reply_markup=markup), cat)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    text = T.HELP.format(bot=BOT_USERNAME)
    if msg.chat.type == "private" and msg.from_user:
        text += f"\n\nТвой Telegram ID: <code>{msg.from_user.id}</code>"
        if not owner_id():
            game.set_setting("owner", str(msg.from_user.id))
            logging.info("Владелец бота: %s (%s)", msg.from_user.id, msg.from_user.full_name)
            text += "\n\n👑 Ты первым написал боту — теперь ты его владелец."
        if is_owner(msg.from_user):
            await msg.answer(text)
            await msg.answer(T.ADMIN_HELP.format(bot=BOT_USERNAME))
            return
    sent = await msg.reply(text)
    if is_group(msg):
        schedule_message(sent, "notice")


@router.message(Command("myid"))
async def cmd_myid(msg: Message) -> None:
    await msg.reply(f"Твой Telegram ID: <code>{msg.from_user.id}</code>")


@router.message(Command("grow"))
async def cmd_grow(msg: Message) -> None:
    if await group_guard(msg):
        await send_reply(msg, render_grow(str(msg.chat.id), msg.from_user))


@router.message(Command("top"))
async def cmd_top(msg: Message) -> None:
    if await group_guard(msg):
        await send_reply(msg, render_top(str(msg.chat.id), 0))


@router.message(Command("dod", "dick_of_day"))
async def cmd_dod(msg: Message) -> None:
    if await group_guard(msg):
        await send_reply(msg, render_dod(str(msg.chat.id)))


@router.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    if await group_guard(msg):
        await send_reply(msg, render_stats(str(msg.chat.id), msg.from_user))


@router.message(Command("loan"))
async def cmd_loan(msg: Message) -> None:
    if await group_guard(msg):
        await send_reply(msg, render_loan(str(msg.chat.id), msg.from_user))


@router.message(Command("pvp", "battle", "attack", "fight"))
async def cmd_pvp(msg: Message, command: CommandObject) -> None:
    if not await group_guard(msg):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) <= 0:
        await send_reply(msg, (T.PVP_NO_ARGS, None, "notice"))
        return
    chat, bet = str(msg.chat.id), int(arg)
    game.player(chat, msg.from_user.id, msg.from_user.full_name, create=False)
    if not game.has_enough(chat, msg.from_user.id, bet):
        await send_reply(msg, (T.PVP_NOT_ENOUGH_INITIATOR, None, "notice"))
        return
    text, markup = pvp_offer(msg.from_user, bet)
    schedule_message(await msg.answer(text, reply_markup=markup), "application")


@router.message(Command("activate"))
async def cmd_activate(msg: Message) -> None:
    if is_group(msg):
        await msg.answer(T.ACTIVATE_TEXT.format(bot=BOT_USERNAME), reply_markup=kb([(T.ACTIVATE_BUTTON, "act")]))


# --- владелец ------------------------------------------------------------------------

def owner_id() -> int:
    """OWNER_ID из окружения, иначе тот, кто первым написал боту /start в личку."""
    return OWNER_ID or int(game.setting("owner") or 0)


def is_owner(user: User | None) -> bool:
    return bool(user) and owner_id() != 0 and user.id == owner_id()


def do_seed(chat: str, owner: User, force: bool) -> str:
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    length = OWNER_LENGTH or int(data.get("owner_length") or 0)
    try:
        n = game.seed(chat, data["players"], owner.id, owner.full_name, length, force=force)
    except ValueError as e:
        return f"Не загрузил: {e}. Чтобы перезаписать — /seed force (или инлайн <code>seed force</code>)."
    game.set_setting("admin_chat", chat)
    return f"Топ загружен: {n} игроков. Ты на первом месте с {T.b(f'{length} см')} 😎"


@router.message(Command("seed"))
async def cmd_seed(msg: Message, command: CommandObject) -> None:
    if not is_owner(msg.from_user):
        await msg.reply(T.OWNER_ONLY)
        return
    if not is_group(msg):
        await msg.reply(T.NOT_GROUP)
        return
    await msg.reply(do_seed(str(msg.chat.id), msg.from_user, (command.args or "").strip() == "force"))
    await cmd_activate(msg)


@router.message(Command("link"))
async def cmd_link(msg: Message, command: CommandObject) -> None:
    """Ответом на сообщение человека: /link <место в топе или имя> — отдаёт ему эту запись."""
    if not is_owner(msg.from_user):
        await msg.reply(T.OWNER_ONLY)
        return
    target_user = msg.reply_to_message.from_user if msg.reply_to_message else None
    arg = (command.args or "").strip()
    if not is_group(msg) or not target_user or not arg:
        await msg.reply("Ответь на сообщение человека: <code>/link 15</code> или <code>/link Назар</code>")
        return
    row = game.link(str(msg.chat.id), arg, target_user.id, target_user.full_name)
    if not row:
        await msg.reply("Не нашёл такую запись в топе.")
        return
    length = row["length"]
    await msg.reply(f"Готово: {T.b(T.name(row['name']))} теперь {T.b(f'{length} см')}.")


@router.message(Command("setcm"))
async def cmd_setcm(msg: Message, command: CommandObject) -> None:
    """/setcm N — себе, или ответом на сообщение — другому."""
    if not is_owner(msg.from_user):
        await msg.reply(T.OWNER_ONLY)
        return
    arg = (command.args or "").strip()
    if not is_group(msg) or not arg.lstrip("-").isdigit():
        await msg.reply("Использование: <code>/setcm 100</code> (можно ответом на сообщение человека)")
        return
    who = msg.reply_to_message.from_user if msg.reply_to_message else msg.from_user
    row = game.player(str(msg.chat.id), who.id, who.full_name, create=True)
    game.set_length(row["id"], int(arg))
    await msg.reply(f"{T.b(T.name(who.full_name))}: теперь {T.b(f'{int(arg)} см')}.")


def chats_text() -> str:
    current = game.admin_chat()
    lines = ["Чаты с игроками:"]
    for i, (chat, n) in enumerate(game.chats(), start=1):
        mark = " ← сейчас управляешь этим" if chat == current else ""
        lines.append(f"{i}. чат {i} — {n} игроков{mark}")
    lines.append("\nПереключиться: <code>чат 2</code>")
    return "\n".join(lines) if len(lines) > 2 else "Пока нет ни одного чата с игроками."


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def dm_admin(msg: Message) -> None:
    """Админка в личке: всё то же, что в инлайне, но без следов в чате."""
    if not is_owner(msg.from_user):
        await msg.reply(T.HELP.format(bot=BOT_USERNAME))
        return
    text = msg.text.strip()
    low = text.casefold()
    if low in ("чаты", "чат"):
        await msg.reply(chats_text())
        return
    if low.startswith("чат ") and low[4:].strip().isdigit():
        chats = game.chats()
        n = int(low[4:].strip())
        if 1 <= n <= len(chats):
            game.set_setting("admin_chat", chats[n - 1][0])
        await msg.reply(chats_text())
        return
    if low in ("топ", "top"):
        chat = game.admin_chat()
        await msg.reply(render_top(chat, 0)[0] if chat else "Пока нет ни одного чата с игроками.")
        return
    act = admin.parse(game, game.admin_chat(), text, msg.from_user.id)
    if act is None:
        await msg.reply("Не понял 🤔 Напиши <code>админ</code> — покажу все команды.")
    elif act.error:
        await msg.reply("⚠️ " + T.name(act.error))
    else:
        await msg.reply(act.run().replace("{bot}", BOT_USERNAME) if act.run else T.name(act.title))


# --- инлайн --------------------------------------------------------------------

PENDING: dict[str, tuple[admin.Action, float]] = {}


def article(rid: str, title: str, text: str, markup: InlineKeyboardMarkup | None = None,
            description: str | None = None) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=rid, title=title[:200], description=description,
        input_message_content=InputTextMessageContent(message_text=text), reply_markup=markup,
    )


def admin_results(act: admin.Action) -> list[InlineQueryResultArticle]:
    if act.error:
        return [article("noop", "⚠️ " + act.error, T.CLEANED, description="Ничего не изменится")]
    if act.info_only:
        info = re.sub(r"<[^>]+>", "", act.run() if act.run else "")[:250] if act.title != "Топ чата" else ""
        return [article("noop", act.title, T.CLEANED, description=info or "Ничего не изменится")]
    now = time.time()
    for k in [k for k, (_, ts) in PENDING.items() if now - ts > 600]:
        PENDING.pop(k, None)
    key = secrets.token_hex(4)
    PENDING[key] = (act, now)
    results = [article(f"as:{key}", "🤫 " + act.title, T.CLEANED,
                       description="Тихо: в чате будет только «🧹 Это сообщение вычищено…»")]
    if act.announce:
        results.append(article(f"ap:{key}", "📢 " + act.title, act.announce(),
                               description="С объявлением в чате"))
    return results


@router.inline_query()
async def inline(query: InlineQuery) -> None:
    q, u = query.query.strip(), query.from_user
    button = None
    if is_owner(u):
        button = InlineQueryResultsButton(text="⚙️ Админка (в личке никто не видит)", start_parameter="admin")
        act = admin.parse(game, game.admin_chat(), q, u.id)
        if act is not None:
            await query.answer(admin_results(act), cache_time=0, is_personal=True, button=button)
            return
    results = []
    if q.isdigit() and int(q) > 0:
        text, markup = pvp_offer(u, int(q))
        results.append(article("pvp", T.INLINE_PVP_TITLE.format(bet=int(q)), text, markup))
    if q in ("seed", "seed force") and is_owner(u):
        flag = "f" if q.endswith("force") else "n"
        results.append(article("seed", "Загрузить топ в этот чат", "Загрузка топа…",
                               kb([("Загрузить", f"seed:{u.id}:{flag}")])))
    for kind, title in T.INLINE_TITLES.items():
        results.append(article(kind, title, T.INLINE_TEXT, kb([(T.INLINE_BUTTON, f"in:{kind}:{u.id}")])))
    await query.answer(results, cache_time=0, is_personal=True, button=button)


@router.chosen_inline_result()
async def inline_chosen(res: ChosenInlineResult) -> None:
    """Приходит, если в @BotFather включён /setinlinefeedback."""
    rid = res.result_id
    if rid[:3] in ("as:", "ap:"):
        item = PENDING.pop(rid[3:], None)
        if item and is_owner(res.from_user) and item[0].run:
            logging.info("Админ: %s", item[0].run())
        return
    if res.inline_message_id and rid != "noop":
        schedule_inline(res.inline_message_id, "application")


# --- кнопки --------------------------------------------------------------------

def callback_chat(cq: CallbackQuery) -> str:
    if cq.message and cq.message.chat:
        chat = str(cq.message.chat.id)
        if cq.chat_instance:
            game.remember_chat_instance(cq.chat_instance, chat)
    else:
        chat = game.chat_for_instance(cq.chat_instance)
    game.touch_username(chat, cq.from_user.id, cq.from_user.username)
    if is_owner(cq.from_user) and game.find(chat, cq.from_user.id):
        game.set_setting("admin_chat", chat)
    return chat


async def edit(cq: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None,
               cat: str = "event") -> None:
    if cq.inline_message_id:
        await cq.bot.edit_message_text(text=text, inline_message_id=cq.inline_message_id, reply_markup=markup)
        schedule_inline(cq.inline_message_id, cat)
    elif cq.message:
        await cq.message.edit_text(text, reply_markup=markup)
        schedule_message(cq.message, cat)


@router.callback_query(F.data == "act")
async def cb_activate(cq: CallbackQuery) -> None:
    callback_chat(cq)
    await cq.answer(T.ACTIVATED)
    await edit(cq, T.ACTIVATED, cat="notice")


@router.callback_query(F.data.startswith("top:"))
async def cb_top(cq: CallbackQuery) -> None:
    text, markup, cat = render_top(callback_chat(cq), int(cq.data.split(":")[1]))
    await edit(cq, text, markup, cat)
    await cq.answer()


@router.callback_query(F.data.startswith("pvp:"))
async def cb_pvp(cq: CallbackQuery) -> None:
    text, alert = render_battle(callback_chat(cq), cq.data, cq.from_user)
    if alert:
        await cq.answer(alert, show_alert=True)
        return
    await cq.answer()
    await edit(cq, text, cat="event")


@router.callback_query(F.data.startswith("loan:"))
async def cb_loan(cq: CallbackQuery) -> None:
    _, uid, answer = cq.data.split(":")
    if int(uid) != cq.from_user.id:
        await cq.answer(T.ANOTHER_USER, show_alert=True)
        return
    chat = callback_chat(cq)
    if answer != "y":
        await edit(cq, T.LOAN_REFUSED, cat="event")
    else:
        row = game.player(chat, cq.from_user.id, cq.from_user.full_name, create=False)
        ok = row is not None and not row["debt"] and game.take_loan(row["id"])
        await edit(cq, T.LOAN_SUCCESS if ok else T.LOAN_POSITIVE, cat="event" if ok else "notice")
    await cq.answer()


@router.callback_query(F.data.startswith("seed:"))
async def cb_seed(cq: CallbackQuery) -> None:
    _, uid, flag = cq.data.split(":")
    if int(uid) != cq.from_user.id or not is_owner(cq.from_user):
        await cq.answer(T.ANOTHER_USER, show_alert=True)
        return
    await edit(cq, do_seed(callback_chat(cq), cq.from_user, flag == "f"), cat="notice")
    await cq.answer()


RENDERERS = {
    "grow": lambda chat, u: render_grow(chat, u),
    "top": lambda chat, u: render_top(chat, 0),
    "dod": lambda chat, u: render_dod(chat),
    "stats": lambda chat, u: render_stats(chat, u),
    "loan": lambda chat, u: render_loan(chat, u),
}


@router.callback_query(F.data.startswith("in:"))
async def cb_inline(cq: CallbackQuery) -> None:
    _, kind, uid = cq.data.split(":")
    if int(uid) != cq.from_user.id:
        await cq.answer(T.ANOTHER_USER, show_alert=True)
        return
    if kind not in RENDERERS:
        await cq.answer("Неизвестная команда")
        return
    chat, u = callback_chat(cq), cq.from_user
    ban = banned_text(chat, u)
    text, markup, cat = (ban, None, "notice") if ban else RENDERERS[kind](chat, u)
    await edit(cq, text, markup, cat)
    await cq.answer()


# --- запуск --------------------------------------------------------------------

COMMANDS = [
    BotCommand(command="grow", description="Вырасти пиписю!"),
    BotCommand(command="top", description="Узнай рейтинг самых больших волын в чате"),
    BotCommand(command="dod", description="Выбор Писюна Дня"),
    BotCommand(command="pvp", description="Сражайся с пипирками друзей!"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="loan", description="Минус? Возьми кредит!"),
    BotCommand(command="help", description="Я обязательно помогу тебе, друг"),
]


def check_network(host: str = "api.telegram.org", port: int = 443) -> None:
    """Пишет в консоль, видит ли сервер Telegram, — чтобы на хостинге было понятно, где застряли."""
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as e:
        logging.error("DNS: не могу найти %s (%s) — у сервера нет интернета или DNS", host, e)
        return
    logging.info("DNS: %s -> %s", host, ", ".join(addrs))
    for ip in addrs:
        t = time.monotonic()
        try:
            with socket.create_connection((ip, port), timeout=10):
                logging.info("Связь с %s:%s есть (%.0f мс)", ip, port, (time.monotonic() - t) * 1000)
        except OSError as e:
            logging.error("Нет связи с %s:%s: %s", ip, port, e)


async def main() -> None:
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    logging.info("Старт бота, проверяю связь с Telegram…")
    check_network()
    raw = os.environ.get("BOT_TOKEN", "")
    if not raw.strip():
        raise SystemExit("Не задан BOT_TOKEN: добавь его в Environment (или файл .env рядом с bot.py)")
    token = find_token(raw)
    if not token:
        raise SystemExit(
            f"BOT_TOKEN задан, но это не похоже на токен (длина {len(raw)}, "
            f"начинается с {raw[:4]!r}). Нужен вид 1234567890:AAAbbb... — "
            "без кавычек, пробелов и без слов BOT_TOKEN= внутри значения."
        )
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logging.info("Спрашиваю у Telegram, кто я…")
    me = await bot.get_me(request_timeout=30)
    BOT_USERNAME = me.username
    if not owner_id():
        logging.warning("Владельца ещё нет — первый, кто напишет боту /start в личку, станет владельцем")
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(cleanup_worker(bot))
    logging.info("Запущен как @%s, база: %s", me.username, DB_PATH)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
