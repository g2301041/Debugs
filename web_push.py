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
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urlsplit


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def safe_push_error(exc, code=None):
    if code is not None:
        return f'HTTP {code}'
    if type(exc).__name__ == 'VapidException':
        # 例外の全文には鍵やURLが含まれる可能性があるので公開しない。
        detail = str(exc)
        if "Missing 'sub'" in detail:
            reason = '連絡先設定 VAPID_SUBJECT の形式が不正です'
        elif "Missing 'aud'" in detail:
            reason = '通知サービスの認証先URLの形式が不正です'
        elif 'No private key' in detail:
            reason = '通知用の秘密鍵を読み込めません'
        else:
            reason = '通知用の認証設定を処理できません'
        return 'VapidException [診断版1]: ' + reason
    return type(exc).__name__


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
               or host.endswith('.push.services.mozilla.com')
               or host.endswith('.push.apple.com')
               or host == 'notify.windows.com'
               or host.endswith('.notify.windows.com'))
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
        cur.execute('''CREATE TABLE IF NOT EXISTS bp_vapid (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
            private_key TEXT NOT NULL, public_key TEXT NOT NULL)''')
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
        for table in ('bp_vapid', 'bp_devices', 'bp_events', 'bp_jobs'):
            cur.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def make_vapid_pair():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization as s
    key = ec.generate_private_key(ec.SECP256R1())
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    return (encode(key.private_bytes(s.Encoding.DER, s.PrivateFormat.PKCS8, s.NoEncryption())),
            encode(key.public_key().public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint)))


@lru_cache(maxsize=1)
def vapid_settings():
    private = os.environ.get('VAPID_PRIVATE_KEY', '')
    public = os.environ.get('VAPID_PUBLIC_KEY', '')
    if bool(private) != bool(public):
        raise ValueError('VAPID_PRIVATE_KEYとVAPID_PUBLIC_KEYは両方設定するか、両方未設定にしてください')
    if not private:
        with database() as cur:
            cur.execute('SELECT private_key,public_key FROM bp_vapid WHERE singleton=TRUE')
            row = cur.fetchone()
            if row is None:
                pair = make_vapid_pair()
                cur.execute('INSERT INTO bp_vapid(singleton,private_key,public_key) '
                            'VALUES(TRUE,%s,%s) ON CONFLICT DO NOTHING', pair)
                cur.execute('SELECT private_key,public_key FROM bp_vapid WHERE singleton=TRUE')
                row = cur.fetchone()
            private, public = row
    # py-vapidの連絡先URL検証は末尾の / を拒否するため取り除く。
    # 鍵は変更しないので、既存の通知登録をそのまま利用できる。
    subject = (os.environ.get('VAPID_SUBJECT') or 'https://test-m7ms.onrender.com').strip()
    if subject.startswith('https://'):
        subject = subject.rstrip('/')
    return private, public, subject


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
        try:
            _, key, _ = vapid_settings()
            response = jsonify(publicKey=key, ready=True, version='20260910-free2', mode='on_request', diagnosticRevision='vapid-diagnostic-1')
        except ValueError as error:
            response = jsonify(publicKey='', ready=False, error=str(error), version='20260910-free2')
        except Exception:
            response = jsonify(publicKey='', ready=False,
                               error='通知用の鍵をデータベースから準備できませんでした。', version='20260910-free2')
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
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except (TypeError, KeyError, AttributeError):
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
            event_key = 'test:' + secrets.token_hex(16)
            cur.execute('INSERT INTO bp_jobs(token_hash,event_key,payload) VALUES (%s,%s,%s)',
                        (token, event_key, payload))
        # DBの保存完了後、このリクエスト中に実際に送信する。
        return jsonify(success=True, delivery=dispatch_pending(event_key=event_key))

    @app.post('/api/push/retry')
    def retry():
        token = owner()
        return jsonify(success=True, delivery=dispatch_pending(token_hash=token))

    @app.get('/api/push/status')
    def status():
        token = owner()
        with database() as cur:
            cur.execute('SELECT state,error FROM bp_jobs WHERE token_hash=%s ORDER BY id DESC LIMIT 1', (token,))
            row = cur.fetchone()
        return jsonify(state=row[0] if row else 'none', error=row[1] if row else None)


def deliver_one(job_id=None):
    from pywebpush import webpush, WebPushException
    private_key, _, subject = vapid_settings()
    with database() as cur:
        extra = ' AND j.id=%s' if job_id is not None else ''
        cur.execute('''SELECT j.id,j.payload,j.attempts,d.subscription,d.enabled,
            j.created_at < now()-interval '1 hour', j.token_hash
            FROM bp_jobs j JOIN bp_devices d USING(token_hash)
            WHERE j.state='pending' AND j.due_at<=now()''' + extra + '''
            ORDER BY j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED''',
                    (job_id,) if job_id is not None else ())
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
                    endpoint_host = urlsplit(sub['endpoint']).hostname or ''
                    windows = endpoint_host == 'notify.windows.com' or endpoint_host.endswith('.notify.windows.com')
                    headers = {'X-WNS-Type': 'wns/raw', 'X-WNS-Cache-Policy': 'cache'} if windows else {}
                    response = webpush(validate_subscription(sub),
                        data=json.dumps(payload, ensure_ascii=False),
                        vapid_private_key=private_key,
                        vapid_claims={'sub': subject},
                        ttl=3600, timeout=10, requests_session=session, headers=headers)
                if not 200 <= response.status_code < 300:
                    raise ValueError('unexpected response')
            except Exception as exc:
                if isinstance(exc, WebPushException) and exc.response is not None:
                    code = exc.response.status_code
                error = safe_push_error(exc, code)
                state = 'pending' if attempts < 7 and (code is None or code in (429, 500, 502, 503, 504)) else 'failed'
                if type(exc).__name__ == 'VapidException':
                    state = 'failed'  # 設定エラーは待ち続けても解消しない。
                if code in (404, 410):
                    cur.execute('UPDATE bp_devices SET enabled=FALSE WHERE token_hash=%s', (token,))
        cur.execute('''UPDATE bp_jobs SET state=%s,error=%s,attempts=attempts+1,
            due_at=now()+(%s * interval '1 second') WHERE id=%s''',
                    (state, error, min(900, 15 * 2 ** attempts), job))
        return True


def dispatch_pending(event_key=None, token_hash=None):
    """保存後に送信し終わるまで待つ。別プロセスや常駐ワーカーは不要。"""
    if (event_key is None) == (token_hash is None):
        raise ValueError('通知イベントか端末のどちらかを指定してください')
    try:
        vapid_settings()
    except Exception:
        return 'not_configured'
    column, value = ('event_key', event_key) if event_key is not None else ('token_hash', token_hash)
    try:
        with database() as cur:
            cur.execute(f"SELECT id FROM bp_jobs WHERE {column}=%s "
                        "AND state='pending' AND due_at<=now() ORDER BY id", (value,))
            ids = [row[0] for row in cur.fetchall()]
        # 全員分を対象にする。大量配信向けではなく、小規模利用を想定。
        # ワーカーというサービスは作らず、同じHTTPリクエスト内で完了を待つ。
        if ids:
            def send(job_id):
                try:
                    return deliver_one(job_id)
                except Exception as error:
                    print('Push delivery:', type(error).__name__, flush=True)
                    return False
            with ThreadPoolExecutor(max_workers=4) as executor:
                completed = list(executor.map(send, ids))
            if not all(completed):
                return 'pending'
        with database() as cur:
            cur.execute(f'SELECT state FROM bp_jobs WHERE {column}=%s', (value,))
            states = [row[0] for row in cur.fetchall()]
        if not states:
            return 'none'
        if 'pending' in states:
            return 'pending'
        if 'failed' in states:
            return 'failed'
        if 'accepted' in states:
            return 'accepted'
        return states[0]
    except Exception as error:
        # 熊情報は既にコミット済み。通知失敗を投稿の保存失敗として返さない。
        print('Push dispatch:', type(error).__name__, flush=True)
        return 'pending'


if __name__ == '__main__':
    if '--keys' in sys.argv:
        private, public = make_vapid_pair()
        print('VAPID_PRIVATE_KEY=' + private)
        print('VAPID_PUBLIC_KEY=' + public)
    else:
        print('無料構成ではこのファイルを常駐させません。server.pyから投稿時に送信します。')
        print('鍵を作成する場合: python web_push.py --keys')
