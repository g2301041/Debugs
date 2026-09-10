import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2
from flask import g, jsonify, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent

# 最後に取得した位置を何分間使うか。
LOCATION_TTL_MINUTES = int(
    os.environ.get("LOCATION_TTL_MINUTES", "60")
)


def get_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])


# =========================================================
# 通知専用テーブル
# 元の熊情報テーブルは変更しません。
# =========================================================

def init_notification_db():
    conn = get_connection()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(731907)"
                )

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bear_line_subscribers (
                        user_id TEXT PRIMARY KEY,
                        lat DOUBLE PRECISION NOT NULL,
                        lng DOUBLE PRECISION NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS bear_line_events (
                        event_key TEXT PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS bear_line_queue (
                        id BIGSERIAL PRIMARY KEY,
                        event_key TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        message TEXT NOT NULL,
                        retry_key UUID NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_error TEXT,
                        UNIQUE(event_key, user_id)
                    );

                    CREATE INDEX IF NOT EXISTS bear_line_queue_pending
                    ON bear_line_queue(status, next_attempt_at);

                    ALTER TABLE bear_line_subscribers
                    ENABLE ROW LEVEL SECURITY;

                    ALTER TABLE bear_line_events
                    ENABLE ROW LEVEL SECURITY;

                    ALTER TABLE bear_line_queue
                    ENABLE ROW LEVEL SECURITY;
                """)

    finally:
        conn.close()


# =========================================================
# 座標・距離
# =========================================================

def validate_coordinates(lat, lng):
    if isinstance(lat, bool) or isinstance(lng, bool):
        raise ValueError("座標が不正です")

    lat = float(lat)
    lng = float(lng)

    if not (
        math.isfinite(lat)
        and math.isfinite(lng)
        and -90 <= lat <= 90
        and -180 <= lng <= 180
    ):
        raise ValueError("座標が不正です")

    return lat, lng


def get_bear_coordinates(entry):
    lat = entry.get(
        "x(緯度)",
        entry.get("緯度", entry.get("lat"))
    )

    lng = entry.get(
        "y(経度)",
        entry.get("経度", entry.get("lng"))
    )

    return validate_coordinates(lat, lng)


def calculate_distance(lat1, lng1, lat2, lng2):
    lat1, lng1 = validate_coordinates(lat1, lng1)
    lat2, lng2 = validate_coordinates(lat2, lng2)

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    delta_phi = phi2 - phi1
    delta_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(delta_lambda / 2) ** 2
    )

    a = min(1.0, max(0.0, a))

    return 6371.0 * 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )


# =========================================================
# 通知文
# =========================================================

def make_message(entry, distance_km):
    def value(*keys, default="不明", limit=500):
        for key in keys:
            if entry.get(key):
                return str(entry[key])[:limit]

        return default

    return "\n".join([
        "⚠️ クマ出没・警戒通知",
        "",
        "登録された位置から5km以内に熊の新しい情報があります。",
        "",
        "■ 日時: " + value(
            "目撃日時", "date", limit=100
        ),
        "■ 場所: " + value(
            "地番情報", "location", "address"
        ),
        f"■ 距離: 登録位置から約 {distance_km:.2f}km",
        "■ 状況: " + value(
            "情報種別", "status", "type", limit=100
        ),
        "■ 詳細: " + value(
            "目撃時の状況",
            "detail",
            "comment",
            default="詳細情報なし",
            limit=1000
        ),
        "■ 情報元: " + value(
            "source",
            default="アプリ投稿",
            limit=100
        ),
    ])


# =========================================================
# 重複通知防止
# =========================================================

def get_event_key(entry):
    # くまだす用APIから呼ばれた場合。
    source_id = getattr(g, "kumadas_source_id", None)

    if source_id:
        return "kumadas:" + source_id

    # 追加JSが投稿に付ける通知用ID。
    notification_id = request.headers.get(
        "X-Bear-Notification-ID"
    )

    if notification_id:
        try:
            return "app:" + str(uuid.UUID(notification_id))
        except ValueError:
            pass

    # 旧APIを直接呼ぶ場合は、同じ内容の再通知を防ぎます。
    raw = json.dumps(
        entry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":")
    )

    return "legacy:" + hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# =========================================================
# 元のserver.pyの保存後に呼ばれる処理
# =========================================================

def queue_notification(entry, *args, **kwargs):
    bear_lat, bear_lng = get_bear_coordinates(entry)
    event_key = get_event_key(entry)

    conn = get_connection()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bear_line_events(event_key)
                    VALUES (%s)
                    ON CONFLICT DO NOTHING
                    RETURNING event_key
                """, (event_key,))

                if cur.fetchone() is None:
                    g.line_queued_count = 0
                    return False

                cur.execute("""
                    SELECT user_id, lat, lng
                    FROM bear_line_subscribers
                    WHERE enabled = TRUE
                      AND updated_at >=
                          NOW() - (%s * INTERVAL '1 minute')
                """, (LOCATION_TTL_MINUTES,))

                users = cur.fetchall()
                queued_count = 0

                for user_id, user_lat, user_lng in users:
                    distance_km = calculate_distance(
                        user_lat,
                        user_lng,
                        bear_lat,
                        bear_lng
                    )

                    if distance_km > 5.0:
                        continue

                    cur.execute("""
                        INSERT INTO bear_line_queue (
                            event_key,
                            user_id,
                            message,
                            retry_key
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT(event_key, user_id)
                        DO NOTHING
                    """, (
                        event_key,
                        user_id,
                        make_message(entry, distance_km),
                        str(uuid.uuid4())
                    ))

                    queued_count += cur.rowcount

                g.line_queued_count = queued_count

        print(
            f"[LINE] 通知を{queued_count}件予約しました",
            flush=True
        )

        return True

    finally:
        conn.close()


# =========================================================
# LINE本人確認
# ブラウザーが申告するユーザーIDは信用せず、
# LINEの検証APIから取得します。
# =========================================================

def get_verified_user_id():
    channel_id = os.environ.get("LINE_LOGIN_CHANNEL_ID")
    authorization = request.headers.get("Authorization", "")

    if (
        not channel_id
        or not authorization.startswith("Bearer ")
        or len(authorization) > 10000
    ):
        raise ValueError("LINEログインが必要です")

    id_token = authorization[7:]

    payload = urllib.parse.urlencode({
        "id_token": id_token,
        "client_id": channel_id
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.line.me/oauth2/v2.1/verify",
        data=payload,
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            result = json.load(res)

        user_id = result.get("sub", "")

        if not re.fullmatch(r"U[0-9a-f]{32}", user_id):
            raise ValueError()

        return user_id

    except Exception:
        raise ValueError(
            "LINE認証を確認できません。再ログインしてください"
        ) from None


def is_import_authorized():
    expected = os.environ.get("IMPORT_API_KEY", "")

    if not expected:
        return False

    return hmac.compare_digest(
        request.headers.get("Authorization", ""),
        "Bearer " + expected
    )


# =========================================================
# 元のFlaskアプリへの追加
# =========================================================

def install_extension(app):
    init_notification_db()

    @app.before_request
    def protect_files():
        if request.endpoint == "index":
            # 古いHTMLのキャッシュで通知UIが抜けるのを防ぐ。
            request.environ.pop("HTTP_IF_NONE_MATCH", None)
            request.environ.pop(
                "HTTP_IF_MODIFIED_SINCE", None
            )

        # 元の任意ファイル配信からPythonソースが漏れるのを防ぐ。
        if request.endpoint == "send_static":
            path = request.view_args.get("path", "")

            if path not in {
                "app.js",
                "style.css",
                "debug.js",
                "data.json"
            }:
                return "", 404

        # 元の全件移行APIはデータ消去を伴うため管理者限定。
        if (
            request.endpoint == "force_import"
            and not is_import_authorized()
        ):
            return jsonify(
                success=False,
                message="管理者キーが必要です"
            ), 401

    @app.after_request
    def add_notification_script(response):
        if (
            request.path == "/"
            and response.status_code == 200
            and response.mimetype == "text/html"
        ):
            response.direct_passthrough = False

            html = response.get_data(as_text=True)

            script = (
                '<script src="/line-addon/script.js">'
                '</script>'
            )

            if script not in html:
                html = html.replace(
                    "</body>",
                    script + "\n</body>"
                )

                response.set_data(html)

            response.headers.pop("ETag", None)
            response.headers["Cache-Control"] = "no-store"

        if (
            request.path == "/api/save"
            and response.status_code == 200
            and not hasattr(g, "line_queued_count")
        ):
            app.logger.error(
                "保存後のLINE通知予約を確認できません。"
                "通知用DBと投稿座標を確認してください。"
            )

        return response

    @app.get("/line-addon/script.js")
    def notification_script():
        return send_from_directory(
            BASE_DIR,
            "line_notifications.js"
        )

    @app.get("/line-addon/config")
    def notification_config():
        return jsonify(
            liffId=os.environ.get("LIFF_ID", ""),
            friendUrl=os.environ.get("LINE_FRIEND_URL", ""),
            locationTtlMinutes=LOCATION_TTL_MINUTES
        )

    @app.post("/line-addon/location")
    def save_user_location():
        try:
            user_id = get_verified_user_id()

        except ValueError as error:
            return jsonify(
                success=False,
                message=str(error)
            ), 401

        data = request.get_json(silent=True)

        try:
            if not isinstance(data, dict):
                raise ValueError()

            lat, lng = validate_coordinates(
                data.get("lat"),
                data.get("lng")
            )

            accuracy = float(data.get("accuracy"))

            if not 0 <= accuracy <= 1000:
                raise ValueError()

        except (ValueError, TypeError):
            return jsonify(
                success=False,
                message="位置を正確に取得できません。再取得してください"
            ), 400

        conn = get_connection()

        try:
            with conn:
                with conn.cursor() as cur:
                    if data.get("enable") is True:
                        cur.execute("""
                            INSERT INTO bear_line_subscribers (
                                user_id, lat, lng
                            )
                            VALUES (%s, %s, %s)
                            ON CONFLICT(user_id)
                            DO UPDATE SET
                                lat = EXCLUDED.lat,
                                lng = EXCLUDED.lng,
                                enabled = TRUE,
                                updated_at = NOW()
                        """, (user_id, lat, lng))

                    else:
                        cur.execute("""
                            UPDATE bear_line_subscribers
                            SET lat = %s,
                                lng = %s,
                                updated_at = NOW()
                            WHERE user_id = %s
                              AND enabled = TRUE
                        """, (lat, lng, user_id))

                        if cur.rowcount == 0:
                            return jsonify(
                                success=False,
                                message="通知は停止中です"
                            ), 409

            return jsonify(success=True)

        finally:
            conn.close()

    @app.post("/line-addon/disable")
    def disable_user_notifications():
        try:
            user_id = get_verified_user_id()

        except ValueError as error:
            return jsonify(
                success=False,
                message=str(error)
            ), 401

        conn = get_connection()

        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE bear_line_subscribers
                        SET enabled = FALSE
                        WHERE user_id = %s
                    """, (user_id,))

            return jsonify(success=True)

        finally:
            conn.close()

    @app.post("/line-addon/kumadas")
    def kumadas_notification():
        if not is_import_authorized():
            return jsonify(
                success=False,
                message="管理者キーが必要です"
            ), 401

        data = request.get_json(silent=True)

        try:
            if not isinstance(data, dict):
                raise ValueError()

            entry = data.get("entry")
            source_id = data.get("source_id")
            notify = data.get("notify", False)

            if (
                not isinstance(entry, dict)
                or not isinstance(source_id, str)
                or not source_id
                or len(source_id) > 200
                or not isinstance(notify, bool)
            ):
                raise ValueError()

            get_bear_coordinates(entry)

        except (ValueError, TypeError):
            return jsonify(
                success=False,
                message="送信データの形式が不正です"
            ), 400

        g.kumadas_source_id = source_id

        if notify:
            entry = dict(entry)
            entry["source"] = "くまだす"

            queue_notification(entry)

            return jsonify(
                success=True,
                queued=g.line_queued_count
            )

        # 初回の過去情報はIDだけ登録し、通知しません。
        conn = get_connection()

        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO bear_line_events(event_key)
                        VALUES (%s)
                        ON CONFLICT DO NOTHING
                    """, ("kumadas:" + source_id,))

            return jsonify(success=True, queued=0)

        finally:
            conn.close()


# =========================================================
# LINE送信
# 提示された動作例と同じurllib.requestを使います。
# =========================================================

def send_line(user_id, message, retry_key):
    token = os.environ.get("LINE_ACCESS_TOKEN")

    if not token:
        raise RuntimeError("LINE_ACCESS_TOKENが未設定です")

    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }

    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "X-Line-Retry-Key": str(retry_key)
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            if res.status != 200:
                raise RuntimeError("LINEが送信要求を受理しませんでした")

    except urllib.error.HTTPError as error:
        # 同じキーですでに受理された通知は再送しません。
        if (
            error.code == 409
            and error.headers.get("x-line-accepted-request-id")
        ):
            return

        raise RuntimeError(
            f"LINE HTTPエラー: {error.code}"
        ) from None


# =========================================================
# 送信ワーカー
# python line_extension.py --worker で起動します。
# =========================================================

def process_one_notification():
    conn = get_connection()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        user_id,
                        message,
                        retry_key,
                        created_at >= NOW() - INTERVAL '1 hour'
                    FROM bear_line_queue
                    WHERE status = 'pending'
                      AND next_attempt_at <= NOW()
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """)

                row = cur.fetchone()

                if row is None:
                    return False

                job_id, user_id, message, retry_key, fresh = row

                cur.execute("""
                    SELECT 1
                    FROM bear_line_subscribers
                    WHERE user_id = %s
                      AND enabled = TRUE
                      AND updated_at >=
                          NOW() - (%s * INTERVAL '1 minute')
                    FOR UPDATE
                """, (user_id, LOCATION_TTL_MINUTES))

                active_user = cur.fetchone()

                if active_user is None or not fresh:
                    cur.execute("""
                        UPDATE bear_line_queue
                        SET status = 'cancelled'
                        WHERE id = %s
                    """, (job_id,))

                    return True

                try:
                    send_line(user_id, message, retry_key)

                except Exception as error:
                    reason = (
                        str(error)
                        if isinstance(error, RuntimeError)
                        else type(error).__name__
                    )

                    cur.execute("""
                        UPDATE bear_line_queue
                        SET
                            attempts = attempts + 1,
                            status = CASE
                                WHEN attempts + 1 >= 8
                                THEN 'failed'
                                ELSE 'pending'
                            END,
                            next_attempt_at =
                                NOW() + (
                                    LEAST(
                                        600,
                                        30 * POWER(2, attempts)
                                    ) * INTERVAL '1 second'
                                ),
                            last_error = %s
                        WHERE id = %s
                    """, (reason, job_id))

                    print(
                        f"[LINE] job={job_id} error={reason}",
                        flush=True
                    )

                else:
                    cur.execute("""
                        UPDATE bear_line_queue
                        SET status = 'sent',
                            last_error = NULL
                        WHERE id = %s
                    """, (job_id,))

                    print(
                        f"[LINE] job={job_id} accepted",
                        flush=True
                    )

        return True

    finally:
        conn.close()


def run_worker():
    init_notification_db()

    while True:
        try:
            if not process_one_notification():
                time.sleep(3)

        except Exception as error:
            print(
                "[WORKER] " + type(error).__name__,
                flush=True
            )

            time.sleep(10)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        run_worker()
    else:
        print(
            "送信ワーカーは次のコマンドで起動してください:\n"
            "python line_extension.py --worker"
        )
