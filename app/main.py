import os
import asyncio
import time  # OSのtimeモジュール (タイムゾーン用)
from datetime import datetime, timedelta, date, time as pytime, timezone # datetimeのtimeをpytimeとして扱う
from io import BytesIO
from typing import Dict, Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from supabase import create_client, Client
from PIL import Image

# タイムゾーンの設定はインポート直後に行うのが安全
os.environ['TZ'] = 'Asia/Tokyo'
if hasattr(time, 'tzset'):
    time.tzset()

# 以降の from datetime import ... はすべて削除してください

from app.date.calendar_utils import get_day_position # パスが正しいか確認してください

import threading
from app.server import run as run_server


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
        start_time: pytime,
        window_minutes: int,
        required_minutes: int,
        monitor_offset_minutes: int,
        calendar_base_prefix: str,
        is_night: bool,
        mention_role_id: Optional[int] = None, # 1. 引数に追加
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
        self.mention_role_id = mention_role_id # 2. selfに代入して保持

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
    try:
        res = supabase.table("clubs").select("*").eq("guild_id", guild_id).execute()
        data = res.data
    except Exception as e:
        print(f"Error loading clubs: {e}")
        return

    clubs_by_name: Dict[str, ClubConfig] = {}
    for row in data:
        # DBの時刻文字列をPythonのtimeオブジェクトに変換
        try:
           # 文字列から時刻を取り出し、pytime型として明示的に扱う（またはそのまま .time() メソッドを使う）
            start_t = datetime.strptime(row["start_time"], "%H:%M:%S").time()
        except ValueError:
            start_t = datetime.strptime(row["start_time"], "%H:%M").time()
        
        club_cfg = ClubConfig(
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
            mention_role_id=row.get("mention_role_id"), # ★ DBから取得
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
    mention_role_id: int, # ★ 引数に追加
    is_night: bool = False,
    window_minutes: int = 15,
    required_minutes: int = 6,
    monitor_offset_minutes: int = 20,
) -> ClubConfig:
    # 1. 既存チェック
    try:
        res = supabase.table("clubs").select("*").eq("name", name).eq("guild_id", guild_id).execute()
        if res.data:
            raise ValueError("同じ名前の部活がすでに登録されています")
    except Exception as e:
        print(f"Check error: {e}")

    # 2. 挿入用データの作成（必ずコロン ':' を使う）
    insert_data = {
        "name": name,
        "guild_id": guild_id,
        "voice_channel_id": voice_channel_id,
        "start_time": f"{start_time_str}:00", # 秒を付与
        "window_minutes": window_minutes,
        "required_minutes": required_minutes,
        "monitor_offset_minutes": monitor_offset_minutes,
        "calendar_base_prefix": calendar_base_prefix,
        "is_night": is_night,
        "mention_role_id": mention_role_id, # ★ 追加
    }

    # 3. DBへの挿入（リスト [ ] で囲んで渡す）
    try:
        insert_res = (
            supabase.table("clubs")
            .insert([insert_data])  # ここをリスト形式にする
            .execute()
        )
        row = insert_res.data[0]
    except Exception as e:
        # ここで「Object of type set...」が出る場合は、insert_dataの中身に問題があります
        raise RuntimeError(f"Supabase insert error: {e}")

# 4. キャッシュ更新と返却
    start_t = datetime.strptime(start_time_str, "%H:%M").time()
    cfg = ClubConfig(
        club_id=row["id"],
        name=row["name"],
        guild_id=row["guild_id"],
        voice_channel_id=row["voice_channel_id"],
        start_time=start_t,
        window_minutes=row["window_minutes"],
        required_minutes=row["required_minutes"],
        monitor_offset_minutes=row["monitor_offset_minutes"], # ★ここを追加
        calendar_base_prefix=row["calendar_base_prefix"],
        is_night=row["is_night"],
        mention_role_id=row["mention_role_id"], # ★ ここにも追加
    )
    if guild_id not in club_cache:
        club_cache[guild_id] = {}
    club_cache[guild_id][cfg.name] = cfg
    return cfg

async def record_stamp_if_needed(club: ClubConfig, user_id: int, date_obj: date, seconds_in_window: int):
    if seconds_in_window < int(club.required_timedelta.total_seconds()):
        return

    try:
        # 重複チェック
        res = supabase.table("stamps").select("*").eq("user_id", user_id).eq("guild_id", club.guild_id).eq("club_id", club.club_id).eq("date", date_obj.isoformat()).execute()
        if res.data: return

        # 挿入
        supabase.table("stamps").insert({
            "user_id": user_id,
            "guild_id": club.guild_id,
            "club_id": club.club_id,
            "date": date_obj.isoformat(),
        }).execute()

# --- ここから通知処理を追加 ---
        # 通知を送りたいチャンネルIDを事前に取得するか、特定の名前のチャンネルを探します
        # 例: 「スタンプ通知」という名前のチャンネルに送る場合
        guild = bot.get_guild(club.guild_id)
        if guild:
            # チャンネル名で探す例（特定のIDにする場合は guild.get_channel(ID)）
            target_channel = discord.utils.get(guild.text_channels, name="スタンプ帳確認")
            if target_channel:
                await target_channel.send(f"🎉 <@{user_id}> さん、今日の **{club.name}** スタンプを獲得しました！")
        # ----------------------------
    except Exception as e:
        print(f"Error recording stamp: {e}")


async def get_stats_for_user(club: ClubConfig, user_id: int) -> Tuple[int, int, int]:
    res = (
        supabase.table("stamps")
        .select("date")
        .eq("user_id", user_id)
        .eq("guild_id", club.guild_id)
        .eq("club_id", club.club_id)
        .order("date", desc=False)
        .execute()
    )

    dates = sorted(list(set([datetime.strptime(r["date"], "%Y-%m-%d").date() for r in res.data])))
    if not dates:
        return 0, 0, 0

    total = len(dates)
    max_streak = 0
    current_streak = 0
    
    # 全期間の最大連続日数を計算
    temp_streak = 1
    for i in range(1, len(dates)):
        if dates[i] == dates[i-1] + timedelta(days=1):
            temp_streak += 1
        else:
            max_streak = max(max_streak, temp_streak)
            temp_streak = 1
    max_streak = max(max_streak, temp_streak)

    # 「現在」の連続日数を計算（昨日または今日にスタンプがあるか）
    today = date.today()
    if dates[-1] == today or dates[-1] == today - timedelta(days=1):
        current_streak = 1
        for i in range(len(dates)-1, 0, -1):
            if dates[i] == dates[i-1] + timedelta(days=1):
                current_streak += 1
            else:
                break
    else:
        current_streak = 0

    return total, current_streak, max_streak

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
    target_date: date,
    stamp_dates: List[date],
) -> BytesIO:
    # ベース画像を読み込み
    img = load_calendar_base_image(club, target_date)
    
    # スタンプ画像を読み込み（images/stamp.png が必要）
    base_dir = os.path.join(os.path.dirname(__file__), "images")
    stamp_path = os.path.join(base_dir, "stamp.png")
    if not os.path.exists(stamp_path):
        raise FileNotFoundError(f"スタンプ画像が見つかりません: {stamp_path}")
    
    stamp_img = Image.open(stamp_path).convert("RGBA")
    
# --- ここでサイズを調整 ---
    # マス目のサイズ (150, 100) より少し小さくすると綺麗に収まります
    # 例: 幅 120px にリサイズ（アスペクト比を維持する場合）
    target_width = 250 
    ratio = target_width / stamp_img.width
    target_height = int(stamp_img.height * ratio)
    stamp_img = stamp_img.resize((target_width, target_height), Image.LANCZOS)
    # --------------------------

    # スタンプを合成
    for d in stamp_dates:
        try:
            x, y = get_day_position(d)
            # 中央寄せにしたい場合は、座標にオフセットを加える
            # 例: (x + 15, y + 10) など
            img.alpha_composite(stamp_img, dest=(int(x + 15), int(y + 5)))
        except Exception as e:
            print(f"Stamp position error for {d}: {e}")
            continue

    buf = BytesIO()
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
    print(f"現在時刻: {datetime.now()}")
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

notified_keys = set()

@tasks.loop(seconds=30)
async def presence_checker():
    # 修正：JSTを指定して取得
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)

    five_min_later_str = (now + timedelta(minutes=5)).strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")

    for guild in bot.guilds:
        guild_clubs = club_cache.get(guild.id, {})
        if not guild_clubs:
            continue

        for club in guild_clubs.values():
            # --- 追加: 5分前通知ロジック ---
            club_time_str = club.start_time.strftime("%H:%M")
            notify_key = f"notif_{club.club_id}_{today_str}"

            if five_min_later_str == club_time_str and notify_key not in notified_keys:
                target_channel = discord.utils.get(guild.text_channels, name="スタンプ帳確認")
                if target_channel and club.mention_role_id:
                    try:
                        await target_channel.send(
                            f"🔔 <@&{club.mention_role_id}> **{club.name}** の開始5分前です！\n"
                            f"VC: <#{club.voice_channel_id}> に集まりましょう！"
                        )
                        notified_keys.add(notify_key)
                    except Exception as e:
                        print(f"Error sending notification: {e}")
            # ----------------------------
            
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
                    
                    print(f"DEBUG: {member.display_name} is in window! Current seconds: {presence_accumulator[key]}")

                    # 必要時間を超えたらスタンプ
                    seconds = presence_accumulator[key]
                    await record_stamp_if_needed(club, member.id, key_date, seconds)

# ====== スラッシュコマンド ======

@bot.tree.command(name="ping", description="動作確認")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong")


@bot.tree.command(name="add_club", description="新しい部活(VC監視)設定を追加します")
@app_commands.default_permissions(administrator=True) # ★これを追加
async def add_club(
    interaction: discord.Interaction,
    name: str,
    voice_channel: discord.VoiceChannel,
    start_time_str: str,
    calendar_base_prefix: str,
    mention_role: discord.Role, # ★ ここに受け取り用の引数を追加
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
            mention_role_id=mention_role.id, # ★ ここでIDを渡す
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
        f"通知ロール: {mention_role.mention}\n" # ★ 確認メッセージにロールを表示
        f"開始時刻: {cfg.start_time.strftime('%H:%M')}\n"
        f"VC: {voice_channel.mention}\n"
        f"監視開始: 開始 {cfg.monitor_offset_minutes} 分前から\n"
        f"判定窓: {cfg.window_minutes} 分 / 必要滞在: {cfg.required_minutes} 分\n"
        f"カレンダーベース: {cfg.calendar_base_prefix} (night={cfg.is_night})"
    )
 

# 候補を出すための関数
async def club_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    guild_id = interaction.guild_id
    clubs = club_cache.get(guild_id, {})
    return [
        app_commands.Choice(name=name, value=name)
        for name in clubs.keys() if current.lower() in name.lower()
    ][:25] # 最大25件まで表示可能

# --- 部活削除コマンド ---
@bot.tree.command(name="remove_club", description="登録済みの部活設定を削除します")
@app_commands.default_permissions(administrator=True) # 管理者のみ
@app_commands.autocomplete(club_name=club_autocomplete) # 名前を選択式に
async def remove_club(interaction: discord.Interaction, club_name: str):
    await interaction.response.defer(ephemeral=True) # 処理に時間がかかる場合に備えて

    # キャッシュまたはDBから対象を取得
    club = await get_or_load_club(interaction.guild_id, club_name)
    if not club:
        await interaction.followup.send(f"部活 `{club_name}` は見つかりませんでした。", ephemeral=True)
        return

    try:
        # 1. Supabaseから削除
        supabase.table("clubs").delete().eq("id", club.club_id).execute()

        # 2. キャッシュからも削除
        if interaction.guild_id in club_cache:
            if club_name in club_cache[interaction.guild_id]:
                del club_cache[interaction.guild_id][club_name]

        await interaction.followup.send(f"部活 `{club_name}` の設定を完全に削除しました。", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"削除中にエラーが発生しました: {e}", ephemeral=True)



@bot.tree.command(name="card", description="スタンプカードを表示します")
@app_commands.autocomplete(club_name=club_autocomplete) # ここで候補関数を紐付け
async def card(
    interaction: discord.Interaction, 
    club_name: str, 
    member: Optional[discord.Member] = None
):
    await interaction.response.defer()
    if not member: member = interaction.user

    club = await get_or_load_club(interaction.guild_id, club_name)
    if not club:
        await interaction.followup.send("部活が見つかりません。", ephemeral=True)
        return

    today = date.today()
    # 修正: 引数に today を追加
    stamp_dates = await get_stamp_dates_for_month(club, member.id, today)

    try:
        # 修正: asyncio.to_thread を使用
        buf = await asyncio.to_thread(apply_stamps_to_calendar, club, today, stamp_dates)
    except Exception as e:
        await interaction.followup.send(f"画像生成エラー: {e}", ephemeral=True)
        return
    
# 統計情報の取得
    total_days, current_streak, max_streak = await get_stats_for_user(club, member.id)

    file = discord.File(buf, filename="stamp_card.png")
    
    # --- 装飾版 Embed ---
    embed = discord.Embed(
        title=f"✨ {club.name} STAMP CARD ✨",
        description=f"{member.mention} さんの活動記録です。毎日コツコツ頑張りましょう！",
        color=0xffd700, # 豪華なゴールドカラー
    )

    # ユーザー情報を左側に、アイコンを右上に配置
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url="https://emojicdn.elk.sh/🏆") # 達成感を出すトロフィーアイコン

    # 統計情報をフィールドに分けて表示（インラインで横並び）
    embed.add_field(
        name="📊 累計", 
        value=f"```fix\n{total_days} 日分\n```", 
        inline=True
    )
    embed.add_field(
        name="🔥 現在継続", 
        value=f"```yaml\n{current_streak} 日連続\n```", 
        inline=True
    )
    embed.add_field(
        name="👑 自己ベスト", 
        value=f"```arm\n{max_streak} 日連続\n```", 
        inline=True
    )

    # 下部にメッセージを追加
    status_msg = "その調子です！🚀" if current_streak > 0 else "明日からまた始めましょう！🌱"
    embed.set_footer(text=f"判定時刻: {club.start_time.strftime('%H:%M')}〜 | {status_msg}")
    
    embed.set_image(url="attachment://stamp_card.png")
    
    await interaction.followup.send(file=file, embed=embed)


# ====== ランキング表示 ======
async def get_ranking(club: ClubConfig, period: str) -> List[Tuple[int, int]]:
    """
    period: 'week', 'month', 'year'
    戻り値: [(user_id, count), ...] のリスト
    """
    now = datetime.now(timezone(timedelta(hours=9)))
    today = now.date()

    if period == 'week':
        # 月曜日を開始日とする
        start_date = today - timedelta(days=today.weekday())
    elif period == 'month':
        start_date = today.replace(day=1)
    elif period == 'year':
        start_date = today.replace(month=1, day=1)
    else:
        return []

    res = (
        supabase.table("stamps")
        .select("user_id")
        .eq("guild_id", club.guild_id)
        .eq("club_id", club.club_id)
        .gte("date", start_date.isoformat())
        .execute()
    )

    # ユーザーごとにカウント
    counts = {}
    for r in res.data:
        uid = r["user_id"]
        counts[uid] = counts.get(uid, 0) + 1

    # カウント順にソートして上位10名を取得
    sorted_ranking = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return sorted_ranking[:10]


# ====== ランキングコマンド ======
@bot.tree.command(name="ranking", description="部活のスタンプランキングを表示します")
@app_commands.describe(period="集計期間を選択してください")
@app_commands.choices(period=[
    app_commands.Choice(name="週間 (今週)", value="week"),
    app_commands.Choice(name="月間 (今月)", value="month"),
    app_commands.Choice(name="年間 (今年)", value="year"),
])
@app_commands.autocomplete(club_name=club_autocomplete)
async def ranking(interaction: discord.Interaction, club_name: str, period: str):
    await interaction.response.defer()

    club = await get_or_load_club(interaction.guild_id, club_name)
    if not club:
        await interaction.followup.send("部活が見つかりません。", ephemeral=True)
        return

    ranking_data = await get_ranking(club, period)
    
    period_label = {"week": "週間", "month": "月間", "year": "年間"}[period]
    
    embed = discord.Embed(
        title=f"🏆 {club.name} {period_label}ランキング",
        color=0xffd700 if period == "year" else 0x5865f2,
        description=f"現在のトップ10を表示します（{date.today().isoformat()} 時点）"
    )

    if not ranking_data:
        embed.description = "まだこの期間のスタンプ記録がありません。🌱"
    else:
        ranking_list = []
        for i, (user_id, count) in enumerate(ranking_data, 1):
            # メンバー名を取得（キャッシュになければID表示）
            member = interaction.guild.get_member(user_id)
            name = member.display_name if member else f"User({user_id})"
            
            # メダル絵文字の装飾
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**{i}位**")
            ranking_list.append(f"{medal} {name} ― `{count}個`")
        
        embed.add_field(name="順位 ― 獲得数", value="\n".join(ranking_list), inline=False)

    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)



# ====== /callm 機能のUIコンポーネント (安定版) ======

class MemberSelectView(discord.ui.View):
    def __init__(self, members: List[discord.Member], page=0):
        super().__init__(timeout=180)
        self.members = members
        self.page = page
        self.per_page = 25
        
        start = self.page * self.per_page
        end = start + self.per_page
        current_members = self.members[start:end]

        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id))
            for m in current_members
        ]
        
        if options:
            self.select = discord.ui.Select(
                placeholder=f"メンションする人を選択 (Page {self.page + 1})",
                min_values=1,
                max_values=len(options),
                options=options
            )
            # セレクトメニュー自体にコールバックを持たせず、ボタンで一括処理
            self.add_item(self.select)

        # ページ移動ボタン
        prev_btn = discord.ui.Button(label="◀ 前", disabled=(self.page == 0), style=discord.ButtonStyle.gray)
        prev_btn.callback = self.prev_page
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(label="次 ▶", disabled=not (len(self.members) > end), style=discord.ButtonStyle.gray)
        next_btn.callback = self.next_page
        self.add_item(next_btn)

        send_btn = discord.ui.Button(label="メッセージを入力して送信", style=discord.ButtonStyle.green)
        send_btn.callback = self.open_modal
        self.add_item(send_btn)

    async def prev_page(self, interaction: discord.Interaction):
        # ページ切り替え時は response.edit_message を使う
        await interaction.response.edit_message(view=MemberSelectView(self.members, self.page - 1))

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=MemberSelectView(self.members, self.page + 1))

    async def open_modal(self, interaction: discord.Interaction):
        # ボタン押下時にセレクトメニューの値を確認
        if not hasattr(self, 'select') or not self.select.values:
            return await interaction.response.send_message("メンバーが選択されていません。上の一覧から選んでください。", ephemeral=True)
        
        mentions = " ".join([f"<@{m_id}>" for m_id in self.select.values])
        # モーダル表示。ここは defer してはいけない。
        await interaction.response.send_modal(CallmMessageModal(mentions))

class CallmMessageModal(discord.ui.Modal, title='送信メッセージ入力'):
    content = discord.ui.TextInput(
        label='メッセージ内容',
        style=discord.TextStyle.paragraph,
        placeholder='連絡事項を入力してください',
        required=True
    )
    
    def __init__(self, mentions: str):
        super().__init__()
        self.mentions = mentions

    async def on_submit(self, interaction: discord.Interaction):
        # 送信処理が重い場合を想定し、まず defer
        await interaction.response.defer()
        # その後、followup で送信
        await interaction.followup.send(f"{self.mentions}\n\n{self.content.value}")


# ====== /callm 機能のUIコンポーネント (最終安定版) ======

class MemberSelectView(discord.ui.View):
    def __init__(self, members: List[discord.Member], page=0):
        super().__init__(timeout=180)
        self.members = members
        self.page = page
        self.per_page = 25
        
        start = self.page * self.per_page
        end = start + self.per_page
        current_members = self.members[start:end]

        options = []
        for m in current_members:
            # ニックネームがあれば表示、なければユーザー名
            label = m.display_name[:25] # 25文字制限対策
            options.append(discord.SelectOption(label=label, value=str(m.id)))
        
        if options:
            self.select = discord.ui.Select(
                placeholder=f"メンション先を選択 (Page {self.page + 1})",
                min_values=1,
                max_values=len(options),
                options=options
            )
            self.add_item(self.select)

        # ページ移動ボタン
        self.add_item(discord.ui.Button(label="◀ 前", disabled=(self.page == 0), custom_id="callm_prev"))
        has_next = len(self.members) > end
        self.add_item(discord.ui.Button(label="次 ▶", disabled=not has_next, custom_id="callm_next"))

        send_btn = discord.ui.Button(label="メッセージを入力して送信", style=discord.ButtonStyle.green)
        send_btn.callback = self.open_modal
        self.add_item(send_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # ボタンクリック時のカスタムID判定
        cid = interaction.data.get("custom_id")
        if cid == "callm_prev":
            await interaction.response.edit_message(view=MemberSelectView(self.members, self.page - 1))
            return False
        elif cid == "callm_next":
            await interaction.response.edit_message(view=MemberSelectView(self.members, self.page + 1))
            return False
        return True

    async def open_modal(self, interaction: discord.Interaction):
        # 選択内容の確認
        if not hasattr(self, 'select') or not self.select.values:
            return await interaction.response.send_message("メンバーが選択されていません。リストから選んでからボタンを押してください。", ephemeral=True)
        
        mentions = " ".join([f"<@{m_id}>" for m_id in self.select.values])
        # Modalは response.defer 状態では出せないので、そのまま送る
        await interaction.response.send_modal(CallmMessageModal(mentions))

class CallmMessageModal(discord.ui.Modal, title='送信メッセージ入力'):
    content = discord.ui.TextInput(label='内容', style=discord.TextStyle.paragraph, required=True)
    
    def __init__(self, mentions: str):
        super().__init__()
        self.mentions = mentions

    async def on_submit(self, interaction: discord.Interaction):
        # 送信前に一度 defer してタイムアウトを防ぐ
        await interaction.response.defer()
        # 実際の送信
        await interaction.followup.send(f"{self.mentions}\n\n{self.content.value}")


# ====== /callm コマンド本体 (最終安定版) ======

@bot.tree.command(name="callm", description="登録済みロールからメンバーを選んで一括メンションします")
async def callm(interaction: discord.Interaction):
    # 即座に defer を実行（3秒ルール回避）
    await interaction.response.defer(ephemeral=True)

    try:
        # Supabase からデータ取得
        res = supabase.table("callm_roles").select("role_id").eq("guild_id", interaction.guild_id).execute()
        role_ids = [row['role_id'] for row in res.data]

        if not role_ids:
            return await interaction.followup.send("登録済みロールがありません。`/callm_add` で追加してください。", ephemeral=True)

        options = []
        for r_id in role_ids:
            role = interaction.guild.get_role(r_id)
            if role:
                options.append(discord.SelectOption(label=role.name, value=str(role.id)))

        if not options:
            return await interaction.followup.send("有効なロールが見つかりませんでした。", ephemeral=True)

        # 最初のロール選択メニュー
        view = discord.ui.View()
        select = discord.ui.Select(placeholder="ロールを選んでください", options=options)

        async def callback(inter: discord.Interaction):
            # 重要：ロール選択時も即座にレスポンスを返す
            selected_role = inter.guild.get_role(int(select.values[0]))
            
            # Intentsが有効でない場合、selected_role.membersは空になります
            members = selected_role.members
            if not members:
                # Intentsの警告をログに出す
                print(f"DEBUG: Role {selected_role.name} has no members. Check Privileged Intents!")
                return await inter.response.send_message("メンバー情報を取得できません。BotのIntents設定を確認してください。", ephemeral=True)

            # メッセージ自体を書き換えてメンバー一覧を出す
            await inter.response.edit_message(
                content=f"**{selected_role.name}** のメンバーを選択:",
                view=MemberSelectView(members)
            )

        select.callback = callback
        view.add_item(select)
        
        # followup で最初の画面を表示
        await interaction.followup.send("呼び出すロールを選択してください：", view=view, ephemeral=True)

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        await interaction.followup.send(f"実行中にエラーが発生しました: {e}", ephemeral=True)


# ====== Bot 起動 ======

async def main():
    async with bot:
        # スラッシュコマンドの同期
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())







