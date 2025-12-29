import calendar
from datetime import date
from typing import Tuple


# === ここをあなたのカレンダー画像に合わせて調整してください ===
# 例：7列×6行のマス目として、左上を基準に計算
CALENDAR_CONFIG = {
    "base_width": 1200,   # calendar_base_yyyy_mm.png の幅
    "base_height": 900,   # 高さ
    "offset_x": 155,       # 最初の列のXオフセット
    "offset_y": 380,      # 最初の行のYオフセット
    "cell_width": 300,    # 1日分の幅
    "cell_height": 275,   # 1日分の高さ
}


def get_day_position(target_date: date) -> Tuple[int, int]:
    """
    カレンダー画像上で、その月の target_date がどの座標に来るかを返す。
    戻り値: (x, y)
    """
    year = target_date.year
    month = target_date.month
    day = target_date.day

    cal = calendar.Calendar(firstweekday=6)  # 日曜始まり (6: Sunday)
    month_days = list(cal.itermonthdates(year, month))

    # month_days は、カレンダーの全マス（日付が前月・翌月にまたがる場合も）を日付順に並べたリスト
    # 7列ごとに1週間とみなす
    for idx, d in enumerate(month_days):
        if d.month == month and d.day == day:
            row = idx // 7
            col = idx % 7
            x = CALENDAR_CONFIG["offset_x"] + col * CALENDAR_CONFIG["cell_width"]
            y = CALENDAR_CONFIG["offset_y"] + row * CALENDAR_CONFIG["cell_height"]
            return x, y

    # 万が一見つからない場合は左上を返す
    return CALENDAR_CONFIG["offset_x"], CALENDAR_CONFIG["offset_y"]
