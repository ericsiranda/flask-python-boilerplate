import os
from flask import Flask, render_template, jsonify
from vercel_blob import generate_client_token

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/upload-token', methods=['POST'])
def upload_token():
    try:
        token = generate_client_token(
            allowed_content_types=['video/*'],
            maximum_size_in_bytes=500 * 1024 * 1024,
        )
        return jsonify({'token': token})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)