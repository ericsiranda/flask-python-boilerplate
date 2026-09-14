import os
import json
import time
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob

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
        ai_edits = []  # File hasil edit AI
        
        for item in files.get('blobs', []):
            pathname = item.get('pathname', '')
            
            # File hasil AI: ai_edit_<session>.webm
            if pathname.startswith('ai_edit_'):
                ai_edits.append({
                    'pathname': pathname,
                    'url': item.get('url'),
                    'size': item.get('size', 0),
                    'session_id': pathname.replace('ai_edit_', '').replace('.webm', ''),
                })
                continue
            
            # File potongan: cut_<session>_<index>.webm
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
                })
        
        # Urutkan chunks
        for sid in sessions:
            sessions[sid]['chunks'].sort(key=lambda x: x['index'])
        
        # Cek apakah session sudah punya hasil AI
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

# ==================== SUBMIT TO AI (SIMULASI) ====================
@app.route('/api/submit-to-ai', methods=['POST'])
def submit_to_ai():
    """
    Menerima video untuk diproses AI.
    
    DI SINI NANTI: Integrasikan dengan API AI sesungguhnya (OpenAI, Replicate, dll).
    
    Untuk sementara, kita SIMULASIKAN:
    1. Gabungkan chunks menjadi 1 video utuh (placeholder URL).
    2. Buat file 'ai_edit_<session>.webm' sebagai hasil.
    """
    try:
        data = request.json
        session_id = data.get('session_id', '')
        chunks = data.get('chunks', [])
        prompt = data.get('prompt', '')
        is_single = data.get('is_single', False)
        single_url = data.get('single_url', '')
        single_pathname = data.get('pathname', '')
        
        print(f"[AI] Memproses sesi: {session_id}")
        print(f"[AI] Prompt: {prompt}")
        print(f"[AI] Chunks: {len(chunks) if not is_single else 1}")
        
        # ============================================================
        # SIMULASI AI: Untuk sekarang, kita hanya BUKTI bahwa alur
        # sudah bekerja. Hasil AI = file tiruan dari chunk pertama.
        #
        # DI MASA DEPAN, di sini akan:
        # 1. Download semua chunks.
        # 2. Gabungkan jadi 1 video (ffmpeg di cloud function).
        # 3. Kirim ke AI (OpenAI/Replicate) dengan prompt.
        # 4. Simpan hasil sebagai file baru.
        # ============================================================
        
        # Simulasi: Buat file hasil AI dengan nama khusus
        # Kita ambil chunk pertama sebagai "placeholder hasil AI"
        if is_single:
            # File tunggal
            ai_filename = f"ai_edit_single_{int(time.time())}.webm"
            # Download dari URL single dan upload ulang dengan nama baru
            import requests as req_lib
            file_data = req_lib.get(single_url).content
            result = put(ai_filename, file_data, access='public', multipart=True)
            new_session_id = ai_filename.replace('ai_edit_single_', '').replace('.webm', '')
        else:
            # File potongan
            ai_filename = f"ai_edit_{session_id}.webm"
            # Ambil chunk pertama sebagai placeholder
            if chunks:
                import requests as req_lib
                file_data = req_lib.get(chunks[0]['url']).content
                result = put(ai_filename, file_data, access='public', multipart=True)
            else:
                return jsonify({'error': 'Tidak ada chunk'}), 400
            new_session_id = session_id
        
        return jsonify({
            'success': True,
            'message': 'Video berhasil dikirim ke AI (simulasi). Hasil edit telah dibuat.',
            'ai_result': {
                'pathname': result.pathname,
                'url': result.url,
                'session_id': new_session_id,
            }
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)