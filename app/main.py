import os
import datetime
import threading

import discord
from discord import app_commands, AllowedMentions
from discord.ext import commands, tasks

from dotenv import load_dotenv
from supabase import create_client
from PIL import Image
from fastapi import FastAPI
import uvicorn

# =====================
# 環境変数
# =====================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GUILD_ID = int(os.getenv("GUILD_ID"))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =====================
# 設定
# =====================
REQUIRED_MINUTES = 6
REQUIRED_SECONDS = REQUIRED_MINUTES * 60

WINDOW_MINUTES = 15

STAMP_NOTIFY_CHANNEL_ID = 1448494342527258788  # 通知テキストチャンネルID
TARGET_VC_ID = 1420270687356190810             # 対象VC ID

IMAGE_DIR = "images"
DATA_DIR = "data"

os.makedirs(DATA_DIR, exist_ok=True)

# VCセッション情報
# vc_sessions[user_id] = {
#   "period": "morning" / "night",
#   "total": float(滞在秒数),
#   "last_join": datetime,
#   "date": date
# }
vc_sessions = {}

# その日のスタンプ済ユーザー
# key = (user_id, period, date)
stamped_users = set()

# 日付リセット用
_last_reset_date = datetime.date.today()

# =====================
# Discord Bot
# =====================
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True

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
# 共通関数
# =====================
def today():
    return datetime.date.today()

def reset_daily_if_needed():
    """日付が変わったら stamped_users / vc_sessions をリセット"""
    global _last_reset_date, stamped_users, vc_sessions
    now_date = today()
    if now_date != _last_reset_date:
        stamped_users.clear()
        vc_sessions.clear()
        _last_reset_date = now_date


def get_period_window(now: datetime.datetime):
    """監視ウィンドウを返す
    monitor: 監視開始 (11:00前に入ってる人の検知用)
    start:   カウント開始 (ここ以降の時間だけカウント)
    end:     判定終了
    """
    today_date = now.date()

    morning = {
        "period": "morning",
        "monitor": datetime.datetime.combine(today_date, datetime.time(10, 40)),
        "start":   datetime.datetime.combine(today_date, datetime.time(11, 0)),
        "end":     datetime.datetime.combine(today_date, datetime.time(11, 15)),
    }

    night = {
        "period": "night",
        "monitor": datetime.datetime.combine(today_date, datetime.time(22, 40)),
        "start":   datetime.datetime.combine(today_date, datetime.time(23, 0)),
        "end":     datetime.datetime.combine(today_date, datetime.time(23, 15)),
    }

    for w in (morning, night):
        if w["monitor"] <= now <= w["end"]:
            return w

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
        .execute()
        .data
    )

    dates = sorted(
        datetime.date.fromisoformat(r["stamp_date"]) for r in rows
    )

    total = len(dates)

    # 最大連続
    max_streak = 0
    streak = 0
    prev = None

    for d in dates:
        if prev and (d - prev).days == 1:
            streak += 1
        else:
            streak = 1
        max_streak = max(max_streak, streak)
        prev = d

    # 現在連続
    current_streak = 0
    if dates:
        current_streak = 1
        for i in range(len(dates) - 1, 0, -1):
            if (dates[i] - dates[i - 1]).days == 1:
                current_streak += 1
            else:
                break

    return total, current_streak, max_streak

# =====================
# カレンダー作成
# =====================
def find_calendar_image(period: str, ym: str):
    # ファイル名を統一
    if period == "morning":
        name = f"calendar_base_{ym}.png"
    else:
        name = f"calendar_nt_base_{ym}.png"

    path = os.path.join(IMAGE_DIR, name)
    if os.path.exists(path):
        return path

    # なければ最新の画像を使う（すべての calendar_*.png を候補に）
    files = sorted(
        f for f in os.listdir(IMAGE_DIR)
        if f.startswith("calendar_") and f.endswith(".png")
    )
    if not files:
        raise FileNotFoundError("カレンダー画像がありません")

    return os.path.join(IMAGE_DIR, files[-1])


def create_calendar(user_id: int, period: str):
    now = datetime.date.today()
    ym = now.strftime("%Y_%m")

    base_path = find_calendar_image(period, ym)
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

    # ===== 設定 =====
    CELL_W = 320
    CELL_H = 265
    STAMP_SIZE = 250
    START_X = 155
    START_Y = 395

    # スタンプ画像
    stamp_img = Image.open(
        os.path.join(IMAGE_DIR, "stamp.png")
    ).convert("RGBA")

    stamp_img = stamp_img.resize(
        (STAMP_SIZE, STAMP_SIZE),
        Image.Resampling.LANCZOS
    )

    # 月初の曜日
    first_day = datetime.date(now.year, now.month, 1)
    first_weekday = first_day.weekday()      # 月曜=0
    start_col = (first_weekday + 1) % 7      # 日曜始まり

    # ===== スタンプ配置 =====
    for r in rows:
        d = datetime.date.fromisoformat(r["stamp_date"])

        if d.year != now.year or d.month != now.month:
            continue

        index = start_col + (d.day - 1)
        col = index % 7
        row = index // 7

        x = START_X + col * CELL_W
        y = START_Y + row * CELL_H

        x_center = x + (CELL_W - STAMP_SIZE) // 2
        y_center = y + (CELL_H - STAMP_SIZE) // 2

        img.paste(stamp_img, (x_center, y_center), stamp_img)

    img.save(output_path)
    return output_path

# =====================
# スタンプコマンド
# =====================
async def send_stamp(interaction: discord.Interaction, period: str):
    await interaction.response.defer(thinking=True)

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

@bot.tree.command(
    name="stamp_m",
    description="朝のスタンプカードと参加記録を表示"
)
async def stamp_m(interaction: discord.Interaction):
    await send_stamp(interaction, "morning")

@bot.tree.command(
    name="stamp_n",
    description="夜のスタンプカードと参加記録を表示"
)
async def stamp_n(interaction: discord.Interaction):
    await send_stamp(interaction, "night")

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

@bot.tree.command(name="ranking_morning_total", description="朝のトータル参加ランキングを表示")
async def rmt(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("朝 トータル", get_ranking("morning"))
    )

@bot.tree.command(name="ranking_morning_month", description="朝の月間参加ランキングを表示")
async def rmm(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("朝 今月", get_ranking("morning", True))
    )

@bot.tree.command(name="ranking_night_total", description="夜のトータル参加ランキングを表示")
async def rnt(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("夜 トータル", get_ranking("night"))
    )

@bot.tree.command(name="ranking_night_month", description="夜の月間参加ランキングを表示")
async def rnm(interaction: discord.Interaction):
    await interaction.response.send_message(
        ranking_text("夜 今月", get_ranking("night", True))
    )

# =====================
# スタンプ通知（共通）
# =====================
async def notify_stamp_success(member: discord.Member, period: str):
    channel = bot.get_channel(STAMP_NOTIFY_CHANNEL_ID)
    if not channel:
        return

    label = "🌅 朝" if period == "morning" else "🌙 夜"

    await channel.send(
        f"{member.mention} {label}のスタンプを獲得しました！🎉",
        allowed_mentions=AllowedMentions(users=True)
    )

# =====================
# VC監視 & スタンプロジック
# =====================
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    reset_daily_if_needed()

    now = datetime.datetime.now()
    window = get_period_window(now)
    if not window:
        return

    period = window["period"]
    start_time = window["start"]
    end_time = window["end"]

    key_today = (member.id, period, today())

    # すでにスタンプ済みなら何もしない
    if key_today in stamped_users:
        return

    # ===== VC入室（対象VCに入ったとき） =====
    if after.channel and after.channel.id == TARGET_VC_ID:
        session = vc_sessions.get(member.id)

        if not session or session.get("date") != today() or session.get("period") != period:
            # 新しいセッションを開始
            vc_sessions[member.id] = {
                "period": period,
                "total": 0.0,
                "last_join": now,
                "date": today(),
            }
        else:
            # 同じ日・同じ部。再入室なので last_join を更新
            session["last_join"] = now

        return

    # ===== VC退出 or 他チャンネルへ移動（対象VCから出たとき） =====
    if before.channel and before.channel.id == TARGET_VC_ID:
        session = vc_sessions.get(member.id)
        if not session:
            return

        # この退出までの滞在時間をカウント（カウント時間帯に補正）
        effective_join = max(session["last_join"], start_time)
        effective_leave = min(now, end_time)

        if effective_leave > effective_join:
            delta = (effective_leave - effective_join).total_seconds()
            session["total"] += delta

        # 6分達成したか判定
        if session["total"] >= REQUIRED_SECONDS:
            if key_today not in stamped_users:
                success = record_stamp(member.id, period)
                stamped_users.add(key_today)

                if success:
                    await notify_stamp_success(member, period)

        # 対象VCから出たのでセッションは終了
        vc_sessions.pop(member.id, None)

# =====================
# 自動判定タスク（退出しなくてもスタンプを押す）
# =====================
@tasks.loop(seconds=30)
async def check_auto_stamp():
    reset_daily_if_needed()

    now = datetime.datetime.now()
    window = get_period_window(now)
    if not window:
        return

    period = window["period"]
    start_time = window["start"]
    end_time = window["end"]

    # 判定時間を過ぎたら、自動でその時点の滞在を締めて判定
    if now < end_time:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    # 辞書をコピーしてイテレート（中で pop するため）
    for user_id, session in list(vc_sessions.items()):
        # 他の日や他の部のセッションなら無視
        if session.get("date") != today() or session.get("period") != period:
            continue

        member = guild.get_member(user_id)
        if not member:
            vc_sessions.pop(user_id, None)
            continue

        key_today = (user_id, period, today())

        # すでにスタンプ済みならセッション削除だけ
        if key_today in stamped_users:
            vc_sessions.pop(user_id, None)
            continue

        # 判定時間終了時点までの滞在を締める
        effective_join = max(session["last_join"], start_time)
        effective_leave = end_time

        if effective_leave > effective_join:
            delta = (effective_leave - effective_join).total_seconds()
            session["total"] += delta

        # 6分達成していればスタンプ付与
        if session["total"] >= REQUIRED_SECONDS:
            success = record_stamp(user_id, period)
            stamped_users.add(key_today)

            if success:
                await notify_stamp_success(member, period)

        # 判定時間を過ぎたのでセッション終了
        vc_sessions.pop(user_id, None)

# =====================
# 起動時処理
# =====================
@bot.event
async def setup_hook():
    guild = discord.Object(id=GUILD_ID)

    # ギルドコマンドを一度クリアしてから同期
    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)
    print("✅ Guild slash commands RESET & synced")

    # 自動判定タスクを開始
    check_auto_stamp.start()

# =====================
# メイン
# =====================
if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    bot.run(TOKEN)
