import os
import time
import json
import bcrypt
import redis
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob
import requests as req_lib

app = Flask(__name__)

# ==================== REDIS (VERCEL KV) ====================
def get_redis():
    """Koneksi ke Vercel KV via Redis URL."""
    # Vercel KV menyediakan beberapa env var, coba semua
    redis_url = (
        os.environ.get('KV_URL') or 
        os.environ.get('REDIS_URL') or
        os.environ.get('KV_REST_API_URL')
    )
    
    if not redis_url:
        print("⚠️ Tidak ada KV_URL / REDIS_URL di environment")
        return None
    
    try:
        # Decode responses=True agar string otomatis (tidak bytes)
        return redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
    except Exception as e:
        print(f"⚠️ Gagal connect Redis: {e}")
        return None


@app.route('/')
def index():
    return render_template('index.html')

# ==================== USER MANAGEMENT ====================

@app.route('/api/users/has-any', methods=['GET'])
def has_any_user():
    """Cek apakah ada user terdaftar (untuk login gate)."""
    try:
        r = get_redis()
        if not r:
            # Fallback: anggap tidak ada user jika DB tidak tersedia
            return jsonify({'success': True, 'has_users': False, 'count': 0, 'db_available': False})
        
        count = r.scard('users')
        return jsonify({
            'success': True, 
            'has_users': count > 0, 
            'count': count,
            'db_available': True
        })
    
    except Exception as e:
        print(f"Error has_any_user: {e}")
        return jsonify({'success': True, 'has_users': False, 'count': 0, 'db_available': False})


@app.route('/api/users/list', methods=['GET'])
def list_users():
    """Ambil daftar user (tanpa password)."""
    try:
        r = get_redis()
        if not r:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        
        user_ids = r.smembers('users')
        users = []
        
        for uid in user_ids:
            user_data = r.hgetall(f'user:{uid}')
            if user_data and user_data.get('username'):
                users.append({
                    'username': user_data.get('username'),
                    'createdAt': user_data.get('createdAt', ''),
                })
        
        users.sort(key=lambda u: u.get('createdAt', ''))
        
        return jsonify({'success': True, 'users': users})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/add', methods=['POST'])
def add_user():
    """Tambah user baru."""
    try:
        data = request.json
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        
        # Validasi
        if not username or len(username) < 3:
            return jsonify({'error': 'Username minimal 3 karakter'}), 400
        
        if not password or len(password) < 4:
            return jsonify({'error': 'Password minimal 4 karakter'}), 400
        
        r = get_redis()
        if not r:
            return jsonify({'error': 'Database tidak tersedia. Hubungi admin.'}), 500
        
        username_lower = username.lower()
        
        # Cek duplikat
        if r.sismember('usernames', username_lower):
            return jsonify({'error': 'Username sudah digunakan'}), 400
        
        # Hash password dengan bcrypt
        password_hash = bcrypt.hashpw(
            password.encode('utf-8'), 
            bcrypt.gensalt()
        ).decode('utf-8')
        
        # Simpan ke Redis
        r.hset(f'user:{username_lower}', mapping={
            'username': username,
            'password_hash': password_hash,
            'createdAt': datetime.utcnow().isoformat(),
        })
        
        r.sadd('users', username_lower)
        r.sadd('usernames', username_lower)
        
        return jsonify({'success': True, 'message': 'User berhasil ditambahkan'})
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/login', methods=['POST'])
def login_user():
    """Verifikasi login user."""
    try:
        data = request.json
        username = (data.get('username') or '').strip().lower()
        password = data.get('password') or ''
        
        if not username or not password:
            return jsonify({'error': 'Username & password wajib diisi'}), 400
        
        r = get_redis()
        if not r:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        
        user_data = r.hgetall(f'user:{username}')
        
        if not user_data:
            return jsonify({'error': 'Username atau password salah'}), 401
        
        password_hash = user_data.get('password_hash', '')
        
        if not password_hash:
            return jsonify({'error': 'Data user rusak'}), 500
        
        # Verifikasi bcrypt
        try:
            valid = bcrypt.checkpw(
                password.encode('utf-8'), 
                password_hash.encode('utf-8')
            )
        except Exception as e:
            print(f"Bcrypt error: {e}")
            return jsonify({'error': 'Verifikasi gagal'}), 500
        
        if not valid:
            return jsonify({'error': 'Username atau password salah'}), 401
        
        return jsonify({
            'success': True,
            'user': {
                'username': user_data.get('username'),
                'createdAt': user_data.get('createdAt', ''),
            }
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/delete', methods=['POST'])
def delete_user():
    """Hapus user."""
    try:
        data = request.json
        username = (data.get('username') or '').strip().lower()
        
        if not username:
            return jsonify({'error': 'Username wajib diisi'}), 400
        
        r = get_redis()
        if not r:
            return jsonify({'error': 'Database tidak tersedia'}), 500
        
        if not r.sismember('usernames', username):
            return jsonify({'error': 'User tidak ditemukan'}), 404
        
        r.delete(f'user:{username}')
        r.srem('users', username)
        r.srem('usernames', username)
        
        return jsonify({'success': True, 'message': 'User berhasil dihapus'})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== UPLOAD ====================
@app.route('/api/upload', methods=['POST'])
def upload_video():
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'Tidak ada file video'}), 400
        
        file = request.files['video']
        if file.filename == '':
            return jsonify({'error': 'Nama file kosong'}), 400
        
        file_content = file.read()
        
        result = put(
            file.filename,
            file_content,
            access='public',
            multipart=True
        )
        
        return jsonify({
            'success': True,
            'url': result.url,
            'pathname': result.pathname,
            'filename': file.filename,
            'size': len(file_content)
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
        
        file_content = file.read()
        chunk_filename = f"cut_{session_id}_{chunk_index:03d}.webm"
        
        result = put(
            chunk_filename,
            file_content,
            access='public',
            multipart=True
        )
        
        return jsonify({
            'success': True,
            'url': result.url,
            'pathname': result.pathname,
            'chunk_index': chunk_index,
            'size': len(file_content)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/finalize-cut', methods=['POST'])
def finalize_cut():
    try:
        data = request.json
        return jsonify({
            'success': True,
            'session_id': data.get('session_id'),
            'total_chunks': data.get('total_chunks')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/list-files-grouped', methods=['GET'])
def list_files_grouped():
    try:
        files = vercel_blob.list()
        
        sessions = {}
        singles = []
        ai_edits = []
        
        for item in files.get('blobs', []):
            pathname = item.get('pathname', '')
            
            if pathname.startswith('ai_edit_'):
                ai_edits.append({
                    'pathname': pathname,
                    'url': item.get('url'),
                    'size': item.get('size', 0),
                    'session_id': pathname.replace('ai_edit_', '').replace('.webm', ''),
                    'uploadedAt': item.get('uploadedAt'),
                })
                continue
            
            if pathname.startswith('cut_') and pathname.endswith('.webm'):
                parts = pathname.replace('.webm', '').split('_')
                if len(parts) >= 3:
                    session_id = parts[1]
                    try:
                        chunk_index = int(parts[2])
                    except ValueError:
                        continue
                    
                    if session_id not in sessions:
                        sessions[session_id] = {
                            'session_id': session_id,
                            'chunks': [],
                            'total_size': 0,
                            'total_chunks': 0,
                        }
                    
                    sessions[session_id]['chunks'].append({
                        'pathname': pathname,
                        'url': item.get('url'),
                        'size': item.get('size', 0),
                        'index': chunk_index,
                    })
                    sessions[session_id]['total_size'] += item.get('size', 0)
                    sessions[session_id]['total_chunks'] += 1
            else:
                singles.append({
                    'pathname': pathname,
                    'url': item.get('url'),
                    'size': item.get('size', 0),
                    'is_single': True,
                    'uploadedAt': item.get('uploadedAt'),
                })
        
        for sid in sessions:
            sessions[sid]['chunks'].sort(key=lambda x: x['index'])
        
        ai_edit_map = {edit['session_id']: edit for edit in ai_edits}
        
        result = []
        for sid, session in sessions.items():
            has_ai_edit = sid in ai_edit_map
            result.append({
                'pathname': f"video_utuh_{sid}.webm",
                'url': session['chunks'][0]['url'],
                'size': session['total_size'],
                'is_single': False,
                'session_id': sid,
                'chunks': session['chunks'],
                'total_chunks': session['total_chunks'],
                'ai_status': 'done' if has_ai_edit else 'idle',
                'ai_result': ai_edit_map.get(sid),
            })
        
        result.extend(singles)
        
        return jsonify({'success': True, 'files': result})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete-file', methods=['POST'])
def delete_file():
    try:
        data = request.json
        url = data.get('url')
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
        new_name = (data.get('new_name') or '').strip()
        
        if not old_url or not old_pathname or not new_name:
            return jsonify({'error': 'Data tidak lengkap'}), 400
        
        import re
        safe_name = re.sub(r'[^\w\s\-\.]', '', new_name)
        
        if not safe_name.endswith('.webm'):
            safe_name = safe_name + '.webm'
        
        if safe_name == old_pathname:
            return jsonify({'success': True, 'message': 'Nama sama, tidak berubah'})
        
        file_response = req_lib.get(old_url)
        if file_response.status_code != 200:
            return jsonify({'error': f'Gagal download: {file_response.status_code}'}), 500
        
        result = put(
            safe_name,
            file_response.content,
            access='public',
            multipart=True
        )
        
        vercel_blob.delete(old_url)
        
        return jsonify({
            'success': True,
            'new_url': result.url,
            'new_pathname': result.pathname,
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
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
        
        return jsonify({
            'success': True,
            'ai_result': {
                'pathname': result.pathname,
                'url': result.url,
                'session_id': session_id,
            }
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/reset-ai', methods=['POST'])
def reset_ai():
    try:
        files = vercel_blob.list()
        deleted = 0
        
        for item in files.get('blobs', []):
            pathname = item.get('pathname', '')
            if pathname.startswith('ai_edit_'):
                vercel_blob.delete(item.get('url'))
                deleted += 1
        
        return jsonify({'success': True, 'deleted': deleted})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)