import os
import time
import re
import json
import bcrypt
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob
import requests as req_lib

app = Flask(__name__)


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
                videos_json TEXT DEFAULT '[]',
                images_json TEXT DEFAULT '[]',
                time VARCHAR(50),
                platform VARCHAR(50),
                account VARCHAR(100),
                caption TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        try:
            cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS videos_json TEXT DEFAULT '[]'")
            cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS images_json TEXT DEFAULT '[]'")
        except Exception as e:
            print(f"Migration note: {e}")
            conn.rollback()

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


# ==================== SANITIZE FILENAME HELPER ====================
def sanitize_filename(original_name, default_ext='mp4'):
    if not original_name:
        return f"file_{int(time.time())}.{default_ext}"

    if '.' in original_name:
        base, ext = original_name.rsplit('.', 1)
    else:
        base, ext = original_name, default_ext

    safe_base = re.sub(r'[^\w\-]', '_', base)
    safe_base = re.sub(r'_+', '_', safe_base)
    safe_base = safe_base.strip('_')
    safe_base = safe_base[:50] if safe_base else 'file'
    safe_ext = re.sub(r'[^\w]', '', ext)[:10] or default_ext

    return f"{safe_base}_{int(time.time())}.{safe_ext}"


# ==================== AGNES AI VIDEO ====================
@app.route('/api/edit-video', methods=['POST'])
def edit_video_agnes():
    data = request.json
    prompt = data.get('prompt')
    image_url = data.get('image_url')
    video_url = data.get('video_url')

    if not prompt:
        return jsonify({'error': 'Prompt wajib disediakan'}), 400

    agnes_api_key = os.environ.get('AGNES_API_KEY')
    if not agnes_api_key:
        return jsonify({'error': 'AGNES_API_KEY belum di-set'}), 500

    try:
        source_url = video_url or image_url
        if not source_url:
            return jsonify({'error': 'image_url atau video_url wajib diisi'}), 400

        is_video_input = source_url.lower().endswith(('.mp4', '.webm', '.mov', '.avi', '.mkv'))
        print(f"🎬 Source: {source_url[:80]}...")
        print(f"📦 Tipe input: {'video' if is_video_input else 'gambar'}")

        submit_url = "https://apihub.agnes-ai.com/v1/videos"

        if is_video_input:
            submit_payload = {
                "model": "agnes-video-v2.0",
                "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
                "videos": [{"url": source_url, "role": "reference"}]
            }
        else:
            submit_payload = {
                "model": "agnes-video-v2.0",
                "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
                "image": source_url
            }

        submit_headers = {
            "Authorization": f"Bearer {agnes_api_key}",
            "Content-Type": "application/json"
        }

        submit_res = req_lib.post(submit_url, headers=submit_headers, json=submit_payload, timeout=60)
        submit_data = submit_res.json()

        video_id = submit_data.get('video_id') or submit_data.get('id') or submit_data.get('task_id')
        if (not submit_res.ok or not video_id) and is_video_input:
            print("⚠️ Fallback ke mode image...")
            fallback = {
                "model": "agnes-video-v2.0", "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
                "image": source_url
            }
            submit_res = req_lib.post(submit_url, headers=submit_headers, json=fallback, timeout=60)
            submit_data = submit_res.json()
            video_id = submit_data.get('video_id') or submit_data.get('id') or submit_data.get('task_id')

        if not video_id:
            return jsonify({'error': f'Gagal submit ke Agnes AI: {submit_data}'}), 500

        result_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}"
        result_headers = {"Authorization": f"Bearer {agnes_api_key}"}

        result_data = None
        for i in range(60):
            time.sleep(10)
            try:
                r = req_lib.get(result_url, headers=result_headers, timeout=30)
                result_data = r.json()
                status = result_data.get('status') or result_data.get('data', {}).get('status')
                print(f"Polling {i+1}/60: {status}")
                if status == 'completed':
                    break
                elif status == 'failed':
                    return jsonify({'error': f"Agnes gagal: {result_data}"}), 500
            except Exception as e:
                print(f"⚠️ Poll err: {e}")

        final_status = result_data.get('status') if result_data else None
        if final_status != 'completed':
            return jsonify({'error': f'Timeout. Status: {final_status}'}), 500

        output_url = (result_data.get('video_url') or
                      result_data.get('url') or
                      result_data.get('data', {}).get('video_url') or
                      result_data.get('data', {}).get('url'))

        if not output_url:
            return jsonify({'error': f'URL video tidak ditemukan'}), 500

        video_dl = req_lib.get(output_url, timeout=120)
        if video_dl.status_code != 200:
            return jsonify({'error': 'Gagal download'}), 500

        filename = f"ai_edit_{int(time.time())}.mp4"
        blob_result = put(filename, video_dl.content, access='public', multipart=True)

        return jsonify({
            'success': True,
            'video_url': blob_result.url,
            'pathname': blob_result.pathname,
            'video_id': video_id
        })

    except Exception as e:
        print(f"❌ Agnes Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== AGNES AI IMAGE-TO-VIDEO ====================
@app.route('/api/image-to-video', methods=['POST'])
def image_to_video_agnes():
    data = request.json
    prompt = data.get('prompt')
    image_url = data.get('image_url')

    if not prompt or not image_url:
        return jsonify({'error': 'Prompt & image_url wajib'}), 400

    agnes_api_key = os.environ.get('AGNES_API_KEY')
    if not agnes_api_key:
        return jsonify({'error': 'AGNES_API_KEY belum di-set'}), 500

    try:
        submit_url = "https://apihub.agnes-ai.com/v1/videos"
        payload = {
            "model": "agnes-video-v2.0",
            "prompt": prompt,
            "height": 768, "width": 1152,
            "num_frames": 121, "frame_rate": 24,
            "image": image_url
        }
        headers = {
            "Authorization": f"Bearer {agnes_api_key}",
            "Content-Type": "application/json"
        }

        res = req_lib.post(submit_url, headers=headers, json=payload, timeout=60)
        res_data = res.json()
        video_id = res_data.get('video_id') or res_data.get('id') or res_data.get('task_id')
        if not video_id:
            return jsonify({'error': f'Gagal submit: {res_data}'}), 500

        result_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}"
        result_data = None
        for i in range(60):
            time.sleep(10)
            try:
                r = req_lib.get(result_url, headers={"Authorization": f"Bearer {agnes_api_key}"}, timeout=30)
                result_data = r.json()
                status = result_data.get('status') or result_data.get('data', {}).get('status')
                if status == 'completed':
                    break
                elif status == 'failed':
                    return jsonify({'error': f"Agnes gagal: {result_data}"}), 500
            except Exception:
                pass

        final_status = result_data.get('status') if result_data else None
        if final_status != 'completed':
            return jsonify({'error': f'Timeout. Status: {final_status}'}), 500

        output_url = (result_data.get('video_url') or
                      result_data.get('url') or
                      result_data.get('data', {}).get('video_url') or
                      result_data.get('data', {}).get('url'))
        if not output_url:
            return jsonify({'error': 'URL video tidak ditemukan'}), 500

        video_dl = req_lib.get(output_url, timeout=120)
        filename = f"ai_edit_{int(time.time())}.mp4"
        blob_result = put(filename, video_dl.content, access='public', multipart=True)

        return jsonify({
            'success': True,
            'video_url': blob_result.url,
            'pathname': blob_result.pathname,
            'video_id': video_id
        })

    except Exception as e:
        print(f"❌ Agnes I2V Error: {e}")
        return jsonify({'error': str(e)}), 500


# ==================== AGNES AI IMAGE EDITING ====================
@app.route('/api/edit-image', methods=['POST'])
def edit_image_agnes():
    data = request.json
    prompt = data.get('prompt')
    image_url = data.get('image_url')

    if not prompt or not image_url:
        return jsonify({'error': 'Prompt & image_url wajib'}), 400

    agnes_api_key = os.environ.get('AGNES_API_KEY')
    if not agnes_api_key:
        return jsonify({'error': 'AGNES_API_KEY belum di-set'}), 500

    try:
        url = "https://apihub.agnes-ai.com/v1/images/generations"
        payload = {
            "model": "agnes-image-2.1-flash",
            "prompt": prompt,
            "size": "1024x1024",
            "extra_body": {
                "image": [image_url],
                "response_format": "url"
            }
        }
        headers = {
            "Authorization": f"Bearer {agnes_api_key}",
            "Content-Type": "application/json"
        }

        res = req_lib.post(url, headers=headers, json=payload, timeout=120)
        res_data = res.json()
        if not res.ok:
            return jsonify({'error': f'Agnes error: {res_data}'}), 500

        image_result_url = None
        if res_data.get('data') and len(res_data['data']) > 0:
            image_result_url = res_data['data'][0].get('url')
        if not image_result_url:
            return jsonify({'error': f'URL gambar tidak ditemukan'}), 500

        img_dl = req_lib.get(image_result_url, timeout=60)
        filename = f"ai_edit_image_{int(time.time())}.png"
        blob_result = put(filename, img_dl.content, access='public', multipart=True)

        return jsonify({
            'success': True,
            'image_url': blob_result.url,
            'pathname': blob_result.pathname
        })

    except Exception as e:
        print(f"❌ Agnes Image Error: {e}")
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
            cur.close(); conn.close()
            return jsonify({'error': 'Username sudah digunakan'}), 400

        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (username, password_hash))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'success': True, 'message': 'User berhasil ditambahkan'})
    except Exception as e:
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
        cur.close(); conn.close()

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
        cur.close(); conn.close()
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
        cur.close(); conn.close()
        schedules = []
        for r in rows:
            try:
                videos = json.loads(r.get('videos_json') or '[]')
            except:
                videos = []
            try:
                images = json.loads(r.get('images_json') or '[]')
            except:
                images = []
            schedules.append({
                'id': r['id'],
                'session_id': r['session_id'] or '',
                'video_name': r['video_name'] or '',
                'video_url': r['video_url'] or '',
                'videos': videos,
                'images': images,
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

        videos = data.get('videos', [])
        images = data.get('images', [])

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO schedules 
            (session_id, video_name, video_url, videos_json, images_json, time, platform, account, caption)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            data.get('session_id', ''),
            videos[0]['name'] if videos else '',
            videos[0]['url'] if videos else '',
            json.dumps(videos),
            json.dumps(images),
            data.get('time', ''),
            data.get('platform', ''),
            data.get('account', ''),
            data.get('caption', ''),
        ))
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close(); conn.close()
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
        cur.close(); conn.close()
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
        cur.close(); conn.close()
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
        cur.execute("SELECT id FROM medsos_accounts WHERE platform = %s AND LOWER(username) = LOWER(%s)",
                    (platform, username))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({'error': 'Akun sudah terhubung'}), 400

        cur.execute("INSERT INTO medsos_accounts (platform, username) VALUES (%s, %s)", (platform, username))
        conn.commit()
        cur.close(); conn.close()
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
        cur.execute("DELETE FROM medsos_accounts WHERE platform = %s AND LOWER(username) = LOWER(%s)",
                    (platform, username))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== BLOB UPLOAD ====================
@app.route('/api/upload-media', methods=['POST'])
def upload_media():
    try:
        file = request.files.get('file')
        if not file or file.filename == '':
            return jsonify({'error': 'Tidak ada file'}), 400

        ext = file.filename.lower().rsplit('.', 1)[-1] if '.' in file.filename else ''
        VIDEO_EXT = ['mp4', 'webm', 'mov', 'avi', 'mkv']
        IMAGE_EXT = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']

        if ext in VIDEO_EXT:
            media_type = 'video'
            default_ext = 'mp4'
        elif ext in IMAGE_EXT:
            media_type = 'image'
            default_ext = 'jpg'
        else:
            return jsonify({'error': f'Tipe file tidak didukung: .{ext}'}), 400

        safe_name = sanitize_filename(file.filename, default_ext)
        file_content = file.read()
        result = put(safe_name, file_content, access='public', multipart=True)

        return jsonify({
            'success': True, 'url': result.url, 'pathname': result.pathname,
            'filename': safe_name, 'size': len(file_content), 'type': media_type
        })
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

        safe_session_id = re.sub(r'[^\w]', '_', str(session_id))[:30] or 'session'
        file_content = file.read()
        chunk_filename = f"cut_{safe_session_id}_{chunk_index:03d}.webm"
        result = put(chunk_filename, file_content, access='public', multipart=True)
        return jsonify({'success': True, 'url': result.url, 'pathname': result.pathname,
                       'chunk_index': chunk_index, 'size': len(file_content)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/finalize-cut', methods=['POST'])
def finalize_cut():
    return jsonify({'success': True})


# ==================== LIST FILES ====================
@app.route('/api/list-files-grouped', methods=['GET'])
def list_files_grouped():
    try:
        files = vercel_blob.list()
        sessions = {}
        videos_raw = []
        images_raw = []
        video_ai_edits = {}
        image_ai_edits = []

        VIDEO_EXT = ['mp4', 'webm', 'mov', 'avi', 'mkv']
        IMAGE_EXT = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']

        for item in files.get('blobs', []):
            pathname = item.get('pathname', '')
            url = item.get('url', '')
            size = item.get('size', 0)
            uploaded_at = item.get('uploadedAt', '')

            ext = pathname.lower().rsplit('.', 1)[-1] if '.' in pathname else ''
            is_video = ext in VIDEO_EXT
            is_image = ext in IMAGE_EXT

            if pathname.startswith('ai_edit_image_'):
                sid = pathname.replace('ai_edit_image_', '').rsplit('.', 1)[0]
                image_ai_edits.append({
                    'pathname': pathname, 'url': url, 'size': size,
                    'uploadedAt': uploaded_at, 'session_id': sid
                })
                continue

            if pathname.startswith('ai_edit_') and is_video:
                sid = pathname.replace('ai_edit_', '').rsplit('.', 1)[0]
                video_ai_edits[sid] = {
                    'pathname': pathname, 'url': url, 'size': size,
                    'uploadedAt': uploaded_at
                }
                continue

            if pathname.startswith('cut_') and is_video:
                parts = pathname.rsplit('.', 1)[0].split('_')
                if len(parts) >= 3:
                    sid = parts[1]
                    try:
                        idx = int(parts[2])
                    except:
                        continue
                    if sid not in sessions:
                        sessions[sid] = {'session_id': sid, 'chunks': [], 'total_size': 0, 'total_chunks': 0}
                    sessions[sid]['chunks'].append({'pathname': pathname, 'url': url, 'size': size, 'index': idx})
                    sessions[sid]['total_size'] += size
                    sessions[sid]['total_chunks'] += 1
                continue

            if is_video:
                videos_raw.append({
                    'pathname': pathname, 'url': url, 'size': size,
                    'is_single': True, 'file_type': 'video',
                    'uploadedAt': uploaded_at
                })
            elif is_image:
                images_raw.append({
                    'pathname': pathname, 'url': url, 'size': size,
                    'is_single': True, 'file_type': 'image',
                    'uploadedAt': uploaded_at
                })

        for sid in sessions:
            sessions[sid]['chunks'].sort(key=lambda x: x['index'])

        videos_final = []
        for sid, s in sessions.items():
            has_ai = sid in video_ai_edits
            videos_final.append({
                'pathname': f"video_utuh_{sid}.webm",
                'url': s['chunks'][0]['url'],
                'size': s['total_size'],
                'is_single': False,
                'session_id': sid,
                'chunks': s['chunks'],
                'total_chunks': s['total_chunks'],
                'file_type': 'video',
                'ai_status': 'done' if has_ai else 'idle',
                'ai_result': video_ai_edits.get(sid),
                'uploadedAt': s['chunks'][0].get('uploadedAt', '')
            })

        videos_final.extend(videos_raw)

        videos_ai = []
        for sid, ai in video_ai_edits.items():
            videos_ai.append({
                'pathname': ai['pathname'],
                'url': ai['url'],
                'size': ai['size'],
                'is_single': True,
                'session_id': sid,
                'file_type': 'video',
                'is_ai': True,
                'uploadedAt': ai['uploadedAt']
            })

        images_ai = []
        for ai in image_ai_edits:
            images_ai.append({
                'pathname': ai['pathname'],
                'url': ai['url'],
                'size': ai['size'],
                'is_single': True,
                'session_id': ai['session_id'],
                'file_type': 'image',
                'is_ai': True,
                'uploadedAt': ai['uploadedAt']
            })

        return jsonify({
            'success': True,
            'videos_raw': videos_final,
            'videos_ai': videos_ai,
            'images_raw': images_raw,
            'images_ai': images_ai
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete-file', methods=['POST'])
def delete_file():
    try:
        url = request.json.get('url')
        if not url:
            return jsonify({'error': 'URL tidak diberikan'}), 400
        vercel_blob.delete(url)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rename-file', methods=['POST'])
def rename_file():
    try:
        data = request.json
        old_url = data.get('old_url')
        old_pathname = data.get('old_pathname')
        new_name = data.get('new_name', '').strip()
        if not old_url or not old_pathname or not new_name:
            return jsonify({'error': 'Data tidak lengkap'}), 400

        orig_ext = old_pathname.rsplit('.', 1)[-1] if '.' in old_pathname else 'mp4'
        safe_name = sanitize_filename(new_name, orig_ext)
        if not safe_name.endswith(f'.{orig_ext}'):
            safe_name = f"{safe_name}.{orig_ext}"

        if safe_name == old_pathname:
            return jsonify({'success': True})

        resp = req_lib.get(old_url)
        if resp.status_code != 200:
            return jsonify({'error': 'Gagal download'}), 500
        result = put(safe_name, resp.content, access='public', multipart=True)
        vercel_blob.delete(old_url)
        return jsonify({'success': True, 'new_url': result.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    init_tables()
    return render_template('index.html')


if __name__ == '__main__':
    app.run(debug=True)