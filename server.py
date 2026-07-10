import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# 1. データベースの初期化（余計なリセット処理はすべて排除）
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS bear_data (
            id SERIAL PRIMARY KEY,
            json_records TEXT NOT NULL
        );
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS bear_archive (
            id SERIAL PRIMARY KEY,
            archive_name TEXT NOT NULL,
            json_records TEXT NOT NULL
        );
    ''')

    # 🩹 以前のバージョンで作られた既存テーブルに列が無い場合でも
    #    落ちないように、無ければ追加する（既存データはそのまま保持）
    cur.execute("ALTER TABLE bear_data ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
    cur.execute("ALTER TABLE bear_archive ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")

    conn.commit()
    cur.close()
    conn.close()
    print("[INIT] データベースのテーブル確認が完了しました。")

init_db()

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/<path:path>')
def send_static(path):
    if path == 'data.json':
        return load_data()
    return send_from_directory(BASE_DIR, path)


# 2. データの読み込みAPI（過去5年分の目撃日時を正確に判定して合体）
# ⭕ 【超安全・日付エラー対策版】データの読み込みAPI
@app.route('/api/load', methods=['GET'])
def load_data():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # ① 通常テーブル（直近の投稿データ）を取得 ── 🌟ここにあるデータは5年フィルターを無視して100%絶対に表示する
        cur.execute('SELECT json_records FROM bear_data ORDER BY id DESC;')
        active_rows = cur.fetchall()
        new_posted_records = []
        for row in active_rows:
            new_posted_records.extend(json.loads(row[0]))
            
        # ② アーカイブテーブル（2万件の過去データ）を取得
        cur.execute('SELECT json_records FROM bear_archive;')
        archive_rows = cur.fetchall()
        old_archive_records = []
        for row in archive_rows:
            old_archive_records.extend(json.loads(row[0]))
            
        cur.close()
        conn.close()

        # 📅 アーカイブデータ（古いデータ）に対してのみ「5年間のフィルタリング」を適用する
        filtered_data = []
        five_years_ago = datetime.now() - timedelta(days=5*365)
        
        for item in old_archive_records:
            if not item or "目撃日時" not in item or not item["目撃日時"]:
                continue
            try:
                # 「/」や「-」など、どんな区切り文字でも日付を抽出できるように柔軟にパース
                date_str = item["目撃日時"].replace('-', '/').split(" ")[0] # "2022/5/19" を取得
                item_date = datetime.strptime(date_str, '%Y/%m/%d')
                
                if item_date >= five_years_ago:
                    filtered_data.append(item)
            except Exception:
                # 判定エラーになった古いデータも、念のため消さずに残す（安全策）
                filtered_data.append(item)
        
        # 🌟 「新しく投稿されたデータ」と「5年分に絞った過去データ」を合体させて画面に返す！
        final_combined_data = new_posted_records + filtered_data
        return jsonify(final_combined_data)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 3. 🌟 【軽量版】投稿保存API（新規1件だけを送信・追記するので高速＆通信量が少ない）
@app.route('/api/save', methods=['POST'])
def save_data():
    try:
        entry = request.get_json()
        if not isinstance(entry, dict):
            return jsonify({"success": False, "message": "無効なデータ形式です"}), 400

        conn = get_db_connection()
        cur = conn.cursor()

        # 新規1件を bear_data に追記するだけ（既存データの再送信・全消去はしない）
        cur.execute(
            'INSERT INTO bear_data (json_records, updated_at) VALUES (%s, %s);',
            (json.dumps([entry], ensure_ascii=False), datetime.now())
        )
        conn.commit()

        # 🚨 bear_data に溜まった総件数が1万件を超えたら、まとめてアーカイブへ退避
        cur.execute('SELECT json_records FROM bear_data ORDER BY id ASC;')
        rows = cur.fetchall()
        all_records = []
        for row in rows:
            all_records.extend(json.loads(row[0]))

        if len(all_records) >= 10000:
            now = datetime.now()
            archive_name = f"archive_{now.strftime('%Y%m%d_%H%M%S')}"
            cur.execute(
                'INSERT INTO bear_archive (archive_name, json_records, created_at) VALUES (%s, %s, %s);',
                (archive_name, json.dumps(all_records, ensure_ascii=False), now)
            )
            cur.execute('DELETE FROM bear_data;')
            conn.commit()

        cur.close()
        conn.close()

        return jsonify({"success": True, "message": "投稿をデータベースに保存しました！"})
    except Exception as e:
        return jsonify({"success": False, "message": f"保存エラー: {str(e)}"}), 500


# 4. 【最新版】分割インポート用コマンド
@app.route('/api/force-import', methods=['GET'])
def force_import():
    try:
        page = int(request.args.get('page', 1))
        chunk_size = 3000
        
        local_json_path = os.path.join(BASE_DIR, 'data.json')
        if not os.path.exists(local_json_path):
            return jsonify({"status": "error", "message": "data.jsonが見つかりません"}), 404
            
        with open(local_json_path, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
            
        total_count = len(raw_data)
        chunks = [raw_data[i:i + chunk_size] for i in range(0, len(raw_data), chunk_size)]
        total_pages = len(chunks)
        
        if page < 1 or page > total_pages:
            return jsonify({"status": "error", "message": "無効なページ番号です"}), 400
            
        current_chunk = chunks[page - 1]
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 最初だけ完全リセット
        if page == 1:
            cur.execute('TRUNCATE TABLE bear_data CASCADE;')
            cur.execute('TRUNCATE TABLE bear_archive CASCADE;')
            
        if page < total_pages:
            archive_name = f"archive_init_part{page}"
            cur.execute(
                'INSERT INTO bear_archive (archive_name, json_records) VALUES (%s, %s);',
                (archive_name, json.dumps(current_chunk, ensure_ascii=False))
            )
        else:
            cur.execute('INSERT INTO bear_data (json_records) VALUES (%s);', (json.dumps(current_chunk, ensure_ascii=False),))
            
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({
            "status": "success", 
            "message": f"【ステップ {page} / {total_pages}】データ移行成功！",
            "next_url": f"/api/force-import?page={page + 1}" if page < total_pages else "全データのインポートが完了しました！"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
