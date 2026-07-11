import asyncio
from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from helper.helper_func import encode

#===============================================================#
# In-memory buffers used to group album/batch uploads (media_group_id)
# together before saving + posting them as a single batch link.
#===============================================================#
_media_group_buffer = {}   # media_group_id -> list[Message]
_media_group_locks = {}    # media_group_id -> True while a debounce task is scheduled

MEDIA_GROUP_WAIT = 2  # seconds to wait for every part of an album to arrive

# Commands that must NOT be treated as "save this as a file post"
EXCLUDED_COMMANDS = [
    'start', 'shortner', 'users', 'broadcast', 'batch', 'genlink', 'stats',
    'pbroadcast', 'db', 'adddb', 'add_db', 'removedb', 'rm_db', 'ban', 'unban',
    'addpremium', 'delpremium', 'premiumusers', 'request', 'profile', 'nbatch',
    'setpostchannel'
]

#===============================================================#

@Client.on_message(filters.private & ~filters.command(EXCLUDED_COMMANDS))
async def channel_post(client: Client, message: Message):
    if message.from_user.id not in client.admins:
        return await message.reply(client.reply_text)

    if message.media_group_id:
        await _handle_media_group(client, message)
    else:
        await _handle_single(client, message)

#===============================================================#

async def _handle_single(client: Client, message: Message):
    reply_text = await message.reply_text("Please Wait...!", quote=True)
    try:
        post_message = await message.copy(chat_id=client.db, disable_notification=True)
    except FloodWait as e:
        await asyncio.sleep(e.x)
        post_message = await message.copy(chat_id=client.db, disable_notification=True)
    except Exception as e:
        print(e)
        await reply_text.edit_text("Something went Wrong..!")
        return

    converted_id = post_message.id * abs(client.db)
    link = await _build_link(client, f"get-{converted_id}")

    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Share URL", url=f'https://telegram.me/share/url?url={link}')]])
    await reply_text.edit(f"<b>Here is your link</b>\n\n{link}", reply_markup=reply_markup, disable_web_page_preview=True)

    if not client.disable_btn:
        await post_message.edit_reply_markup(reply_markup)

    await _auto_post(client, file_count=1, link=link)

#===============================================================#

async def _handle_media_group(client: Client, message: Message):
    gid = message.media_group_id
    _media_group_buffer.setdefault(gid, []).append(message)

    # Only the first arriving message in the group schedules processing;
    # the rest just add themselves to the buffer above and return.
    if gid in _media_group_locks:
        return
    _media_group_locks[gid] = True

    await asyncio.sleep(MEDIA_GROUP_WAIT)
    group_messages = sorted(_media_group_buffer.pop(gid, []), key=lambda m: m.id)
    _media_group_locks.pop(gid, None)

    if not group_messages:
        return

    status = await group_messages[0].reply_text(
        f"Please Wait, saving {len(group_messages)} files...!", quote=True
    )

    copied = []
    try:
        for msg in group_messages:
            try:
                copied_msg = await msg.copy(chat_id=client.db, disable_notification=True)
            except FloodWait as e:
                await asyncio.sleep(e.x)
                copied_msg = await msg.copy(chat_id=client.db, disable_notification=True)
            copied.append(copied_msg)
    except Exception as e:
        print(e)
        await status.edit_text("Something went wrong while saving the batch..!")
        return

    # Messages were copied in order, so their IDs in the DB channel are
    # contiguous -> encode as a normal start-end batch link.
    first_id = copied[0].id * abs(client.db)
    last_id = copied[-1].id * abs(client.db)
    link = await _build_link(client, f"get-{first_id}-{last_id}")

    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Share URL", url=f'https://telegram.me/share/url?url={link}')]])
    await status.edit(
        f"<b>Here is your batch link ({len(copied)} files)</b>\n\n{link}",
        reply_markup=reply_markup, disable_web_page_preview=True
    )

    await _auto_post(client, file_count=len(copied), link=link)

#===============================================================#

async def _build_link(client, string):
    base64_string = await encode(string)
    return f"https://t.me/{client.username}?start={base64_string}"

#===============================================================#

async def _auto_post(client: Client, file_count: int, link: str):
    """Post an announcement with a running post number to the configured
    post channel, if one has been set via /setpostchannel."""
    try:
        post_channel = await client.mongodb.get_bot_setting('post_channel')
    except Exception as e:
        client.LOGGER(__name__, client.name).warning(f"Could not fetch post_channel setting: {e}")
        return

    if not post_channel:
        return

    try:
        post_number = await client.mongodb.get_next_post_number()
    except Exception as e:
        client.LOGGER(__name__, client.name).warning(f"Could not get next post number: {e}")
        return

    file_suffix = f" ({file_count} files)" if file_count > 1 else ""
    caption = f"<b>📁 Post {post_number}{file_suffix}</b>"
    button_text = "📥 Get Batch" if file_count > 1 else "📥 Get File"

    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=link)]])

    try:
        await client.send_message(
            chat_id=int(post_channel),
            text=caption,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    except Exception as e:
        client.LOGGER(__name__, client.name).warning(
            f"Failed to auto-post to post channel {post_channel}: {e}"
        )

#===============================================================#

@Client.on_message(filters.command('setpostchannel') & filters.private)
async def set_post_channel(client: Client, message: Message):
    if message.from_user.id not in client.admins:
        return await message.reply(client.reply_text)

    args = message.text.split()
    if len(args) < 2:
        current = await client.mongodb.get_bot_setting('post_channel')
        return await message.reply(
            f"<b>Usage:</b> <code>/setpostchannel &lt;channel_id&gt;</code>\n\n"
            f"<b>Current post channel:</b> <code>{current or 'Not set'}</code>\n\n"
            f"Send <code>/setpostchannel 0</code> to disable auto-posting."
        )

    try:
        channel_id = int(args[1])
    except ValueError:
        return await message.reply("**✗ Invalid channel ID.**")

    if channel_id == 0:
        await client.mongodb.update_bot_setting('post_channel', None)
        return await message.reply("**✓ Auto-posting has been disabled.**")

    try:
        chat = await client.get_chat(channel_id)
        test = await client.send_message(channel_id, "✅ This channel is now set as the auto-post channel.")
    except Exception as e:
        return await message.reply(
            f"**✗ Error accessing channel:** `{e}`\n\n"
            f"Make sure the bot is an admin in that channel and the ID is correct."
        )

    await client.mongodb.update_bot_setting('post_channel', channel_id)
    await message.reply(f"**✓ Post channel set to:** {chat.title} (`{channel_id}`)")

#===============================================================#

@Client.on_message(filters.channel & filters.incoming)
async def new_post(client: Client, message: Message):
    if message.chat.id != client.db:
        return
    if client.disable_btn:
        return

    converted_id = message.id * abs(client.db)
    string = f"get-{converted_id}"
    base64_string = await encode(string)
    link = f"https://t.me/{client.username}?start={base64_string}"
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Share URL", url=f'https://telegram.me/share/url?url={link}')]])
    try:
        await message.edit_reply_markup(reply_markup)
    except Exception as e:
        print(e)
        pass
