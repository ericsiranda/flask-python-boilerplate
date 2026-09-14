import os
from flask import Flask, render_template, jsonify, request, send_file
from vercel.blob import put
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
        
        # Baca isi file
        file_content = file.read()
        
        # Upload ke Vercel Blob dengan akses private
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
    try:
        # Ambil file private dari Vercel Blob
        # SDK resmi akan otomatis menggunakan token untuk autentikasi
        blob_content = get(pathname)
        
        return send_file(
            BytesIO(blob_content),
            mimetype='video/mp4',
            as_attachment=False
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)