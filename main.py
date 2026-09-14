import os
import json
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

# ==================== HANDLE UPLOAD (TOKEN UNTUK CLIENT UPLOAD) ====================
@app.route('/api/upload-token', methods=['POST'])
def handle_upload_token():
    """
    Endpoint ini dipanggil oleh browser SEBELUM upload dimulai.
    Tugasnya: memberikan 'izin' (token) ke browser agar bisa upload langsung ke Vercel Blob.
    """
    try:
        body = request.json
        type_ = body.get('type')
        
        if type_ == 'blob.generate-client-token':
            # Berikan izin upload
            from vercel.blob import generate_client_token
            
            pathname = body.get('payload', {}).get('pathname', 'video.mp4')
            
            token = generate_client_token(
                pathname=pathname,
                allowed_content_types=['video/*'],
                maximum_size_in_bytes=5 * 1024 * 1024 * 1024,  # 5 GB
                valid_until=int(__import__('time').time()) + 3600,  # 1 jam
            )
            
            return jsonify({'clientToken': token})
        
        elif type_ == 'blob.upload-completed':
            # Browser memberitahu server bahwa upload selesai
            print(f"Upload completed: {body}")
            return jsonify({'success': True})
        
        else:
            return jsonify({'error': f'Unknown type: {type_}'}), 400
    
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