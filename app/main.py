import os
from dotenv import load_dotenv

import json
import datetime
import calendar
import asyncio
import discord
from discord.ext import commands, tasks
from PIL import Image # type: ignore

load_dotenv()  # .env読み込み

TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))
GUILD_ID = int(os.getenv("GUILD_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
# === intents / bot ===
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# join_times の構造を統一:
# { user_id: {"start": datetime, "acc": float_seconds} }
join_times = {}

# 当日スタンプ済みユーザー（ユーザーIDの集合）
stamped_users = set()
# 最後に stamped_users をリセットした日（ISO文字列）
stamped_reset_date = None

# データファイル
DATA_FILE = "data/stamps.json"
os.makedirs("data", exist_ok=True)


# ==========================
# データ処理
# ==========================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==========================
# 時間チェック
# ==========================
def is_radio_time():
    # 11:00〜11:15 の間のみ有効（タイムゾーンはホストマシンのローカル時間）
    now = datetime.datetime.now().time()
    return datetime.time(11, 0) <= now <= datetime.time(11, 15)


# ==========================
# スタンプ付与ヘルパー
# ==========================
def save_stamp_by_uid(uid_int: int):
    """UID（int）で当日分のスタンプを保存（重複防止）"""
    today = datetime.date.today().isoformat()
    month = today[:7]
    uid = str(uid_int)

    data = load_data()
    if uid not in data:
        data[uid] = {}

    if month not in data[uid]:
        data[uid][month] = []

    if today not in data[uid][month]:
        data[uid][month].append(today)
        save_data(data)
        return True
    return False

async def give_stamp(user_id: int):
    """非同期版ラッパー（UI 用に呼ぶときは await）"""
    added = save_stamp_by_uid(user_id)
    if added:
        stamped_users.add(user_id)
    return added


# ==========================
# ボイス参加の記録（入退室イベント）
# ==========================
@bot.event
async def on_voice_state_update(member, before, after):
    # is_radio_time() の外では無視
    if not is_radio_time():
        return

    uid = member.id

    # --- 入室時 ---
    if after.channel and after.channel.id == VOICE_CHANNEL_ID:
        rec = join_times.get(uid)
        if rec is None:
            # 新規に開始時刻セット、accは0
            join_times[uid] = {"start": datetime.datetime.now(), "acc": 0.0}
        else:
            # 再入室（startがNoneなら再設定）
            if rec.get("start") is None:
                rec["start"] = datetime.datetime.now()
        return

    # --- 退室・別チャンネル移動時 ---
    left_channel = (
        before.channel and before.channel.id == VOICE_CHANNEL_ID
        and (not after.channel or after.channel.id != VOICE_CHANNEL_ID)
    )
    if not left_channel:
        return

    rec = join_times.get(uid)
    if not rec:
        return

    # 加算（start があるなら）
    if rec.get("start"):
        rec["acc"] += (datetime.datetime.now() - rec["start"]).total_seconds()
        rec["start"] = None

    # 閾値（8分 = 480秒）を満たしたら付与
    if rec["acc"] >= 480:
        await give_stamp(uid)

    # ※ メモリは残しておく（再入室で加算を続けられる）
    # ただし、stamped_users がある場合はこれ以上付与不要（give_stamp 内で重複は弾かれる）


# ==========================
# 自動チェックタスク（滞在中の人を定期的にチェック）
# ==========================
@tasks.loop(seconds=20)
async def auto_stamp_check():
    global stamped_reset_date

    # 起動中に日付変わりで stamped_users をリセットする
    today_iso = datetime.date.today().isoformat()
    if stamped_reset_date != today_iso:
        stamped_users.clear()
        stamped_reset_date = today_iso

    if not is_radio_time():
        return

    now = datetime.datetime.now()

    # チャンネル取得（guild経由の方が確実）
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if not channel:
        return

    # 現在VCにいるメンバーについて start/accを更新し、閾値到達で付与
    for member in list(channel.members):
        if member.bot:
            continue
        uid = member.id

        # stamped_users にすでに入っているならスキップ
        if uid in stamped_users:
            continue

        rec = join_times.get(uid)
        if rec is None:
            # 新規参加（イベントが来なかった場合に備えてここでstartを入れる）
            join_times[uid] = {"start": now, "acc": 0.0}
            continue

        # startがある場合は経過時間を計算（まだ抜けていない）
        if rec.get("start"):
            elapsed = (now - rec["start"]).total_seconds()
        else:
            elapsed = 0.0

        total = rec.get("acc", 0.0) + elapsed

        if total >= 480:
            added = save_stamp_by_uid(uid)
            if added:
                stamped_users.add(uid)
            # 付与後は記録を初期化して二重付与を防ぐ
            join_times.pop(uid, None)


@auto_stamp_check.before_loop
async def before_auto_stamp():
    await bot.wait_until_ready()


# ==========================
# 連続参加・統計関数
# ==========================
def calc_stats(dates):
    # dates: ["YYYY-MM-DD", ...]
    if not dates:
        return {"total": 0, "current_streak": 0, "max_streak": 0}

    date_objs = sorted([datetime.date.fromisoformat(d) for d in dates])
    total = len(date_objs)

    # current streak（今日から遡る）
    today = datetime.date.today()
    current_streak = 0
    s = set(date_objs)
    d = today
    while d in s:
        current_streak += 1
        d = d - datetime.timedelta(days=1)

    # max streak
    max_streak = 1
    temp_streak = 1
    for i in range(1, len(date_objs)):
        if (date_objs[i] - date_objs[i-1]).days == 1:
            temp_streak += 1
        else:
            temp_streak = 1
        if temp_streak > max_streak:
            max_streak = temp_streak

    return {"total": total, "current_streak": current_streak, "max_streak": max_streak}


# ==========================
# カレンダー画像生成（既存のロジックを利用）
# ==========================
def day_positions():
    pos = {}
    # 設定は既存通り（必要なら微調整）
    start_x = 155
    start_y = 395
    cell_w = 320
    cell_h = 265
    stamp_w = 250
    stamp_h = 250
    offset_x = (cell_w - stamp_w) // 2
    offset_y = (cell_h - stamp_h) // 2

    today = datetime.date.today()
    year = today.year
    month = today.month
    first_weekday = datetime.date(year, month, 1).weekday()
    start_col = (first_weekday + 1) % 7
    last_day = calendar.monthrange(year, month)[1]

    for day in range(1, last_day + 1):
        idx = start_col + (day - 1)
        row = idx // 7
        col = idx % 7
        x = start_x + col * cell_w + offset_x
        y = start_y + row * cell_h + offset_y
        pos[day] = (x, y)
    return pos

def create_calendar(user_id: int):
    today = datetime.date.today()
    ym = today.strftime("%Y_%m")
    ym_key = today.strftime("%Y-%m")

    base_path = f"images/calendar_base_{ym}.png"
    out_path = f"data/calendar_{user_id}.png"

    if not os.path.exists(base_path):
        raise FileNotFoundError(f"Base calendar not found: {base_path}")

    img = Image.open(base_path).convert("RGBA")
    stamp = Image.open("images/stamp.png").convert("RGBA")
    stamp = stamp.resize((250, 250))

    data = load_data()
    days = data.get(str(user_id), {}).get(ym_key, [])

    positions = day_positions()
    for d in days:
        day = int(d[-2:])
        if day in positions:
            img.paste(stamp, positions[day], stamp)
    img.save(out_path)
    return out_path


# ==========================
# スラッシュコマンド（stamp / ranking）
# ==========================
@bot.tree.command(name="stamp", description="自分のスタンプ帳を表示")
async def stamp(interaction: discord.Interaction):
    await interaction.response.defer()
    path = create_calendar(interaction.user.id)

    data = load_data()
    all_dates = []
    user_months = data.get(str(interaction.user.id), {})
    for m in user_months.values():
        all_dates.extend(m)

    stats = calc_stats(all_dates)
    text = (
        f"📊 参加記録\n"
        f"✅ 総参加日数: {stats['total']}日\n"
        f"🔥 継続中連続日数: {stats['current_streak']}日\n"
        f"🏆 最高連続日数: {stats['max_streak']}日"
    )
    await interaction.followup.send(content=text, file=discord.File(path))


@bot.tree.command(name="ranking", description="サーバー内ランキングを表示")
async def ranking(interaction: discord.Interaction):
    await interaction.response.defer()

    data = load_data()
    scores_total = []
    scores_month = []
    scores_streak = []

    today = datetime.date.today()
    this_month = today.strftime("%Y-%m")

    for uid, months in data.items():
        uid_int = int(uid)
        total = sum(len(days) for days in months.values())
        this_month_count = len(months.get(this_month, []))
        all_dates = []
        for m in months.values():
            all_dates.extend(m)
        stats = calc_stats(all_dates)

        scores_total.append((uid_int, total))
        scores_month.append((uid_int, this_month_count))
        scores_streak.append((uid_int, stats["max_streak"]))

    scores_total.sort(key=lambda x: x[1], reverse=True)
    scores_month.sort(key=lambda x: x[1], reverse=True)
    scores_streak.sort(key=lambda x: x[1], reverse=True)

    async def get_name(uid: int):
        member = interaction.guild.get_member(uid)
        if member:
            return member.display_name
        try:
            user = await bot.fetch_user(uid)
            return user.name
        except:
            return f"不明ユーザー({uid})"

    text = "🏆 **ランキング**\n\n"
    text += "【🌟 総合ランキング】\n"
    for i, (uid, score) in enumerate(scores_total[:10]):
        name = await get_name(uid)
        text += f"{i+1}位: {name} - {score}回\n"

    text += "\n【📅 今月のランキング】\n"
    for i, (uid, score) in enumerate(scores_month[:10]):
        name = await get_name(uid)
        text += f"{i+1}位: {name} - {score}回\n"

    text += "\n【🔥 連続参加ランキング】\n"
    for i, (uid, score) in enumerate(scores_streak[:10]):
        name = await get_name(uid)
        text += f"{i+1}位: {name} - {score}日\n"

    await interaction.followup.send(text)


# ==========================
# 起動時セットアップ
# ==========================
@bot.event
async def setup_hook():
    # コマンド同期（ギルド限定でなくグローバルが必要なら変更可）
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        print("setup_hook tree sync error:", e)

    auto_stamp_check.start()


# ==========================
# 実行
# ==========================
import threading
import uvicorn
from server import app

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=8080)

if __name__ == "__main__":
    # FastAPI を起動（Koyebのヘルスチェック用）
    threading.Thread(target=run_api, daemon=True).start()

    # Discord Bot 起動
    bot.run(os.getenv("DISCORD_TOKEN"))


