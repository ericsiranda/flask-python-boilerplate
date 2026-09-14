import os
from flask import Flask, render_template, jsonify, request, Response
from vercel.blob import put, head

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
            'pathname': result.pathname,
            'filename': file.filename,
            'size': len(file_content)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Route untuk mengecek apakah file ada
@app.route('/api/check/<path:pathname>', methods=['GET'])
def check_file(pathname):
    try:
        info = head(pathname)
        return jsonify({'exists': True, 'info': str(info)})
    except Exception as e:
        return jsonify({'exists': False, 'error': str(e)}), 404

if __name__ == '__main__':
    app.run(debug=True)