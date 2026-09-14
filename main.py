import os
from flask import Flask, render_template, jsonify, request
from vercel.blob import presign_url
import vercel_blob

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

# ==================== TOKEN UPLOAD (SIGNED URL) ====================
@app.route('/api/upload-token', methods=['POST'])
def handle_upload_token():
    """
    Endpoint ini dipanggil browser SEBELUM upload dimulai.
    Tugasnya: membuat Signed URL yang memungkinkan browser
    upload langsung ke Vercel Blob tanpa melewati server Flask.
    """
    try:
        body = request.json
        filename = body.get('filename', 'video.mp4')
        
        # Buat signed URL untuk operasi PUT (upload)
        # URL ini berlaku 1 jam untuk satu file tertentu
        url = presign_url(
            pathname=filename,
            operation='put',
            valid_until=3600
        )
        
        return jsonify({'url': url})
    
    except Exception as e:
        import traceback
        traceback.print_exc()
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
        'token_preview': token[:20] + '...' if token else None,
        'store_id': store_id,
    })

if __name__ == '__main__':
    app.run(debug=True)