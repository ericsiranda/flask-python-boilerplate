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

app = Flask(__name__)

# ==================== DATABASE CONNECTION ====================
def build_candidate_urls():
    """
    Bangun daftar kandidat URL koneksi dengan sanitasi.
    Return: list of tuples (key_name, sanitized_url)
    """
    candidates = []
    
    # ---- 1. Ambil komponen ----
    password = None
    host = None
    user = None
    
    for key in os.environ.keys():
        if 'POSTGRES_PASSWORD' in key and not password:
            password = os.environ.get(key)
        if 'POSTGRES_HOST' in key and not host:
            host = os.environ.get(key)
        if 'POSTGRES_USER' in key and not user:
            user = os.environ.get(key)
    
    # ---- 2. BANGUN URL POOLER (paling mungkin berhasil di Vercel) ----
    # Format: postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
    if password and host:
        # Cari ref dari host: db.<ref>.supabase.co
        m = re.match(r'db\.([a-z0-9]+)\.supabase\.co', host)
        if m:
            ref = m.group(1)
            
            # Coba berbagai region pooler
            # Pooler host format: aws-0-<region>.pooler.supabase.com
            pooler_regions = [
                'us-east-1',       # dari error sebelumnya
                'ap-southeast-1',  # Singapore
                'ap-southeast-2',  # Sydney
                'us-west-1',
            ]
            
            for region in pooler_regions:
                pooler_host = f"aws-0-{region}.pooler.supabase.com"
                # PENTING: user = postgres.<ref>, port = 6543
                url = f"postgresql://postgres.{ref}:{password}@{pooler_host}:6543/postgres"
                candidates.append((f'pooler_{region}', url))
    
    # ---- 3. Ambil dari env var langsung (sanitize) ----
    priority_keys = [
        'DB_URL',
        'DATABASE_URL',
        'SUPABASE_POSTGRES_URL_NON_POOLING',
        'POSTGRES_URL_NON_POOLING',
        'SUPABASE_POSTGRES_URL',
        'POSTGRES_URL',
    ]
    
    for key in priority_keys:
        val = os.environ.get(key)
        if val and val.startswith('postgres'):
            sanitized = sanitize_db_url(val)
            candidates.append((key, sanitized))
    
    # ---- 4. Fallback: direct connection (IPv6, mungkin gagal) ----
    if password and host:
        m = re.match(r'db\.([a-z0-9]+)\.supabase\.co', host)
        if m:
            ref = m.group(1)
            url = f"postgresql://postgres:{password}@db.{ref}.supabase.co:5432/postgres?sslmode=require"
            candidates.append(('direct_ipv6', url))
    
    return candidates


def sanitize_db_url(url):
    """
    Bersihkan URL dari parameter yang tidak dikenal psycopg2.
    """
    # Ganti postgres:// → postgresql://
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    
    # Hapus parameter 'supa' dan 'pgbouncer' (tidak dikenal psycopg2)
    url = re.sub(r'[?&]supa=[^&]*', '', url)
    url = re.sub(r'[?&]pgbouncer=[^&]*', '', url)
    
    # Bersihkan ?& atau && yang tersisa
    url = url.replace('?&', '?').replace('&&', '&')
    
    # Hapus ? atau & di akhir
    url = re.sub(r'[?&]$', '', url)
    
    # Pastikan sslmode=require
    if 'sslmode=' not in url:
        separator = '&' if '?' in url else '?'
        url = f"{url}{separator}sslmode=require"
    
    return url


def get_db():
    """
    Koneksi ke Supabase Postgres.
    Coba semua kandidat URL sampai berhasil.
    """
    candidates = build_candidate_urls()
    
    if not candidates:
        print("⚠️ Tidak ada kandidat URL database")
        return None
    
    for key, url in candidates:
        try:
            print(f"DB: mencoba {key}...")
            print(f"DB: URL: {url[:80]}...")
            
            # Koneksi dengan timeout
            conn = psycopg2.connect(url, connect_timeout=10)
            
            # Test dengan query sederhana
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            
            print(f"DB: ✅ BERHASIL dengan {key}")
            return conn
            
        except psycopg2.OperationalError as e:
            error_msg = str(e)[:150].replace('\n', ' ')
            print(f"DB: ❌ {key} gagal: {error_msg}")
            continue
        except Exception as e:
            error_msg = str(e)[:150].replace('\n', ' ')
            print(f"DB: ❌ {key} error: {error_msg}")
            continue
    
    print("DB: ⚠️ Semua kandidat gagal")
    return None


def init_tables():
    """Buat tabel jika belum ada."""
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


@app.route('/')
def index():
    init_tables()
    return render_template('index.html')


# ==================== DEBUG ====================
@app.route('/api/debug-db', methods=['GET'])
def debug_db():
    """Debug: cek env var dan coba koneksi dengan error detail."""
    relevant_keys = []
    for key in sorted(os.environ.keys()):
        if 'POSTGRES' in key or 'DATABASE' in key or key == 'DB_URL':
            val = os.environ.get(key, '')
            relevant_keys.append({
                'key': key,
                'has_value': bool(val),
                'starts_with_postgres': val.startswith('postgres') if val else False,
            })
    
    # Ambil kandidat URL
    candidates = build_candidate_urls()
    sanitized_candidates = []
    for key, url in candidates:
        # Sensor password
        safe_url = re.sub(r':([^:@]+)@', ':***@', url)
        sanitized_candidates.append({
            'key': key,
            'url_preview': safe_url[:100]
        })
    
    # Coba setiap kandidat
    attempts = []
    for key, url in candidates:
        try:
            conn = psycopg2.connect(url, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.close()
            conn.close()
            
            attempts.append({
                'key': key,
                'status': 'success',
                'postgres_version': version[:100]
            })
            
            return jsonify({
                'env_vars': relevant_keys,
                'candidates': sanitized_candidates,
                'connection_attempts': attempts,
                'connection': {
                    'status': 'success',
                    'used_key': key,
                    'postgres_version': version[:100]
                }
            })
        except Exception as e:
            error_msg = str(e)[:200].replace('\n', ' ')
            attempts.append({
                'key': key,
                'status': 'failed',
                'error_type': type(e).__name__,
                'error_message': error_msg
            })
    
    return jsonify({
        'env_vars': relevant_keys,
        'candidates': sanitized_candidates,
        'connection_attempts': attempts,
        'connection': {
            'status': 'all_failed',
            'total_attempts': len(attempts)
        }
    })


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


if __name__ == '__main__':
    app.run(debug=True)