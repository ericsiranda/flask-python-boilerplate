import os
from flask import Flask, render_template, jsonify, request
import vercel_blob

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
        
        # Simpan file ke Vercel Blob (untuk video, gunakan multipart)
        response = vercel_blob.put(
            file.filename,
            file.read(),
            multipart=True  # Direkomendasikan untuk file > 100MB [citation:7]
        )
        
        return jsonify({
            'success': True,
            'url': response['url'],
            'downloadUrl': response['downloadUrl']
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)