import os
import asyncio
from datetime import datetime, timedelta, date, time
from io import BytesIO
from typing import Dict, Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from supabase import create_client, Client
from PIL import Image

from app.date.calendar_utils import get_day_position

import threading
from app.server import run as run_server



# .env 読み込み
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GUILD_ID = int(os.getenv("GUILD_ID"))
guild = discord.Object(id=GUILD_ID)


if not DISCORD_TOKEN:
    raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase の URL / KEY が設定されていません")
if not GUILD_ID:
    raise RuntimeError("環境変数 GUILD_ID が設定されていません")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# Intents 設定（ボイス状態とメンバー情報が必要）
intents = discord.Intents.default()
intents.message_content = False
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ====== サーバー 起動 ======
threading.Thread(
    target=run_server,
    daemon=True
).start()

# ====== データモデル（メモリ上の一時状態） ======

class ClubConfig:
    def __init__(
        self,
        club_id: str,
        name: str,
        guild_id: int,
        voice_channel_id: int,
        start_time: time,
        window_minutes: int,
        required_minutes: int,
        monitor_offset_minutes: int,
        calendar_base_prefix: str,
        is_night: bool,
    ):
        self.club_id = club_id
        self.name = name
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.start_time = start_time
        self.window_minutes = window_minutes
        self.required_minutes = required_minutes
        self.monitor_offset_minutes = monitor_offset_minutes
        self.calendar_base_prefix = calendar_base_prefix
        self.is_night = is_night

    @property
    def window_timedelta(self) -> timedelta:
        return timedelta(minutes=self.window_minutes)

    @property
    def required_timedelta(self) -> timedelta:
        return timedelta(minutes=self.required_minutes)

    @property
    def monitor_offset_timedelta(self) -> timedelta:
        return timedelta(minutes=self.monitor_offset_minutes)




# ギルドごとのClub設定をキャッシュ
club_cache: Dict[int, Dict[str, ClubConfig]] = {}  # guild_id -> {club_name: ClubConfig}

# VC滞在の一時集計（通信量削減のため、こまめにDBには書かず、しきい値到達時に書き込む）
# key: (guild_id, club_id, user_id, date) -> accumulated seconds within window
presence_accumulator: Dict[Tuple[int, str, int, date], int] = {}


# ====== Supabase helper ======

async def load_clubs_for_guild(guild_id: int):
    """Supabase から指定ギルドのクラブ設定を読み込んでキャッシュする"""
    from postgrest.exceptions import APIError # エラー詳細を捕まえたい場合

    try:
        res = supabase.table("clubs").select("*").eq("guild_id", guild_id).execute()
        # 成功した場合はそのまま res.data が使える
        data = res.data 
    except Exception as e:
        print(f"Error loading clubs: {e}")
        return

    clubs_by_name: Dict[str, ClubConfig] = {}
    for row in res.data:
        club_cfg = ClubConfig(
            club_id=row["id"],
            name=row["name"],
            guild_id=row["guild_id"],
            voice_channel_id=row["voice_channel_id"],
            start_time=datetime.strptime(row["start_time"], "%H:%M:%S").time(),
            window_minutes=row["window_minutes"],
            required_minutes=row["required_minutes"],
            monitor_offset_minutes=row["monitor_offset_minutes"],
            calendar_base_prefix=row["calendar_base_prefix"],
            is_night=row["is_night"],
        )
        clubs_by_name[club_cfg.name] = club_cfg

    club_cache[guild_id] = clubs_by_name


async def get_or_load_club(guild_id: int, club_name: str) -> Optional[ClubConfig]:
    if guild_id not in club_cache:
        await load_clubs_for_guild(guild_id)
    return club_cache.get(guild_id, {}).get(club_name)


async def add_club_to_db(
    name: str,
    guild_id: int,
    voice_channel_id: int,
    start_time_str: str,
    calendar_base_prefix: str,
    is_night: bool = False,
    window_minutes: int = 15,
    required_minutes: int = 6,
    monitor_offset_minutes: int = 20,
) -> ClubConfig:
    # 既存チェック
    res = supabase.table("clubs").select("*").eq("name", name).eq("guild_id", guild_id).execute()
    if res.data:
        raise ValueError("同じ名前の部活がすでに登録されています")

    start_t = datetime.strptime(start_time_str, "%H:%M").time()
    try:
        insert_res = (
           supabase.table("clubs")
            .insert({ ... })
            .execute()
        )
        row = insert_res.data[0] # insert_res.error のチェックを消して直接 data にアクセス
    except Exception as e:
        raise RuntimeError(f"Supabase insert error: {e}")

    if insert_res.error:
        raise RuntimeError(f"Supabase insert error: {insert_res.error}")

    row = insert_res.data[0]
    cfg = ClubConfig(
        club_id=row["id"],
        name=row["name"],
        guild_id=row["guild_id"],
        voice_channel_id=row["voice_channel_id"],
        start_time=start_t,
        window_minutes=row["window_minutes"],
        required_minutes=row["required_minutes"],
        monitor_offset_minutes=row["monitor_offset_minutes"],
        calendar_base_prefix=row["calendar_base_prefix"],
        is_night=row["is_night"],
    )

    if guild_id not in club_cache:
        club_cache[guild_id] = {}
    club_cache[guild_id][cfg.name] = cfg
    return cfg


async def record_stamp_if_needed(
    club: ClubConfig,
    user_id: int,
    date_obj: date,
    seconds_in_window: int,
):
    """
    その日の必要時間を超えていたら stamps に書き込み。
    すでにスタンプ済みなら何もしない。
    """
    if seconds_in_window < int(club.required_timedelta.total_seconds()):
        return

    res = (
        supabase.table("stamps")
        .select("*")
        .eq("user_id", user_id)
        .eq("guild_id", club.guild_id)
        .eq("club_id", club.club_id)
        .eq("date", date_obj.isoformat())
        .execute()
    )
    if res.data:
        # すでにスタンプ済み
        return

    ins = (
        supabase.table("stamps")
        .insert(
            {
                "user_id": user_id,
                "guild_id": club.guild_id,
                "club_id": club.club_id,
                "date": date_obj.isoformat(),
            }
        )
        .execute()
    )
    if ins.error:
        print("Error inserting stamp:", ins.error)


async def get_stats_for_user(club: ClubConfig, user_id: int) -> Tuple[int, int, int]:
    """
    total_days, current_streak, max_streak を返す
    """
    res = (
        supabase.table("stamps")
        .select("date")
        .eq("user_id", user_id)
        .eq("guild_id", club.guild_id)
        .eq("club_id", club.club_id)
        .order("date", desc=False)
        .execute()
    )

    dates = [datetime.strptime(r["date"], "%Y-%m-%d").date() for r in res.data]
    if not dates:
        return 0, 0, 0

    total = len(dates)

    # 連続日数と最大連続日数を計算
    max_streak = 1
    current_streak = 1
    for i in range(1, len(dates)):
        if dates[i] == dates[i - 1] + timedelta(days=1):
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 1

    # 今日含めて現在連続かどうか
    today = date.today()
    # stamps の最後の日付から後ろをみて現在連続かを再計算
    current = 1
    for i in range(len(dates) - 1, 0, -1):
        if dates[i] == dates[i - 1] + timedelta(days=1):
            current += 1
        else:
            break
    # ただし、最後の日付が今日でないなら連続は 0 にする
    if dates[-1] != today:
        current = 0

    return total, current, max_streak


# ====== スタンプカード画像生成 ======

def load_calendar_base_image(club: ClubConfig, target_date: date) -> Image.Image:
    """
    指定日のカレンダー画像ベースを読み込む。
    ファイル名: images/calendar_base_yyyy_mm(.png or _n.png)
    prefix で切り替え可能とする。
    """
    year = target_date.year
    month = target_date.month

    base_dir = os.path.join(os.path.dirname(__file__), "images")

    # ベース名（例）: calendar_base_2025_01.png / calendar_base_2025_01_n.png
    if club.is_night:
        filename = f"{club.calendar_base_prefix}_{year}_{month:02d}_n.png"
    else:
        filename = f"{club.calendar_base_prefix}_{year}_{month:02d}.png"

    path = os.path.join(base_dir, filename)

    if not os.path.exists(path):
        # デフォルト名 fallback
        if club.is_night:
            default_name = f"calendar_base_{year}_{month:02d}_n.png"
        else:
            default_name = f"calendar_base_{year}_{month:02d}.png"
        path = os.path.join(base_dir, default_name)

    if not os.path.exists(path):
        raise FileNotFoundError(f"カレンダーベース画像が見つかりません: {path}")

    img = Image.open(path).convert("RGBA")
    return img


def apply_stamps_to_calendar(
    club: ClubConfig,
    target_month: date,
    stamp_dates: List[date],
) -> BytesIO:
    """
    指定monthのカレンダーに stamp_dates の日日付にスタンプを押した画像を生成し、BytesIO を返す。
    """
    img = load_calendar_base_image(club, target_month)
    base_dir = os.path.join(os.path.dirname(__file__), "images")
    stamp_path = os.path.join(base_dir, "stamp.png")
    if not os.path.exists(stamp_path):
        raise FileNotFoundError(f"スタンプ画像が見つかりません: {stamp_path}")

    stamp_img = Image.open(stamp_path).convert("RGBA")

    for d in stamp_dates:
        if d.year == target_month.year and d.month == target_month.month:
            x, y = get_day_position(d)
            img.alpha_composite(stamp_img, dest=(x, y))

    buf = BytesIO()
    buf.name = "stamp_calendar.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def get_stamp_dates_for_month(club: ClubConfig, user_id: int, month_date: date) -> List[date]:
    start_d = date(month_date.year, month_date.month, 1)
    if month_date.month == 12:
        end_d = date(month_date.year + 1, 1, 1)
    else:
        end_d = date(month_date.year, month_date.month + 1, 1)

    res = (
        supabase.table("stamps")
        .select("date")
        .eq("user_id", user_id)
        .eq("guild_id", club.guild_id)
        .eq("club_id", club.club_id)
        .gte("date", start_d.isoformat())
        .lt("date", end_d.isoformat())
        .order("date", desc=False)
        .execute()
    )

    return [datetime.strptime(r["date"], "%Y-%m-%d").date() for r in res.data]


# ====== VC監視ロジック ======

def get_today_window_range(club: ClubConfig, tz: Optional[datetime.tzinfo] = None) -> Tuple[datetime, datetime]:
    """
    今日の club の「判定窓」の開始と終了 (datetime) を返す。
    offset は「監視開始」のために別管理で使う。
    """
    now = datetime.now(tz=tz)
    start_dt = datetime.combine(now.date(), club.start_time).replace(tzinfo=now.tzinfo)
    end_dt = start_dt + club.window_timedelta
    return start_dt, end_dt


def get_today_monitor_range(club: ClubConfig, tz: Optional[datetime.tzinfo] = None) -> Tuple[datetime, datetime]:
    """
    今日の「監視開始～終了」の範囲を返す。
    （例）11:00開始で offset=20, window=15 の場合
      監視: 10:40～11:15
    """
    start_window, end_window = get_today_window_range(club, tz=tz)
    monitor_start = start_window - club.monitor_offset_timedelta
    monitor_end = end_window
    return monitor_start, monitor_end

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # 指定したギルドに対してグローバルコマンドをコピー
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"Synced slash commands to {GUILD_ID}")

bot = MyBot()
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # クラブ設定ロード
    for g in bot.guilds:
        await load_clubs_for_guild(g.id)
    print("Club configs loaded.")
    presence_checker.start()


def get_club_for_voice_channel(guild_id: int, channel_id: int) -> List[ClubConfig]:
    """
    そのVCを監視対象にしているクラブを返す（複数の可能性もあるのでリスト）
    """
    clubs = club_cache.get(guild_id, {})
    result = []
    for cfg in clubs.values():
        if cfg.voice_channel_id == channel_id:
            result.append(cfg)
    return result


@bot.event
async def on_voice_state_update(member, before, after):
    """
    VC入退室を検知して、監視時間内なら presence_accumulator に滞在時間を積算する。
    ただし「リアルタイムで秒数カウント」するのではなく、
    presence_checker で定期的に状態を確認してもよいが、
    ここでは「join/leave と同時に時間を記録する」簡易方式は難しいため、
    別アプローチをとる。
    ----
    通信量削減＆ロジック簡略化のため、
    実際には periodic check（presence_checker）で
    今 VC にいるユーザを見て、その時刻に応じて秒数加算する。
    なので on_voice_state_update では何もしなくてもよいが、
    将来の拡張のために置いておく。
    """
    return  # ここでは特に何もしない。すべて presence_checker に任せる。


@tasks.loop(seconds=30)
async def presence_checker():
    """
    30秒おきに全ギルドの対象VCを巡回し、
    今いるメンバーを確認し、「今が監視範囲＆判定窓内」であれば
    presence_accumulator に滞在時間を加算し、必要時間を超えたら stamps を付与する。
    """
    now = datetime.now()

    for guild in bot.guilds:
        guild_clubs = club_cache.get(guild.id, {})
        if not guild_clubs:
            continue

        for club in guild_clubs.values():
            monitor_start, monitor_end = get_today_monitor_range(club, tz=now.tzinfo)
            window_start, window_end = get_today_window_range(club, tz=now.tzinfo)

            # 今日の監視時間外ならスキップ
            if not (monitor_start <= now <= monitor_end):
                continue

            # VC オブジェクト取得
            channel = guild.get_channel(club.voice_channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue

            # VCに現在いるメンバー
            members = channel.members

            for member in members:
                if member.bot:
                    continue
                # 判定窓内にいるときだけ滞在時間をカウント（「11時以前からいた」人も、
                # 実際の必要時間カウントは 11:00〜11:15 の間とする）
                if window_start <= now <= window_end:
                    key_date = window_start.date()
                    key = (guild.id, club.club_id, member.id, key_date)
                    # 30秒ぶん加算
                    presence_accumulator[key] = presence_accumulator.get(key, 0) + 30

                    # 必要時間を超えたらスタンプ
                    seconds = presence_accumulator[key]
                    await record_stamp_if_needed(club, member.id, key_date, seconds)


# ====== スラッシュコマンド ======

@bot.tree.command(name="ping", description="動作確認")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong")


@bot.tree.command(name="add_club", description="新しい部活(VC監視)設定を追加します")
async def add_club(
    interaction: discord.Interaction,
    name: str,
    voice_channel: discord.VoiceChannel,
    start_time_str: str,
    calendar_base_prefix: str,
    is_night: bool = False,
):
    """
    例: /add_club name:morning voice_channel:#朝活 start_time_str:11:00 calendar_base_prefix:calendar_base
         is_night:false
    """
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ使用できます。", ephemeral=True)
        return

    try:
        cfg = await add_club_to_db(
            name=name,
            guild_id=interaction.guild_id,
            voice_channel_id=voice_channel.id,
            start_time_str=start_time_str,
            calendar_base_prefix=calendar_base_prefix,
            is_night=is_night,
        )
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(f"エラーが発生しました: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"部活 `{cfg.name}` を登録しました。\n"
        f"開始時刻: {cfg.start_time.strftime('%H:%M')}\n"
        f"VC: {voice_channel.mention}\n"
        f"監視開始: 開始 {cfg.monitor_offset_minutes} 分前から\n"
        f"判定窓: {cfg.window_minutes} 分 / 必要滞在: {cfg.required_minutes} 分\n"
        f"カレンダーベース: {cfg.calendar_base_prefix} (night={cfg.is_night})"
    )


@bot.tree.command(name="card", description="スタンプカードを表示します")
async def card(
    interaction: discord.Interaction,
    club_name: str,
    member: Optional[discord.Member] = None,
):
    """
    例: /card club_name:morning member:@自分
    member 省略時は自分。
    """
    await interaction.response.defer()

    if not member:
        member = interaction.user

    club = await get_or_load_club(interaction.guild_id, club_name)
    if not club:
        await interaction.followup.send("その名前の部活設定が見つかりません。", ephemeral=True)
        return

    # 今月のスタンプ日取得
    today = date.today()
    stamp_dates = await get_stamp_dates_for_month(club, member.id)

    # カード画像生成
    try:
        buf = apply_stamps_to_calendar(club, today, stamp_dates)
    except FileNotFoundError as e:
        await interaction.followup.send(f"画像ファイルが見つかりません: {e}", ephemeral=True)
        return

    # 統計情報
    total_days, current_streak, max_streak = await get_stats_for_user(club, member.id)

    file = discord.File(buf, filename="stamp_card.png")
    embed = discord.Embed(
        title=f"{club.name} スタンプカード - {member.display_name}",
        description=(
            f"総参加日数: **{total_days}日**\n"
            f"現在の連続参加日数: **{current_streak}日**\n"
            f"最高連続参加日数: **{max_streak}日**"
        ),
        color=discord.Color.green(),
    )
    embed.set_image(url="attachment://stamp_card.png")
    await interaction.followup.send(file=file, embed=embed)


# ====== Bot 起動 ======

async def main():
    async with bot:
        # スラッシュコマンドの同期
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())




