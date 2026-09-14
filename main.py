import os
from flask import Flask, render_template, jsonify, request, send_file
import vercel_blob
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
        
        # Upload ke Vercel Blob
        # Parameter: filename, data, options (dict), multipart
        response = vercel_blob.put(
            file.filename,
            file_content,
            {
                # Opsi tambahan bisa ditambahkan di sini jika perlu
                # Contoh: "addRandomSuffix": "true"
            },
            multipart=True  # Untuk file besar
        )
        
        return jsonify({
            'success': True,
            'url': response['url'],
            'pathname': response.get('pathname', file.filename)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Route khusus untuk mengakses file private
@app.route('/api/file/<path:pathname>', methods=['GET'])
def get_file(pathname):
    """
    Route ini digunakan untuk mengambil file dari Vercel Blob 
    yang disimpan sebagai private. File akan di-stream ke browser.
    """
    try:
        # Ambil file dari Vercel Blob
        blob_content = vercel_blob.get(pathname)
        
        if blob_content is None:
            return jsonify({'error': 'File tidak ditemukan'}), 404
        
        # Tentukan tipe konten (bisa disesuaikan)
        # Karena ini video, kita gunakan video/mp4 sebagai default
        content_type = 'video/mp4'
        
        # Kirim file sebagai response
        return send_file(
            BytesIO(blob_content),
            mimetype=content_type,
            as_attachment=False
        )
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)