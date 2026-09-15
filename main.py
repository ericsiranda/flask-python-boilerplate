import os
import time
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob
import requests

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

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

# ==================== UPLOAD CHUNK ====================
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

# ==================== FINALIZE CUT ====================
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

# ==================== LIST FILES GROUPED ====================
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

# ==================== DELETE FILE ====================
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

# ==================== RENAME FILE ====================
@app.route('/api/rename-file', methods=['POST'])
def rename_file():
    """
    Rename file di Vercel Blob.
    Karena Vercel Blob tidak support rename langsung,
    kita download file lama, upload dengan nama baru, lalu hapus yang lama.
    """
    try:
        data = request.json
        old_url = data.get('old_url')
        old_pathname = data.get('old_pathname')
        new_name = data.get('new_name', '').strip()
        
        if not old_url or not old_pathname or not new_name:
            return jsonify({'error': 'Data tidak lengkap'}), 400
        
        # Bersihkan nama baru (hilangkan karakter berbahaya)
        import re
        safe_name = re.sub(r'[^\w\s\-\.]', '', new_name)
        
        # Pastikan ekstensi .webm
        if not safe_name.endswith('.webm'):
            safe_name = safe_name + '.webm'
        
        # Sama dengan nama lama? Skip
        if safe_name == old_pathname:
            return jsonify({'success': True, 'message': 'Nama sama, tidak berubah'})
        
        # Download file lama
        file_response = requests.get(old_url)
        if file_response.status_code != 200:
            return jsonify({'error': f'Gagal download file lama: {file_response.status_code}'}), 500
        
        file_content = file_response.content
        
        # Upload dengan nama baru
        result = put(
            safe_name,
            file_content,
            access='public',
            multipart=True
        )
        
        # Hapus file lama
        vercel_blob.delete(old_url)
        
        return jsonify({
            'success': True,
            'new_url': result.url,
            'new_pathname': result.pathname,
            'new_filename': safe_name,
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ==================== SUBMIT TO AI (SIMULASI) ====================
@app.route('/api/submit-to-ai', methods=['POST'])
def submit_to_ai():
    try:
        data = request.json
        session_id = data.get('session_id', '')
        chunks = data.get('chunks', [])
        prompt = data.get('prompt', '')
        is_single = data.get('is_single', False)
        single_url = data.get('single_url', '')
        
        if is_single:
            ai_filename = f"ai_edit_single_{int(time.time())}.webm"
            file_data = requests.get(single_url).content
            result = put(ai_filename, file_data, access='public', multipart=True)
        else:
            ai_filename = f"ai_edit_{session_id}.webm"
            if chunks:
                file_data = requests.get(chunks[0]['url']).content
                result = put(ai_filename, file_data, access='public', multipart=True)
            else:
                return jsonify({'error': 'Tidak ada chunk'}), 400
        
        return jsonify({
            'success': True,
            'message': 'Video berhasil diproses AI (simulasi).',
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

# ==================== RESET ALL AI ====================
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