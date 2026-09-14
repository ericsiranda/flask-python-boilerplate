import os
import time
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob

app = Flask(__name__)

# Penyimpanan sementara untuk sesi potong (in-memory)
# Catatan: Vercel Functions bersifat stateless, ini hanya untuk demo
CUT_SESSIONS = {}

@app.route('/')
def index():
    return render_template('index.html')

# ==================== UPLOAD (KOMPRES) ====================
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

# ==================== UPLOAD CHUNK (POTONG) ====================
@app.route('/api/upload-chunk', methods=['POST'])
def upload_chunk():
    """
    Menerima setiap potongan video, lalu upload ke Vercel Blob.
    Setiap potongan menjadi file terpisah (chunk_001.webm, chunk_002.webm, dst.)
    """
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'Tidak ada file video'}), 400
        
        file = request.files['video']
        session_id = request.form.get('session_id', 'default')
        chunk_index = int(request.form.get('chunk_index', 0))
        
        file_content = file.read()
        
        # Nama file chunk: cut_<session>_<index>.webm
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

# ==================== FINALIZE CUT (OPSIONAL) ====================
@app.route('/api/finalize-cut', methods=['POST'])
def finalize_cut():
    """
    Dipanggil setelah semua chunk selesai di-upload.
    Bisa digunakan untuk mencatat metadata sesi.
    """
    try:
        data = request.json
        session_id = data.get('session_id')
        total_chunks = data.get('total_chunks')
        original_name = data.get('original_name', 'video.mp4')
        
        return jsonify({
            'success': True,
            'message': f'Sesi {session_id} selesai dengan {total_chunks} potongan.',
            'session_id': session_id,
            'original_name': original_name
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== LIST FILES ====================
@app.route('/api/list-files', methods=['GET'])
def list_files():
    try:
        files = vercel_blob.list()
        
        file_list = []
        for item in files.get('blobs', []):
            file_list.append({
                'pathname': item.get('pathname'),
                'url': item.get('url'),
                'size': item.get('size', 0),
                'uploadedAt': item.get('uploadedAt'),
            })
        
        return jsonify({
            'success': True,
            'files': file_list
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

if __name__ == '__main__':
    app.run(debug=True)