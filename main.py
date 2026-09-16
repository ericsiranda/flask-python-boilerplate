import os
import time
import re
import bcrypt
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob
import requests as req_lib
from google import genai
from google.genai import types

app = Flask(__name__)

# Inisialisasi klien Gemini (untuk Veo)
gemini_client = genai.Client()


# ==================== DATABASE CONNECTION ====================
def get_db():
    priority_keys = ['DB_URL', 'DATABASE_URL', 'SUPABASE_POSTGRES_URL', 'POSTGRES_URL']
    db_url = None
    used_key = None

    for key in priority_keys:
        val = os.environ.get(key)
        if val and val.startswith('postgres'):
            db_url = val
            used_key = key
            break

    if not db_url:
        for key in sorted(os.environ.keys()):
            if ('POSTGRES_URL' in key or 'DATABASE_URL' in key) and 'PRISMA' not in key:
                val = os.environ.get(key)
                if val and val.startswith('postgres'):
                    db_url = val
                    used_key = key
                    break

    if not db_url:
        print("⚠️ Tidak ada URL database di environment")
        return None

    try:
        print(f"DB: mencoba {used_key}...")
        conn = psycopg2.connect(db_url, sslmode='require', connect_timeout=10)
        print(f"DB: ✅ berhasil dengan {used_key}")
        return conn
    except Exception as e:
        print(f"DB: ❌ {used_key} gagal: {str(e)[:150]}")
        return None


def init_tables():
    conn = get_db()
    if not conn:
        return False, "Database tidak tersedia"

    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(100),
                video_name VARCHAR(255),
                video_url TEXT,
                time VARCHAR(50),
                platform VARCHAR(50),
                account VARCHAR(100),
                caption TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS medsos_accounts (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(50) NOT NULL,
                username VARCHAR(100) NOT NULL,
                connected_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(platform, username)
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True, None
    except Exception as e:
        print(f"Init tables error: {e}")
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return False, str(e)


# ==================== VEO VIDEO GENERATION ====================
@app.route('/api/generate-video', methods=['POST'])
def generate_video():
    data = request.json
    prompt = data.get('prompt')

    if not prompt:
        return jsonify({'error': 'Prompt tidak boleh kosong'}), 400

    try:
        print(f"🎬 Generating video with Veo... Prompt: {prompt[:50]}...")

        operation = gemini_client.models.generate_videos(
            model="veo-3.1-generate-preview",
            prompt=prompt,
        )

        while not operation.done:
            print("⏳ Waiting for video generation...")
            time.sleep(8)
            operation = gemini_client.operations.get(operation)

        generated = operation.response.generated_videos[0]
        video_bytes = gemini_client.files.download(file=generated.video)

        filename = f"ai_generated_{int(time.time())}.mp4"
        result = put(filename, video_bytes, access='public', multipart=True)

        return jsonify({
            'success': True,
            'video_url': result.url,
            'pathname': result.pathname
        })

    except Exception as e:
        print(f"❌ Veo Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== INSTAGRAM POSTING ====================
@app.route('/api/instagram/post', methods=['POST'])
def instagram_post():
    data = request.json
    video_url = data.get('video_url')
    caption = data.get('caption', '')

    if not video_url:
        return jsonify({'error': 'Video URL tidak disediakan'}), 400

    access_token = os.environ.get('INSTAGRAM_ACCESS_TOKEN')
    ig_user_id = os.environ.get('INSTAGRAM_USER_ID')

    if not access_token or not ig_user_id:
        return jsonify({'error': 'Instagram credentials belum di-set'}), 500

    try:
        container_url = f"https://graph.instagram.com/v21.0/{ig_user_id}/media"
        container_payload = {
            'media_type': 'REELS',
            'video_url': video_url,
            'caption': caption,
            'access_token': access_token
        }

        container_res = req_lib.post(container_url, data=container_payload)
        container_data = container_res.json()

        if 'id' not in container_data:
            return jsonify({'error': f'Gagal create container: {container_data}'}), 500

        creation_id = container_data['id']
        print(f"✅ Container created: {creation_id}")

        max_retries = 30
        for i in range(max_retries):
            status_url = f"https://graph.instagram.com/v21.0/{creation_id}?fields=status_code&access_token={access_token}"
            status_res = req_lib.get(status_url)
            status_data = status_res.json()

            if status_data.get('status_code') == 'FINISHED':
                print("✅ Video ready to publish")
                break
            elif status_data.get('status_code') == 'ERROR':
                return jsonify({'error': 'Video processing error'}), 500

            time.sleep(5)

        publish_url = f"https://graph.instagram.com/v21.0/{ig_user_id}/media_publish"
        publish_payload = {
            'creation_id': creation_id,
            'access_token': access_token
        }

        publish_res = req_lib.post(publish_url, data=publish_payload)
        publish_data = publish_res.json()

        return jsonify({
            'success': True,
            'media_id': publish_data.get('id')
        })

    except Exception as e:
        print(f"❌ Instagram Error: {e}")
        return jsonify({'error': str(e)}), 500


# ==================== USER MANAGEMENT ====================
@app.route('/api/users/has-any', methods=['GET'])
def has_any_user():
    try:
        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'success': True, 'has_users': False, 'count': 0, 'db_available': False})
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({'success': True, 'has_users': count > 0, 'count': count, 'db_available': True})
    except Exception as e:
        print(f"Error has_any_user: {e}")
        return jsonify({'success': True, 'has_users': False, 'count': 0, 'db_available': False})


@app.route('/api/users/list', methods=['GET'])
def list_users():
    try:
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT username, created_at FROM users ORDER BY created_at ASC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        users = [{'username': r['username'], 'createdAt': r['created_at'].isoformat() if r['created_at'] else ''} for r in rows]
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/add', methods=['POST'])
def add_user():
    try:
        data = request.json
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''

        if not username or len(username) < 3:
            return jsonify({'error': 'Username minimal 3 karakter'}), 400
        if not password or len(password) < 4:
            return jsonify({'error': 'Password minimal 4 karakter'}), 400

        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500

        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': 'Username sudah digunakan'}), 400

        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (username, password_hash))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'message': 'User berhasil ditambahkan'})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/login', methods=['POST'])
def login_user():
    try:
        data = request.json
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''

        if not username or not password:
            return jsonify({'error': 'Username & password wajib diisi'}), 400

        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT username, password_hash FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return jsonify({'error': 'Username atau password salah'}), 401
        if not bcrypt.checkpw(password.encode('utf-8'), row['password_hash'].encode('utf-8')):
            return jsonify({'error': 'Username atau password salah'}), 401

        return jsonify({'success': True, 'user': {'username': row['username']}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/delete', methods=['POST'])
def delete_user():
    try:
        data = request.json
        username = (data.get('username') or '').strip()
        if not username:
            return jsonify({'error': 'Username wajib'}), 400

        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== SCHEDULE ====================
@app.route('/api/schedules/list', methods=['GET'])
def list_schedules():
    try:
        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM schedules ORDER BY time ASC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        schedules = []
        for r in rows:
            schedules.append({
                'id': r['id'],
                'session_id': r['session_id'] or '',
                'video_name': r['video_name'] or '',
                'video_url': r['video_url'] or '',
                'time': r['time'] or '',
                'platform': r['platform'] or '',
                'account': r['account'] or '',
                'caption': r['caption'] or '',
            })
        return jsonify({'success': True, 'schedules': schedules})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedules/add', methods=['POST'])
def add_schedule():
    try:
        data = request.json
        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO schedules (session_id, video_name, video_url, time, platform, account, caption)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            data.get('session_id', ''),
            data.get('video_name', ''),
            data.get('video_url', ''),
            data.get('time', ''),
            data.get('platform', ''),
            data.get('account', ''),
            data.get('caption', ''),
        ))
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedules/delete', methods=['POST'])
def delete_schedule():
    try:
        data = request.json
        schedule_id = data.get('id')
        if not schedule_id:
            return jsonify({'error': 'ID wajib'}), 400

        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== MEDSOS ACCOUNTS ====================
@app.route('/api/medsos/list', methods=['GET'])
def list_medsos():
    try:
        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT platform, username, connected_at FROM medsos_accounts ORDER BY connected_at ASC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        accounts = []
        for r in rows:
            accounts.append({
                'platform': r['platform'],
                'username': r['username'],
                'connectedAt': r['connected_at'].isoformat() if r['connected_at'] else '',
            })
        return jsonify({'success': True, 'accounts': accounts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/medsos/add', methods=['POST'])
def add_medsos():
    try:
        data = request.json
        platform = data.get('platform', '')
        username = data.get('username', '')

        if not platform or not username:
            return jsonify({'error': 'Data tidak lengkap'}), 400

        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM medsos_accounts WHERE platform = %s AND LOWER(username) = LOWER(%s)",
            (platform, username)
        )
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'error': 'Akun sudah terhubung'}), 400

        cur.execute("INSERT INTO medsos_accounts (platform, username) VALUES (%s, %s)", (platform, username))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/medsos/delete', methods=['POST'])
def delete_medsos():
    try:
        data = request.json
        platform = data.get('platform', '')
        username = data.get('username', '')
        if not platform or not username:
            return jsonify({'error': 'Data tidak lengkap'}), 400

        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM medsos_accounts WHERE platform = %s AND LOWER(username) = LOWER(%s)",
            (platform, username)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== BLOB VIDEO ====================
@app.route('/api/upload', methods=['POST'])
def upload_video():
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'Tidak ada file video'}), 400
        file = request.files['video']
        if file.filename == '':
            return jsonify({'error': 'Nama file kosong'}), 400
        file_content = file.read()
        result = put(file.filename, file_content, access='public', multipart=True)
        return jsonify({'success': True, 'url': result.url, 'pathname': result.pathname,
                       'filename': file.filename, 'size': len(file_content)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload-chunk', methods=['POST'])
def upload_chunk():
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'Tidak ada file video'}), 400
        file = request.files['video']
        session_id = request.form.get('session_id', 'default')
        chunk_index = int(request.form.get('chunk_index', 0))
        file_content = file.read()
        chunk_filename = f"cut_{session_id}_{chunk_index:03d}.webm"
        result = put(chunk_filename, file_content, access='public', multipart=True)
        return jsonify({'success': True, 'url': result.url, 'pathname': result.pathname,
                       'chunk_index': chunk_index, 'size': len(file_content)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/finalize-cut', methods=['POST'])
def finalize_cut():
    return jsonify({'success': True})


@app.route('/api/list-files-grouped', methods=['GET'])
def list_files_grouped():
    try:
        files = vercel_blob.list()
        sessions, singles, ai_edits = {}, [], []

        for item in files.get('blobs', []):
            pathname = item.get('pathname', '')
            if pathname.startswith('ai_edit_'):
                ai_edits.append({'pathname': pathname, 'url': item.get('url'),
                                'size': item.get('size', 0), 'uploadedAt': item.get('uploadedAt'),
                                'session_id': pathname.replace('ai_edit_', '').replace('.webm', '')})
                continue
            if pathname.startswith('cut_') and pathname.endswith('.webm'):
                parts = pathname.replace('.webm', '').split('_')
                if len(parts) >= 3:
                    sid = parts[1]
                    try: idx = int(parts[2])
                    except: continue
                    if sid not in sessions:
                        sessions[sid] = {'session_id': sid, 'chunks': [], 'total_size': 0, 'total_chunks': 0}
                    sessions[sid]['chunks'].append({'pathname': pathname, 'url': item.get('url'),
                                                    'size': item.get('size', 0), 'index': idx})
                    sessions[sid]['total_size'] += item.get('size', 0)
                    sessions[sid]['total_chunks'] += 1
            else:
                singles.append({'pathname': pathname, 'url': item.get('url'),
                               'size': item.get('size', 0), 'is_single': True})

        for sid in sessions: sessions[sid]['chunks'].sort(key=lambda x: x['index'])
        ai_edit_map = {e['session_id']: e for e in ai_edits}

        result = []
        for sid, s in sessions.items():
            has_ai = sid in ai_edit_map
            result.append({'pathname': f"video_utuh_{sid}.webm", 'url': s['chunks'][0]['url'],
                          'size': s['total_size'], 'is_single': False, 'session_id': sid,
                          'chunks': s['chunks'], 'total_chunks': s['total_chunks'],
                          'ai_status': 'done' if has_ai else 'idle', 'ai_result': ai_edit_map.get(sid)})
        result.extend(singles)
        return jsonify({'success': True, 'files': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete-file', methods=['POST'])
def delete_file():
    try:
        url = request.json.get('url')
        if not url: return jsonify({'error': 'URL tidak diberikan'}), 400
        vercel_blob.delete(url)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rename-file', methods=['POST'])
def rename_file():
    try:
        data = request.json
        old_url, old_pathname, new_name = data.get('old_url'), data.get('old_pathname'), data.get('new_name', '').strip()
        if not old_url or not old_pathname or not new_name:
            return jsonify({'error': 'Data tidak lengkap'}), 400
        safe_name = re.sub(r'[^\w\s\-\.]', '', new_name)
        if not safe_name.endswith('.webm'): safe_name += '.webm'
        if safe_name == old_pathname: return jsonify({'success': True})
        resp = req_lib.get(old_url)
        if resp.status_code != 200: return jsonify({'error': 'Gagal download'}), 500
        result = put(safe_name, resp.content, access='public', multipart=True)
        vercel_blob.delete(old_url)
        return jsonify({'success': True, 'new_url': result.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/submit-to-ai', methods=['POST'])
def submit_to_ai():
    try:
        data = request.json
        session_id = data.get('session_id', '')
        chunks = data.get('chunks', [])
        is_single = data.get('is_single', False)
        single_url = data.get('single_url', '')

        if is_single:
            ai_filename = f"ai_edit_single_{int(time.time())}.webm"
            file_data = req_lib.get(single_url).content
            result = put(ai_filename, file_data, access='public', multipart=True)
        else:
            ai_filename = f"ai_edit_{session_id}.webm"
            if chunks:
                file_data = req_lib.get(chunks[0]['url']).content
                result = put(ai_filename, file_data, access='public', multipart=True)
            else:
                return jsonify({'error': 'Tidak ada chunk'}), 400

        return jsonify({'success': True, 'ai_result': {
            'pathname': result.pathname, 'url': result.url, 'session_id': session_id}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    init_tables()
    return render_template('index.html')


if __name__ == '__main__':
    app.run(debug=True)