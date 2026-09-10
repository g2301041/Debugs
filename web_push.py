import base64
import hashlib
import json
import math
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from urllib.parse import urlsplit


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def database():
    import psycopg2
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    finally:
        conn.close()


def coordinates(value):
    if value is None:
        return None
    if isinstance(value['lat'], bool) or isinstance(value['lng'], bool):
        raise ValueError('緯度・経度が不正です')
    lat, lng = float(value['lat']), float(value['lng'])
    if not (math.isfinite(lat) and math.isfinite(lng)
            and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError('緯度・経度が不正です')
    return {'lat': lat, 'lng': lng}


def distance(a, b):
    p, q = math.radians(a['lat']), math.radians(b['lat'])
    h = (math.sin((q - p) / 2) ** 2
         + math.cos(p) * math.cos(q)
         * math.sin(math.radians(b['lng'] - a['lng']) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def matching_places(point, settings):
    return [(label, distance(point, settings[key]))
            for key, label in [('last', '最終取得位置'), ('fixed', '指定場所')]
            if settings.get(key) is not None
            and distance(point, settings[key]) <= 5]


def validate_subscription(sub):
    url = urlsplit(sub['endpoint'])
    host = url.hostname or ''
    allowed = (host in {'fcm.googleapis.com', 'web.push.apple.com',
                       'updates.push.services.mozilla.com'}
               or host.endswith('.push.services.mozilla.com'))
    if not (url.scheme == 'https' and url.port in (None, 443)
            and not url.username and not url.password and allowed
            and not url.fragment and len(sub['endpoint']) < 4096):
        raise ValueError('このブラウザーの通知先には対応していません')
    for key, size in [('p256dh', 65), ('auth', 16)]:
        raw = sub['keys'][key]
        if not isinstance(raw, str) or len(raw) > 128:
            raise ValueError('通知用の鍵が不正です')
        if len(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))) != size:
            raise ValueError('通知用の鍵が不正です')
    return {'endpoint': sub['endpoint'], 'keys': sub['keys']}


def init_push_db():
    with database() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS bp_devices (
            token_hash TEXT PRIMARY KEY, endpoint TEXT UNIQUE NOT NULL,
            subscription JSONB NOT NULL, settings JSONB NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS bp_events (
            event_key TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        cur.execute('''CREATE TABLE IF NOT EXISTS bp_jobs (
            id BIGSERIAL PRIMARY KEY,
            token_hash TEXT NOT NULL REFERENCES bp_devices(token_hash),
            event_key TEXT NOT NULL, payload JSONB NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
            due_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), error TEXT,
            UNIQUE(token_hash, event_key))''')
        cur.execute('CREATE INDEX IF NOT EXISTS bp_jobs_due ON bp_jobs(state, due_at)')
        # Supabaseの公開APIから位置情報・通知先を読ませない。
        for table in ('bp_devices', 'bp_events', 'bp_jobs'):
            cur.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def enqueue(cur, entry, event_key, notify=True):
    """熊情報の保存と同じトランザクション内で呼ぶ。新規ならTrue。"""
    cur.execute('INSERT INTO bp_events(event_key) VALUES (%s) '
                'ON CONFLICT DO NOTHING RETURNING event_key', (event_key,))
    if cur.fetchone() is None:
        return False
    if not notify:
        return True
    point = coordinates({'lat': entry.get('x(緯度)', entry.get('lat')),
                         'lng': entry.get('y(経度)', entry.get('lng'))})
    cur.execute('SELECT token_hash, settings FROM bp_devices WHERE enabled')
    for token, settings in cur.fetchall():
        matches = matching_places(point, settings)
        if not matches:
            continue
        places = '・'.join(label for label, _ in matches)
        km = min(km for _, km in matches)
        location = str(entry.get('地番情報') or entry.get('location') or '場所不明')[:120]
        date = str(entry.get('目撃日時') or entry.get('date') or '日時不明')[:40]
        payload = {'title': '🐻 5km以内に新しい熊情報',
                   'body': f'{places}から約{km:.2f}km\n{location}\n{date}',
                   'tag': event_key, 'url': '/'}
        cur.execute('INSERT INTO bp_jobs(token_hash,event_key,payload) '
                    'VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                    (token, event_key, json.dumps(payload, ensure_ascii=False)))
    return True


def install_push(app):
    from flask import request, jsonify, abort
    init_push_db()

    def owner():
        raw = request.headers.get('Authorization', '')
        if not re.fullmatch(r'Bearer [0-9a-f]{64}', raw):
            abort(401)
        return digest(raw[7:])

    @app.get('/api/push/config')
    def config():
        key = os.environ.get('VAPID_PUBLIC_KEY', '')
        missing = [name for name in ('VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY', 'VAPID_SUBJECT')
                   if not os.environ.get(name)]
        response = jsonify(publicKey=key, ready=not missing, missing=missing,
                           version='20260910-fix1')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/api/push/device', methods=['POST', 'DELETE'])
    def device():
        token = owner()
        if request.method == 'DELETE':
            with database() as cur:
                cur.execute('UPDATE bp_devices SET enabled=FALSE WHERE token_hash=%s', (token,))
                cur.execute("UPDATE bp_jobs SET state='cancelled' "
                            "WHERE token_hash=%s AND state='pending'", (token,))
            return jsonify(success=True)
        try:
            body = request.get_json()
            sub = validate_subscription(body['subscription'])
            settings = {k: coordinates(body.get(k)) for k in ('last', 'fixed')}
            # 日時は表示用。最終位置には自動の有効期限を設けない。
            settings['lastAt'] = str(body.get('lastAt', ''))[:40]
            if not settings['last'] and not settings['fixed']:
                raise ValueError('最終位置か指定場所を設定してください')
        except (ValueError, TypeError, KeyError, AttributeError):
            return jsonify(error='通知先または位置情報が不正です'), 400
        with database() as cur:
            cur.execute('SELECT token_hash FROM bp_devices WHERE endpoint=%s', (sub['endpoint'],))
            row = cur.fetchone()
            if row and row[0] != token:
                return jsonify(error='以前の通知登録が残っています。サイトのデータを消去して再登録してください'), 409
            cur.execute('''INSERT INTO bp_devices(token_hash,endpoint,subscription,settings)
                VALUES (%s,%s,%s,%s) ON CONFLICT(token_hash) DO UPDATE SET
                endpoint=EXCLUDED.endpoint, subscription=EXCLUDED.subscription,
                settings=EXCLUDED.settings, enabled=TRUE''',
                        (token, sub['endpoint'], json.dumps(sub), json.dumps(settings)))
        return jsonify(success=True)

    @app.route('/api/push/test', methods=['POST'])
    def test():
        token = owner()
        with database() as cur:
            cur.execute('SELECT 1 FROM bp_devices WHERE token_hash=%s AND enabled', (token,))
            if not cur.fetchone():
                return jsonify(error='先に通知を有効にしてください'), 400
            cur.execute("SELECT 1 FROM bp_jobs WHERE token_hash=%s "
                        "AND created_at > now()-interval '1 minute'", (token,))
            if cur.fetchone():
                return jsonify(error='1分待ってから再試行してください'), 429
            payload = json.dumps({'title': 'ベアウェザー 通知テスト',
                                  'body': 'この端末への通知が届きました。',
                                  'tag': 'test-' + secrets.token_hex(8), 'url': '/'})
            cur.execute('INSERT INTO bp_jobs(token_hash,event_key,payload) VALUES (%s,%s,%s)',
                        (token, 'test:' + secrets.token_hex(16), payload))
        return jsonify(success=True)

    @app.get('/api/push/status')
    def status():
        token = owner()
        with database() as cur:
            cur.execute('SELECT state,error FROM bp_jobs WHERE token_hash=%s ORDER BY id DESC LIMIT 1', (token,))
            row = cur.fetchone()
        return jsonify(state=row[0] if row else 'none', error=row[1] if row else None)


def deliver_one():
    from pywebpush import webpush, WebPushException
    with database() as cur:
        cur.execute('''SELECT j.id,j.payload,j.attempts,d.subscription,d.enabled,
            j.created_at < now()-interval '1 hour', j.token_hash
            FROM bp_jobs j JOIN bp_devices d USING(token_hash)
            WHERE j.state='pending' AND j.due_at<=now()
            ORDER BY j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED''')
        row = cur.fetchone()
        if not row:
            return False
        job, payload, attempts, sub, enabled, expired, token = row
        state, error, code = 'accepted', None, None
        if not enabled or expired:
            state = 'cancelled' if not enabled else 'expired'
        else:
            try:
                # リダイレクトを追わせず、登録済み通知サービスのみに送る。
                import requests
                class PushSession(requests.Session):
                    def request(self, *args, **kwargs):
                        kwargs['allow_redirects'] = False
                        return super().request(*args, **kwargs)
                with PushSession() as session:
                    response = webpush(validate_subscription(sub),
                        data=json.dumps(payload, ensure_ascii=False),
                        vapid_private_key=os.environ['VAPID_PRIVATE_KEY'],
                        vapid_claims={'sub': os.environ['VAPID_SUBJECT']},
                        ttl=3600, timeout=10, requests_session=session)
                if not 200 <= response.status_code < 300:
                    raise ValueError('unexpected response')
            except Exception as exc:
                if isinstance(exc, WebPushException) and exc.response is not None:
                    code = exc.response.status_code
                error = f'HTTP {code}' if code else type(exc).__name__
                state = 'pending' if attempts < 7 and (code is None or code in (429, 500, 502, 503, 504)) else 'failed'
                if code in (404, 410):
                    cur.execute('UPDATE bp_devices SET enabled=FALSE WHERE token_hash=%s', (token,))
        cur.execute('''UPDATE bp_jobs SET state=%s,error=%s,attempts=attempts+1,
            due_at=now()+(%s * interval '1 second') WHERE id=%s''',
                    (state, error, min(900, 15 * 2 ** attempts), job))
        return True


if __name__ == '__main__':
    if '--keys' in sys.argv:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization as s
        key = ec.generate_private_key(ec.SECP256R1())
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
        print('VAPID_PRIVATE_KEY=' + encode(key.private_bytes(s.Encoding.DER, s.PrivateFormat.PKCS8, s.NoEncryption())))
        print('VAPID_PUBLIC_KEY=' + encode(key.public_key().public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint)))
    else:
        for name in ('DATABASE_URL', 'VAPID_PRIVATE_KEY', 'VAPID_PUBLIC_KEY', 'VAPID_SUBJECT'):
            if not os.environ.get(name):
                raise SystemExit(f'{name} を設定してください')
        init_push_db()
        while True:
            try:
                if not deliver_one():
                    time.sleep(3)
            except Exception as exc:
                print('Push worker:', type(exc).__name__, flush=True)
                time.sleep(10)
