import json
import urllib.request
import urllib.error
import math
from datetime import datetime

# =========================================================
# 1. 設定エリア
# =========================================================
LINE_ACCESS_TOKEN = "rYoHKe/26fKDyrXUkXUqtCSrqgy/9KCqa9HK3DNTAp47IxJHr/mkgON9iSdWVtuoKqKyNa9fAap6XhwYE4QJOzWmoyyJqJ1vmH3N8TM+dIiK4JeFkb0xCSxU2uyWH1dB/WjCekHCNTBX3luQzl7DZgdB04t89/1O/w1cDnyilFU="
USER_ID = "U83ffcfd731f340eb571d8f050131eae2"

# 基準となる位置情報（例：秋田市役所やユーザーの拠点位置など）
# 必要に応じてご自身の基準座標（緯度, 経度）に書き換えてください。
BASE_LAT = 39.7186
BASE_LNG = 140.1024


# =========================================================
# 2. 距離計算関数（2地点の緯度経度からkm距離を算出）
# =========================================================
def calculate_distance(lat1, lon1, lat2, lon2):
    """
    ヒュベニ（Haversine）の公式を用いて、地球上の2点間の距離(km)を計算します。
    """
    R = 6371.0  # 地球の半径 (km)

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0)**2 + \
        math.cos(phi1) * math.cos(phi2) * \
        math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


# =========================================================
# 3. 投稿データを受け取り 5km 判定＆ LINE 通知を行う関数
# =========================================================
def check_and_send_line_notification(item_data, user_lat=BASE_LAT, user_lng=BASE_LNG):
    """
    新規投稿（クマダスやアプリ内投稿）の辞書データを受け取り、5km以内ならLINE送信する。
    """
    try:
        # 投稿データの緯度経度を取得
        item_lat = float(item_data.get("lat", 0))
        item_lng = float(item_data.get("lng", 0))
    except (ValueError, TypeError):
        print("⚠️ 投稿データの緯度・経度が不適切です。通知処理をスキップします。")
        return False

    # 1. 基準位置からの距離(km)を算出
    distance_km = calculate_distance(user_lat, user_lng, item_lat, item_lng)
    distance_rounded = round(distance_km, 2)

    print(f"🔍 距離判定中: 投稿場所からの距離 = {distance_rounded} km")

    # 2. 5km 判定（5kmより遠い場合はスキップ）
    if distance_km > 5.0:
        print(f"🛑 【通知スキップ】 距離が 5km を超えているため（{distance_rounded}km）、LINE通知は送信されません。")
        return False

    print(f"⭕ 【条件クリア】 5km以内のため（{distance_rounded}km）、LINE通知を送信します。")

    # 3. 投稿データの各項目を抽出（なければデフォルト値）
    date_str = item_data.get("date") or datetime.now().strftime("%Y年%m月%d日 %H:%M")
    location_str = item_data.get("location") or item_data.get("address") or "場所情報なし"
    status_str = item_data.get("status") or item_data.get("type") or "目撃情報"
    detail_str = item_data.get("detail") or item_data.get("comment") or "詳細情報なし"
    source_str = item_data.get("source") or "アプリ投稿"

    # 4. メッセージの組み立て
    message_text = f"""⚠️ 【クマ出没・警戒通知】 ⚠️

周辺（{distance_rounded}km圏内）でクマの情報が投稿されました。

■ 日時: {date_str}
■ 場所: {location_str}
■ 距離: 現在地から約 {distance_rounded}km
■ 状況: {status_str}
■ 詳細: {detail_str}
■ 情報元: {source_str}

安全な場所に移動し、身の安全を確保してください。"""

    # 5. LINE Messaging API へ Push 送信
    url = "https://api.line.me/v2/bot/message/push"
    payload = {
        "to": USER_ID,
        "messages": [{"type": "text", "text": message_text}]
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        
        with urllib.request.urlopen(req) as res:
            print("✅ LINE通知の送信が完了しました！")
            return True

    except urllib.error.HTTPError as e:
        print(f"❌ [LINE HTTPエラー]: {e.code}")
    except Exception as e:
        print(f"❌ [LINE エラー]: {e}")
    
    return False


# 単体テスト用（このファイルを直接実行した際の動作確認）
if __name__ == "__main__":
    # 5km以内のテストデータ（例: 秋田駅付近）
    test_post_near = {
        "date": "2026年07月29日 10:00",
        "location": "秋田市中通 1丁目付近",
        "lat": 39.7169,
        "lng": 140.1283,
        "status": "目撃情報",
        "detail": "体長1.2m程度のクマが1頭目撃されました。",
        "source": "クマダス"
    }

    print("--- テスト1: 5km以内の投稿 ---")
    check_and_send_line_notification(test_post_near)
