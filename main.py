import os
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
        
        # Format: cut_<session>_<index>.webm
        # Format ini penting agar grouping backend bisa mengenali
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
        session_id = data.get('session_id')
        total_chunks = data.get('total_chunks')
        original_name = data.get('original_name', 'video.mp4')
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'original_name': original_name,
            'total_chunks': total_chunks
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== LIST FILES GROUPED ====================
@app.route('/api/list-files-grouped', methods=['GET'])
def list_files_grouped():
    """
    List file yang sudah dikelompokkan berdasarkan session ID.
    File dengan prefix 'cut_<session>_<index>.webm' akan digabung
    menjadi satu entri di tabel.
    """
    try:
        files = vercel_blob.list()
        
        sessions = {}
        singles = []
        
        for item in files.get('blobs', []):
            pathname = item.get('pathname', '')
            
            # Cek apakah ini file chunk: cut_<session>_<index>.webm
            if pathname.startswith('cut_') and pathname.endswith('.webm'):
                # Format: cut_mu1fcikd_000.webm
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
                # File utuh (bukan potongan)
                singles.append({
                    'pathname': pathname,
                    'url': item.get('url'),
                    'size': item.get('size', 0),
                    'is_single': True,
                })
        
        # Urutkan chunks di setiap session berdasarkan index
        for sid in sessions:
            sessions[sid]['chunks'].sort(key=lambda x: x['index'])
        
        # Buat list hasil akhir
        result = []
        
        # Tambahkan grouped sessions
        for sid, session in sessions.items():
            result.append({
                'pathname': f"video_utuh_{sid}.webm",
                'url': session['chunks'][0]['url'],
                'size': session['total_size'],
                'is_single': False,
                'session_id': sid,
                'chunks': session['chunks'],
                'total_chunks': session['total_chunks'],
            })
        
        # Tambahkan file tunggal
        result.extend(singles)
        
        return jsonify({
            'success': True,
            'files': result
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== DELETE FILE ====================
@app.route('/api/delete-file', methods=['POST'])
def delete_file():
    try:
        data = request.json
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL file tidak diberikan'}), 400
        
        vercel_blob.delete(url)
        
        return jsonify({'success': True, 'message': 'File berhasil dihapus'})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== SUBMIT TO AI (Placeholder) ====================
@app.route('/api/submit-to-ai', methods=['POST'])
def submit_to_ai():
    """
    Placeholder untuk proses AI.
    Menerima session_id + daftar chunks, akan diproses nanti.
    """
    try:
        data = request.json
        session_id = data.get('session_id')
        chunks = data.get('chunks', [])
        prompt = data.get('prompt', '')
        schedule = data.get('schedule', '')
        is_single = data.get('is_single', False)
        single_url = data.get('single_url', '')
        
        if is_single:
            # File tunggal (tidak dipotong)
            print(f"[AI] Memproses file tunggal: {single_url}")
            print(f"[AI] Prompt: {prompt}")
            print(f"[AI] Jadwal: {schedule}")
        else:
            # File potongan
            print(f"[AI] Memproses sesi: {session_id}")
            print(f"[AI] Jumlah potongan: {len(chunks)}")
            print(f"[AI] Prompt: {prompt}")
            print(f"[AI] Jadwal: {schedule}")
            print(f"[AI] Chunks: {[c['pathname'] for c in chunks]}")
        
        # Di sini nanti akan dipanggil AI API untuk edit video.
        # Hasilnya adalah 1 file utuh yang siap diupload ke Instagram.
        
        return jsonify({
            'success': True,
            'message': 'Data diterima. Proses AI akan diimplementasikan nanti.',
            'session_id': session_id,
            'total_chunks': len(chunks) if not is_single else 1,
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)