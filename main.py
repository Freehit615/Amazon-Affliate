"""Amazon affiliate deal forwarder — Telethon UserBot.

Run:            python main.py
Make session:   python main.py --login      (run locally, copy the printed STRING_SESSION)
"""
import asyncio
import html
import io
import logging
import os
import re
import sys
from typing import Optional

import aiohttp
from telethon import Button, TelegramClient, events, utils as tl_utils
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (BotCommand, BotCommandScopeDefault, Channel, Chat,
                               InputPeerChannel, InputPeerChat)

import utils as U
from database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("bot")

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # optional: if set, this bot posts to the destination
ENV_ADMINS = [int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.lstrip("-").isdigit()]
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
# Multi-deal posts: reuse the source image on every deal? (default: no -> per-product Amazon image)
MULTI_USE_SOURCE_MEDIA = os.getenv("MULTI_DEAL_USE_SOURCE_MEDIA", "0") == "1"
CAPTION_LIMIT = 1024


class State:
    client: TelegramClient = None   # userbot: reads sources + takes admin commands
    bot: TelegramClient = None      # bot (optional): posts deals to destination
    bot_target = None
    db: Database = None
    http: aiohttp.ClientSession = None
    me = None
    dest = None
    dest_id: Optional[int] = None
    lock: asyncio.Lock = None


S = State()


def esc(x) -> str:
    return html.escape(str(x), quote=False)


# ------------------------------------------------------------------ resolving
async def resolve_chat(raw: str, bare_ok: bool = False):
    """Resolve an ID (-100…/bare) or @username / t.me link to an entity. None on failure."""
    raw = raw.strip()
    m = U.USERNAME_RE.match(raw)
    if m or (bare_ok and re.fullmatch(r"[A-Za-z]\w{3,31}", raw)):
        try:
            return await S.client.get_entity(m.group(1) if m else raw)
        except Exception as e:
            log.warning("username resolve failed (%s): %s", raw, e)
            return None
    if U.ID_RE.match(raw):
        peers = U.peers_for(raw)
        for attempt in range(2):
            for p in peers:
                try:
                    return await S.client.get_entity(p)
                except Exception:
                    pass
            if attempt == 0:  # refresh the entity cache once, then retry
                try:
                    await S.client.get_dialogs()
                except Exception as e:
                    log.warning("get_dialogs failed: %s", e)
    return None


async def refresh_dest():
    S.dest, S.dest_id = None, None
    ref = S.db.settings.get("destination_channel")
    if not ref:
        return None
    ent = await resolve_chat(ref, bare_ok=True)
    if ent is None:
        log.error("Destination %r could not be resolved (is the account a member?)", ref)
        return None
    S.dest, S.dest_id = ent, tl_utils.get_peer_id(ent)
    if getattr(ent, "username", None):
        S.bot_target = ent.username
    elif isinstance(ent, Channel):
        S.bot_target = InputPeerChannel(ent.id, 0)  # bots may address channels they admin with hash 0
    else:
        S.bot_target = InputPeerChat(ent.id)
    return ent


# ------------------------------------------------------------- admin commands
HELP = (
    "<b>📖 Command Guide</b>\n\n"
    "<b>Sources</b>\n"
    "• Channel/group ID bhejein (bina command) — source add\n"
    "• <code>/del_source &lt;channel_id&gt;</code> — source hatayein\n"
    "• <code>/status</code> — active sources aur destination\n\n"
    "<b>Post Settings</b>\n"
    "• <code>/set_header &lt;text&gt;</code> — header (<code>off</code> = hatayein)\n"
    "• <code>/set_footer &lt;text&gt;</code> — footer (<code>off</code> = hatayein)\n"
    "• <code>/set_tag &lt;tag&gt;</code> — Amazon affiliate tag\n"
    "• <code>/set_dest &lt;id | @username&gt;</code> — destination channel"
)

DENIED = "🔒 <b>Access Restricted</b>\n\nYeh bot private hai aur sirf authorised admins ke liye hai."

MENU = [
    ("start", "Bot start karein"),
    ("help", "Saare commands dekhein"),
    ("status", "Active sources aur destination"),
    ("set_header", "Post header set karein"),
    ("set_footer", "Post footer set karein"),
    ("set_tag", "Affiliate tag set karein"),
    ("set_dest", "Destination channel set karein"),
    ("del_source", "Source delete karein"),
]


async def add_source(event, raw: str):
    db = S.db
    if U.ID_RE.match(raw):
        if db.has_source(U.marked_ids(raw)) is not None:
            return await event.reply("⚠️ Yeh channel/group already saved hai.")
    ent = await resolve_chat(raw)
    if not isinstance(ent, (Channel, Chat)):
        return await event.reply(
            "❌ <b>Source add nahi hua</b>\n\n"
            "ID/username valid nahi hai, ya account is channel/group ka member nahi hai."
        )
    if getattr(ent, "left", False):
        if getattr(ent, "username", None):
            try:
                await S.client(JoinChannelRequest(ent))
            except Exception as e:
                return await event.reply(f"❌ <b>Channel join nahi ho paya</b>\n\n{esc(e)}")
        else:
            return await event.reply(
                "❌ <b>Source add nahi hua</b>\n\nAccount is private channel ka member nahi hai. Pehle join karein."
            )
    cid = tl_utils.get_peer_id(ent)
    title = tl_utils.get_display_name(ent) or str(cid)
    if not await db.add_source(cid, title, event.sender_id):
        return await event.reply("⚠️ Yeh channel/group already saved hai.")
    await event.reply(
        "✅ <b>Source Added</b>\n\n"
        f"📌 <b>Name:</b> {esc(title)}\n🆔 <b>ID:</b> <code>{cid}</code>\n\n"
        "Monitoring turant shuru ho gayi hai."
    )


async def cmd_start(event, arg):
    sender = await event.get_sender()
    name = esc(getattr(sender, "first_name", None) or "Admin")
    await event.reply(
        f"👋 <b>Namaste, {name}!</b>\n\n"
        "<b>Amazon Deals Automation</b> mein aapka swagat hai. Yeh system aapke source channels se "
        "Amazon deals uthakar affiliate link ke saath destination channel par post karta hai.\n\n"
        "🚀 <b>Quick Setup</b>\n"
        "1️⃣ <code>/set_tag</code> — affiliate tag\n"
        "2️⃣ <code>/set_dest</code> — destination channel\n"
        "3️⃣ Source channel ki ID bhejein\n\n"
        "Saare commands ke liye /help dekhein."
    )


async def cmd_help(event, arg):
    await event.reply(HELP)


async def cmd_del_source(event, arg):
    if not U.ID_RE.match(arg):
        return await event.reply(
            "ℹ️ <b>Usage</b>\n<code>/del_source &lt;channel_id&gt;</code>\n\n"
            "Source ki ID /status mein dekh sakte hain."
        )
    rows = await S.db.remove_source(U.marked_ids(arg))
    if not rows:
        return await event.reply("⚠️ Yeh source already deleted hai ya list me nahi hai.")
    cid, title = rows[0]
    await event.reply(
        "🗑️ <b>Source Removed</b>\n\n"
        f"📌 <b>Name:</b> {esc(title)}\n🆔 <b>ID:</b> <code>{cid}</code>\n\n"
        "Ab is source se posts forward nahi hongi."
    )


async def cmd_status(event, arg):
    src = S.db.sources
    lines = [f"📡 <b>Active Sources</b> ({len(src)})\n"]
    if src:
        for n, (cid, title) in enumerate(sorted(src.items(), key=lambda x: str(x[1]).lower()), 1):
            lines.append(f"{n}. {esc(title)}\n    🆔 <code>{cid}</code>")
    else:
        lines.append("Abhi koi source add nahi hai. Channel ki ID bhejkar add karein.")
    lines.append("\n📢 <b>Destination Channel</b>\n")
    dest = S.dest or await refresh_dest()
    if dest:
        uname = f"\n    🔗 @{dest.username}" if getattr(dest, "username", None) else ""
        lines.append(f"{esc(tl_utils.get_display_name(dest))}{uname}\n    🆔 <code>{S.dest_id}</code>")
    else:
        lines.append("⚠️ Set nahi hai. <code>/set_dest &lt;id&gt;</code> use karein.")
    await event.reply("\n".join(lines))


def _text_setter(field: str, label: str):
    async def handler(event, arg):
        cur = S.db.settings.get(field)
        if not arg:
            return await event.reply(
                f"📝 <b>{label.capitalize()}</b>\n\n"
                f"<b>Current:</b> {esc(cur) if cur else '— set nahi hai —'}\n\n"
                f"<b>Usage</b>\n<code>/set_{label} &lt;text&gt;</code>\n<code>/set_{label} off</code> — hatane ke liye"
            )
        if arg.lower() in ("off", "none", "clear"):
            await S.db.set_field(field, None)
            return await event.reply(f"🧹 <b>{label.capitalize()} Removed</b>\n\nAb posts mein {label} nahi judega.")
        await S.db.set_field(field, arg)
        await event.reply(f"✅ <b>{label.capitalize()} Updated</b>\n\n{esc(arg)}")

    return handler


async def cmd_set_tag(event, arg):
    arg = arg.strip().lstrip("?").removeprefix("tag=")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", arg):
        return await event.reply(
            "ℹ️ <b>Usage</b>\n<code>/set_tag yourtag-21</code>\n\nTag mein sirf letters, numbers, <code>-</code> aur <code>_</code> allowed hain."
        )
    await S.db.set_field("affiliate_tag", arg)
    await event.reply(f"✅ <b>Affiliate Tag Updated</b>\n\n🏷️ <code>{esc(arg)}</code>")


async def cmd_set_dest(event, arg):
    ent = await resolve_chat(arg, bare_ok=True) if arg else None
    if not isinstance(ent, (Channel, Chat)):
        return await event.reply(
            "❌ <b>Destination set nahi hua</b>\n\n"
            "<b>Usage:</b> <code>/set_dest &lt;id | @username&gt;</code>\n"
            "Account us channel ka member ho, aur posting bot channel mein admin ho."
        )
    await S.db.set_field("destination_channel", str(tl_utils.get_peer_id(ent)))
    await refresh_dest()
    await event.reply(
        "✅ <b>Destination Updated</b>\n\n"
        f"📢 <b>Name:</b> {esc(tl_utils.get_display_name(ent))}\n🆔 <b>ID:</b> <code>{S.dest_id}</code>"
    )


COMMANDS = {
    "/start": cmd_start,
    "/help": cmd_help,
    "/del_source": cmd_del_source,
    "/status": cmd_status,
    "/set_header": _text_setter("header_text", "header"),
    "/set_footer": _text_setter("footer_text", "footer"),
    "/set_tag": cmd_set_tag,
    "/set_dest": cmd_set_dest,
}


async def handle_admin(event):
    if event.sender_id not in S.db.admins:  # strict admin check (defence in depth)
        return
    via_bot = S.bot is not None and event.client is S.bot
    text = (event.raw_text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        handler = COMMANDS.get(cmd)
        if handler:
            return await handler(event, arg)
        if via_bot:
            await event.reply("❓ <b>Unknown command</b>\n\nSaare commands ke liye /help dekhein.")
        return
    if U.ID_RE.match(text) or U.USERNAME_RE.match(text):  # direct ID -> add source
        return await add_source(event, text)
    if via_bot:
        await event.reply(
            "ℹ️ Source add karne ke liye valid channel/group ID bhejein "
            "(jaise <code>-1001234567890</code>), ya /help dekhein."
        )


async def on_bot_message(event):
    try:
        if not event.is_private:
            return
        if event.sender_id not in S.db.admins:
            return await event.reply(DENIED)
        await handle_admin(event)
    except Exception:
        log.exception("bot handler error")


# ------------------------------------------------------------ deal processing
def has_sendable_media(msg) -> bool:
    if msg.photo:
        return True
    doc = msg.document
    return bool(doc and not msg.sticker and (doc.mime_type or "").startswith(("image/", "video/")))


async def source_media_file(msg):
    """Bots can't reuse the userbot's file reference -> download, then re-upload."""
    data = await S.client.download_media(msg, file=bytes)
    bio = io.BytesIO(data)
    bio.name = "media" + (msg.file.ext if msg.file and msg.file.ext else ".jpg")
    return bio


async def post_deal(msg, deal: U.Deal, use_source_media: bool):
    cfg = S.db.settings
    meta = None
    sender = S.bot or S.client
    target = S.bot_target if S.bot else S.dest
    buttons = [Button.url("🛒 Check Price", deal.url)] if S.bot else None

    async def get_meta():
        nonlocal meta
        if meta is None:
            meta = await U.fetch_product_meta(S.http, deal.page, deal.asin)
        return meta

    title = deal.title or (await get_meta())["title"] or "Amazon Deal"
    text = U.format_post(title, deal.url, cfg.get("header_text"), cfg.get("footer_text"),
                         CAPTION_LIMIT - 24, link_line=not S.bot)

    if use_source_media:
        try:
            media = await source_media_file(msg) if S.bot else msg.media
            await sender.send_file(target, media, caption=text, buttons=buttons, supports_streaming=True)
            return
        except Exception as e:
            log.warning("source media send failed, falling back to Amazon image: %s", e)

    image = await U.download_image(S.http, (await get_meta())["image"])
    if image:
        try:
            await sender.send_file(target, image, caption=text, buttons=buttons)
            return
        except Exception as e:
            log.warning("image send failed, falling back to text: %s", e)
    await sender.send_message(target, text, buttons=buttons, link_preview=False)


async def handle_source(event):
    cid = event.chat_id
    if cid not in S.db.sources or cid == S.dest_id:
        return
    msg = event.message
    if not msg.raw_text:
        return
    tag = S.db.settings.get("affiliate_tag")
    if not tag:
        return log.warning("affiliate_tag not set; ignoring post from %s", cid)
    if not S.dest and not await refresh_dest():
        return log.warning("destination not set/resolvable; ignoring post from %s", cid)

    s, links = U.parse_links(msg)
    deals = await U.build_deals(S.http, s, links, tag)
    if not deals:
        return
    log.info("Source %s -> %d deal(s)", cid, len(deals))
    use_media = has_sendable_media(msg) and (len(deals) == 1 or MULTI_USE_SOURCE_MEDIA)
    async with S.lock:  # keep order + rate limit across overlapping posts
        for d in deals:
            try:
                await post_deal(msg, d, use_media)
            except Exception:
                log.exception("Failed to post deal %s", d.url)
            await asyncio.sleep(SEND_DELAY)


async def on_message(event):
    try:
        if event.is_private:
            # Commands come from admins in DM, or from the account owner in Saved Messages.
            if event.sender_id in S.db.admins and (not event.out or event.chat_id == S.me.id):
                await handle_admin(event)
            return
        await handle_source(event)
    except Exception:
        log.exception("handler error")


# ----------------------------------------------------------------- bootstrap
async def main():
    missing = [k for k, v in {"API_ID": API_ID, "API_HASH": API_HASH, "STRING_SESSION": STRING_SESSION,
                              "DATABASE_URL": DATABASE_URL}.items() if not v]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)} (run `python main.py --login` to get STRING_SESSION)")

    S.lock = asyncio.Lock()
    S.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=20))
    S.db = Database(DATABASE_URL)
    await S.db.connect()
    await S.db.init_schema()

    S.client = TelegramClient(
        StringSession(STRING_SESSION), API_ID, API_HASH,
        connection_retries=-1, retry_delay=3, auto_reconnect=True, flood_sleep_threshold=60,
    )
    S.client.parse_mode = "html"
    await S.client.connect()
    if not await S.client.is_user_authorized():
        sys.exit("STRING_SESSION is invalid/expired. Generate a new one with `python main.py --login`.")
    S.me = await S.client.get_me()

    await S.db.seed([*ENV_ADMINS, S.me.id], os.getenv("DESTINATION_CHANNEL"), os.getenv("AFFILIATE_TAG"))
    await S.db.load()
    await S.client.get_dialogs()  # warm the entity cache (needed for private channels by ID)
    await refresh_dest()

    for raw in re.split(r"[,\s]+", os.getenv("SOURCE_CHANNEL", "")):  # optional env-seeded source(s)
        if not raw or S.db.has_source(U.marked_ids(raw) if U.ID_RE.match(raw) else []):
            continue
        ent = await resolve_chat(raw)
        if isinstance(ent, (Channel, Chat)):
            await S.db.add_source(tl_utils.get_peer_id(ent), tl_utils.get_display_name(ent) or raw, S.me.id)
            log.info("Seeded source from env: %s", raw)
        else:
            log.warning("SOURCE_CHANNEL %s could not be resolved (is the account a member?)", raw)

    if BOT_TOKEN:
        S.bot = TelegramClient(StringSession(), API_ID, API_HASH, flood_sleep_threshold=60)
        S.bot.parse_mode = "html"
        await S.bot.start(bot_token=BOT_TOKEN)
        log.info("Posting via bot @%s", (await S.bot.get_me()).username)
        S.bot.add_event_handler(on_bot_message, events.NewMessage(incoming=True))
        try:  # bot menu (the "/" command list)
            await S.bot(SetBotCommandsRequest(
                scope=BotCommandScopeDefault(), lang_code="",
                commands=[BotCommand(command=c, description=d) for c, d in MENU],
            ))
        except Exception as e:
            log.warning("could not set bot menu: %s", e)
    S.client.add_event_handler(on_message, events.NewMessage())
    log.info("Userbot up as %s | admins=%s | sources=%d | destination=%s",
             S.me.id, sorted(S.db.admins), len(S.db.sources), S.dest_id)
    if not S.db.settings.get("affiliate_tag"):
        log.warning("No affiliate tag yet — send /set_tag <tag> to the userbot.")
    try:
        await S.client.run_until_disconnected()
    finally:
        if S.bot:
            await S.bot.disconnect()
        await S.http.close()
        await S.db.close()


async def login():
    api_id = int(os.getenv("API_ID") or input("API_ID: "))
    api_hash = os.getenv("API_HASH") or input("API_HASH: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as c:  # prompts phone/code/2FA
        print("\n=== STRING_SESSION (keep secret) ===\n" + c.session.save() + "\n")


if __name__ == "__main__":
    asyncio.run(login() if "--login" in sys.argv else main())
