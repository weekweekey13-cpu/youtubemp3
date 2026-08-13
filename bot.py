"""
YouTube → MP3 Telegram-бот.

Пользователь кидает ссылку на YouTube — получает MP3.
Перед скачиванием обязательна подписка на каналы из админки.
Админ @bonamartin69: каналы, рестарт.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageOriginChannel, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "bonamartin69").split(",")
    if u.strip()
}

DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
STATS_PATH = DATA_DIR / "stats.json"
CACHE_PATH = DATA_DIR / "file_cache.json"
CACHE_LIMIT = 3000

MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "1200"))  # 20 мин
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "128")

VIDEO_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:embed/|v/|shorts/|live/|clip/)|"
    r"(?:music\.)?youtube(?:-nocookie)?\.com/watch\S*?[?&]v=)"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)
ANY_YT_RE = re.compile(
    r"(?P<url>(?:https?://)?(?:www\.|m\.)?(?:music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>]+)",
    re.IGNORECASE,
)
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TG_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_+]+)",
    re.IGNORECASE,
)

STARTED_AT = time.time()
user_locks: dict[int, asyncio.Lock] = {}
pending_action: dict[int, str] = {}
settings_lock = threading.Lock()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("youtubemp3")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


DEFAULT_CHANNELS = [
    {
        "id": "emilyfox777",
        "title": "Emily Fox",
        "username": "emilyfox777",
        "chat_id": -1003167848024,
        "url": "https://t.me/emilyfox777",
        "button_text": "Подписаться на Emily Fox",
    }
]


def default_settings() -> dict[str, Any]:
    return {"channels": [dict(ch) for ch in DEFAULT_CHANNELS]}


def load_json(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не смог прочитать %s", path)
    return fallback


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_settings() -> dict[str, Any]:
    with settings_lock:
        existed = SETTINGS_PATH.exists()
        data = load_json(SETTINGS_PATH, default_settings())
        if not isinstance(data, dict):
            data = default_settings()
        data.setdefault("channels", [])
        if not existed:
            save_json(SETTINGS_PATH, data)
        return data


def save_settings(data: dict[str, Any]) -> None:
    with settings_lock:
        save_json(SETTINGS_PATH, data)


def load_stats() -> dict[str, Any]:
    data = load_json(STATS_PATH, {"downloads": 0, "users": []})
    data.setdefault("downloads", 0)
    data.setdefault("users", [])
    return data


def bump_stats(user_id: int) -> None:
    with settings_lock:
        stats = load_stats()
        stats["downloads"] = int(stats.get("downloads", 0)) + 1
        users = set(stats.get("users") or [])
        users.add(int(user_id))
        stats["users"] = sorted(users)
        save_json(STATS_PATH, stats)


def load_cache() -> dict[str, Any]:
    data = load_json(CACHE_PATH, {})
    return data if isinstance(data, dict) else {}


def get_cached_track(video_id: str | None) -> dict[str, Any] | None:
    if not video_id:
        return None
    with settings_lock:
        item = load_cache().get(video_id)
    if not isinstance(item, dict) or not item.get("file_id"):
        return None
    return item


def save_cached_track(video_id: str, meta: dict[str, Any]) -> None:
    if not video_id or not meta.get("file_id"):
        return
    with settings_lock:
        cache = load_cache()
        cache[video_id] = {
            "file_id": meta["file_id"],
            "title": meta.get("title") or "YouTube audio",
            "artist": meta.get("artist") or "YouTube",
            "duration": int(meta.get("duration") or 0),
            "filename": meta.get("filename") or "",
            "ts": time.time(),
        }
        if len(cache) > CACHE_LIMIT:
            oldest = sorted(cache.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0))
            for key, _ in oldest[: len(cache) - CACHE_LIMIT]:
                cache.pop(key, None)
        save_json(CACHE_PATH, cache)


def drop_cached_track(video_id: str) -> None:
    if not video_id:
        return
    with settings_lock:
        cache = load_cache()
        if video_id in cache:
            cache.pop(video_id, None)
            save_json(CACHE_PATH, cache)


def is_admin(user) -> bool:
    if user is None:
        return False
    uname = (user.username or "").lstrip("@").lower()
    return bool(uname) and uname in ADMIN_USERNAMES


def get_lock(user_id: int) -> asyncio.Lock:
    lock = user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        user_locks[user_id] = lock
    return lock


def extract_youtube_url(text: str) -> str | None:
    if not text:
        return None
    text = text.strip().strip("<>")
    vid = VIDEO_ID_RE.search(text)
    if vid:
        return f"https://www.youtube.com/watch?v={vid.group(1)}"
    any_yt = ANY_YT_RE.search(text)
    if any_yt:
        raw = any_yt.group("url").rstrip(").,]\"'")
        if not raw.lower().startswith("http"):
            raw = "https://" + raw
        return raw
    if BARE_ID_RE.fullmatch(text):
        return f"https://www.youtube.com/watch?v={text}"
    return None


def parse_channel_input(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None

    if text.startswith("@"):
        username = text[1:].strip()
        if not username:
            return None
        return {
            "id": username.lower(),
            "title": "@" + username,
            "username": username,
            "chat_id": "@" + username,
            "url": f"https://t.me/{username}",
        }

    m = TG_LINK_RE.search(text)
    if m:
        slug = m.group(1)
        url = text if text.lower().startswith("http") else "https://" + text
        if slug.startswith("+"):
            return {
                "id": slug,
                "title": "Приватный канал",
                "username": "",
                "chat_id": "",
                "url": url if url.startswith("http") else f"https://t.me/{slug}",
            }
        return {
            "id": slug.lower(),
            "title": "@" + slug,
            "username": slug,
            "chat_id": "@" + slug,
            "url": f"https://t.me/{slug}",
        }

    if text.startswith("-") and text[1:].isdigit():
        return {
            "id": text,
            "title": f"Чат {text}",
            "username": "",
            "chat_id": int(text),
            "url": "",
        }
    return None


def channel_button_title(ch: dict[str, Any], index: int) -> str:
    title = (ch.get("title") or "").strip()
    if title:
        return title[:40]
    username = (ch.get("username") or "").strip()
    if username:
        return "@" + username.lstrip("@")
    return f"Канал {index}"


def subscribe_button_label(ch: dict[str, Any], index: int) -> str:
    custom = (ch.get("button_text") or "").strip()
    if custom:
        return custom[:64]
    return f"Подписаться на {channel_button_title(ch, index)}"[:64]


def check_button_label(settings: dict[str, Any] | None = None) -> str:
    data = settings if settings is not None else load_settings()
    custom = (data.get("check_button") or "").strip()
    return (custom or "✅ Я подписался")[:64]


def fmt_duration(sec: int | float | None) -> str:
    if not sec:
        return "—"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " ", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name or "audio")[:80]


def channel_public_url(ch: dict[str, Any]) -> str:
    url = (ch.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    username = (ch.get("username") or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}"
    chat_id = ch.get("chat_id")
    if chat_id is not None:
        raw = str(chat_id)
        if raw.startswith("-100") and raw[4:].isdigit():
            return f"https://t.me/c/{raw[4:]}/1"
    return ""


def subscribe_rows(channels: list[dict[str, Any]]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for i, ch in enumerate(channels, start=1):
        label = subscribe_button_label(ch, i)
        url = channel_public_url(ch)
        if url:
            rows.append([InlineKeyboardButton(label, url=url)])
        else:
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"subneed:{ch.get('id')}")]
            )
    if channels:
        rows.append([InlineKeyboardButton(check_button_label(), callback_data="check_sub")])
    return rows


def subscribe_keyboard(channels: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(subscribe_rows(channels))


def user_home_keyboard(admin: bool, channels: list[dict[str, Any]] | None = None) -> InlineKeyboardMarkup:
    rows = subscribe_rows(channels or [])
    rows.append([InlineKeyboardButton("🎵 Как скачать?", callback_data="help")])
    if admin:
        rows.append([InlineKeyboardButton("🛠 Админка", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard(channels: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, ch in enumerate(channels):
        title = channel_button_title(ch, i + 1)
        rows.append(
            [
                InlineKeyboardButton(f"📢 {title}", callback_data=f"chinfo:{ch.get('id')}"),
                InlineKeyboardButton("✏️", callback_data=f"chbtn:{ch.get('id')}"),
                InlineKeyboardButton("🗑", callback_data=f"chdel:{ch.get('id')}"),
            ]
        )
    rows.append([InlineKeyboardButton("✏️ Текст «Я подписался»", callback_data="checkbtn")])
    rows.append([InlineKeyboardButton("👁 Как видят кнопку подписки", callback_data="subpreview")])
    rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="chadd")])
    rows.append([InlineKeyboardButton("♻️ Рестарт бота", callback_data="restart_ask")])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def channel_candidates(ch: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    raw = ch.get("chat_id")
    if raw not in (None, ""):
        out.append(raw)
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            out.append(int(raw))
    username = (ch.get("username") or "").strip().lstrip("@")
    if username:
        out.append("@" + username)
    seen: set[str] = set()
    uniq: list[Any] = []
    for item in out:
        key = str(item).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def is_bot_rights_error(err: BaseException) -> bool:
    text = str(err).lower()
    return any(
        s in text
        for s in (
            "member list is inaccessible",
            "chat not found",
            "bot is not a member",
            "not enough rights",
            "need administrator",
            "chat_admin_required",
        )
    )


async def check_one_channel(bot, user_id: int, ch: dict[str, Any]) -> tuple[str, str]:
    """Возвращает ('ok'|'left'|'norights'|'bad', подробность)."""
    candidates = channel_candidates(ch)
    if not candidates:
        return "norights", "нет chat_id у канала"
    last = ""
    for cid in candidates:
        try:
            member = await bot.get_chat_member(cid, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return "left", str(member.status)
            if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", True) is False:
                return "left", "restricted"
            return "ok", str(member.status)
        except (Forbidden, BadRequest, TelegramError) as e:
            last = str(e)
            log.warning("Проверка %s / %s: %s", ch.get("username") or ch.get("id"), cid, e)
            if is_bot_rights_error(e):
                return "norights", last
    return "bad", last or "неизвестная ошибка"


async def required_missing(
    bot, user_id: int, channels: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(не подписан, каналы которые бот не может проверить)."""
    missing: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    for ch in channels:
        status, _detail = await check_one_channel(bot, user_id, ch)
        if status == "ok":
            continue
        if status == "left":
            missing.append(ch)
        else:
            broken.append(ch)
    return missing, broken


def subscribe_block_text(
    channels: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    broken: list[dict[str, Any]],
) -> str:
    if broken and not missing:
        names = ", ".join(channel_button_title(ch, i) for i, ch in enumerate(broken, start=1))
        return (
            "⚠️ <b>Не могу проверить подписку</b>\n\n"
            f"Канал: {names}\n\n"
            "Ты можешь быть подписан, но Telegram не отдаёт список участников, "
            "пока бот не станет <b>админом</b> этого канала.\n\n"
            "Админ канала: добавь <code>@dowloadmp3youtube_mp3bot</code> администратором "
            "(права можно оставить минимальные), затем нажми «Я подписался»."
        )
    extra = ""
    if broken:
        names = ", ".join(channel_button_title(ch, i) for i, ch in enumerate(broken, start=1))
        extra = (
            f"\n\n⚠️ Ещё не проверяется: {names}. "
            "Туда тоже нужно добавить бота админом."
        )
    return (
        "🔒 <b>Сначала подпишись на каналы</b>\n\n"
        "Без подписки скачивание недоступно.\n"
        "Нажми «Подписаться», вступи, затем «Я подписался»."
        + extra
    )


async def reply_subscribe_gate(
    update: Update,
    channels: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    broken: list[dict[str, Any]],
) -> None:
    text = subscribe_block_text(channels, missing, broken)
    markup = subscribe_keyboard(channels)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
            )
        except BadRequest:
            await update.callback_query.message.reply_html(
                text, reply_markup=markup, disable_web_page_preview=True
            )
    elif update.effective_message:
        await update.effective_message.reply_html(
            text, reply_markup=markup, disable_web_page_preview=True
        )


async def ensure_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if is_admin(user):
        return True
    channels = load_settings().get("channels") or []
    if not channels:
        return True
    missing, broken = await required_missing(context.bot, user.id, channels)
    if not missing and not broken:
        return True
    await reply_subscribe_gate(update, channels, missing, broken)
    return False


def welcome_text(admin: bool) -> str:
    extra = "\n\n🛠 Тебе доступна <b>админка</b>." if admin else ""
    return (
        "🎵 <b>YouTube → MP3</b>\n\n"
        "Кинь ссылку на видео — я быстро и бесплатно сделаю MP3.\n\n"
        "Подходит:\n"
        "• youtube.com/watch?v=…\n"
        "• youtu.be/…\n"
        "• youtube.com/shorts/…\n"
        "• music.youtube.com/…\n\n"
        f"Лимит: до {MAX_DURATION_SEC // 60} минут.{extra}"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    pending_action.pop(user.id, None)
    channels = load_settings().get("channels") or []
    text = welcome_text(is_admin(user))
    if channels:
        text += "\n\n📢 Сначала подпишись на канал кнопкой ниже, потом кидай ссылку на YouTube."
    await update.message.reply_html(
        text,
        reply_markup=user_home_keyboard(is_admin(user), channels),
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        channels = load_settings().get("channels") or []
        await update.message.reply_html(
            welcome_text(is_admin(update.effective_user)),
            reply_markup=user_home_keyboard(is_admin(update.effective_user), channels),
        )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_admin(update.effective_user):
        if update.message:
            await update.message.reply_text("Недостаточно прав.")
        return
    await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(load_settings()["channels"]))


def admin_text() -> str:
    settings = load_settings()
    stats = load_stats()
    channels = settings.get("channels") or []
    uptime = int(time.time() - STARTED_AT)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    lines = [
        "🛠 <b>Админка YouTube MP3</b>",
        "",
        f"📢 Каналов для подписки: <b>{len(channels)}</b>",
        f"⬇️ Скачиваний: <b>{int(stats.get('downloads', 0))}</b>",
        f"👤 Пользователей: <b>{len(stats.get('users') or [])}</b>",
        f"⏱ Аптайм: {h}ч {m}м {s}с",
        "",
        "Добавляй/убирай каналы. Бот должен быть <b>админом</b> каждого канала, иначе не проверит подписку.",
        "",
        "Чтобы добавить: нажми «Добавить канал» и пришли ссылку вида",
        "<code>https://t.me/channel</code> или <code>@channel</code>.",
        "",
        "✏️ — поменять текст кнопки, например <code>Подписаться на наш канал</code>.",
        "",
        "Пароль Google боту не нужен. По желанию пришли файл <code>cookies.txt</code> — для сложных роликов.",
    ]
    if channels:
        lines.append("")
        lines.append("<b>Сейчас обязательны:</b>")
        for i, ch in enumerate(channels, start=1):
            url = ch.get("url") or ""
            title = channel_button_title(ch, i)
            lines.append(f"{i}. {title}" + (f" — {url}" if url else ""))
    return "\n".join(lines)


async def show_admin(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await query.edit_message_text(
            admin_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(load_settings()["channels"]),
            disable_web_page_preview=True,
        )
    except BadRequest:
        await query.message.reply_html(
            admin_text(),
            reply_markup=admin_keyboard(load_settings()["channels"]),
            disable_web_page_preview=True,
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    data = query.data or ""
    await query.answer()

    if data == "help":
        try:
            await query.edit_message_text(
                welcome_text(is_admin(user)),
                parse_mode=ParseMode.HTML,
                reply_markup=user_home_keyboard(is_admin(user), load_settings().get("channels") or []),
            )
        except BadRequest:
            pass
        return

    if data.startswith("subneed:"):
        await query.answer(
            "У этого канала нет публичной ссылки. Админ должен добавить https://t.me/канал или инвайт.",
            show_alert=True,
        )
        return

    if data == "check_sub":
        channels = load_settings().get("channels") or []
        if not channels:
            await query.edit_message_text(
                "✅ Ограничений нет. Пришли ссылку на YouTube.",
                reply_markup=user_home_keyboard(is_admin(user), channels),
            )
            return
        missing, broken = await required_missing(context.bot, user.id, channels)
        if broken and not missing:
            await query.answer(
                "Бот не админ канала — не видит подписку. Добавь @dowloadmp3youtube_mp3bot админом.",
                show_alert=True,
            )
            await reply_subscribe_gate(update, channels, missing, broken)
            return
        if missing or broken:
            await query.answer("Ещё не на всех каналах. Подпишись и нажми снова.", show_alert=True)
            await reply_subscribe_gate(update, channels, missing, broken)
            return
        await query.edit_message_text(
            "✅ Подписка есть. Кидай ссылку на YouTube — пришлю MP3.",
            reply_markup=user_home_keyboard(is_admin(user), channels),
        )
        return

    if not is_admin(user):
        await query.answer("Только для админа.", show_alert=True)
        return

    if data == "admin":
        pending_action.pop(user.id, None)
        await show_admin(query, context)
        return

    if data == "subpreview":
        channels = load_settings().get("channels") or []
        if not channels:
            await query.answer("Сначала добавь канал.", show_alert=True)
            return
        await query.edit_message_text(
            "Так кнопку «Подписаться» видят пользователи. Нажми — откроется канал.",
            reply_markup=InlineKeyboardMarkup(
                subscribe_rows(channels)
                + [[InlineKeyboardButton("⬅️ Назад в админку", callback_data="admin")]]
            ),
            disable_web_page_preview=True,
        )
        return

    if data == "chadd":
        pending_action[user.id] = "add_channel"
        await query.edit_message_text(
            "➕ Пришли ссылку на канал:\n\n"
            "<code>https://t.me/имя</code>\n"
            "<code>@имя</code>\n\n"
            "Для приватного канала сначала добавь бота админом, "
            "затем пришли ссылку-приглашение или id чата (например <code>-100123…</code>).\n\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]
            ),
        )
        return

    if data.startswith("chdel:"):
        ch_id = data.split(":", 1)[1]
        settings = load_settings()
        before = len(settings["channels"])
        settings["channels"] = [c for c in settings["channels"] if str(c.get("id")) != ch_id]
        save_settings(settings)
        removed = before - len(settings["channels"])
        await query.answer("Удалил." if removed else "Уже нет.")
        await show_admin(query, context)
        return

    if data.startswith("chinfo:"):
        ch_id = data.split(":", 1)[1]
        ch = next((c for c in load_settings()["channels"] if str(c.get("id")) == ch_id), None)
        if not ch:
            await query.answer("Канал не найден.")
            await show_admin(query, context)
            return
        chat_id = ch.get("chat_id") or ""
        status = "не проверял"
        if chat_id:
            try:
                me = await context.bot.get_chat_member(chat_id, context.bot.id)
                status = f"бот в канале: {me.status}"
            except Exception as e:
                status = f"ошибка проверки: {e}"
        text = (
            f"📢 <b>{channel_button_title(ch, 1)}</b>\n\n"
            f"кнопка: <code>{subscribe_button_label(ch, 1)}</code>\n"
            f"id: <code>{ch.get('id')}</code>\n"
            f"chat_id: <code>{chat_id or '—'}</code>\n"
            f"ссылка: {ch.get('url') or '—'}\n"
            f"{status}\n\n"
            "Бот должен быть администратором канала."
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✏️ Текст кнопки", callback_data=f"chbtn:{ch_id}")],
                    [InlineKeyboardButton("🗑 Убрать из обязательных", callback_data=f"chdel:{ch_id}")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="admin")],
                ]
            ),
        )
        return

    if data.startswith("chbtn:"):
        ch_id = data.split(":", 1)[1]
        ch = next((c for c in load_settings()["channels"] if str(c.get("id")) == ch_id), None)
        if not ch:
            await query.answer("Канал не найден.")
            await show_admin(query, context)
            return
        pending_action[user.id] = f"rename_btn:{ch_id}"
        await query.edit_message_text(
            "✏️ Пришли новый текст кнопки.\n\n"
            f"Сейчас: <code>{subscribe_button_label(ch, 1)}</code>\n\n"
            "Например: <code>Подписаться на наш канал</code>\n"
            "Не больше 64 символов.\n\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]
            ),
        )
        return

    if data == "checkbtn":
        pending_action[user.id] = "rename_check"
        await query.edit_message_text(
            "✏️ Пришли текст кнопки проверки подписки.\n\n"
            f"Сейчас: <code>{check_button_label()}</code>\n\n"
            "Например: <code>✅ Я подписался</code>\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]
            ),
        )
        return

    if data == "restart_ask":
        await query.edit_message_text(
            "♻️ Перезапустить бота сейчас?\nНа Render процесс завершится и сервис поднимется заново.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Да, рестарт", callback_data="restart_now")],
                    [InlineKeyboardButton("Отмена", callback_data="admin")],
                ]
            ),
        )
        return

    if data == "restart_now":
        await query.edit_message_text("♻️ Перезапускаю… Напиши /start через 20–40 секунд.")
        log.info("Админ %s запросил рестарт", user.username)
        await asyncio.sleep(0.4)
        os._exit(0)


async def resolve_channel(bot, parsed: dict[str, Any]) -> dict[str, Any]:
    chat_id = parsed.get("chat_id")
    if not chat_id:
        return parsed
    try:
        chat = await bot.get_chat(chat_id)
        parsed["title"] = chat.title or parsed.get("title") or str(chat_id)
        if chat.username:
            parsed["username"] = chat.username
            parsed["chat_id"] = "@" + chat.username
            parsed["url"] = f"https://t.me/{chat.username}"
            parsed["id"] = chat.username.lower()
        else:
            parsed["chat_id"] = chat.id
            parsed["id"] = str(chat.id)
            if not parsed.get("url"):
                try:
                    invite = await bot.export_chat_invite_link(chat.id)
                    parsed["url"] = invite
                except TelegramError:
                    pass
        return parsed
    except TelegramError as e:
        parsed["resolve_error"] = str(e)
        return parsed


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    user = update.effective_user
    if not user or not is_admin(user):
        return False
    action = pending_action.get(user.id)
    if not action:
        return False

    if action.startswith("rename_btn:"):
        ch_id = action.split(":", 1)[1]
        label = " ".join((text or "").split())
        if not label or len(label) > 64:
            await update.message.reply_text("Текст пустой или длиннее 64 символов. Пришли короче.")
            return True
        settings = load_settings()
        found = False
        for ch in settings["channels"]:
            if str(ch.get("id")) == ch_id:
                ch["button_text"] = label
                found = True
                break
        if not found:
            pending_action.pop(user.id, None)
            await update.message.reply_text("Канал не найден.")
            return True
        save_settings(settings)
        pending_action.pop(user.id, None)
        await update.message.reply_html(
            f"✅ Кнопка теперь: <code>{label}</code>\nТак её видят пользователи:",
            reply_markup=subscribe_keyboard(settings["channels"]),
        )
        await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
        return True

    if action == "rename_check":
        label = " ".join((text or "").split())
        if not label or len(label) > 64:
            await update.message.reply_text("Текст пустой или длиннее 64 символов. Пришли короче.")
            return True
        settings = load_settings()
        settings["check_button"] = label
        save_settings(settings)
        pending_action.pop(user.id, None)
        await update.message.reply_html(
            f"✅ Кнопка проверки теперь: <code>{label}</code>",
            reply_markup=subscribe_keyboard(settings["channels"]),
        )
        await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
        return True

    if action != "add_channel":
        return False

    parsed = parse_channel_input(text)
    if not parsed:
        await update.message.reply_html(
            "Не понял ссылку. Пришли <code>https://t.me/канал</code> или <code>@канал</code>."
        )
        return True

    parsed = await resolve_channel(context.bot, parsed)
    settings = load_settings()
    existing_ids = {str(c.get("id")) for c in settings["channels"]}
    existing_chats = {str(c.get("chat_id")) for c in settings["channels"]}
    if str(parsed.get("id")) in existing_ids or str(parsed.get("chat_id")) in existing_chats:
        pending_action.pop(user.id, None)
        await update.message.reply_text("Этот канал уже в списке.")
        await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
        return True

    if not parsed.get("url") and parsed.get("username"):
        parsed["url"] = f"https://t.me/{parsed['username']}"

    if not parsed.get("url"):
        await update.message.reply_html(
            "Сохранил канал, но нет публичной ссылки. "
            "Пользователь не сможет нажать «Подписаться». "
            "Добавь бота админом и пришли ссылку-приглашение ещё раз."
        )

    warn = ""
    chat_id = parsed.get("chat_id")
    if chat_id:
        try:
            me = await context.bot.get_chat_member(chat_id, context.bot.id)
            if me.status not in (
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ):
                warn = (
                    "\n\n⚠️ Бот не админ этого канала — проверка подписки не сработает. "
                    "Добавь @dowloadmp3youtube_mp3bot админом."
                )
        except TelegramError:
            warn = (
                "\n\n⚠️ Не смог заглянуть в канал. Добавь бота админом "
                "(@dowloadmp3youtube_mp3bot), иначе подписку не проверить."
            )
    elif parsed.get("url", "").find("+") != -1:
        warn = (
            "\n\n⚠️ Приватный инвайт. Напиши ещё id канала (число вроде <code>-100…</code>) "
            "после того, как добавишь бота админом — или перешли любое сообщение из канала."
        )

    if not parsed.get("button_text"):
        parsed["button_text"] = f"Подписаться на {channel_button_title(parsed, 1)}"[:64]
    settings["channels"].append(parsed)
    save_settings(settings)
    pending_action.pop(user.id, None)
    title = channel_button_title(parsed, len(settings["channels"]))
    await update.message.reply_html(
        f"✅ Добавил <b>{title}</b>. Пользователи должны на него подписаться.{warn}\n\n"
        "Так выглядит кнопка у пользователей:",
        reply_markup=subscribe_keyboard(settings["channels"]),
        disable_web_page_preview=True,
    )
    await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
    return True


HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def youtube_id(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None, timeout: int = 25) -> dict[str, Any]:
    hdrs = {"User-Agent": HTTP_UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = Request(url, data=data, headers=hdrs, method=method)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8", "replace"))
    return parsed if isinstance(parsed, dict) else {}


class DownloadProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "start"
        self.percent = 0
        self.title = ""

    def set(self, phase: str, percent: int, title: str | None = None) -> None:
        with self._lock:
            self.phase = phase
            self.percent = max(0, min(100, int(percent)))
            if title:
                self.title = title[:80]

    def snapshot(self) -> tuple[str, int, str]:
        with self._lock:
            return self.phase, self.percent, self.title


def progress_bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(percent / 10)
    return "█" * filled + "░" * (10 - filled)


def format_progress(prog: DownloadProgress) -> str:
    phase, percent, title = prog.snapshot()
    labels = {
        "start": "Готовлю",
        "convert": "Собираю MP3",
        "download": "Скачиваю",
        "send": "Отправляю",
    }
    line = f"⬇️ {labels.get(phase, 'Качаю')} {progress_bar(percent)} {percent}%"
    if title:
        return f"{line}\n🎵 {title}"
    return line


def loader_percent(raw: Any) -> int | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 100:
        value = value / 10.0
    return max(0, min(100, int(value)))


def http_download(url: str, dest: Path, timeout: int = 90, progress: DownloadProgress | None = None) -> None:
    req = Request(url, headers={"User-Agent": HTTP_UA, "Accept": "*/*"})
    limit = MAX_FILE_MB * 1024 * 1024
    with urlopen(req, timeout=timeout) as resp, dest.open("wb") as out:
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        n = 0
        last_pct = -1
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"Файл слишком большой. Лимит Telegram — {MAX_FILE_MB} МБ.")
            out.write(chunk)
            if progress:
                if total > 0:
                    pct = 70 + int(25 * n / total)
                else:
                    pct = min(94, 70 + n // 300_000)
                if pct != last_pct:
                    progress.set("download", pct)
                    last_pct = pct
    if not dest.exists() or dest.stat().st_size < 1000:
        raise RuntimeError("Пустой файл, YouTube не отдал аудио.")
    if progress:
        progress.set("download", 95)


def convert_to_mp3(src: Path, dest: Path) -> None:
    if src.suffix.lower() == ".mp3":
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-b:a",
        f"{AUDIO_QUALITY}k",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size < 1000:
        err = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"Не смог сконвертировать в MP3. {err}".strip())


def pick_best_audio(medias: list[dict[str, Any]]) -> dict[str, Any] | None:
    audios = [m for m in medias if m.get("url") and str(m.get("type") or "").lower() == "audio"]
    if not audios:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int]:
        ext = str(item.get("ext") or "").lower()
        pref = 3 if ext == "m4a" else 2 if ext == "mp3" else 1 if ext in {"opus", "webm"} else 0
        nums = re.findall(r"(\d+)", str(item.get("quality") or item.get("label") or ""))
        br = max((int(n) for n in nums), default=0)
        return (pref, br)

    return max(audios, key=score)


def download_via_loader(url: str, workdir: Path, progress: DownloadProgress | None = None) -> dict[str, Any] | None:
    from urllib.parse import quote

    if progress:
        progress.set("convert", 3)
    start_url = "https://loader.to/ajax/download.php?format=mp3&url=" + quote(url, safe="")
    try:
        start = http_json("GET", start_url, timeout=15)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("loader.to старт: %s", e)
        return None
    start_info = start.get("info") if isinstance(start.get("info"), dict) else {}
    start_title = str(start.get("title") or start_info.get("title") or "")
    if progress and start_title:
        progress.set("convert", 8, start_title)
    text = str(start.get("text") or start.get("message") or "")
    if not start.get("success"):
        low = text.lower()
        if any(s in low for s in ("unavailable", "removed", "private", "not available")):
            raise RuntimeError("UNAVAILABLE")
        log.warning("loader.to отказ: %s", start)
        return None
    progress_url = start.get("progress_url")
    if not progress_url:
        return None
    data = start
    deadline = time.time() + 120
    while time.time() < deadline:
        if data.get("success") in (1, True) and data.get("download_url"):
            break
        time.sleep(0.4)
        try:
            data = http_json("GET", progress_url, timeout=8)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("loader.to progress: %s", e)
            continue
        msg = str(data.get("text") or data.get("message") or "")
        if any(s in msg.lower() for s in ("unavailable", "removed", "private")):
            raise RuntimeError("UNAVAILABLE")
        pct = loader_percent(data.get("progress"))
        if progress and pct is not None:
            data_info = data.get("info") if isinstance(data.get("info"), dict) else {}
            title = str(data.get("title") or data_info.get("title") or start_title)
            progress.set("convert", max(5, int(pct * 0.7)), title)
    download_url = data.get("download_url")
    if not download_url:
        log.warning("loader.to: нет download_url %s", data)
        return None
    if progress:
        progress.set("download", 72, start_title)
    mp3_path = workdir / "audio.mp3"
    http_download(download_url, mp3_path, progress=progress)
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    title = (data.get("title") or info.get("title") or start.get("title") or "YouTube audio").strip()
    duration = int(data.get("video_duration") or info.get("duration") or 0)
    if duration and duration > MAX_DURATION_SEC:
        mp3_path.unlink(missing_ok=True)
        raise RuntimeError(f"Видео длиннее {MAX_DURATION_SEC // 60} минут.")
    return {
        "path": mp3_path,
        "thumb": None,
        "title": title,
        "artist": "YouTube",
        "duration": duration,
        "webpage_url": url,
        "id": youtube_id(url) or "",
    }


def download_via_clipto(url: str, workdir: Path, progress: DownloadProgress | None = None) -> dict[str, Any] | None:
    try:
        data = http_json(
            "POST",
            "https://www.clipto.com/api/youtube",
            {"url": url},
            {
                "Origin": "https://www.clipto.com",
                "Referer": "https://www.clipto.com/",
            },
            timeout=30,
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("Clipto ошибка: %s", e)
        return None
    if not data.get("success"):
        log.warning("Clipto: видео недоступно (%s)", data.get("error") or data.get("message") or data)
        raise RuntimeError("UNAVAILABLE")
    media = pick_best_audio(data.get("medias") or [])
    if not media:
        log.warning("Clipto: нет аудиодорожки")
        return None
    ext = str(media.get("ext") or "m4a").lstrip(".")
    raw_path = workdir / f"src.{ext}"
    if progress:
        progress.set("download", 40, str(data.get("title") or ""))
    http_download(media["url"], raw_path, progress=progress)
    mp3_path = workdir / "audio.mp3"
    convert_to_mp3(raw_path, mp3_path)
    duration = int(data.get("duration") or 0)
    if duration and duration > MAX_DURATION_SEC:
        mp3_path.unlink(missing_ok=True)
        raise RuntimeError(f"Видео длиннее {MAX_DURATION_SEC // 60} минут.")
    return {
        "path": mp3_path,
        "thumb": None,
        "title": (data.get("title") or "YouTube audio").strip(),
        "artist": (data.get("author") or "YouTube").strip() or "YouTube",
        "duration": duration,
        "webpage_url": url,
        "id": youtube_id(url) or "",
    }


def cookies_file() -> str | None:
    env_path = os.getenv("YT_COOKIES_FILE", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    bundled = DATA_DIR / "cookies.txt"
    if bundled.exists() and bundled.stat().st_size > 20:
        return str(bundled)
    raw = os.getenv("YT_COOKIES", "").strip()
    if raw:
        tmp = DATA_DIR / "cookies.env.txt"
        tmp.write_text(raw.replace("\\n", "\n"), encoding="utf-8")
        return str(tmp)
    return None


def download_via_ytdlp(url: str, workdir: Path, progress: DownloadProgress | None = None) -> dict[str, Any] | None:
    import yt_dlp
    from yt_dlp.utils import DownloadError, YoutubeDLError

    def match_filter(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            return "Это прямой эфир, скачать нельзя"
        duration = info.get("duration")
        if duration and int(duration) > MAX_DURATION_SEC:
            return f"Видео длиннее {MAX_DURATION_SEC // 60} минут"
        return None

    outtmpl = str(workdir / "ytdlp.%(ext)s")
    cookie = cookies_file()
    opts: dict[str, Any] = {
        "format": "bestaudio[abr<=160]/bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": {"default": outtmpl},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "overwrites": True,
        "cachedir": False,
        "retries": 1,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "socket_timeout": 12,
        "geo_bypass": True,
        "match_filter": match_filter,
        "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
    }
    if cookie:
        opts["cookiefile"] = cookie
    if progress:
        def _hook(event: dict[str, Any]) -> None:
            if event.get("status") != "downloading":
                return
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            got = event.get("downloaded_bytes") or 0
            if total:
                progress.set("download", int(100 * got / total))

        opts["progress_hooks"] = [_hook]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
            if info.get("_type") == "playlist" and info.get("entries"):
                info = next((e for e in info["entries"] if e), {}) or {}
    except (DownloadError, YoutubeDLError) as e:
        log.warning("yt-dlp: %s", e)
        raise RuntimeError(str(e)) from e
    audio = find_file(workdir, {".m4a", ".mp3", ".webm", ".opus", ".ogg"})
    if not audio:
        return None
    return {
        "path": audio,
        "thumb": find_file(workdir, {".jpg", ".jpeg", ".png", ".webp"}),
        "title": (info.get("title") or "YouTube audio").strip(),
        "artist": (info.get("uploader") or info.get("channel") or "YouTube").strip(),
        "duration": int(info.get("duration") or 0),
        "webpage_url": info.get("webpage_url") or url,
        "id": info.get("id") or youtube_id(url) or "",
    }


def download_mp3(url: str, workdir: Path, progress: DownloadProgress | None = None) -> dict[str, Any]:
    last_err = ""
    dest = workdir / "loader_to"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        got = download_via_loader(url, dest, progress)
        if got:
            log.info("Скачал через loader.to: %s", got.get("title"))
            return got
    except RuntimeError as e:
        if str(e) == "UNAVAILABLE":
            raise RuntimeError("Это видео недоступно (удалено, приватное или заблокировано).") from e
        last_err = str(e)
        log.warning("loader.to: %s", e)
    except Exception as e:
        last_err = str(e)
        log.warning("loader.to unexpected: %s", e)

    if cookies_file():
        ydest = workdir / "ytdlp"
        ydest.mkdir(parents=True, exist_ok=True)
        try:
            got = download_via_ytdlp(url, ydest, progress)
            if got:
                log.info("Скачал через yt-dlp: %s", got.get("title"))
                return got
        except RuntimeError as e:
            last_err = str(e)
            log.warning("yt-dlp: %s", e)
    raise RuntimeError(humanize_ytdlp_error(last_err) if last_err else "Не получилось скачать. Попробуй другую ссылку.")


def find_file(folder: Path, exts: set[str]) -> Path | None:
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files[0]


def humanize_ytdlp_error(msg: str) -> str:
    low = (msg or "").lower()
    if "too long" in low or "длиннее" in low:
        return f"Видео длиннее {MAX_DURATION_SEC // 60} минут."
    if "live" in low or "эфир" in low:
        return "Это прямой эфир — скачать нельзя."
    if "unavailable" in low or "private" in low or "removed" in low or "удалено" in low:
        return "Это видео недоступно (удалено, приватное или заблокировано)."
    if "not a bot" in low or "sign in to confirm" in low:
        return "YouTube режет сервер. Подожди минуту и кинь ссылку ещё раз."
    if "age" in low and "restrict" in low:
        return "Не смог обойти ограничение на этом ролике. Попробуй другую ссылку на него."
    if "http error 403" in low or "blocked" in low:
        return "Сервер временно не отдал файл. Попробуй ещё раз через минуту."
    return "Не смог скачать это видео. Проверь ссылку и попробуй ещё раз."


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    text = msg.text or msg.caption or ""

    if await handle_admin_text(update, context, text):
        return

    url = extract_youtube_url(text)
    if not url:
        if is_admin(user) and pending_action.get(user.id) == "add_channel":
            return
        await msg.reply_html(
            "Пришли обычную ссылку на YouTube.\nНапример: <code>https://youtu.be/dQw4w9WgXcQ</code>"
        )
        return

    if not await ensure_subscribed(update, context):
        return

    video_id = youtube_id(url)
    cached = get_cached_track(video_id)
    if cached:
        try:
            cache_kwargs: dict[str, Any] = {
                "audio": cached["file_id"],
                "filename": cached.get("filename") or f"{safe_filename(cached.get('title') or 'audio')}.mp3",
                "title": (cached.get("title") or "YouTube audio")[:64],
                "performer": (cached.get("artist") or "YouTube")[:64],
                "caption": (
                    f"🎵 {cached.get('title') or 'YouTube audio'}\n"
                    f"⏱ {fmt_duration(cached.get('duration'))}"
                ),
            }
            if cached.get("duration"):
                cache_kwargs["duration"] = int(cached["duration"])
            await msg.reply_audio(**cache_kwargs)
            bump_stats(user.id)
            return
        except TelegramError:
            log.warning("Кэш file_id не сработал для %s — качаю заново", video_id)
            drop_cached_track(video_id)

    lock = get_lock(user.id)
    if lock.locked():
        await msg.reply_text("Уже качаю твой предыдущий трек. Подожди немного.")
        return

    async with lock:
        progress = DownloadProgress()
        progress.set("start", 2)
        status = await msg.reply_text(format_progress(progress))
        workdir = Path(tempfile.mkdtemp(prefix="ytmp3_", dir=str(DATA_DIR)))
        try:
            dl_task = asyncio.create_task(asyncio.to_thread(download_mp3, url, workdir, progress))
            last_text = ""
            deadline = time.time() + 180
            while not dl_task.done():
                if time.time() > deadline:
                    dl_task.cancel()
                    raise asyncio.TimeoutError
                text = format_progress(progress)
                if text != last_text:
                    try:
                        await status.edit_text(text)
                    except BadRequest:
                        pass
                    last_text = text
                await asyncio.sleep(1)
            result = dl_task.result()
            progress.set("send", 97, result.get("title") or "")
            try:
                await status.edit_text(format_progress(progress))
            except BadRequest:
                pass

            ext = result["path"].suffix.lower().lstrip(".") or "mp3"
            if ext == "opus":
                ext = "ogg"
            thumb_file = None
            audio_file = open(result["path"], "rb")
            try:
                kwargs: dict[str, Any] = {
                    "audio": audio_file,
                    "filename": f"{safe_filename(result['title'])}.{ext}",
                    "title": result["title"][:64],
                    "performer": result["artist"][:64],
                    "caption": f"🎵 {result['title']}\n⏱ {fmt_duration(result['duration'])}",
                }
                if result["duration"]:
                    kwargs["duration"] = result["duration"]
                if result["thumb"] and result["thumb"].exists():
                    thumb_file = open(result["thumb"], "rb")
                    kwargs["thumbnail"] = thumb_file
                sent = await msg.reply_audio(**kwargs)
            finally:
                audio_file.close()
                if thumb_file:
                    thumb_file.close()

            if video_id and getattr(sent, "audio", None) and sent.audio.file_id:
                save_cached_track(
                    video_id,
                    {
                        "file_id": sent.audio.file_id,
                        "title": result["title"],
                        "artist": result["artist"],
                        "duration": sent.audio.duration or result["duration"],
                        "filename": f"{safe_filename(result['title'])}.{ext}",
                    },
                )
            bump_stats(user.id)
            try:
                await status.delete()
            except TelegramError:
                pass
        except asyncio.TimeoutError:
            await status.edit_text("⏱ Слишком долго качается. Попробуй другое видео или ещё раз.")
        except RuntimeError as e:
            try:
                await status.edit_text(f"❌ {e}")
            except BadRequest:
                await msg.reply_text(f"❌ {e}")
        except TelegramError as e:
            log.exception("Telegram send failed")
            try:
                await status.edit_text(f"❌ Не смог отправить файл: {e}")
            except BadRequest:
                pass
        except Exception:
            log.exception("Download failed")
            try:
                await status.edit_text("❌ Ошибка при скачивании. Попробуй другую ссылку.")
            except BadRequest:
                pass
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


async def handle_cookies_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.document or not is_admin(user):
        return
    name = (msg.document.file_name or "").lower()
    if "cookie" not in name and name != "cookies.txt":
        await msg.reply_text("Нужен файл cookies.txt (Netscape). Пароль Google не присылай.")
        return
    if msg.document.file_size and msg.document.file_size > 2_000_000:
        await msg.reply_text("Файл слишком большой.")
        return
    tg_file = await msg.document.get_file()
    dest = DATA_DIR / "cookies.txt"
    await tg_file.download_to_drive(custom_path=str(dest))
    raw = dest.read_text(encoding="utf-8", errors="replace")
    if "# Netscape" not in raw and "youtube.com" not in raw:
        dest.unlink(missing_ok=True)
        await msg.reply_text("Это не cookies YouTube. Экспортируй cookies.txt расширением Get cookies.txt LOCALLY.")
        return
    await msg.reply_text("Сохранил cookies. Сложные ролики теперь можно пробовать ещё раз.")


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    if is_admin(user) and pending_action.get(user.id) == "add_channel":
        origin = getattr(msg, "forward_origin", None)
        chat = None
        if isinstance(origin, MessageOriginChannel):
            chat = origin.chat
        elif getattr(msg, "forward_from_chat", None):
            chat = msg.forward_from_chat
        if chat:
            username = getattr(chat, "username", None) or ""
            parsed = {
                "id": (username.lower() if username else str(chat.id)),
                "title": chat.title or (f"@{username}" if username else str(chat.id)),
                "username": username,
                "chat_id": f"@{username}" if username else chat.id,
                "url": f"https://t.me/{username}" if username else "",
            }
            fake_text = parsed["url"] or str(parsed["chat_id"])
            pending_action[user.id] = "add_channel"
            await handle_admin_text(
                update,
                context,
                fake_text if username else str(chat.id),
            )
            return

    await handle_link(update, context)


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _ok(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/health", "/api/ping", "/api/health"):
            self._ok(
                {
                    "ok": True,
                    "service": "youtubemp3",
                    "uptime_sec": int(time.time() - STARTED_AT),
                }
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/health", "/api/ping", "/api/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def start_http() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="http")
    thread.start()
    log.info("HTTP health on :%s", PORT)


def main() -> None:
    if not BOT_TOKEN:
        print("Нет BOT_TOKEN. Пропиши его в .env или в переменных Render.", file=sys.stderr)
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg не найден в PATH — конвертация в MP3 может не сработать")

    start_http()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies_file))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forward))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.Entity("url") | filters.CaptionEntity("url"), handle_link))

    def _stop(*_args) -> None:
        log.info("stop signal")

    signal.signal(signal.SIGTERM, _stop)
    chans = load_settings().get("channels") or []
    log.info(
        "Бот запущен. Админы: %s. Каналы: %s",
        ", ".join(sorted(ADMIN_USERNAMES)) or "—",
        ", ".join(channel_public_url(c) or str(c.get("id")) for c in chans) or "нет",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
