import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
import uuid
import urllib.request
import urllib.error

from pathlib import Path

import psycopg2

from flask import (
    jsonify,
    request,
    send_from_directory,
)


ROOT = Path(__file__).resolve().parent

# 最後に取得した位置を何分間使うか。
TTL = int(
    os.environ.get("LOCATION_TTL_MINUTES", "60")
)


# =========================================================
# DB接続
# =========================================================

def connect():
    return psycopg2.connect(
        os.environ["DATABASE_URL"]
    )


# =========================================================
# 専用URLのキーとLINE送信先の対応
# 環境変数で管理します。
# =========================================================

def recipients():
    values = json.loads(
        os.environ.get("LINE_RECIPIENTS_JSON", "{}")
    )

    if not isinstance(values, dict):
        raise ValueError(
            "LINE_RECIPIENTS_JSONの形式が不正です"
        )

    for key, uid in values.items():
        if (
            not re.fullmatch(
                r"[A-Za-z0-9_-]{32,128}",
                key
            )
            or not isinstance(uid, str)
            or not re.fullmatch(
                r"U[0-9a-f]{32}",
                uid
            )
        ):
            raise ValueError(
                "専用キーまたはLINEユーザーIDの形式が不正です"
            )

    return values


# =========================================================
# 座標・距離
# =========================================================

def coordinates(lat, lng):
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


def bear_coordinates(entry):
    return coordinates(
        entry.get(
            "x(緯度)",
            entry.get("緯度", entry.get("lat"))
        ),
        entry.get(
            "y(経度)",
            entry.get("経度", entry.get("lng"))
        )
    )


def distance(lat1, lng1, lat2, lng2):
    lat1, lng1 = coordinates(lat1, lng1)
    lat2, lng2 = coordinates(lat2, lng2)

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(
            math.radians(lng2 - lng1) / 2
        ) ** 2
    )

    a = min(1.0, max(0.0, a))

    return 6371 * 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )


# =========================================================
# 通知専用テーブル
# 元の熊情報テーブルは変更しません。
# =========================================================

def init_db():
    conn = connect()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(731908)"
                )

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gps_line_users (
                        user_id TEXT PRIMARY KEY,
                        lat DOUBLE PRECISION NOT NULL,
                        lng DOUBLE PRECISION NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS gps_line_events (
                        event_key TEXT PRIMARY KEY
                    );

                    CREATE TABLE IF NOT EXISTS gps_line_jobs (
                        id BIGSERIAL PRIMARY KEY,
                        event_key TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        message TEXT NOT NULL,
                        retry_key UUID NOT NULL,
                        state TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        error TEXT,
                        UNIQUE(event_key, user_id)
                    );

                    CREATE INDEX IF NOT EXISTS gps_line_jobs_due
                    ON gps_line_jobs(state, due_at);

                    ALTER TABLE gps_line_users
                    ENABLE ROW LEVEL SECURITY;

                    ALTER TABLE gps_line_events
                    ENABLE ROW LEVEL SECURITY;

                    ALTER TABLE gps_line_jobs
                    ENABLE ROW LEVEL SECURITY;
                """)

    finally:
        conn.close()


# =========================================================
# 通知文
# =========================================================

def message(entry, km):
    def val(*keys, default="不明", limit=500):
        for key in keys:
            if entry.get(key):
                return str(entry[key])[:limit]

        return default

    return "\n".join([
        "🐻 熊の新しい情報が追加されました",
        "",
        "■ 日時：" + val(
            "目撃日時",
            "発生日時",
            "date",
            limit=100
        ),
        "■ 場所：" + val(
            "地番情報",
            "location",
            "address"
        ),
        f"■ 距離：最後に取得した位置から約 {km:.2f}km",
        "■ 状況：" + val(
            "情報種別",
            "status",
            "type",
            limit=100
        ),
        "■ 詳細：" + val(
            "目撃時の状況",
            "detail",
            "comment",
            limit=1000
        ),
        "■ 情報元：" + val(
            "source",
            default="サイト投稿",
            limit=100
        )
    ])


# =========================================================
# 5km以内の人への通知を予約
# =========================================================

def enqueue(entry, event_key, notify=True):
    lat, lng = bear_coordinates(entry)

    allowed_ids = set(
        recipients().values()
    )

    conn = connect()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO gps_line_events(event_key)
                    VALUES (%s)
                    ON CONFLICT DO NOTHING
                    RETURNING event_key
                """, (event_key,))

                # 同じ情報は再通知しません。
                if not cur.fetchone():
                    return 0

                # 初回の過去情報はIDだけ記録します。
                if not notify:
                    return 0

                cur.execute("""
                    SELECT user_id, lat, lng
                    FROM gps_line_users
                    WHERE enabled
                      AND updated_at >=
                          NOW() - (%s * INTERVAL '1 minute')
                """, (TTL,))

                count = 0

                for uid, user_lat, user_lng in cur.fetchall():
                    km = distance(
                        user_lat,
                        user_lng,
                        lat,
                        lng
                    )

                    if uid not in allowed_ids or km > 5:
                        continue

                    cur.execute("""
                        INSERT INTO gps_line_jobs (
                            event_key,
                            user_id,
                            message,
                            retry_key
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (
                        event_key,
                        uid,
                        message(entry, km),
                        str(uuid.uuid4())
                    ))

                    count += cur.rowcount

                return count

    finally:
        conn.close()


# =========================================================
# 元のserver.pyが保存後に呼ぶ関数
# 関数名は元のものを、そのまま使います。
# =========================================================

def check_and_send_line_notification(entry):
    raw = json.dumps(
        entry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":")
    )

    key = "site:" + hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    count = enqueue(entry, key)

    print(
        f"[LINE] {count}件を予約しました",
        flush=True
    )

    return count


# =========================================================
# くまだす連携・管理用キー
# =========================================================

def import_authorized():
    key = os.environ.get(
        "IMPORT_API_KEY",
        ""
    )

    return bool(key) and hmac.compare_digest(
        request.headers.get(
            "Authorization",
            ""
        ),
        "Bearer " + key
    )


# =========================================================
# 元のFlaskアプリに通知機能を追加
# =========================================================

def install(app):
    recipients()
    init_db()

    @app.before_request
    def protect_files():
        if request.endpoint == "index":
            request.environ.pop(
                "HTTP_IF_NONE_MATCH",
                None
            )

            request.environ.pop(
                "HTTP_IF_MODIFIED_SINCE",
                None
            )

        # 元の任意ファイル配信から
        # Pythonや秘密設定が漏れるのを防ぎます。
        if request.endpoint == "send_static":
            path = request.view_args.get("path")

            if path not in {
                "app.js",
                "style.css",
                "debug.js",
                "data.json"
            }:
                return "", 404

        # 元の全件移行APIは管理者限定にします。
        if request.endpoint == "force_import":
            if not import_authorized():
                return jsonify(
                    success=False,
                    message="管理者キーが必要です"
                ), 401

    @app.after_request
    def inject_script(response):
        if (
            request.path == "/"
            and response.status_code == 200
            and response.mimetype == "text/html"
        ):
            response.direct_passthrough = False

            html = response.get_data(
                as_text=True
            )

            tag = (
                '<script src="/line/location.js">'
                '</script>'
            )

            if tag not in html:
                response.set_data(
                    html.replace(
                        "</body>",
                        tag + "\n</body>"
                    )
                )

            response.headers.pop(
                "ETag",
                None
            )

            response.headers[
                "Cache-Control"
            ] = "no-store"

        return response

    @app.get("/line/location.js")
    def location_script():
        return send_from_directory(
            ROOT,
            "line_location.js"
        )

    @app.post("/line/location")
    def location():
        key = request.headers.get(
            "X-Line-Location-Key",
            ""
        )

        uid = recipients().get(key)

        if not uid:
            return jsonify(
                success=False,
                message="専用URLを確認してください"
            ), 401

        data = request.get_json(
            silent=True
        )

        if not isinstance(data, dict):
            return jsonify(
                success=False,
                message="形式が不正です"
            ), 400

        if data.get("stop") is not True:
            try:
                lat, lng = coordinates(
                    data.get("lat"),
                    data.get("lng")
                )

                accuracy = float(
                    data.get("accuracy")
                )

                if not 0 <= accuracy <= 1000:
                    raise ValueError()

            except (ValueError, TypeError):
                return jsonify(
                    success=False,
                    message="位置を正確に取得できませんでした"
                ), 400

        conn = connect()

        try:
            with conn:
                with conn.cursor() as cur:
                    if data.get("stop") is True:
                        cur.execute("""
                            UPDATE gps_line_users
                            SET enabled = FALSE
                            WHERE user_id = %s
                        """, (uid,))

                    elif data.get("enable") is True:
                        cur.execute("""
                            INSERT INTO gps_line_users (
                                user_id,
                                lat,
                                lng
                            )
                            VALUES (%s, %s, %s)
                            ON CONFLICT(user_id)
                            DO UPDATE SET
                                lat = EXCLUDED.lat,
                                lng = EXCLUDED.lng,
                                enabled = TRUE,
                                updated_at = NOW()
                        """, (
                            uid,
                            lat,
                            lng
                        ))

                    else:
                        cur.execute("""
                            UPDATE gps_line_users
                            SET
                                lat = %s,
                                lng = %s,
                                updated_at = NOW()
                            WHERE user_id = %s
                              AND enabled
                        """, (
                            lat,
                            lng,
                            uid
                        ))

                        if not cur.rowcount:
                            return jsonify(
                                success=False,
                                message="通知は停止中です"
                            ), 409

            return jsonify(
                success=True,
                ttl=TTL
            )

        finally:
            conn.close()

    @app.post("/line/kumadas")
    def kumadas():
        if not import_authorized():
            return jsonify(
                success=False
            ), 401

        data = request.get_json(
            silent=True
        )

        try:
            if not isinstance(data, dict):
                raise ValueError()

            source_id = data.get("source_id")
            entry = data.get("entry")
            notify = data.get("notify", False)

            if (
                not isinstance(source_id, str)
                or not source_id
                or len(source_id) > 200
                or not isinstance(entry, dict)
                or not isinstance(notify, bool)
            ):
                raise ValueError()

            bear_coordinates(entry)

        except (ValueError, TypeError):
            return jsonify(
                success=False,
                message="形式が不正です"
            ), 400

        entry = dict(
            entry,
            source="くまだす"
        )

        count = enqueue(
            entry,
            "kumadas:" + source_id,
            notify
        )

        return jsonify(
            success=True,
            queued=count
        )


# =========================================================
# LINEへ送信
# =========================================================

def send_line(uid, text, retry_key):
    token = os.environ.get(
        "LINE_ACCESS_TOKEN"
    )

    if not token:
        raise RuntimeError(
            "LINE_ACCESS_TOKEN未設定"
        )

    payload = {
        "to": uid,
        "messages": [
            {
                "type": "text",
                "text": text
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
        with urllib.request.urlopen(
            req,
            timeout=10
        ) as res:
            if res.status != 200:
                raise RuntimeError(
                    "LINEが受理しませんでした"
                )

    except urllib.error.HTTPError as error:
        if (
            error.code == 409
            and error.headers.get(
                "x-line-accepted-request-id"
            )
        ):
            return

        raise RuntimeError(
            f"LINE HTTP {error.code}"
        ) from None


# =========================================================
# 通知ワーカー
# =========================================================

def process_one():
    allowed_ids = set(
        recipients().values()
    )

    conn = connect()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        user_id,
                        message,
                        retry_key,
                        created_at >=
                            NOW() - INTERVAL '1 hour'
                    FROM gps_line_jobs
                    WHERE state = 'pending'
                      AND due_at <= NOW()
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """)

                row = cur.fetchone()

                if not row:
                    return False

                (
                    job_id,
                    uid,
                    text,
                    retry_key,
                    fresh
                ) = row

                cur.execute("""
                    SELECT 1
                    FROM gps_line_users
                    WHERE user_id = %s
                      AND enabled
                      AND updated_at >=
                          NOW() - (%s * INTERVAL '1 minute')
                    FOR UPDATE
                """, (
                    uid,
                    TTL
                ))

                active = cur.fetchone()

                if (
                    not active
                    or not fresh
                    or uid not in allowed_ids
                ):
                    cur.execute("""
                        UPDATE gps_line_jobs
                        SET state = 'cancelled'
                        WHERE id = %s
                    """, (job_id,))

                    return True

                try:
                    send_line(
                        uid,
                        text,
                        retry_key
                    )

                except Exception as error:
                    reason = (
                        str(error)
                        if isinstance(error, RuntimeError)
                        else type(error).__name__
                    )

                    cur.execute("""
                        UPDATE gps_line_jobs
                        SET
                            attempts = attempts + 1,
                            state = CASE
                                WHEN attempts + 1 >= 8
                                THEN 'failed'
                                ELSE 'pending'
                            END,
                            due_at = NOW() + (
                                LEAST(
                                    600,
                                    30 * POWER(2, attempts)
                                ) * INTERVAL '1 second'
                            ),
                            error = %s
                        WHERE id = %s
                    """, (
                        reason,
                        job_id
                    ))

                    print(
                        f"[LINE] job={job_id} error={reason}",
                        flush=True
                    )

                else:
                    cur.execute("""
                        UPDATE gps_line_jobs
                        SET
                            state = 'sent',
                            error = NULL
                        WHERE id = %s
                    """, (job_id,))

                    print(
                        f"[LINE] job={job_id} accepted",
                        flush=True
                    )

        return True

    finally:
        conn.close()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        recipients()
        init_db()

        while True:
            try:
                if not process_one():
                    time.sleep(3)

            except Exception as error:
                print(
                    "[WORKER] " + type(error).__name__,
                    flush=True
                )

                time.sleep(10)

    else:
        print(
            "起動方法："
            "python line_notifier.py --worker"
        )
