import os
import time
import re
import json
import calendar
import bcrypt
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, jsonify, request
from vercel.blob import put
import vercel_blob
import requests as req_lib

app = Flask(__name__)

# ==================== TIMEZONE WIB (UTC+7) ====================
WIB = timezone(timedelta(hours=7))


# ==================== DATABASE CONNECTION ====================
def get_db():
    priority_keys = ['DB_URL', 'DATABASE_URL', 'SUPABASE_POSTGRES_URL', 'POSTGRES_URL']
    db_url = None
    used_key = None

    for key in priority_keys:
        val = os.environ.get(key)
        if val and val.startswith('postgres'):
            db_url = val
            used_key = key
            break

    if not db_url:
        for key in sorted(os.environ.keys()):
            if ('POSTGRES_URL' in key or 'DATABASE_URL' in key) and 'PRISMA' not in key:
                val = os.environ.get(key)
                if val and val.startswith('postgres'):
                    db_url = val
                    used_key = key
                    break

    if not db_url:
        print("⚠️ Tidak ada URL database di environment")
        return None

    try:
        conn = psycopg2.connect(db_url, sslmode='require', connect_timeout=10)
        return conn
    except Exception as e:
        print(f"DB: ❌ {used_key} gagal: {str(e)[:150]}")
        return None


def init_tables():
    conn = get_db()
    if not conn:
        return False, "Database tidak tersedia"

    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(100),
                video_name VARCHAR(255),
                video_url TEXT,
                videos_json TEXT DEFAULT '[]',
                images_json TEXT DEFAULT '[]',
                time VARCHAR(50),
                platform VARCHAR(50),
                account VARCHAR(100),
                caption TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                posted_at TIMESTAMP,
                post_result TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        try:
            cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS videos_json TEXT DEFAULT '[]'")
            cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS images_json TEXT DEFAULT '[]'")
            cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'pending'")
            cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS posted_at TIMESTAMP")
            cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS post_result TEXT")
        except Exception as e:
            print(f"Migration note: {e}")
            conn.rollback()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS medsos_accounts (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(50) NOT NULL,
                username VARCHAR(100) NOT NULL,
                connected_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(platform, username)
            )
        """)

        # === TABEL PROMPT SCHEDULES ===
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prompt_schedules (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255),
                prompt TEXT NOT NULL,
                source_type VARCHAR(20) DEFAULT 'text',
                source_image_url TEXT,
                repeat_type VARCHAR(20) DEFAULT 'once',
                repeat_interval_min INT DEFAULT 0,
                time VARCHAR(50),
                day_of_week INT,
                day_of_month INT,
                next_run_at TIMESTAMP,
                last_run_at TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                auto_post BOOLEAN DEFAULT TRUE,
                platform VARCHAR(50),
                account VARCHAR(100),
                caption TEXT,
                post_delay_min INT DEFAULT 5,
                posted_schedule_id INT,
                last_result TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_prompt_schedules_next_run ON prompt_schedules(next_run_at) WHERE is_active = TRUE")
        except Exception as e:
            print(f"Index note: {e}")
            conn.rollback()

        # === TABEL POSTING HISTORY (untuk anti-duplikat & log) ===
        cur.execute("""
            CREATE TABLE IF NOT EXISTS posting_history (
                id SERIAL PRIMARY KEY,
                schedule_id INT,
                media_url TEXT NOT NULL,
                media_name VARCHAR(255),
                platform VARCHAR(50),
                account VARCHAR(100),
                caption TEXT,
                media_id VARCHAR(255),
                container_id VARCHAR(255),
                posted_at TIMESTAMP DEFAULT NOW(),
                source VARCHAR(50) DEFAULT 'cron'
            )
        """)
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_posting_history_media_url ON posting_history(media_url)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_posting_history_posted_at ON posting_history(posted_at DESC)")
        except Exception as e:
            print(f"Index history note: {e}")
            conn.rollback()

        conn.commit()
        cur.close()
        conn.close()
        return True, None
    except Exception as e:
        print(f"Init tables error: {e}")
        try:
            conn.rollback(); conn.close()
        except: pass
        return False, str(e)


# ==================== SANITIZE FILENAME ====================
def sanitize_filename(original_name, default_ext='mp4'):
    if not original_name:
        return f"file_{int(time.time())}.{default_ext}"
    if '.' in original_name:
        base, ext = original_name.rsplit('.', 1)
    else:
        base, ext = original_name, default_ext
    safe_base = re.sub(r'[^\w\-]', '_', base)
    safe_base = re.sub(r'_+', '_', safe_base).strip('_')[:50] or 'file'
    safe_ext = re.sub(r'[^\w]', '', ext)[:10] or default_ext
    return f"{safe_base}_{int(time.time())}.{safe_ext}"


# ==================== INSTAGRAM GRAPH API POSTING ====================
def post_to_instagram(media_url, caption, media_type, ig_user_id, access_token, is_video=True):
    try:
        create_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
        if is_video:
            create_params = {
                'media_type': 'REELS',
                'video_url': media_url,
                'caption': caption or '',
                'access_token': access_token
            }
        else:
            create_params = {
                'image_url': media_url,
                'caption': caption or '',
                'access_token': access_token
            }

        create_res = req_lib.post(create_url, data=create_params, timeout=(10, 90))
        create_data = create_res.json()
        print(f"[IG] Create container: {create_data}")

        if 'id' not in create_data:
            return {'success': False, 'error': f"Gagal create container: {create_data}"}

        container_id = create_data['id']

        if is_video:
            finished = False
            for i in range(30):
                time.sleep(10)
                status_url = f"https://graph.facebook.com/v21.0/{container_id}"
                status_res = req_lib.get(status_url, params={
                    'fields': 'status_code,status',
                    'access_token': access_token
                }, timeout=(10, 60))
                status_data = status_res.json()
                status_code = status_data.get('status_code')
                print(f"[IG] Polling {i+1}/30: {status_code}")

                if status_code == 'FINISHED':
                    finished = True
                    print(f"[IG] Container FINISHED, tambah delay 15 detik untuk safety...")
                    time.sleep(15)
                    break
                elif status_code == 'ERROR':
                    return {'success': False, 'error': f"Container error: {status_data}"}

            if not finished:
                return {'success': False, 'error': 'Timeout: container tidak selesai diproses IG'}

        publish_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish"

        for attempt in range(5):
            publish_res = req_lib.post(publish_url, data={
                'creation_id': container_id,
                'access_token': access_token
            }, timeout=(10, 90))
            publish_data = publish_res.json()
            print(f"[IG] Publish attempt {attempt+1}/5: {publish_data}")

            if 'id' in publish_data:
                return {
                    'success': True,
                    'container_id': container_id,
                    'media_id': publish_data['id']
                }

            error_code = publish_data.get('error', {}).get('code')
            if error_code == 9007 and attempt < 4:
                wait_time = 10 + (attempt * 5)
                print(f"[IG] Media belum ready (9007), retry dalam {wait_time} detik...")
                time.sleep(wait_time)
                continue
            else:
                return {'success': False, 'error': f"Gagal publish: {publish_data}"}

        return {
            'success': False,
            'error': 'Gagal publish setelah 5x retry (error 9007 persistent). Coba tunggu 1-2 menit lalu retry manual dari UI.'
        }

    except Exception as e:
        print(f"[IG] Error: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


# ==================== ANTI-DUPLIKAT: CEK MEDIA SUDAH PERNAH DIPOSTING ====================
def media_already_posted(media_url, within_hours=24):
    try:
        conn = get_db()
        if not conn:
            return None
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, schedule_id, media_name, platform, account, media_id, posted_at
            FROM posting_history
            WHERE media_url = %s
              AND posted_at >= NOW() - INTERVAL '%s hours'
            ORDER BY posted_at DESC
            LIMIT 1
        """, (media_url, within_hours))
        row = cur.fetchone()
        cur.close(); conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[AntiDup] Cek error: {e}")
        return None


def record_posting_history(schedule_id, media_url, media_name, platform, account,
                            caption, media_id, container_id, source='cron'):
    try:
        conn = get_db()
        if not conn:
            return False
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO posting_history
            (schedule_id, media_url, media_name, platform, account, caption,
             media_id, container_id, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (schedule_id, media_url, media_name, platform, account,
              caption, media_id, container_id, source))
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        print(f"[AntiDup] Record error: {e}")
        return False


# ==================== HITUNG NEXT RUN ====================
def calculate_next_run(sched, from_time=None):
    now = from_time or datetime.now(WIB).replace(tzinfo=None)
    repeat_type = (sched.get('repeat_type') or 'once').lower()

    if repeat_type == 'once':
        return None

    if repeat_type == 'interval':
        minutes = int(sched.get('repeat_interval_min') or 0)
        if minutes <= 0:
            return None
        return now + timedelta(minutes=minutes)

    time_str = (sched.get('time') or '00:00').strip()
    try:
        hh, mm = map(int, time_str.split(':'))
    except:
        hh, mm = 0, 0

    if repeat_type == 'daily':
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if repeat_type == 'weekly':
        target_dow = int(sched.get('day_of_week') or 0)
        py_target = 6 if target_dow == 0 else (target_dow - 1)
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days_ahead = (py_target - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if repeat_type == 'monthly':
        target_dom = int(sched.get('day_of_month') or 1)
        try:
            candidate = now.replace(day=target_dom, hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            last_day = calendar.monthrange(now.year, now.month)[1]
            candidate = now.replace(day=min(target_dom, last_day), hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            if now.month == 12:
                y, m = now.year + 1, 1
            else:
                y, m = now.year, now.month + 1
            last_day = calendar.monthrange(y, m)[1]
            candidate = datetime(y, m, min(target_dom, last_day), hh, mm)
        return candidate

    return None


# ==================== AGNES TEXT-TO-IMAGE ====================
def generate_image_agnes(prompt, agnes_api_key):
    """
    Generate gambar dari text prompt via Agnes AI.
    Return: dict { success, image_url, pathname, filename } atau { success: False, error }
    """
    try:
        url = "https://apihub.agnes-ai.com/v1/images/generations"
        payload = {
            "model": "agnes-image-2.1-flash",
            "prompt": prompt,
            "size": "1024x1024",
            "extra_body": {"response_format": "url"}
        }
        headers = {"Authorization": f"Bearer {agnes_api_key}", "Content-Type": "application/json"}

        res_data = None
        last_err = None
        for attempt in range(3):
            try:
                print(f"[Text2Img] Submit attempt {attempt+1}/3...")
                res = req_lib.post(url, headers=headers, json=payload, timeout=(30, 180))
                res_data = res.json()
                break
            except req_lib.exceptions.ReadTimeout as e:
                last_err = f"Timeout attempt {attempt+1}: {e}"
                print(f"[Text2Img] ⚠️ {last_err}")
                if attempt < 2:
                    time.sleep(5 + attempt * 5)
            except Exception as e:
                last_err = str(e)
                print(f"[Text2Img] ⚠️ {last_err}")
                break

        if not res_data:
            return {'success': False, 'error': f'Gagal submit ke Agnes: {last_err}'}

        if res_data.get('error'):
            return {'success': False, 'error': f"Agnes error: {res_data['error']}"}

        image_result_url = None
        if res_data.get('data') and len(res_data['data']) > 0:
            image_result_url = res_data['data'][0].get('url')
        if not image_result_url:
            return {'success': False, 'error': f'URL gambar tidak ditemukan: {res_data}'}

        # Download & upload ke Blob
        img_dl = req_lib.get(image_result_url, timeout=(30, 180))
        if img_dl.status_code != 200:
            return {'success': False, 'error': f'Gagal download gambar: HTTP {img_dl.status_code}'}

        filename = f"ai_edit_image_{int(time.time())}.png"
        blob_result = put(filename, img_dl.content, access='public', multipart=True)

        return {
            'success': True,
            'image_url': blob_result.url,
            'pathname': blob_result.pathname,
            'filename': filename
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {'success': False, 'error': str(e)}


# ==================== CORE EKSEKUSI PROMPT SCHEDULE ====================
def execute_prompt_schedule(sched):
    """
    Mendukung 3 mode:
    - text          : Text-to-Video via Agnes
    - text_to_image : Text-to-Image via Agnes
    - image         : Image-to-Video via Agnes
    """
    result = {
        'prompt_schedule_id': sched['id'],
        'title': sched.get('title') or '',
        'success': False,
        'step': 'start'
    }

    try:
        agnes_api_key = os.environ.get('AGNES_API_KEY')
        if not agnes_api_key:
            result['error'] = 'AGNES_API_KEY belum di-set'
            return result

        prompt = sched.get('prompt') or ''
        source_type = (sched.get('source_type') or 'text').lower()
        source_image_url = sched.get('source_image_url') or ''

        # === BRANCH: TEXT-TO-IMAGE ===
        if source_type == 'text_to_image':
            result['step'] = 'generate_image'
            print(f"[PromptSched] Mode TEXT-TO-IMAGE untuk id={sched['id']}")
            img_result = generate_image_agnes(prompt, agnes_api_key)

            if not img_result['success']:
                result['error'] = img_result.get('error', 'Gagal generate gambar')
                return result

            result['image_url'] = img_result['image_url']
            result['pathname'] = img_result['pathname']
            result['success'] = True

            # === AUTO-POST KE SCHEDULES (IG) - sebagai IMAGE ===
            if sched.get('auto_post'):
                result['step'] = 'schedule_ig'
                try:
                    conn = get_db()
                    if conn:
                        delay = int(sched.get('post_delay_min') or 5)
                        post_time = datetime.now(WIB).replace(tzinfo=None) + timedelta(minutes=delay)
                        post_time_str = post_time.strftime('%Y-%m-%dT%H:%M')

                        images_json = json.dumps([{
                            'url': img_result['image_url'],
                            'name': img_result['filename'],
                            'is_primary': True
                        }])

                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO schedules
                            (session_id, video_name, video_url, videos_json, images_json,
                             time, platform, account, caption, status)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id
                        """, (
                            f"promptsched_{sched['id']}_{int(time.time())}",
                            img_result['filename'],
                            img_result['image_url'],
                            json.dumps([]),
                            images_json,
                            post_time_str,
                            sched.get('platform') or '',
                            sched.get('account') or '',
                            sched.get('caption') or ''
                        ))
                        new_sched_id = cur.fetchone()[0]
                        cur.execute("UPDATE prompt_schedules SET posted_schedule_id = %s WHERE id = %s",
                                    (new_sched_id, sched['id']))
                        conn.commit(); cur.close(); conn.close()

                        result['posted_schedule_id'] = new_sched_id
                        result['post_at'] = post_time_str
                except Exception as e:
                    print(f"[PromptSched] Auto-post text2img error: {e}")
                    result['auto_post_error'] = str(e)

            return result

        # === STEP 1 (VIDEO): SUBMIT KE AGNES (DENGAN RETRY) ===
        result['step'] = 'submit_agnes'
        submit_url = "https://apihub.agnes-ai.com/v1/videos"
        headers = {"Authorization": f"Bearer {agnes_api_key}", "Content-Type": "application/json"}

        if source_type == 'image' and source_image_url:
            payload = {
                "model": "agnes-video-v2.0", "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
                "image": source_image_url
            }
        else:
            payload = {
                "model": "agnes-video-v2.0", "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
            }
            default_img = os.environ.get('AGNES_DEFAULT_IMAGE')
            if default_img:
                payload["image"] = default_img

        submit_data = None
        last_submit_error = None
        for attempt in range(3):
            try:
                print(f"[PromptSched] Submit ke Agnes attempt {attempt+1}/3 (timeout=180s)...")
                submit_res = req_lib.post(
                    submit_url, headers=headers, json=payload,
                    timeout=(30, 180)
                )
                submit_data = submit_res.json()
                print(f"[PromptSched] Submit response: {str(submit_data)[:300]}")
                break
            except req_lib.exceptions.ReadTimeout as e:
                last_submit_error = f"Read timeout attempt {attempt+1}: {e}"
                print(f"[PromptSched] ⚠️ {last_submit_error}")
                if attempt < 2:
                    wait = 5 + (attempt * 5)
                    print(f"[PromptSched] Retry dalam {wait} detik...")
                    time.sleep(wait)
            except req_lib.exceptions.ConnectionError as e:
                last_submit_error = f"Connection error attempt {attempt+1}: {e}"
                print(f"[PromptSched] ⚠️ {last_submit_error}")
                if attempt < 2:
                    time.sleep(5)
            except Exception as e:
                last_submit_error = str(e)
                print(f"[PromptSched] ⚠️ Submit error: {last_submit_error}")
                break

        if not submit_data:
            result['error'] = f'Gagal submit ke Agnes setelah 3x retry: {last_submit_error}'
            return result

        video_id = submit_data.get('video_id') or submit_data.get('id') or submit_data.get('task_id')
        if not video_id:
            result['error'] = f'Gagal submit ke Agnes: {submit_data}'
            return result

        result['agnes_video_id'] = video_id
        print(f"[PromptSched] ✅ Video ID: {video_id}")

        # === STEP 2: POLLING STATUS ===
        result['step'] = 'polling'
        result_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}"
        result_data = None

        for i in range(60):
            time.sleep(10)
            try:
                r = req_lib.get(
                    result_url,
                    headers={"Authorization": f"Bearer {agnes_api_key}"},
                    timeout=(10, 60)
                )
                result_data = r.json()
                status = result_data.get('status') or result_data.get('data', {}).get('status')
                print(f"[PromptSched] Polling {i+1}/60: {status}")
                if status == 'completed':
                    break
                elif status == 'failed':
                    result['error'] = f'Agnes gagal: {result_data}'
                    return result
            except req_lib.exceptions.ReadTimeout:
                print(f"[PromptSched] Polling {i+1}/60: timeout, lanjut polling...")
                continue
            except Exception as ex:
                print(f"[PromptSched] Polling error: {ex}")
                continue

        if not result_data or (result_data.get('status') != 'completed'):
            result['error'] = 'Timeout menunggu Agnes AI'
            return result

        output_url = (result_data.get('video_url') or result_data.get('url') or
                      result_data.get('data', {}).get('video_url') or
                      result_data.get('data', {}).get('url'))
        if not output_url:
            result['error'] = 'URL video tidak ditemukan di response Agnes'
            return result

        # === STEP 3: DOWNLOAD & UPLOAD KE BLOB ===
        result['step'] = 'upload_blob'
        video_dl = req_lib.get(output_url, timeout=(30, 300))
        if video_dl.status_code != 200:
            result['error'] = f'Gagal download video: HTTP {video_dl.status_code}'
            return result

        filename = f"ai_edit_{int(time.time())}_{sched['id']}.mp4"
        blob_result = put(filename, video_dl.content, access='public', multipart=True)

        result['video_url'] = blob_result.url
        result['pathname'] = blob_result.pathname
        result['success'] = True

        # === STEP 4: AUTO-POST KE SCHEDULES (IG) ===
        if sched.get('auto_post'):
            result['step'] = 'schedule_ig'
            try:
                conn = get_db()
                if conn:
                    delay = int(sched.get('post_delay_min') or 5)
                    post_time = datetime.now(WIB).replace(tzinfo=None) + timedelta(minutes=delay)
                    post_time_str = post_time.strftime('%Y-%m-%dT%H:%M')

                    videos_json = json.dumps([{
                        'url': blob_result.url,
                        'name': filename,
                        'is_primary': True
                    }])

                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO schedules
                        (session_id, video_name, video_url, videos_json, images_json,
                         time, platform, account, caption, status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id
                    """, (
                        f"promptsched_{sched['id']}_{int(time.time())}",
                        filename,
                        blob_result.url,
                        videos_json,
                        json.dumps([]),
                        post_time_str,
                        sched.get('platform') or '',
                        sched.get('account') or '',
                        sched.get('caption') or ''
                    ))
                    new_sched_id = cur.fetchone()[0]

                    cur.execute("UPDATE prompt_schedules SET posted_schedule_id = %s WHERE id = %s",
                                (new_sched_id, sched['id']))
                    conn.commit()
                    cur.close()
                    conn.close()

                    result['posted_schedule_id'] = new_sched_id
                    result['post_at'] = post_time_str
            except Exception as e:
                print(f"[PromptSched] Auto-post scheduling error: {e}")
                result['auto_post_error'] = str(e)

        return result
    except Exception as e:
        import traceback; traceback.print_exc()
        result['error'] = str(e)
        return result


# ==================== CRON: EXECUTE ====================
@app.route('/api/cron/execute-schedules', methods=['GET'])
def cron_execute_schedules():
    cron_secret = os.environ.get('CRON_SECRET')
    if cron_secret:
        req_secret = request.args.get('secret', '')
        if req_secret != cron_secret:
            return jsonify({'error': 'Unauthorized'}), 401

    try:
        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'error': 'DB tidak tersedia'}), 500

        now = datetime.now(WIB).replace(tzinfo=None)
        now_str = now.strftime('%Y-%m-%dT%H:%M')

        print(f"[CRON] ========== START ({now_str}) ==========")

        # ============ BAGIAN 1: PROMPT SCHEDULES (AI) ============
        prompt_results = []
        cur_ps = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur_ps.execute("""
            SELECT * FROM prompt_schedules
            WHERE is_active = TRUE
              AND next_run_at IS NOT NULL
              AND next_run_at <= %s
            ORDER BY next_run_at ASC
            LIMIT 5
        """, (now,))
        due_prompts = cur_ps.fetchall()
        cur_ps.close()

        print(f"[CRON] Prompt schedules due: {len(due_prompts)}")

        for ps in due_prompts:
            print(f"[CRON] Executing prompt_schedule id={ps['id']} '{ps.get('title')}'")

            try:
                cur_u = conn.cursor()
                cur_u.execute("UPDATE prompt_schedules SET last_run_at = %s WHERE id = %s",
                              (now, ps['id']))
                conn.commit()
                cur_u.close()
            except: pass

            res = execute_prompt_schedule(ps)
            prompt_results.append(res)

            try:
                cur_lr = conn.cursor()
                cur_lr.execute("UPDATE prompt_schedules SET last_result = %s WHERE id = %s",
                               (json.dumps(res)[:4000], ps['id']))
                conn.commit()
                cur_lr.close()
            except: pass

            next_run = calculate_next_run(ps, now)

            try:
                cur_u2 = conn.cursor()
                if next_run:
                    cur_u2.execute("""
                        UPDATE prompt_schedules 
                        SET next_run_at = %s, is_active = TRUE 
                        WHERE id = %s
                    """, (next_run, ps['id']))
                else:
                    cur_u2.execute("""
                        UPDATE prompt_schedules 
                        SET next_run_at = NULL, is_active = FALSE 
                        WHERE id = %s
                    """, (ps['id'],))
                conn.commit()
                cur_u2.close()
            except Exception as e:
                print(f"[CRON] Update next_run error: {e}")

        # ============ BAGIAN 2: SCHEDULES BIASA (IG POSTING) ============
        window_start = now - timedelta(hours=24)
        window_str = window_start.strftime('%Y-%m-%dT%H:%M')

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM schedules 
            WHERE status = 'pending'
            AND time <= %s 
            AND time >= %s
            ORDER BY time ASC
        """, (now_str, window_str))
        due_schedules = cur.fetchall()
        cur.close()

        print(f"[CRON] IG schedules due: {len(due_schedules)}")

        results = []
        ig_user_id = os.environ.get('IG_USER_ID')
        ig_token = os.environ.get('IG_ACCESS_TOKEN')

        for sched in due_schedules:
            result = {
                'id': sched['id'],
                'platform': sched['platform'],
                'account': sched['account'],
                'status': 'pending'
            }

            # === LOCK ATOMIK: hanya 1 proses yang bisa ambil schedule ini ===
            try:
                cur_lock = conn.cursor()
                cur_lock.execute("""
                    UPDATE schedules 
                    SET status = 'processing' 
                    WHERE id = %s AND status = 'pending'
                    RETURNING id
                """, (sched['id'],))
                locked = cur_lock.fetchone()
                conn.commit()
                cur_lock.close()

                if not locked:
                    print(f"[CRON] Schedule {sched['id']} sudah diambil proses lain, skip.")
                    continue
            except Exception as e:
                print(f"[CRON] Lock error: {e}")
                continue

            try:
                try:
                    videos = json.loads(sched.get('videos_json') or '[]')
                except: videos = []
                try:
                    images = json.loads(sched.get('images_json') or '[]')
                except: images = []

                if not ig_user_id or not ig_token:
                    result['status'] = 'failed'
                    result['error'] = 'IG_USER_ID / IG_ACCESS_TOKEN belum di-set'
                    cur3 = conn.cursor()
                    cur3.execute("UPDATE schedules SET status='failed', post_result=%s WHERE id=%s",
                                 (json.dumps(result), sched['id']))
                    conn.commit(); cur3.close()
                    results.append(result)
                    continue

                media_url = None
                media_name = None
                is_video = True
                if videos:
                    media_url = videos[0]['url']
                    media_name = videos[0].get('name') or sched.get('video_name') or ''
                    is_video = True
                elif images:
                    media_url = images[0]['url']
                    media_name = images[0].get('name') or ''
                    is_video = False

                if not media_url:
                    result['status'] = 'failed'
                    result['error'] = 'Tidak ada media'
                    cur4 = conn.cursor()
                    cur4.execute("UPDATE schedules SET status='failed', post_result=%s WHERE id=%s",
                                 (json.dumps(result), sched['id']))
                    conn.commit(); cur4.close()
                    results.append(result)
                    continue

                # === CEK DUPLIKAT: media yang sama sudah pernah diposting? ===
                duplicate = media_already_posted(media_url, within_hours=24)
                if duplicate:
                    dup_time = duplicate['posted_at'].strftime('%Y-%m-%d %H:%M') if duplicate.get('posted_at') else '?'
                    result['status'] = 'skipped'
                    result['error'] = f"Duplikat: media ini sudah diposting pada {dup_time} (media_id: {duplicate.get('media_id')})"
                    result['duplicate_of'] = duplicate.get('media_id')
                    print(f"[CRON] ⚠️ Skip schedule {sched['id']} — {result['error']}")

                    cur_dup = conn.cursor()
                    cur_dup.execute("""
                        UPDATE schedules 
                        SET status='skipped', post_result=%s, posted_at=NOW()
                        WHERE id=%s
                    """, (json.dumps(result), sched['id']))
                    conn.commit(); cur_dup.close()
                    results.append(result)
                    continue

                # === POSTING KE INSTAGRAM ===
                ig_result = post_to_instagram(
                    media_url=media_url,
                    caption=sched.get('caption', ''),
                    media_type='REELS' if is_video else 'IMAGE',
                    ig_user_id=ig_user_id,
                    access_token=ig_token,
                    is_video=is_video
                )

                if ig_result['success']:
                    result['status'] = 'posted'
                    result['media_id'] = ig_result['media_id']
                    result['container_id'] = ig_result['container_id']

                    cur5 = conn.cursor()
                    cur5.execute("""
                        UPDATE schedules 
                        SET status='posted', posted_at=NOW(), post_result=%s 
                        WHERE id=%s
                    """, (json.dumps(result), sched['id']))
                    conn.commit(); cur5.close()

                    # === CATAT KE HISTORY (untuk anti-duplikat) ===
                    record_posting_history(
                        schedule_id=sched['id'],
                        media_url=media_url,
                        media_name=media_name,
                        platform=sched.get('platform') or '',
                        account=sched.get('account') or '',
                        caption=sched.get('caption') or '',
                        media_id=ig_result['media_id'],
                        container_id=ig_result['container_id'],
                        source='cron'
                    )
                else:
                    result['status'] = 'failed'
                    result['error'] = ig_result.get('error', 'Unknown error')
                    cur6 = conn.cursor()
                    cur6.execute("UPDATE schedules SET status='failed', post_result=%s WHERE id=%s",
                                 (json.dumps(result), sched['id']))
                    conn.commit(); cur6.close()
            except Exception as e:
                result['status'] = 'failed'
                result['error'] = str(e)
                try:
                    cur7 = conn.cursor()
                    cur7.execute("UPDATE schedules SET status='failed', post_result=%s WHERE id=%s",
                                 (json.dumps(result), sched['id']))
                    conn.commit(); cur7.close()
                except: pass

            results.append(result)

        conn.close()
        print(f"[CRON] ========== END ==========")

        return jsonify({
            'success': True,
            'checked_at': now.isoformat(),
            'prompt_schedules': {
                'due_count': len(due_prompts),
                'results': prompt_results
            },
            'ig_schedules': {
                'due_count': len(due_schedules),
                'results': results
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== MANUAL POST (REVISED: anti-duplikat) ====================
@app.route('/api/schedules/post-now', methods=['POST'])
def post_schedule_now():
    cron_secret = os.environ.get('CRON_SECRET')
    if cron_secret:
        req_secret = request.args.get('secret', '')
        if req_secret != cron_secret:
            return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    schedule_id = data.get('id')
    force = bool(data.get('force', False))
    if not schedule_id:
        return jsonify({'error': 'ID wajib'}), 400

    try:
        conn = get_db()
        if not conn:
            return jsonify({'error': 'DB tidak tersedia'}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM schedules WHERE id = %s", (schedule_id,))
        sched = cur.fetchone()
        cur.close()

        if not sched:
            conn.close()
            return jsonify({'error': 'Jadwal tidak ditemukan'}), 404

        ig_user_id = os.environ.get('IG_USER_ID')
        ig_token = os.environ.get('IG_ACCESS_TOKEN')

        if not ig_user_id or not ig_token:
            conn.close()
            return jsonify({
                'success': False,
                'error': 'IG_USER_ID / IG_ACCESS_TOKEN belum di-set'
            }), 400

        try:
            videos = json.loads(sched.get('videos_json') or '[]')
        except: videos = []
        try:
            images = json.loads(sched.get('images_json') or '[]')
        except: images = []

        media_url = None
        media_name = None
        is_video = True
        if videos:
            media_url = videos[0]['url']
            media_name = videos[0].get('name') or sched.get('video_name') or ''
        elif images:
            media_url = images[0]['url']
            media_name = images[0].get('name') or ''
            is_video = False

        if not media_url:
            conn.close()
            return jsonify({'success': False, 'error': 'Tidak ada media'}), 400

        # === CEK DUPLIKAT (kecuali force=true) ===
        if not force:
            duplicate = media_already_posted(media_url, within_hours=24)
            if duplicate:
                dup_time = duplicate['posted_at'].strftime('%Y-%m-%d %H:%M') if duplicate.get('posted_at') else '?'
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'Media ini sudah diposting pada {dup_time}. Gunakan force=true untuk tetap posting.',
                    'duplicate': True,
                    'duplicate_of': duplicate.get('media_id')
                }), 409

        ig_result = post_to_instagram(
            media_url=media_url,
            caption=sched.get('caption', ''),
            media_type='REELS' if is_video else 'IMAGE',
            ig_user_id=ig_user_id,
            access_token=ig_token,
            is_video=is_video
        )

        if ig_result['success']:
            cur2 = conn.cursor()
            cur2.execute("""
                UPDATE schedules 
                SET status = 'posted', posted_at = NOW(), post_result = %s 
                WHERE id = %s
            """, (json.dumps(ig_result), schedule_id))
            conn.commit()
            cur2.close()

            record_posting_history(
                schedule_id=schedule_id,
                media_url=media_url,
                media_name=media_name,
                platform=sched.get('platform') or '',
                account=sched.get('account') or '',
                caption=sched.get('caption') or '',
                media_id=ig_result['media_id'],
                container_id=ig_result['container_id'],
                source='manual'
            )
        else:
            cur2 = conn.cursor()
            cur2.execute("""
                UPDATE schedules 
                SET status = 'failed', post_result = %s 
                WHERE id = %s
            """, (json.dumps(ig_result), schedule_id))
            conn.commit()
            cur2.close()

        conn.close()
        return jsonify(ig_result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== AGNES AI VIDEO ====================
@app.route('/api/edit-video', methods=['POST'])
def edit_video_agnes():
    data = request.json
    prompt = data.get('prompt')
    image_url = data.get('image_url')
    video_url = data.get('video_url')

    if not prompt:
        return jsonify({'error': 'Prompt wajib disediakan'}), 400

    agnes_api_key = os.environ.get('AGNES_API_KEY')
    if not agnes_api_key:
        return jsonify({'error': 'AGNES_API_KEY belum di-set'}), 500

    try:
        source_url = video_url or image_url
        if not source_url:
            return jsonify({'error': 'image_url atau video_url wajib diisi'}), 400

        is_video_input = source_url.lower().endswith(('.mp4', '.webm', '.mov', '.avi', '.mkv'))
        submit_url = "https://apihub.agnes-ai.com/v1/videos"

        if is_video_input:
            submit_payload = {
                "model": "agnes-video-v2.0", "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
                "videos": [{"url": source_url, "role": "reference"}]
            }
        else:
            submit_payload = {
                "model": "agnes-video-v2.0", "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
                "image": source_url
            }

        headers = {"Authorization": f"Bearer {agnes_api_key}", "Content-Type": "application/json"}
        submit_data = None
        for attempt in range(3):
            try:
                submit_res = req_lib.post(submit_url, headers=headers, json=submit_payload, timeout=(30, 180))
                submit_data = submit_res.json()
                break
            except req_lib.exceptions.ReadTimeout:
                if attempt < 2: time.sleep(5)
            except Exception:
                break

        if not submit_data:
            return jsonify({'error': 'Gagal submit ke Agnes (timeout)'}), 500

        video_id = submit_data.get('video_id') or submit_data.get('id') or submit_data.get('task_id')
        if (not video_id) and is_video_input:
            fallback = {
                "model": "agnes-video-v2.0", "prompt": prompt,
                "height": 768, "width": 1152,
                "num_frames": 121, "frame_rate": 24,
                "image": source_url
            }
            try:
                submit_res = req_lib.post(submit_url, headers=headers, json=fallback, timeout=(30, 180))
                submit_data = submit_res.json()
                video_id = submit_data.get('video_id') or submit_data.get('id') or submit_data.get('task_id')
            except Exception:
                pass

        if not video_id:
            return jsonify({'error': f'Gagal submit: {submit_data}'}), 500

        result_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}"
        result_data = None
        for i in range(60):
            time.sleep(10)
            try:
                r = req_lib.get(result_url, headers={"Authorization": f"Bearer {agnes_api_key}"}, timeout=(10, 60))
                result_data = r.json()
                status = result_data.get('status') or result_data.get('data', {}).get('status')
                if status == 'completed': break
                elif status == 'failed': return jsonify({'error': f"Agnes gagal: {result_data}"}), 500
            except req_lib.exceptions.ReadTimeout:
                continue
            except: pass

        if not result_data or result_data.get('status') != 'completed':
            return jsonify({'error': 'Timeout'}), 500

        output_url = (result_data.get('video_url') or result_data.get('url') or
                      result_data.get('data', {}).get('video_url') or
                      result_data.get('data', {}).get('url'))
        if not output_url:
            return jsonify({'error': 'URL tidak ditemukan'}), 500

        video_dl = req_lib.get(output_url, timeout=(30, 300))
        filename = f"ai_edit_{int(time.time())}.mp4"
        blob_result = put(filename, video_dl.content, access='public', multipart=True)

        return jsonify({'success': True, 'video_url': blob_result.url, 'pathname': blob_result.pathname})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== AGNES IMAGE-TO-VIDEO ====================
@app.route('/api/image-to-video', methods=['POST'])
def image_to_video_agnes():
    data = request.json
    prompt = data.get('prompt')
    image_url = data.get('image_url')

    if not prompt or not image_url:
        return jsonify({'error': 'Prompt & image_url wajib'}), 400

    agnes_api_key = os.environ.get('AGNES_API_KEY')
    if not agnes_api_key:
        return jsonify({'error': 'AGNES_API_KEY belum di-set'}), 500

    try:
        submit_url = "https://apihub.agnes-ai.com/v1/videos"
        payload = {
            "model": "agnes-video-v2.0", "prompt": prompt,
            "height": 768, "width": 1152,
            "num_frames": 121, "frame_rate": 24,
            "image": image_url
        }
        headers = {"Authorization": f"Bearer {agnes_api_key}", "Content-Type": "application/json"}

        res_data = None
        for attempt in range(3):
            try:
                res = req_lib.post(submit_url, headers=headers, json=payload, timeout=(30, 180))
                res_data = res.json()
                break
            except req_lib.exceptions.ReadTimeout:
                if attempt < 2: time.sleep(5)
            except Exception:
                break

        if not res_data:
            return jsonify({'error': 'Gagal submit ke Agnes (timeout)'}), 500

        video_id = res_data.get('video_id') or res_data.get('id') or res_data.get('task_id')
        if not video_id:
            return jsonify({'error': f'Gagal submit: {res_data}'}), 500

        result_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}"
        result_data = None
        for i in range(60):
            time.sleep(10)
            try:
                r = req_lib.get(result_url, headers={"Authorization": f"Bearer {agnes_api_key}"}, timeout=(10, 60))
                result_data = r.json()
                status = result_data.get('status') or result_data.get('data', {}).get('status')
                if status == 'completed': break
                elif status == 'failed': return jsonify({'error': 'Agnes gagal'}), 500
            except req_lib.exceptions.ReadTimeout:
                continue
            except: pass

        if not result_data or result_data.get('status') != 'completed':
            return jsonify({'error': 'Timeout'}), 500

        output_url = (result_data.get('video_url') or result_data.get('url') or
                      result_data.get('data', {}).get('video_url') or
                      result_data.get('data', {}).get('url'))
        if not output_url:
            return jsonify({'error': 'URL tidak ditemukan'}), 500

        video_dl = req_lib.get(output_url, timeout=(30, 300))
        filename = f"ai_edit_{int(time.time())}.mp4"
        blob_result = put(filename, video_dl.content, access='public', multipart=True)

        return jsonify({'success': True, 'video_url': blob_result.url, 'pathname': blob_result.pathname})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== AGNES IMAGE EDITING ====================
@app.route('/api/edit-image', methods=['POST'])
def edit_image_agnes():
    data = request.json
    prompt = data.get('prompt')
    image_url = data.get('image_url')

    if not prompt or not image_url:
        return jsonify({'error': 'Prompt & image_url wajib'}), 400

    agnes_api_key = os.environ.get('AGNES_API_KEY')
    if not agnes_api_key:
        return jsonify({'error': 'AGNES_API_KEY belum di-set'}), 500

    try:
        url = "https://apihub.agnes-ai.com/v1/images/generations"
        payload = {
            "model": "agnes-image-2.1-flash", "prompt": prompt,
            "size": "1024x1024",
            "extra_body": {"image": [image_url], "response_format": "url"}
        }
        headers = {"Authorization": f"Bearer {agnes_api_key}", "Content-Type": "application/json"}

        res = req_lib.post(url, headers=headers, json=payload, timeout=(30, 180))
        res_data = res.json()
        if not res.ok:
            return jsonify({'error': f'Agnes error: {res_data}'}), 500

        image_result_url = None
        if res_data.get('data') and len(res_data['data']) > 0:
            image_result_url = res_data['data'][0].get('url')
        if not image_result_url:
            return jsonify({'error': 'URL gambar tidak ditemukan'}), 500

        img_dl = req_lib.get(image_result_url, timeout=(30, 120))
        filename = f"ai_edit_image_{int(time.time())}.png"
        blob_result = put(filename, img_dl.content, access='public', multipart=True)

        return jsonify({'success': True, 'image_url': blob_result.url, 'pathname': blob_result.pathname})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== PROMPT SCHEDULE CRUD ====================
@app.route('/api/prompt-schedules/list', methods=['GET'])
def list_prompt_schedules():
    try:
        init_tables()
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM prompt_schedules ORDER BY created_at DESC")
        rows = cur.fetchall()
        cur.close(); conn.close()

        items = []
        for r in rows:
            items.append({
                'id': r['id'],
                'title': r['title'] or '',
                'prompt': r['prompt'] or '',
                'source_type': r['source_type'] or 'text',
                'source_image_url': r['source_image_url'] or '',
                'repeat_type': r['repeat_type'] or 'once',
                'repeat_interval_min': r['repeat_interval_min'] or 0,
                'time': r['time'] or '',
                'day_of_week': r['day_of_week'],
                'day_of_month': r['day_of_month'],
                'next_run_at': r['next_run_at'].isoformat() if r.get('next_run_at') else '',
                'last_run_at': r['last_run_at'].isoformat() if r.get('last_run_at') else '',
                'is_active': bool(r['is_active']),
                'auto_post': bool(r['auto_post']),
                'platform': r['platform'] or '',
                'account': r['account'] or '',
                'caption': r['caption'] or '',
                'post_delay_min': r['post_delay_min'] or 5,
                'posted_schedule_id': r['posted_schedule_id'],
                'last_result': r.get('last_result') or '',
                'created_at': r['created_at'].isoformat() if r.get('created_at') else ''
            })
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/prompt-schedules/add', methods=['POST'])
def add_prompt_schedule():
    try:
        data = request.json
        init_tables()
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500

        prompt = (data.get('prompt') or '').strip()
        if not prompt:
            return jsonify({'error': 'Prompt wajib diisi'}), 400

        repeat_type = (data.get('repeat_type') or 'once').lower()

        sched_for_calc = {
            'repeat_type': repeat_type,
            'repeat_interval_min': int(data.get('repeat_interval_min') or 0),
            'time': data.get('time') or '',
            'day_of_week': data.get('day_of_week'),
            'day_of_month': data.get('day_of_month'),
        }

        now = datetime.now(WIB).replace(tzinfo=None)

        if repeat_type == 'once':
            start_at = data.get('start_at') or ''
            if start_at:
                try:
                    next_run = datetime.strptime(start_at, '%Y-%m-%dT%H:%M')
                except:
                    next_run = now + timedelta(minutes=5)
            else:
                next_run = now + timedelta(minutes=5)
        elif repeat_type == 'interval':
            minutes = int(data.get('repeat_interval_min') or 60)
            next_run = now + timedelta(minutes=minutes)
        else:
            next_run = calculate_next_run(sched_for_calc, now)
            if not next_run:
                next_run = now + timedelta(days=1)

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO prompt_schedules
            (title, prompt, source_type, source_image_url,
             repeat_type, repeat_interval_min, time, day_of_week, day_of_month,
             next_run_at, is_active, auto_post, platform, account, caption, post_delay_min)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('title', ''),
            prompt,
            data.get('source_type', 'text'),
            data.get('source_image_url', ''),
            repeat_type,
            int(data.get('repeat_interval_min') or 0),
            data.get('time', ''),
            data.get('day_of_week'),
            data.get('day_of_month'),
            next_run,
            bool(data.get('is_active', True)),
            bool(data.get('auto_post', True)),
            data.get('platform', ''),
            data.get('account', ''),
            data.get('caption', ''),
            int(data.get('post_delay_min') or 5)
        ))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True, 'id': new_id, 'next_run_at': next_run.isoformat()})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/prompt-schedules/update', methods=['POST'])
def update_prompt_schedule():
    try:
        data = request.json
        pid = data.get('id')
        if not pid: return jsonify({'error': 'ID wajib'}), 400
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500

        fields = []
        values = []
        allowed = ['title','prompt','source_type','source_image_url','repeat_type',
                   'repeat_interval_min','time','day_of_week','day_of_month',
                   'is_active','auto_post','platform','account','caption','post_delay_min']
        for f in allowed:
            if f in data:
                fields.append(f"{f} = %s")
                values.append(data[f])

        if not fields:
            return jsonify({'error': 'Tidak ada field yang diupdate'}), 400

        if any(k in data for k in ['repeat_type','repeat_interval_min','time','day_of_week','day_of_month']):
            cur_get = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur_get.execute("SELECT * FROM prompt_schedules WHERE id = %s", (pid,))
            row = cur_get.fetchone()
            cur_get.close()
            if row:
                merged = dict(row)
                merged.update(data)
                now = datetime.now(WIB).replace(tzinfo=None)
                if merged.get('repeat_type') == 'interval':
                    next_run = now + timedelta(minutes=int(merged.get('repeat_interval_min') or 60))
                else:
                    next_run = calculate_next_run(merged, now)
                if next_run:
                    fields.append("next_run_at = %s")
                    values.append(next_run)

        values.append(pid)
        cur = conn.cursor()
        cur.execute(f"UPDATE prompt_schedules SET {', '.join(fields)} WHERE id = %s", values)
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/prompt-schedules/toggle', methods=['POST'])
def toggle_prompt_schedule():
    try:
        data = request.json
        pid = data.get('id')
        if not pid: return jsonify({'error': 'ID wajib'}), 400
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM prompt_schedules WHERE id = %s", (pid,))
        row = cur.fetchone()
        cur.close()
        if not row:
            conn.close()
            return jsonify({'error': 'Tidak ditemukan'}), 404

        new_active = not bool(row['is_active'])
        next_run = row.get('next_run_at')

        now = datetime.now(WIB).replace(tzinfo=None)
        if new_active and (not next_run or next_run < now):
            if row['repeat_type'] == 'interval':
                next_run = now + timedelta(minutes=int(row['repeat_interval_min'] or 60))
            else:
                next_run = calculate_next_run(row, now) or (now + timedelta(minutes=5))

        cur2 = conn.cursor()
        cur2.execute("UPDATE prompt_schedules SET is_active = %s, next_run_at = %s WHERE id = %s",
                     (new_active, next_run, pid))
        conn.commit(); cur2.close(); conn.close()
        return jsonify({'success': True, 'is_active': new_active})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/prompt-schedules/delete', methods=['POST'])
def delete_prompt_schedule():
    try:
        pid = request.json.get('id')
        if not pid: return jsonify({'error': 'ID wajib'}), 400
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("DELETE FROM prompt_schedules WHERE id = %s", (pid,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/prompt-schedules/run-now', methods=['POST'])
def run_prompt_schedule_now():
    try:
        pid = request.json.get('id')
        if not pid: return jsonify({'error': 'ID wajib'}), 400
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM prompt_schedules WHERE id = %s", (pid,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return jsonify({'error': 'Tidak ditemukan'}), 404

        result = execute_prompt_schedule(row)

        try:
            conn2 = get_db()
            if conn2:
                cur2 = conn2.cursor()
                cur2.execute("UPDATE prompt_schedules SET last_run_at = NOW(), last_result = %s WHERE id = %s",
                             (json.dumps(result)[:4000], pid))
                conn2.commit(); cur2.close(); conn2.close()
        except: pass

        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== USER MANAGEMENT ====================
@app.route('/api/users/has-any', methods=['GET'])
def has_any_user():
    try:
        init_tables()
        conn = get_db()
        if not conn:
            return jsonify({'success': True, 'has_users': False, 'count': 0, 'db_available': False})
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        cur.close(); conn.close()
        return jsonify({'success': True, 'has_users': count > 0, 'count': count, 'db_available': True})
    except:
        return jsonify({'success': True, 'has_users': False, 'count': 0, 'db_available': False})


@app.route('/api/users/list', methods=['GET'])
def list_users():
    try:
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT username, created_at FROM users ORDER BY created_at ASC")
        rows = cur.fetchall()
        cur.close(); conn.close()
        users = [{'username': r['username'], 'createdAt': r['created_at'].isoformat() if r['created_at'] else ''} for r in rows]
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/add', methods=['POST'])
def add_user():
    try:
        data = request.json
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        if not username or len(username) < 3:
            return jsonify({'error': 'Username minimal 3 karakter'}), 400
        if not password or len(password) < 4:
            return jsonify({'error': 'Password minimal 4 karakter'}), 400

        init_tables()
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500

        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({'error': 'Username sudah digunakan'}), 400

        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (username, password_hash))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True, 'message': 'User berhasil ditambahkan'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/login', methods=['POST'])
def login_user():
    try:
        data = request.json
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        if not username or not password:
            return jsonify({'error': 'Username & password wajib diisi'}), 400

        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT username, password_hash FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        row = cur.fetchone()
        cur.close(); conn.close()

        if not row: return jsonify({'error': 'Username atau password salah'}), 401
        if not bcrypt.checkpw(password.encode('utf-8'), row['password_hash'].encode('utf-8')):
            return jsonify({'error': 'Username atau password salah'}), 401

        return jsonify({'success': True, 'user': {'username': row['username']}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/delete', methods=['POST'])
def delete_user():
    try:
        data = request.json
        username = (data.get('username') or '').strip()
        if not username: return jsonify({'error': 'Username wajib'}), 400
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== SCHEDULE ====================
@app.route('/api/schedules/list', methods=['GET'])
def list_schedules():
    try:
        init_tables()
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM schedules ORDER BY time ASC")
        rows = cur.fetchall()
        cur.close(); conn.close()
        schedules = []
        for r in rows:
            try: videos = json.loads(r.get('videos_json') or '[]')
            except: videos = []
            try: images = json.loads(r.get('images_json') or '[]')
            except: images = []
            schedules.append({
                'id': r['id'], 'session_id': r['session_id'] or '',
                'video_name': r['video_name'] or '', 'video_url': r['video_url'] or '',
                'videos': videos, 'images': images,
                'time': r['time'] or '', 'platform': r['platform'] or '',
                'account': r['account'] or '', 'caption': r['caption'] or '',
                'status': r.get('status') or 'pending',
                'posted_at': r['posted_at'].isoformat() if r.get('posted_at') else '',
                'post_result': r.get('post_result') or ''
            })
        return jsonify({'success': True, 'schedules': schedules})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedules/add', methods=['POST'])
def add_schedule():
    try:
        data = request.json
        init_tables()
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500

        videos = data.get('videos', [])
        images = data.get('images', [])

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO schedules 
            (session_id, video_name, video_url, videos_json, images_json, time, platform, account, caption, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') RETURNING id
        """, (
            data.get('session_id', ''),
            videos[0]['name'] if videos else '',
            videos[0]['url'] if videos else '',
            json.dumps(videos), json.dumps(images),
            data.get('time', ''), data.get('platform', ''),
            data.get('account', ''), data.get('caption', '')
        ))
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedules/delete', methods=['POST'])
def delete_schedule():
    try:
        data = request.json
        schedule_id = data.get('id')
        if not schedule_id: return jsonify({'error': 'ID wajib'}), 400
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== POSTING HISTORY ====================
@app.route('/api/posting-history', methods=['GET'])
def get_posting_history():
    try:
        limit = int(request.args.get('limit', 100))
        conn = get_db()
        if not conn:
            return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, schedule_id, media_url, media_name, platform, account,
                   media_id, container_id, posted_at, source
            FROM posting_history
            ORDER BY posted_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close(); conn.close()

        items = [{
            'id': r['id'],
            'schedule_id': r['schedule_id'],
            'media_url': r['media_url'],
            'media_name': r['media_name'] or '',
            'platform': r['platform'] or '',
            'account': r['account'] or '',
            'media_id': r['media_id'] or '',
            'posted_at': r['posted_at'].isoformat() if r['posted_at'] else '',
            'source': r['source'] or ''
        } for r in rows]
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== MEDSOS ====================
@app.route('/api/medsos/list', methods=['GET'])
def list_medsos():
    try:
        init_tables()
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT platform, username, connected_at FROM medsos_accounts ORDER BY connected_at ASC")
        rows = cur.fetchall()
        cur.close(); conn.close()
        accounts = [{'platform': r['platform'], 'username': r['username'],
                     'connectedAt': r['connected_at'].isoformat() if r['connected_at'] else ''} for r in rows]
        return jsonify({'success': True, 'accounts': accounts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/medsos/add', methods=['POST'])
def add_medsos():
    try:
        data = request.json
        platform = data.get('platform', '')
        username = data.get('username', '')
        if not platform or not username:
            return jsonify({'error': 'Data tidak lengkap'}), 400
        init_tables()
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("SELECT id FROM medsos_accounts WHERE platform = %s AND LOWER(username) = LOWER(%s)",
                    (platform, username))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({'error': 'Akun sudah terhubung'}), 400
        cur.execute("INSERT INTO medsos_accounts (platform, username) VALUES (%s, %s)", (platform, username))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/medsos/delete', methods=['POST'])
def delete_medsos():
    try:
        data = request.json
        platform = data.get('platform', '')
        username = data.get('username', '')
        if not platform or not username:
            return jsonify({'error': 'Data tidak lengkap'}), 400
        conn = get_db()
        if not conn: return jsonify({'error': 'DB tidak tersedia'}), 500
        cur = conn.cursor()
        cur.execute("DELETE FROM medsos_accounts WHERE platform = %s AND LOWER(username) = LOWER(%s)",
                    (platform, username))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== BLOB UPLOAD ====================
@app.route('/api/upload-media', methods=['POST'])
def upload_media():
    try:
        file = request.files.get('file')
        if not file or file.filename == '':
            return jsonify({'error': 'Tidak ada file'}), 400

        ext = file.filename.lower().rsplit('.', 1)[-1] if '.' in file.filename else ''
        VIDEO_EXT = ['mp4', 'webm', 'mov', 'avi', 'mkv']
        IMAGE_EXT = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']

        if ext in VIDEO_EXT:
            media_type = 'video'; default_ext = 'mp4'
        elif ext in IMAGE_EXT:
            media_type = 'image'; default_ext = 'jpg'
        else:
            return jsonify({'error': f'Tipe tidak didukung: .{ext}'}), 400

        safe_name = sanitize_filename(file.filename, default_ext)
        file_content = file.read()
        result = put(safe_name, file_content, access='public', multipart=True)
        return jsonify({'success': True, 'url': result.url, 'pathname': result.pathname,
                        'filename': safe_name, 'size': len(file_content), 'type': media_type})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload-chunk', methods=['POST'])
def upload_chunk():
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'Tidak ada video'}), 400
        file = request.files['video']
        session_id = request.form.get('session_id', 'default')
        chunk_index = int(request.form.get('chunk_index', 0))
        safe_session_id = re.sub(r'[^\w]', '_', str(session_id))[:30] or 'session'
        file_content = file.read()
        chunk_filename = f"cut_{safe_session_id}_{chunk_index:03d}.webm"
        result = put(chunk_filename, file_content, access='public', multipart=True)
        return jsonify({'success': True, 'url': result.url, 'pathname': result.pathname,
                        'chunk_index': chunk_index, 'size': len(file_content)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/finalize-cut', methods=['POST'])
def finalize_cut():
    return jsonify({'success': True})


# ==================== LIST FILES ====================
@app.route('/api/list-files-grouped', methods=['GET'])
def list_files_grouped():
    try:
        files = vercel_blob.list()
        sessions = {}
        videos_raw = []
        images_raw = []
        video_ai_edits = {}
        image_ai_edits = []

        VIDEO_EXT = ['mp4', 'webm', 'mov', 'avi', 'mkv']
        IMAGE_EXT = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']

        for item in files.get('blobs', []):
            pathname = item.get('pathname', '')
            url = item.get('url', '')
            size = item.get('size', 0)
            uploaded_at = item.get('uploadedAt', '')
            ext = pathname.lower().rsplit('.', 1)[-1] if '.' in pathname else ''
            is_video = ext in VIDEO_EXT
            is_image = ext in IMAGE_EXT

            if pathname.startswith('ai_edit_image_'):
                sid = pathname.replace('ai_edit_image_', '').rsplit('.', 1)[0]
                image_ai_edits.append({'pathname': pathname, 'url': url, 'size': size,
                                       'uploadedAt': uploaded_at, 'session_id': sid})
                continue

            if pathname.startswith('ai_edit_') and is_video:
                sid = pathname.replace('ai_edit_', '').rsplit('.', 1)[0]
                video_ai_edits[sid] = {'pathname': pathname, 'url': url, 'size': size, 'uploadedAt': uploaded_at}
                continue

            if pathname.startswith('cut_') and is_video:
                parts = pathname.rsplit('.', 1)[0].split('_')
                if len(parts) >= 3:
                    sid = parts[1]
                    try: idx = int(parts[2])
                    except: continue
                    if sid not in sessions:
                        sessions[sid] = {'session_id': sid, 'chunks': [], 'total_size': 0, 'total_chunks': 0}
                    sessions[sid]['chunks'].append({'pathname': pathname, 'url': url, 'size': size, 'index': idx})
                    sessions[sid]['total_size'] += size
                    sessions[sid]['total_chunks'] += 1
                continue

            if is_video:
                videos_raw.append({'pathname': pathname, 'url': url, 'size': size,
                                   'is_single': True, 'file_type': 'video', 'uploadedAt': uploaded_at})
            elif is_image:
                images_raw.append({'pathname': pathname, 'url': url, 'size': size,
                                   'is_single': True, 'file_type': 'image', 'uploadedAt': uploaded_at})

        for sid in sessions:
            sessions[sid]['chunks'].sort(key=lambda x: x['index'])

        videos_final = []
        for sid, s in sessions.items():
            has_ai = sid in video_ai_edits
            videos_final.append({
                'pathname': f"video_utuh_{sid}.webm", 'url': s['chunks'][0]['url'],
                'size': s['total_size'], 'is_single': False, 'session_id': sid,
                'chunks': s['chunks'], 'total_chunks': s['total_chunks'],
                'file_type': 'video', 'ai_status': 'done' if has_ai else 'idle',
                'ai_result': video_ai_edits.get(sid), 'uploadedAt': s['chunks'][0].get('uploadedAt', '')
            })
        videos_final.extend(videos_raw)

        videos_ai = []
        for sid, ai in video_ai_edits.items():
            videos_ai.append({'pathname': ai['pathname'], 'url': ai['url'], 'size': ai['size'],
                              'is_single': True, 'session_id': sid, 'file_type': 'video',
                              'is_ai': True, 'uploadedAt': ai['uploadedAt']})

        images_ai = []
        for ai in image_ai_edits:
            images_ai.append({'pathname': ai['pathname'], 'url': ai['url'], 'size': ai['size'],
                              'is_single': True, 'session_id': ai['session_id'], 'file_type': 'image',
                              'is_ai': True, 'uploadedAt': ai['uploadedAt']})

        return jsonify({'success': True, 'videos_raw': videos_final, 'videos_ai': videos_ai,
                        'images_raw': images_raw, 'images_ai': images_ai})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete-file', methods=['POST'])
def delete_file():
    try:
        url = request.json.get('url')
        if not url: return jsonify({'error': 'URL tidak diberikan'}), 400
        vercel_blob.delete(url)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rename-file', methods=['POST'])
def rename_file():
    try:
        data = request.json
        old_url = data.get('old_url')
        old_pathname = data.get('old_pathname')
        new_name = data.get('new_name', '').strip()
        if not old_url or not old_pathname or not new_name:
            return jsonify({'error': 'Data tidak lengkap'}), 400

        orig_ext = old_pathname.rsplit('.', 1)[-1] if '.' in old_pathname else 'mp4'
        safe_name = sanitize_filename(new_name, orig_ext)
        if not safe_name.endswith(f'.{orig_ext}'):
            safe_name = f"{safe_name}.{orig_ext}"
        if safe_name == old_pathname:
            return jsonify({'success': True})

        resp = req_lib.get(old_url, timeout=(30, 300))
        if resp.status_code != 200:
            return jsonify({'error': 'Gagal download'}), 500
        result = put(safe_name, resp.content, access='public', multipart=True)
        vercel_blob.delete(old_url)
        return jsonify({'success': True, 'new_url': result.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    init_tables()
    return render_template('index.html')


if __name__ == '__main__':
    app.run(debug=True)