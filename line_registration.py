import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import jsonify, request

import line_notifier


log = logging.getLogger(__name__)


# =========================================================
# 設定の読み込み
# =========================================================

def settings():
    secret = os.environ.get(
        "LINE_CHANNEL_SECRET",
        ""
    ).strip()

    origin = os.environ.get(
        "APP_URL",
        ""
    ).strip().rstrip("/")

    parsed = urllib.parse.urlsplit(origin)

    if (
        not secret
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "LINE_CHANNEL_SECRETと"
            "APP_URL（HTTPSのサイトURL）を設定してください"
        )

    return secret, origin


# =========================================================
# 自動登録用テーブル
# 元の熊情報は変更しません。
# =========================================================

def init_registration_db():
    conn = line_notifier.connect()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(731909)"
                )

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gps_line_link_users (
                        user_id TEXT PRIMARY KEY,
                        nonce TEXT NOT NULL,
                        followed BOOLEAN NOT NULL DEFAULT FALSE,
                        event_time BIGINT NOT NULL DEFAULT -1
                    );

                    CREATE TABLE IF NOT EXISTS gps_line_webhook_events (
                        event_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    ALTER TABLE gps_line_link_users
                    ENABLE ROW LEVEL SECURITY;

                    ALTER TABLE gps_line_webhook_events
                    ENABLE ROW LEVEL SECURITY;
                """)

    finally:
        conn.close()


# =========================================================
# 利用者専用URLに使うキーを作成
# LINEユーザーIDはURLに載せません。
# =========================================================

def location_key(user_id, nonce):
    secret, _ = settings()

    body = (
        "bear-weather-location:"
        + user_id
        + ":"
        + nonce
    ).encode("utf-8")

    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    return base64.urlsafe_b64encode(
        digest
    ).decode("ascii").rstrip("=")


# =========================================================
# 自動登録された送信先一覧
#
# 前回のline_notifier.pyのrecipients()と
# 同じ形式で返します。
# =========================================================

def registered_recipients():
    conn = line_notifier.connect()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, nonce
                FROM gps_line_link_users
                WHERE followed = TRUE
            """)

            result = {}

            for user_id, nonce in cur.fetchall():
                key = location_key(
                    user_id,
                    nonce
                )

                result[key] = user_id

            return result

    finally:
        conn.close()


# =========================================================
# LINEから届いた通信の署名確認
#
# 利用者の本人確認画面ではありません。
# サーバー内部でLINEからの通信かを確認します。
# =========================================================

def valid_signature(body, signature):
    secret, _ = settings()

    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(
        digest
    ).decode("ascii")

    try:
        return hmac.compare_digest(
            expected.encode("ascii"),
            signature.encode("ascii")
        )

    except (UnicodeError, AttributeError):
        return False


# =========================================================
# 専用URLをLINEで返信
# =========================================================

def reply_with_link(reply_token, user_id, nonce):
    _, origin = settings()

    token = os.environ.get(
        "LINE_ACCESS_TOKEN",
        ""
    ).strip()

    if not token:
        raise RuntimeError(
            "LINE_ACCESS_TOKENが未設定です"
        )

    url = (
        origin
        + "/#line="
        + location_key(user_id, nonce)
    )

    text = (
        "熊情報の通知用URLです。\n\n"
        + url
        + "\n\n"
        "このURLを開き、地図の📍ボタンで"
        "現在地の取得を許可してください。\n"
        "取得した位置から5km以内の新着情報を"
        "LINEでお知らせします。\n\n"
        "位置を取得するまでは通知されません。\n"
        "このURLはあなた専用です。"
        "他の人には共有しないでください。"
    )

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }

    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=10
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    "LINEが返信を受理しませんでした"
                )

        return "sent"

    except urllib.error.HTTPError as error:
        try:
            reason = json.loads(
                error.read()
            ).get("message", "")

        except Exception:
            reason = ""

        # 応答トークンは一度だけ使えます。
        # 使用済み・期限切れの場合は、
        # 次のメッセージで再びURLを返信します。
        if (
            error.code == 400
            and reason == "Invalid reply token"
        ):
            log.warning(
                "LINE返信トークンが使用済みまたは期限切れです。"
                "再メッセージでURLを再送できます。"
            )

            return "expired"

        raise RuntimeError(
            f"LINE返信 HTTP {error.code}"
        ) from None


# =========================================================
# 友だち追加・メッセージ・ブロックの処理
# =========================================================

def process_event(event):
    if not isinstance(event, dict):
        return

    if event.get("mode", "active") != "active":
        return

    kind = event.get("type")
    source = event.get("source") or {}

    if kind not in (
        "follow",
        "unfollow",
        "message"
    ):
        return

    # 個人とのトークだけを対象にします。
    if source.get("type") != "user":
        return

    user_id = source.get("userId", "")
    event_id = event.get("webhookEventId")
    timestamp = event.get("timestamp")

    if (
        not isinstance(user_id, str)
        or not re.fullmatch(
            r"U[0-9a-f]{32}",
            user_id
        )
        or not isinstance(event_id, str)
        or not event_id
        or len(event_id) > 200
        or type(timestamp) is not int
        or timestamp < 0
    ):
        raise ValueError(
            "Webhookイベントの形式が不正です"
        )

    conn = line_notifier.connect()

    try:
        with conn.cursor() as cur:
            # 同じ利用者の処理が同時に実行されないようにします。
            # 接続を閉じるとロックは解除されます。
            cur.execute("""
                SELECT pg_advisory_lock(
                    hashtextextended(%s, 0)
                )
            """, (
                "line-register:" + user_id,
            ))

        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO gps_line_webhook_events (
                        event_id
                    )
                    VALUES (%s)
                    ON CONFLICT DO NOTHING
                """, (event_id,))

                cur.execute("""
                    SELECT status
                    FROM gps_line_webhook_events
                    WHERE event_id = %s
                """, (event_id,))

                current_status = cur.fetchone()[0]

                # 処理済みの再送イベントは無視します。
                if current_status != "pending":
                    return

                cur.execute("""
                    INSERT INTO gps_line_link_users (
                        user_id,
                        nonce
                    )
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    user_id,
                    secrets.token_hex(16)
                ))

                cur.execute("""
                    SELECT
                        nonce,
                        followed,
                        event_time
                    FROM gps_line_link_users
                    WHERE user_id = %s
                """, (user_id,))

                (
                    nonce,
                    followed,
                    last_timestamp
                ) = cur.fetchone()

                # 再送によって古いイベントが後から届いても、
                # 新しいブロック状態などを上書きしません。
                stale = (
                    timestamp < last_timestamp
                    or (
                        timestamp == last_timestamp
                        and not followed
                        and kind != "unfollow"
                    )
                )

                if stale:
                    cur.execute("""
                        UPDATE gps_line_webhook_events
                        SET status = 'ignored'
                        WHERE event_id = %s
                    """, (event_id,))

                    return

                # -----------------------------------------
                # ブロックされた場合
                # -----------------------------------------

                if kind == "unfollow":
                    cur.execute("""
                        UPDATE gps_line_link_users
                        SET
                            nonce = %s,
                            followed = FALSE,
                            event_time = %s
                        WHERE user_id = %s
                    """, (
                        secrets.token_hex(16),
                        timestamp,
                        user_id
                    ))

                    cur.execute("""
                        UPDATE gps_line_users
                        SET enabled = FALSE
                        WHERE user_id = %s
                    """, (user_id,))

                    cur.execute("""
                        UPDATE gps_line_webhook_events
                        SET status = 'done'
                        WHERE event_id = %s
                    """, (event_id,))

                    return

                # -----------------------------------------
                # 友だち追加・メッセージ受信の場合
                # -----------------------------------------

                if not followed:
                    # 初回登録・ブロック解除時は新しいURLにします。
                    nonce = secrets.token_hex(16)

                    # 古い位置で勝手に通知を再開しないようにします。
                    cur.execute("""
                        UPDATE gps_line_users
                        SET enabled = FALSE
                        WHERE user_id = %s
                    """, (user_id,))

                cur.execute("""
                    UPDATE gps_line_link_users
                    SET
                        nonce = %s,
                        followed = TRUE,
                        event_time = %s
                    WHERE user_id = %s
                """, (
                    nonce,
                    timestamp,
                    user_id
                ))

        # 登録を先に確定させます。
        # 返信通信に失敗しても同じURLを再送できます。
        reply_token = event.get("replyToken")

        if reply_token:
            reply_status = reply_with_link(
                reply_token,
                user_id,
                nonce
            )

        else:
            reply_status = "expired"

        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE gps_line_webhook_events
                    SET status = %s
                    WHERE event_id = %s
                """, (
                    reply_status,
                    event_id
                ))

    finally:
        conn.close()


# =========================================================
# 元のアプリへの接続
# =========================================================

def enable_auto_registration(app):
    settings()
    init_registration_db()

    # 手入力のLINE_RECIPIENTS_JSONの代わりに、
    # DBへ自動登録された送信先を使います。
    line_notifier.recipients = registered_recipients

    @app.post("/line/webhook")
    def line_webhook():
        # JSON変換前の受信データで署名を確認します。
        raw = request.get_data()

        signature = request.headers.get(
            "X-Line-Signature",
            ""
        )

        if not valid_signature(raw, signature):
            return jsonify(
                success=False
            ), 403

        try:
            payload = json.loads(raw)

            if (
                not isinstance(payload, dict)
                or not isinstance(
                    payload.get("events"),
                    list
                )
            ):
                raise ValueError()

        except (ValueError, UnicodeError):
            return jsonify(
                success=False
            ), 400

        failed = False

        for event in payload["events"]:
            try:
                process_event(event)

            except Exception as error:
                # トークン・専用URL・ユーザーIDはログに出しません。
                log.error(
                    "LINE登録処理に失敗: %s",
                    type(error).__name__
                )

                failed = True

        return jsonify(
            success=not failed
        ), 503 if failed else 200


# =========================================================
# 自動登録された利用者への通知ワーカー
# =========================================================

if __name__ == "__main__":
    if "--worker" in sys.argv:
        settings()

        init_registration_db()

        line_notifier.recipients = (
            registered_recipients
        )

        line_notifier.init_db()

        while True:
            try:
                if not line_notifier.process_one():
                    time.sleep(3)

            except Exception as error:
                print(
                    "[WORKER] "
                    + type(error).__name__,
                    flush=True
                )

                time.sleep(10)

    else:
        print(
            "起動方法: "
            "python line_registration.py --worker"
        )
