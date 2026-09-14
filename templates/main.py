import os
from flask import Flask, render_template, jsonify, request
from vercel.blob import put, presign_url # Import dari SDK resmi

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/upload-token', methods=['POST'])
def upload_token():
    # Buat signed URL untuk upload dari browser
    data = request.json
    filename = data.get('filename')
    
    # Membuat URL yang aman untuk upload (berlaku 5 menit)
    signed_url = presign_url(
        pathname=f"uploads/{filename}",
        operation="put",
        valid_until=300  # 5 menit
    )
    
    return jsonify({'url': signed_url})

if __name__ == '__main__':
    app.run(debug=True)