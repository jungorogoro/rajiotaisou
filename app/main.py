# =============================
# Discord Bot + Supabase 永続保存 完全版
# Koyeb / Docker 対応
# =============================

import os
import json
import calendar
import asyncio
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
# 環境変数 / 初期設定
# =============================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))
GUILD_ID = int(os.getenv("GUILD_ID"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase 環境変数が設定されていません")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# JST固定
JST = timezone(timedelta(hours=9))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =============================
# Discord Bot
# =============================

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# VC滞在管理
join_times = {}  # {user_id: {"start": datetime, "acc": seconds}}
stamped_users = set()
stamped_reset_date = None

# =============================
# 時間判定
# =============================

def now_jst():
    return datetime.datetime.now(JST)


def today_jst():
    return now_jst().date()


def is_radio_time():
    t = now_jst().time()
    return datetime.time(11, 0) <= t <= datetime.time(11, 15)

# =============================
# Supabase 操作
# =============================

def save_stamp_by_uid(user_id: int) -> bool:
    """当日のスタンプを保存（重複はDB制約で防止）"""
    try:
        supabase.table("stamps").insert({
            "user_id": user_id,
            "stamp_date": today_jst().isoformat()
        }).execute()
        return True
    except Exception:
        return False


def get_user_stamps(user_id: int):
    res = supabase.table("stamps") \
        .select("stamp_date") \
        .eq("user_id", user_id) \
        .execute()
    return [r["stamp_date"] for r in res.data]


def get_all_stamps():
    res = supabase.table("stamps") \
        .select("user_id, stamp_date") \
        .execute()
    return res.data

# =============================
# VC 入退室
# =============================

@bot.event
async def on_voice_state_update(member, before, after):
    if not is_radio_time() or member.bot:
        return

    uid = member.id
    now = now_jst()

    # 入室
    if after.channel and after.channel.id == VOICE_CHANNEL_ID:
        rec = join_times.get(uid)
        if not rec:
            join_times[uid] = {"start": now, "acc": 0.0}
        elif rec.get("start") is None:
            rec["start"] = now
        return

    # 退出
    left = (
        before.channel and before.channel.id == VOICE_CHANNEL_ID
        and (not after.channel or after.channel.id != VOICE_CHANNEL_ID)
    )

    if not left:
        return

    rec = join_times.get(uid)
    if not rec:
        return

    if rec.get("start"):
        rec["acc"] += (now - rec["start"]).total_seconds()
        rec["start"] = None

    if rec["acc"] >= 480:
        if save_stamp_by_uid(uid):
            stamped_users.add(uid)

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

    if not is_radio_time():
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if not channel:
        return

    now = now_jst()

    for member in list(channel.members):
        if member.bot:
            continue

        uid = member.id
        if uid in stamped_users:
            continue

        rec = join_times.get(uid)
        if not rec:
            join_times[uid] = {"start": now, "acc": 0.0}
            continue

        elapsed = 0
        if rec.get("start"):
            elapsed = (now - rec["start"]).total_seconds()

        total = rec["acc"] + elapsed

        if total >= 480:
            if save_stamp_by_uid(uid):
                stamped_users.add(uid)
            join_times.pop(uid, None)

@auto_stamp_check.before_loop
async def before_auto():
    await bot.wait_until_ready()

# =============================
# 統計
# =============================

def calc_stats(dates):
    if not dates:
        return {"total": 0, "current_streak": 0, "max_streak": 0}

    ds = sorted(datetime.date.fromisoformat(d) for d in dates)
    total = len(ds)

    today = today_jst()
    s = set(ds)
    cur = 0
    d = today
    while d in s:
        cur += 1
        d -= timedelta(days=1)

    max_s = 1
    tmp = 1
    for i in range(1, len(ds)):
        if (ds[i] - ds[i-1]).days == 1:
            tmp += 1
        else:
            tmp = 1
        max_s = max(max_s, tmp)

    return {"total": total, "current_streak": cur, "max_streak": max_s}

# =============================
# カレンダー画像
# =============================

def day_positions():
    pos = {}
    start_x, start_y = 155, 395
    cell_w, cell_h = 320, 265
    stamp_w, stamp_h = 250, 250
    ox = (cell_w - stamp_w) // 2
    oy = (cell_h - stamp_h) // 2

    today = today_jst()
    y, m = today.year, today.month
    first = datetime.date(y, m, 1).weekday()
    start_col = (first + 1) % 7
    last_day = calendar.monthrange(y, m)[1]

    for day in range(1, last_day + 1):
        idx = start_col + (day - 1)
        r, c = divmod(idx, 7)
        x = start_x + c * cell_w + ox
        y_ = start_y + r * cell_h + oy
        pos[day] = (x, y_)
    return pos


def create_calendar(user_id: int):
    today = today_jst()
    ym = today.strftime("%Y_%m")

    base = os.path.join(BASE_DIR, "images", f"calendar_base_{ym}.png")
    out = os.path.join(BASE_DIR, "data", f"calendar_{user_id}.png")

    img = Image.open(base).convert("RGBA")
    stamp = Image.open(os.path.join(BASE_DIR, "images", "stamp.png")).convert("RGBA")
    stamp = stamp.resize((250, 250))

    dates = get_user_stamps(user_id)
    pos = day_positions()

    for d in dates:
        dt = datetime.date.fromisoformat(d)
        if dt.month == today.month and dt.day in pos:
            img.paste(stamp, pos[dt.day], stamp)

    img.save(out)
    return out

# =============================
# Slash Commands
# =============================

@bot.tree.command(name="stamp", description="自分のスタンプ帳を表示")
async def stamp(interaction: discord.Interaction):
    await interaction.response.defer()

    path = create_calendar(interaction.user.id)
    dates = get_user_stamps(interaction.user.id)
    stats = calc_stats(dates)

    msg = (
        f"📊 参加記録\n"
        f"✅ 総参加日数: {stats['total']}日\n"
        f"🔥 継続中: {stats['current_streak']}日\n"
        f"🏆 最高: {stats['max_streak']}日"
    )

    await interaction.followup.send(content=msg, file=discord.File(path))


@bot.tree.command(name="ranking", description="ランキング表示")
async def ranking(interaction: discord.Interaction):
    await interaction.response.defer()

    data = get_all_stamps()

    totals = {}
    streaks = {}

    for r in data:
        uid = r["user_id"]
        totals.setdefault(uid, []).append(r["stamp_date"])

    scores_total = []
    scores_streak = []

    for uid, dates in totals.items():
        stats = calc_stats(dates)
        scores_total.append((uid, stats["total"]))
        scores_streak.append((uid, stats["max_streak"]))

    scores_total.sort(key=lambda x: x[1], reverse=True)
    scores_streak.sort(key=lambda x: x[1], reverse=True)

    async def name(uid):
        m = interaction.guild.get_member(uid)
        return m.display_name if m else str(uid)

    text = "🏆 **ランキング**\n\n【総参加】\n"
    for i, (uid, s) in enumerate(scores_total[:10]):
        text += f"{i+1}位: {await name(uid)} - {s}日\n"

    text += "\n【連続】\n"
    for i, (uid, s) in enumerate(scores_streak[:10]):
        text += f"{i+1}位: {await name(uid)} - {s}日\n"

    await interaction.followup.send(text)

# =============================
# 起動処理
# =============================

@bot.event
async def setup_hook():
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        print(e)

    auto_stamp_check.start()


def run_api():
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    threading.Thread(target=run_api, daemon=True).start()
    bot.run(TOKEN)
