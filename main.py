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
        file_size_mb = len(file_content) / 1024 / 1024
        
        # Cek batas 4.5 MB (batas Vercel Function)
        if file_size_mb > 4.5:
            return jsonify({
                'error': f'File terlalu besar ({file_size_mb:.2f} MB). Maksimal 4.5 MB untuk upload via server. Untuk file lebih besar, kompres dulu atau gunakan layanan lain.'
            }), 413
        
        # PUBLIC STORE
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

# ==================== DEBUG ====================
@app.route('/api/debug', methods=['GET'])
def debug_info():
    token = os.environ.get('BLOB_READ_WRITE_TOKEN')
    store_id = os.environ.get('BLOB_STORE_ID')
    return jsonify({
        'token_available': token is not None,
        'store_id_available': store_id is not None,
    })

if __name__ == '__main__':
    app.run(debug=True)