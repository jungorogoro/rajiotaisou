import os
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from supabase import create_client
from PIL import Image
from fastapi import FastAPI
import threading
import uvicorn

# =====================
# 環境変数
# =====================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =====================
# Discord Bot
# =====================
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True


bot = commands.Bot(command_prefix="!", intents=intents)

# =====================
# FastAPI (Koyeb用)
# =====================
app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

def start_server():
    uvicorn.run(app, host="0.0.0.0", port=8080)

# =====================
# 設定
# =====================
MORNING_START = datetime.time(11, 0)
NIGHT_START = datetime.time(23, 0)
REQUIRED_MINUTES = 8

IMAGE_DIR = "images"
DATA_DIR = "data"

os.makedirs(DATA_DIR, exist_ok=True)

# =====================
# 共通関数
# =====================
def today():
    return datetime.date.today()

def is_time_between(start):
    now = datetime.datetime.now()
    start_dt = datetime.datetime.combine(now.date(), start)
    end_dt = start_dt + datetime.timedelta(minutes=REQUIRED_MINUTES)
    return start_dt <= now <= end_dt

def get_period():
    if is_time_between(MORNING_START):
        return "morning"
    if is_time_between(NIGHT_START):
        return "night"
    return None

# =====================
# スタンプ記録
# =====================
def record_stamp(user_id: int, period: str):
    exists = (
        supabase.table("stamps")
        .select("id")
        .eq("user_id", user_id)
        .eq("period", period)
        .eq("stamp_date", today().isoformat())
        .execute()
        .data
    )

    if exists:
        return False

    supabase.table("stamps").insert({
        "user_id": user_id,
        "stamp_date": today().isoformat(),
        "period": period
    }).execute()
    return True

# =====================
# 統計計算
# =====================
def calc_stats(user_id: int, period: str):
    rows = (
        supabase.table("stamps")
        .select("stamp_date")
        .eq("user_id", user_id)
        .eq("period", period)
        .order("stamp_date")
        .execute()
        .data
    )

    dates = [datetime.date.fromisoformat(r["stamp_date"]) for r in rows]
    total = len(dates)

    max_streak = 0
    current_streak = 0
    streak = 0
    prev = None

    for d in dates:
        if prev and (d - prev).days == 1:
            streak += 1
        else:
            streak = 1
        max_streak = max(max_streak, streak)
        prev = d

    if dates and dates[-1] == today():
        current_streak = streak

    return total, current_streak, max_streak

# =====================
# カレンダー作成
# =====================
def create_calendar(user_id: int, period: str):
    now = datetime.date.today()
    ym = now.strftime("%Y_%m")

    # 画像ファイル名（仕様どおり）
    if period == "morning":
        base_name = f"calendar_base_{ym}.png"
    else:
        base_name = f"calendar_nt_base{ym}.png"  # ← ここ重要（_なし）

    base_path = os.path.join(IMAGE_DIR, base_name)
    output_path = os.path.join(DATA_DIR, f"{user_id}_{period}_{ym}.png")

    img = Image.open(base_path).convert("RGBA")

    rows = (
        supabase.table("stamps")
        .select("stamp_date")
        .eq("user_id", user_id)
        .eq("period", period)
        .execute()
        .data
    )

    stamp_img = Image.open(
        os.path.join(IMAGE_DIR, "stamp.png")
    ).convert("RGBA")

    for r in rows:
        d = datetime.date.fromisoformat(r["stamp_date"])

        # 今月分のみ反映
        if d.year != now.year or d.month != now.month:
            continue

        x = 50 + (d.day - 1) % 7 * 100
        y = 200 + (d.day - 1) // 7 * 100

        img.paste(stamp_img, (x, y), stamp_img)

    img.save(output_path)
    return output_path

# =====================
# スタンプコマンド
# =====================
@bot.tree.command(
    name="stamp_m",
    description="朝のスタンプカードと参加記録を表示"
)
async def send_stamp(interaction: discord.Interaction, period: str):
await interaction.response.defer(thinking=True)  # ← これが超重要

    user_id = interaction.user.id

    total, current, max_streak = calc_stats(user_id, period)
    img_path = create_calendar(user_id, period)

    label = "🌅 朝" if period == "morning" else "🌙 夜"

    text = (
        f"{label}の参加記録\n"
        f"✅ 総参加日数：{total}日\n"
        f"🔥 連続参加中：{current}日\n"
        f"🏆 最多連続：{max_streak}日"
    )

    await interaction.followup.send(
        content=text,
        file=discord.File(img_path)
    )


# =====================
# ランキング
# =====================
def get_ranking(period: str, month_only=False):
    q = supabase.table("stamps").select("user_id, stamp_date").eq("period", period)
    if month_only:
        first = today().replace(day=1).isoformat()
        q = q.gte("stamp_date", first)

    rows = q.execute().data
    scores = {}
    for r in rows:
        scores[r["user_id"]] = scores.get(r["user_id"], 0) + 1

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:10]

def ranking_text(title, data):
    msg = f"🏆 **{title}**\n"
    for i, (u, c) in enumerate(data, 1):
        msg += f"{i}位 <@{u}> {c}回\n"
    return msg

@bot.tree.command(name="ranking_morning_total")
async def rmt(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("朝 トータル", get_ranking("morning"))
    )

@bot.tree.command(name="ranking_morning_month")
async def rmm(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("朝 今月", get_ranking("morning", True))
    )

@bot.tree.command(name="ranking_night_total")
async def rnt(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("夜 トータル", get_ranking("night"))
    )

@bot.tree.command(name="ranking_night_month")
async def rnm(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("夜 今月", get_ranking("night", True))
    )

# =====================
# 起動
# =====================
@bot.event
async def setup_hook():
    # 全体（グローバル）同期
    await bot.tree.sync()
    print("✅ Global slash commands synced")



if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    bot.run(TOKEN)







