import os
from flask import Flask, render_template, jsonify, request
from vercel.blob import presign_url

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/upload-token', methods=['POST'])
def upload_token():
    try:
        data = request.json
        filename = data.get('filename', 'video.mp4')
        
        signed_url = presign_url(
            pathname=f"uploads/{filename}",
            operation="put",
            valid_until=300
        )
        
        return jsonify({'url': signed_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)