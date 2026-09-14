import os
from flask import Flask, render_template, jsonify, request, Response
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
            access='private',
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

# ==================== STREAM VIDEO (untuk preview private) ====================
@app.route('/api/stream/<path:pathname>', methods=['GET'])
def stream_video(pathname):
    """Stream video dari Vercel Blob dengan token autentikasi"""
    try:
        token = os.environ.get('BLOB_READ_WRITE_TOKEN')
        
        if not token:
            print("ERROR: BLOB_READ_WRITE_TOKEN tidak ditemukan di environment")
            return jsonify({'error': 'Token tidak ditemukan'}), 500
        
        # Format URL Vercel Blob
        blob_url = f"https://blob.vercel-storage.com/{pathname}"
        
        print(f"Streaming dari: {blob_url}")
        print(f"Token ada: {token[:10]}...")
        
        # Request file dari Vercel dengan token
        headers = {
            'Authorization': f'Bearer {token}'
        }
        
        req = requests.get(blob_url, headers=headers, stream=True)
        
        print(f"Response status: {req.status_code}")
        print(f"Content-Type: {req.headers.get('Content-Type')}")
        
        if req.status_code != 200:
            return jsonify({'error': f'Gagal: {req.status_code}', 'detail': req.text[:200]}), req.status_code
        
        # Return sebagai streaming response
        return Response(
            req.iter_content(chunk_size=8192),
            content_type=req.headers.get('Content-Type', 'video/mp4'),
            headers={
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'public, max-age=3600'
            }
        )
    
    except Exception as e:
        print(f"ERROR stream: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ==================== DEBUG: CEK TOKEN ====================
@app.route('/api/debug', methods=['GET'])
def debug_info():
    """Route untuk mengecek apakah token tersedia"""
    token = os.environ.get('BLOB_READ_WRITE_TOKEN')
    
    return jsonify({
        'token_available': token is not None,
        'token_preview': token[:20] + '...' if token else None,
        'all_env_keys': list(os.environ.keys())
    })

if __name__ == '__main__':
    app.run(debug=True)