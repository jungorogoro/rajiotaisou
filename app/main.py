import os
import datetime
import calendar
import asyncio

import discord
from discord.ext import commands, tasks
from PIL import Image
from supabase import create_client

# ========= 環境変数 =========
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========= Discord =========
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

join_times = {}

# ========= 時間帯判定 =========
def get_period():
    now = datetime.datetime.now().time()
    if datetime.time(11, 0) <= now <= datetime.time(11, 15):
        return "morning"
    if datetime.time(23, 0) <= now <= datetime.time(23, 15):
        return "night"
    return None

# ========= Supabase保存 =========
def save_stamp(user_id: int, period: str):
    today = datetime.date.today().isoformat()
    supabase.table("stamps").insert({
        "user_id": user_id,
        "stamp_date": today,
        "period": period
    }).execute()

# ========= VC監視 =========
@bot.event
async def on_voice_state_update(member, before, after):
    period = get_period()
    if not period:
        return

    uid = member.id
    now = datetime.datetime.now()

    if after.channel and after.channel.id == VOICE_CHANNEL_ID:
        join_times[uid] = now
        return

    if before.channel and before.channel.id == VOICE_CHANNEL_ID:
        start = join_times.pop(uid, None)
        if start and (now - start).total_seconds() >= 480:
            save_stamp(uid, period)

# ========= カレンダー生成 =========
def create_calendar(user_id: int, period: str):
    today = datetime.date.today()
    ym = today.strftime("%Y_%m")
    ym_key = today.strftime("%Y-%m")

    base = (
        f"images/calendar_base_{ym}.png"
        if period == "morning"
        else f"images/calendar_nt_base{ym}.png"
    )

    out = f"/tmp/calendar_{user_id}_{period}.png"
    img = Image.open(base).convert("RGBA")
    stamp = Image.open("images/stamp.png").convert("RGBA").resize((250, 250))

    data = supabase.table("stamps").select("*").eq("user_id", user_id).eq("period", period).execute().data

    positions = {}
    first = datetime.date(today.year, today.month, 1).weekday()
    start_col = (first + 1) % 7
    for i in range(1, calendar.monthrange(today.year, today.month)[1] + 1):
        idx = start_col + i - 1
        positions[i] = (155 + (idx % 7) * 320, 395 + (idx // 7) * 265)

    for row in data:
        d = int(row["stamp_date"][-2:])
        img.paste(stamp, positions[d], stamp)

    img.save(out)
    return out

# ========= コマンド =========
@bot.tree.command(name="stamp_morning")
async def stamp_morning(interaction: discord.Interaction):
    await interaction.response.defer()
    path = create_calendar(interaction.user.id, "morning")
    await interaction.followup.send(file=discord.File(path))

@bot.tree.command(name="stamp_night")
async def stamp_night(interaction: discord.Interaction):
    await interaction.response.defer()
    path = create_calendar(interaction.user.id, "night")
    await interaction.followup.send(file=discord.File(path))

@bot.tree.command(name="ranking")
async def ranking(interaction: discord.Interaction):
    res = supabase.table("stamps").select("user_id, period").execute().data

    scores = {"morning": {}, "night": {}}
    for r in res:
        scores[r["period"]][r["user_id"]] = scores[r["period"]].get(r["user_id"], 0) + 1

    def top(p):
        return sorted(scores[p].items(), key=lambda x: x[1], reverse=True)[:5]

    msg = "🏆 **ランキング**\n\n🌅 朝\n"
    for i, (u, c) in enumerate(top("morning"), 1):
        msg += f"{i}位 <@{u}> {c}回\n"

    msg += "\n🌙 夜\n"
    for i, (u, c) in enumerate(top("night"), 1):
        msg += f"{i}位 <@{u}> {c}回\n"

    await interaction.response.send_message(msg)

# ========= 起動 =========
@bot.event
async def setup_hook():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

bot.run(TOKEN)
