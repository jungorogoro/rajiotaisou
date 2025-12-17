# =============================
# Discord Bot + Supabase 永続保存 完全版
# 朝・夜 完全分離 / ランキング別 / Koyeb & Docker 対応
# =============================

import os
import calendar
import datetime
from datetime import timedelta, timezone
import threading

import discord
from discord.ext import commands, tasks
from PIL import Image

from dotenv import load_dotenv
from supabase import create_client
import uvicorn
from server import app

# =============================
# 環境変数
# =============================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))
GUILD_ID = int(os.getenv("GUILD_ID"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase 環境変数が未設定")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# JST
JST = timezone(timedelta(hours=9))

def now_jst():
    return datetime.datetime.now(JST)

def today_jst():
    return now_jst().date()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =============================
# Discord Bot
# =============================
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

join_times = {}
stamped_users = set()
stamped_reset_date = None

# =============================
# 時間判定
# =============================

def is_morning_time():
    t = now_jst().time()
    return datetime.time(11, 0) <= t <= datetime.time(11, 15)


def is_night_time():
    t = now_jst().time()
    return datetime.time(23, 0) <= t <= datetime.time(23, 15)

# =============================
# Supabase 操作
# =============================

def save_stamp(table: str, user_id: int) -> bool:
    try:
        supabase.table(table).insert({
            "user_id": user_id,
            "stamp_date": today_jst().isoformat()
        }).execute()
        return True
    except Exception:
        return False


def get_user_stamps(table: str, user_id: int):
    res = supabase.table(table).select("stamp_date").eq("user_id", user_id).execute()
    return [r["stamp_date"] for r in res.data]


def get_all_stamps(table: str):
    return supabase.table(table).select("user_id, stamp_date").execute().data

# =============================
# VC 入退室
# =============================

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    mode = None
    if is_morning_time():
        mode = "stamps"
    elif is_night_time():
        mode = "stamps_night"
    else:
        return

    uid = member.id
    now = now_jst()

    if after.channel and after.channel.id == VOICE_CHANNEL_ID:
        join_times.setdefault((uid, mode), {"start": now, "acc": 0})
        join_times[(uid, mode)]["start"] = now
        return

    if before.channel and before.channel.id == VOICE_CHANNEL_ID:
        rec = join_times.get((uid, mode))
        if not rec:
            return
        if rec["start"]:
            rec["acc"] += (now - rec["start"]).total_seconds()
            rec["start"] = None

        if rec["acc"] >= 480:
            save_stamp(mode, uid)
            stamped_users.add((uid, mode))
            join_times.pop((uid, mode), None)

# =============================
# 定期チェック
# =============================

@tasks.loop(seconds=20)
async def auto_stamp_check():
    global stamped_reset_date

    today = today_jst().isoformat()
    if stamped_reset_date != today:
        stamped_users.clear()
        stamped_reset_date = today

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if not channel:
        return

    for member in channel.members:
        for mode in ("stamps", "stamps_night"):
            if (member.id, mode) in stamped_users:
                continue

            rec = join_times.get((member.id, mode))
            if not rec:
                continue

            elapsed = 0
            if rec.get("start"):
                elapsed = (now_jst() - rec["start"]).total_seconds()

            if rec["acc"] + elapsed >= 480:
                save_stamp(mode, member.id)
                stamped_users.add((member.id, mode))
                join_times.pop((member.id, mode), None)

@auto_stamp_check.before_loop
async def before_auto():
    await bot.wait_until_ready()

# =============================
# 統計
# =============================

def calc_stats(dates):
    if not dates:
        return {"total": 0, "current": 0, "max": 0}

    ds = sorted(datetime.date.fromisoformat(d) for d in dates)
    s = set(ds)

    total = len(ds)
    cur = 0
    d = today_jst()
    while d in s:
        cur += 1
        d -= timedelta(days=1)

    max_s = tmp = 1
    for i in range(1, len(ds)):
        if (ds[i] - ds[i-1]).days == 1:
            tmp += 1
        else:
            tmp = 1
        max_s = max(max_s, tmp)

    return {"total": total, "current": cur, "max": max_s}

# =============================
# カレンダー生成
# =============================

def create_calendar(user_id: int, night=False):
    today = today_jst()
    ym = today.strftime("%Y_%m")

    base = f"calendar_nt_base{ym}.png" if night else f"calendar_base_{ym}.png"
    base_path = os.path.join(BASE_DIR, "images", base)

    out = os.path.join(BASE_DIR, "data", f"calendar_{'night_' if night else ''}{user_id}.png")

    img = Image.open(base_path).convert("RGBA")
    stamp = Image.open(os.path.join(BASE_DIR, "images", "stamp.png")).convert("RGBA").resize((250, 250))

    table = "stamps_night" if night else "stamps"
    dates = get_user_stamps(table, user_id)

    first = datetime.date(today.year, today.month, 1).weekday()
    start_col = (first + 1) % 7

    for d in dates:
        dt = datetime.date.fromisoformat(d)
        if dt.month != today.month:
            continue
        idx = start_col + (dt.day - 1)
        r, c = divmod(idx, 7)
        x = 155 + c * 320 + 35
        y = 395 + r * 265 + 7
        img.paste(stamp, (x, y), stamp)

    img.save(out)
    return out

# =============================
# Slash Commands
# =============================

@bot.tree.command(name="stamp")
async def stamp(interaction: discord.Interaction):
    await interaction.response.defer()
    path = create_calendar(interaction.user.id, night=False)
    stats = calc_stats(get_user_stamps("stamps", interaction.user.id))
    await interaction.followup.send(
        content=f"🌅 朝\n総:{stats['total']} 連続:{stats['current']} 最大:{stats['max']}",
        file=discord.File(path)
    )


@bot.tree.command(name="stamp_night")
async def stamp_night(interaction: discord.Interaction):
    await interaction.response.defer()
    path = create_calendar(interaction.user.id, night=True)
    stats = calc_stats(get_user_stamps("stamps_night", interaction.user.id))
    await interaction.followup.send(
        content=f"🌙 夜\n総:{stats['total']} 連続:{stats['current']} 最大:{stats['max']}",
        file=discord.File(path)
    )


@bot.tree.command(name="ranking_morning")
async def ranking_morning(interaction: discord.Interaction):
    await interaction.response.defer()
    data = get_all_stamps("stamps")
    scores = {}
    for r in data:
        scores.setdefault(r["user_id"], []).append(r["stamp_date"])
    text = "🌅 朝ランキング\n"
    for uid, dates in sorted(scores.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
        m = interaction.guild.get_member(uid)
        text += f"{m.display_name if m else uid}: {len(dates)}日\n"
    await interaction.followup.send(text)


@bot.tree.command(name="ranking_night")
async def ranking_night(interaction: discord.Interaction):
    await interaction.response.defer()
    data = get_all_stamps("stamps_night")
    scores = {}
    for r in data:
        scores.setdefault(r["user_id"], []).append(r["stamp_date"])
    text = "🌙 夜ランキング\n"
    for uid, dates in sorted(scores.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
        m = interaction.guild.get_member(uid)
        text += f"{m.display_name if m else uid}: {len(dates)}日\n"
    await interaction.followup.send(text)

# =============================
# 起動
# =============================

@bot.event
async def setup_hook():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    auto_stamp_check.start()


def run_api():
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    threading.Thread(target=run_api, daemon=True).start()
    bot.run(TOKEN)
