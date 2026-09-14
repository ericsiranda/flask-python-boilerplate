import os
from flask import Flask, render_template, jsonify, request, send_file, Response
from vercel.blob import put
from vercel.blob import get as blob_get
from io import BytesIO

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

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
            'pathname': result.pathname
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/file/<path:pathname>', methods=['GET'])
def get_file(pathname):
    """Mengambil file private dari Vercel Blob dan mengirimkannya ke browser."""
    try:
        blob_content = blob_get(pathname)
        
        if blob_content is None:
            return jsonify({'error': 'File tidak ditemukan'}), 404
        
        # Kirim sebagai response streaming
        return Response(
            blob_content,
            mimetype='video/mp4',
            headers={
                'Content-Disposition': 'inline',
                'Accept-Ranges': 'bytes'
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)