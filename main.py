# ==================== STREAM VIDEO (private) ====================
@app.route('/api/stream', methods=['GET'])
def stream_video():
    """
    Stream video dari Vercel Blob private.
    Menggunakan query parameter: /api/stream?file=namafile.mp4
    """
    try:
        pathname = request.args.get('file')
        
        if not pathname:
            return jsonify({'error': 'Parameter file tidak diberikan'}), 400
        
        token = os.environ.get('BLOB_READ_WRITE_TOKEN')
        
        if not token:
            return jsonify({'error': 'Token tidak ditemukan'}), 500
        
        blob_url = f"https://blob.vercel-storage.com/{pathname}"
        
        headers = {
            'Authorization': f'Bearer {token}'
        }
        
        req = requests.get(blob_url, headers=headers, stream=True)
        
        if req.status_code != 200:
            return jsonify({
                'error': f'Gagal: {req.status_code}',
                'detail': req.text[:300]
            }), req.status_code
        
        return Response(
            req.iter_content(chunk_size=8192),
            status=200,
            content_type=req.headers.get('Content-Type', 'video/mp4'),
            headers={
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'private, max-age=3600',
            }
        )
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500