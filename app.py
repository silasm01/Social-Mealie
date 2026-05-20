from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for, send_file
import base64
from openai import OpenAI
import os
from urllib.parse import urljoin
import cv2
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pyktok as pk
import pandas as pd
import subprocess
import json
import tempfile
import shutil
import uuid
import re
import instaloader
from dotenv import load_dotenv
import sys
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
import secrets
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Mealie configuration
MEALIE_URL = os.getenv("MEALIE_URL", "http://localhost:9925")
MEALIE_TOKEN = os.getenv("MEALIE_TOKEN", "")

# Output language configuration
OUTPUT_LANGUAGE = os.getenv("OUTPUT_LANGUAGE", "English")

# Database configuration
DB_PATH = os.path.abspath(os.path.join('instance', 'users.db'))

# Initialize database
def init_db():
    """Initialize the user database"""
    os.makedirs('instance', exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            pin TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 10,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1
        )
    ''')
    
    # Usage tracking table
    c.execute('''
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            status TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Token and cost tracking table
    c.execute('''
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL UNIQUE,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0.0,
            model_breakdown TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def create_user(name, pin, is_admin=False, daily_limit=10):
    """Create a new user"""
    conn = get_db()
    try:
        hashed_pin = generate_password_hash(pin)
        conn.execute(
            'INSERT INTO users (name, pin, is_admin, daily_limit) VALUES (?, ?, ?, ?)',
            (name, hashed_pin, 1 if is_admin else 0, daily_limit)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def verify_pin(name, pin):
    """Verify user credentials"""
    conn = get_db()
    user = conn.execute(
        'SELECT * FROM users WHERE name = ? AND active = 1',
        (name,)
    ).fetchone()
    conn.close()
    
    if user and check_password_hash(user['pin'], pin):
        return dict(user)
    return None

def get_usage_today(user_id):
    """Get usage count for today"""
    conn = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    count = conn.execute(
        "SELECT COUNT(*) as count FROM usage WHERE user_id = ? AND DATE(timestamp) = ?",
        (user_id, today)
    ).fetchone()['count']
    conn.close()
    return count

def log_usage(user_id, url, status='success'):
    """Log a usage entry"""
    conn = get_db()
    conn.execute(
        'INSERT INTO usage (user_id, url, status) VALUES (?, ?, ?)',
        (user_id, url, status)
    )
    conn.commit()
    conn.close()

# Currency configuration
USD_TO_DKK = 6.8  # Exchange rate

# Token and cost pricing configuration (as of January 2024, converted to DKK)
TOKEN_PRICING = {
    "gpt-4": {"input": 0.03 * USD_TO_DKK, "output": 0.06 * USD_TO_DKK},  # per 1K tokens
    "gpt-4.1-mini": {"input": 0.00015 * USD_TO_DKK, "output": 0.0006 * USD_TO_DKK},  # per 1K tokens
    "gpt-4.1": {"input": 0.005 * USD_TO_DKK, "output": 0.015 * USD_TO_DKK},  # per 1K tokens
    "gpt-4-turbo": {"input": 0.01 * USD_TO_DKK, "output": 0.03 * USD_TO_DKK},  # per 1K tokens
    "gpt-4o": {"input": 0.005 * USD_TO_DKK, "output": 0.015 * USD_TO_DKK},  # per 1K tokens
    "whisper-1": {"input": 0.02 * USD_TO_DKK}  # per minute of audio
}

def calculate_cost(model, input_tokens, output_tokens=0):
    """Calculate cost based on model and tokens used"""
    if model not in TOKEN_PRICING:
        return 0.0
    
    pricing = TOKEN_PRICING[model]
    input_cost = (input_tokens / 1000) * pricing.get("input", 0)
    output_cost = (output_tokens / 1000) * pricing.get("output", 0)
    
    return round(input_cost + output_cost, 6)

def log_token_usage(user_id, request_id, model, input_tokens, output_tokens):
    """Log token usage and calculate cost"""
    total_tokens = input_tokens + output_tokens
    cost = calculate_cost(model, input_tokens, output_tokens)
    
    conn = get_db()
    conn.execute(
        '''INSERT INTO token_usage (user_id, request_id, model, input_tokens, output_tokens, total_tokens, cost)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (user_id, request_id, model, input_tokens, output_tokens, total_tokens, cost)
    )
    conn.commit()
    conn.close()
    
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost": cost,
        "model": model
    }

def log_video_token_usage(user_id, request_id, token_data):
    """Log token usage for a complete video - stores one row per video with model breakdown as JSON.
    
    token_data should be a dict with model names as keys and {'input': X, 'output': Y} as values
    Example: {'gpt-4.1-mini': {'input': 1500, 'output': 250}, 'whisper-1': {'input': 60, 'output': 0}}
    """
    conn = get_db()
    
    total_input = 0
    total_output = 0
    total_cost = 0.0
    
    # Build model breakdown
    model_breakdown = {}
    for model, tokens in token_data.items():
        input_tokens = tokens.get('input', 0)
        output_tokens = tokens.get('output', 0)
        total_tokens_for_model = input_tokens + output_tokens
        cost = calculate_cost(model, input_tokens, output_tokens)
        
        model_breakdown[model] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens_for_model,
            "cost": round(cost, 6)
        }
        
        total_input += input_tokens
        total_output += output_tokens
        total_cost += cost
    
    total_tokens = total_input + total_output
    
    # Store one row per video with model breakdown as JSON
    try:
        conn.execute(
            '''INSERT OR REPLACE INTO token_usage (user_id, request_id, input_tokens, output_tokens, total_tokens, cost, model_breakdown)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, request_id, total_input, total_output, total_tokens, round(total_cost, 6), json.dumps(model_breakdown))
        )
    except sqlite3.IntegrityError:
        # If request_id already exists, update it
        conn.execute(
            '''UPDATE token_usage SET input_tokens = ?, output_tokens = ?, total_tokens = ?, cost = ?, model_breakdown = ?
               WHERE request_id = ? AND user_id = ?''',
            (total_input, total_output, total_tokens, round(total_cost, 6), json.dumps(model_breakdown), request_id, user_id)
        )
    
    conn.commit()
    conn.close()
    
    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 6),
        "model_breakdown": model_breakdown
    }

def get_request_cost_and_tokens(user_id, request_id):
    """Get total cost and tokens for a specific request"""
    conn = get_db()
    result = conn.execute(
        '''SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
                  SUM(total_tokens) as total_tokens, SUM(cost) as total_cost
           FROM token_usage WHERE user_id = ? AND request_id = ?''',
        (user_id, request_id)
    ).fetchone()
    conn.close()
    
    return {
        "input_tokens": result['input_tokens'] or 0,
        "output_tokens": result['output_tokens'] or 0,
        "total_tokens": result['total_tokens'] or 0,
        "total_cost": round(result['total_cost'] or 0, 6)
    }

def get_user_cost_summary(user_id, days=30):
    """Get cost summary for a user over the last N days"""
    conn = get_db()
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    result = conn.execute(
        '''SELECT SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
                  SUM(total_tokens) as total_tokens, SUM(cost) as total_cost, COUNT(*) as total_requests
           FROM token_usage WHERE user_id = ? AND DATE(timestamp) >= ?''',
        (user_id, cutoff_date)
    ).fetchone()
    conn.close()
    
    return {
        "input_tokens": result['input_tokens'] or 0,
        "output_tokens": result['output_tokens'] or 0,
        "total_tokens": result['total_tokens'] or 0,
        "total_cost": round(result['total_cost'] or 0, 6),
        "total_requests": result['total_requests'] or 0
    }

def get_video_cost(user_id, request_id):
    """Get total cost for a specific video/request"""
    conn = get_db()
    result = conn.execute(
        '''SELECT SUM(cost) as total_cost, SUM(total_tokens) as total_tokens,
                  GROUP_CONCAT(DISTINCT model) as models_used
           FROM token_usage WHERE user_id = ? AND request_id = ?''',
        (user_id, request_id)
    ).fetchone()
    conn.close()
    
    return {
        "total_cost": round(result['total_cost'] or 0, 6),
        "total_tokens": result['total_tokens'] or 0,
        "models_used": result['models_used'] or ""
    }

def get_all_videos_with_costs(user_id):
    """Get all processed videos with their costs"""
    conn = get_db()
    results = conn.execute(
        '''SELECT DISTINCT tu.request_id, u.url, MAX(tu.timestamp) as timestamp,
                  SUM(tu.cost) as total_cost, SUM(tu.total_tokens) as total_tokens,
                  GROUP_CONCAT(DISTINCT tu.model) as models_used, COUNT(DISTINCT tu.model) as model_count
           FROM token_usage tu
           JOIN usage u ON u.user_id = tu.user_id
           WHERE tu.user_id = ?
           GROUP BY tu.request_id
           ORDER BY tu.timestamp DESC''',
        (user_id,)
    ).fetchall()
    conn.close()
    
    return [dict(r) for r in results]

def login_required(f):
    """Decorator to require login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"status": "error", "message": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

# Initialize database on startup
init_db()

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def detect_platform(url):
    """Detect if URL is from TikTok or Instagram"""
    if 'tiktok.com' in url:
        return 'tiktok'
    elif 'instagram.com' in url:
        return 'instagram'
    else:
        return 'unknown'

"""Persistent frame storage for used frames."""
FRAME_STORAGE_DIR = os.path.abspath(os.path.join('instance', 'frames'))
os.makedirs(FRAME_STORAGE_DIR, exist_ok=True)

# Keep temp frame directories for active video uploads so Mealie can fetch frame URLs while processing
active_frame_dirs = {}

def get_frame_references(obj):
    """Return a set of referenced frame names found in recipe JSON."""
    refs = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'image' and isinstance(value, str):
                match = re.match(r'^frame_(\d+)(?:\.jpg)?$', value.strip())
                if match:
                    refs.add(match.group(0))
            else:
                refs.update(get_frame_references(value))
    elif isinstance(obj, list):
        for item in obj:
            refs.update(get_frame_references(item))
    return refs

def persist_frame_files(video_id, temp_dir, frame_refs):
    """Persist only the referenced frame image files for later access."""
    if not frame_refs:
        return

    dest_dir = os.path.join(FRAME_STORAGE_DIR, video_id)
    os.makedirs(dest_dir, exist_ok=True)

    for frame_name in frame_refs:
        source_path = os.path.join(temp_dir, f"{frame_name}.jpg")
        dest_path = os.path.join(dest_dir, f"{frame_name}.jpg")
        if os.path.exists(source_path) and not os.path.exists(dest_path):
            shutil.copy2(source_path, dest_path)


def sanitize_recipe_images(obj):
    """Remove any invalid image values so Mealie only sees valid URLs."""
    if isinstance(obj, dict):
        if 'image' in obj:
            image_value = obj['image']
            if not isinstance(image_value, str) or not re.match(r'^https?://', image_value.strip()):
                obj.pop('image', None)
        for value in obj.values():
            sanitize_recipe_images(value)
    elif isinstance(obj, list):
        for item in obj:
            sanitize_recipe_images(item)


def find_first_valid_image_url(obj):
    """Return the first valid http(s) image URL found in a recipe object."""
    if isinstance(obj, dict):
        if 'image' in obj and isinstance(obj['image'], str) and re.match(r'^https?://', obj['image'].strip()):
            return obj['image'].strip()
        for value in obj.values():
            result = find_first_valid_image_url(value)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_first_valid_image_url(item)
            if result:
                return result
    return None


def replace_frame_image_references(obj, video_id, base_url):
    """Replace frame_<n> markers in recipe JSON with actual served image URLs."""
    if not base_url:
        raise ValueError("frame_server_base_url is required")

    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if key == 'image' and isinstance(value, str):
                match = re.match(r'^frame_(\d+)(?:\.jpg)?$', value.strip())
                if match:
                    frame_name = match.group(0)
                    obj[key] = urljoin(base_url, f"frame/{video_id}/{frame_name}.jpg")
            else:
                replace_frame_image_references(value, video_id, base_url)
    elif isinstance(obj, list):
        for item in obj:
            replace_frame_image_references(item, video_id, base_url)

@app.route('/frame/<video_id>/<frame_name>.jpg')
def serve_frame(video_id, frame_name):
    """Serve saved extracted frame images while the recipe is being uploaded."""
    temp_dir = active_frame_dirs.get(video_id)
    if temp_dir:
        temp_path = os.path.join(temp_dir, f"{frame_name}.jpg")
        if os.path.exists(temp_path):
            return send_file(temp_path, mimetype='image/jpeg')

    file_path = os.path.join(FRAME_STORAGE_DIR, video_id, f"{frame_name}.jpg")
    if not os.path.exists(file_path):
        return jsonify({'error': 'Frame not found'}), 404

    return send_file(file_path, mimetype='image/jpeg')

def download_instagram_reel(url, temp_dir):
    """Download Instagram Reel and return video path and caption"""
    L = instaloader.Instaloader(dirname_pattern=temp_dir)
    
    # Extract shortcode from URL
    shortcode_match = re.search(r'/reels/([^/?]+)', url)
    if not shortcode_match:
        shortcode_match = re.search(r'/p/([^/?]+)', url)
    
    if not shortcode_match:
        raise ValueError("Could not extract shortcode from Instagram URL")
    
    shortcode = shortcode_match.group(1)
    
    # Download the post
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=temp_dir)
    
    # Find the downloaded video file
    video_file = None
    for file in os.listdir(temp_dir):
        if file.endswith('.mp4'):
            video_file = os.path.join(temp_dir, file)
            break
    
    if not video_file:
        raise ValueError("No video file found after download")
    
    # Get caption
    caption = post.caption if post.caption else ""
    
    return video_file, caption

def process_video(video_url, video_id, user_id, frame_server_base_url=None):
    """Process a TikTok or Instagram Reel video URL and return the recipe"""
    responses = []
    temp_dir = tempfile.mkdtemp()
    active_frame_dirs[video_id] = temp_dir
    
    # Accumulator for tokens used across all API calls
    token_accumulator = {}
    
    try:
        # Detect platform
        platform = detect_platform(video_url)
        
        if platform == 'unknown':
            yield {"video_id": video_id, "status": "error", "message": "Unsupported platform. Please use TikTok or Instagram Reels URLs."}
            return
        
        # Download video
        platform_name = "TikTok" if platform == 'tiktok' else "Instagram Reel"
        yield {"video_id": video_id, "status": "downloading", "message": f"Downloading {platform_name}..."}
        original_dir = os.getcwd()
        os.chdir(temp_dir)
        
        thumbnail_url = ""  # Initialize for both platforms
        
        if platform == 'tiktok':
            video_data = pk.save_tiktok(video_url, True, return_fns=True, metadata_fn="metadata.csv")
            print(video_data)
            video = video_data["video_fn"]
            extracted_comments = pd.read_csv("metadata.csv")["video_description"].to_list()[0]
            
            meta = requests.get(f"https://www.tiktok.com/oembed?url={video_url}").json()
            thumbnail_url = meta.get("thumbnail_url", "")
            print("Thumbnail URL:", thumbnail_url)
        else:  # Instagram
            video, extracted_comments = download_instagram_reel(video_url, temp_dir)
            # Instagram doesn't provide a direct thumbnail URL from the API
            thumbnail_url = ""
        
        yield {"video_id": video_id, "status": "processing", "message": f"Video downloaded: {os.path.basename(video)}"}
        yield {"video_id": video_id, "status": "processing", "message": f"Extracted description: {extracted_comments}"}
        
        # Check if description is complete
        yield {"video_id": video_id, "status": "analyzing", "message": "Analyzing video description..."}
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[{
                "role": "user",
                "content": f"""Given the following video description: {extracted_comments}, determine if it contains complete cooking instructions including ingredients and steps or if the video that is associated with it is needed to complete the instructions. 
                If it does, respond with 'COMPLETE'. If it is missing ingredients, steps, or is too vague, respond with 'INCOMPLETE'."""
            }]
        )
        
        # Accumulate token usage for description analysis
        if hasattr(response, 'usage'):
            if "gpt-4.1-mini" not in token_accumulator:
                token_accumulator["gpt-4.1-mini"] = {"input": 0, "output": 0}
            token_accumulator["gpt-4.1-mini"]["input"] += response.usage.input_tokens
            token_accumulator["gpt-4.1-mini"]["output"] += response.usage.output_tokens
        
        if "COMPLETE" == response.output_text:
            yield {"video_id": video_id, "status": "processing", "message": "Description contains complete instructions. Generating recipe..."}
            response = client.responses.create(
                model="gpt-4.1",
                input=[{
                    "role": "user",
                    "content": f"""Rewrite the following cooking instructions into a detailed recipe including ingredients, quantities, and step-by-step instructions. Also convert any non-standard units into standard units EU.
                    Description of the recipe: {extracted_comments}.
                    Be concise and factual."""
                }]
            )
            
            # Accumulate token usage for recipe generation
            if hasattr(response, 'usage'):
                if "gpt-4.1" not in token_accumulator:
                    token_accumulator["gpt-4.1"] = {"input": 0, "output": 0}
                token_accumulator["gpt-4.1"]["input"] += response.usage.input_tokens
                token_accumulator["gpt-4.1"]["output"] += response.usage.output_tokens
            
            yield {
                "video_id": video_id,
                "status": "complete",
                "message": "Recipe generated successfully!",
                "recipe": response.output_text
            }
            # Log accumulated tokens for this video
            if token_accumulator:
                log_video_token_usage(user_id, video_id, token_accumulator)
            os.chdir(original_dir)
            shutil.rmtree(temp_dir)
            return
        
        # Process video frames
        yield {"video_id": video_id, "status": "processing", "message": "Extracting video frames..."}
        cap = cv2.VideoCapture(video)
        fps = int(cap.get(cv2.CAP_PROP_FPS))
        frame_count = 0
        frames = []
        
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            
            if frame_count % fps == 0:
                frames.append(frame)
            
            frame_count += 1
        
        cap.release()
        yield {"video_id": video_id, "status": "processing", "message": f"Extracted {len(frames)} frames"}
        
        # Extract and transcribe audio
        yield {"video_id": video_id, "status": "processing", "message": "Extracting and transcribing audio..."}
        audio_path = os.path.join(temp_dir, "extracted_audio.wav")
        # Resolve ffmpeg executable at runtime to avoid FileNotFoundError
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise RuntimeError("ffmpeg not found. Install ffmpeg and add it to PATH: https://ffmpeg.org/download.html")

        subprocess.run([
            ffmpeg_path, '-i', video,
            '-vn',
            '-acodec', 'pcm_s16le',
            '-ar', '16000',
            '-ac', '1',
            audio_path
        ], check=True, capture_output=True)
        
        with open(audio_path, 'rb') as audio_file:
            audio_file.seek(0, 2)
            audio_size = audio_file.tell()
            audio_file.seek(0)
            
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en",
                timestamp_granularities=["segment"],
                response_format="verbose_json"
            )
        
        # Accumulate token usage for whisper (pricing is per minute)
        # Estimate duration from audio file size (16-bit PCM at 16kHz = 2 bytes per sample)
        audio_duration_minutes = max(1, audio_size / (2 * 16000 * 60))
        if "whisper-1" not in token_accumulator:
            token_accumulator["whisper-1"] = {"input": 0, "output": 0}
        token_accumulator["whisper-1"]["input"] += int(audio_duration_minutes * 60)
        
        segments_data = [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip()
            }
            for seg in transcription.segments
        ]
        
        yield {"video_id": video_id, "status": "processing", "message": f"Audio transcribed: {len(segments_data)} segments"}
        
        # Process frames
        yield {"video_id": video_id, "status": "processing", "message": "Analyzing video frames..."}
        
        def process_frame(i, frame):
            temp_path = os.path.join(temp_dir, f"frame_{i}.jpg")
            cv2.imwrite(temp_path, frame)
            base64_image = encode_image(temp_path)
            # os.remove(temp_path)
            
            response = client.responses.create(
                model="gpt-4.1-mini",
                input=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": """You are analyzing a cooking video frame.

                            Describe:
                            1. What cooking action is happening
                            2. Which ingredients are involved
                            3. Cooking method (heat, tool, motion)
                            4. Any implied step even if not spoken
                            5. At last say if any important visual clues such as caramalization or brownness of stuff is shown that is important to show the user. (Eg. salting og straight forward tasks like washing should not be included as they are straight forward)

                            Be concise and factual.
                            Also note any text present in the image.
                        """
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{base64_image}",
                            "detail": "high"
                        }
                    ]
                }]
            )
            
            # Accumulate token usage for frame analysis
            if hasattr(response, 'usage'):
                if "gpt-4.1-mini" not in token_accumulator:
                    token_accumulator["gpt-4.1-mini"] = {"input": 0, "output": 0}
                token_accumulator["gpt-4.1-mini"]["input"] += response.usage.input_tokens
                token_accumulator["gpt-4.1-mini"]["output"] += response.usage.output_tokens
            
            return (f"frame_{i}", response.output_text)
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(process_frame, i, frame): i for i, frame in enumerate(frames)}
            
            for future in as_completed(futures):
                responses.append(future.result())
                yield {"video_id": video_id, "status": "processing", "message": f"Analyzed {len(responses)}/{len(frames)} frames."}
        
        # Generate final recipe
        yield {"video_id": video_id, "status": "generating", "message": "Generating final recipe..."}
        response = client.responses.create(
            model="gpt-4.1",
            input=[{
                "role": "user",
                "content": f"""
Using the following information, generate a Schema.org Recipe JSON object.

INPUT DATA
-----------
Video description:
{extracted_comments}

Frame analyses:
{responses}

Audio transcription segments:
{json.dumps(segments_data, indent=2)}

REQUIREMENTS
------------
- Text should be in {OUTPUT_LANGUAGE}
- Output valid JSON only
- Use Schema.org Recipe
- Do not hallucinate information not in the inputs. Especially for ingredients and quantities and times that where not specified.
- Analyze whether a step would benefit from a image. This is passed by frame analysis. (IMPORTANT NOT EVERYTHING NEEDS AN IMAGE)
- Include:
  name, description, recipeIngredient, recipeInstructions, image if relevant for step (But at most one for each step)
- Use HowToStep for instructions
- Use ISO 8601 durations (PT#M)
- Do not include unsupported fields
- Do not include names in recipeInstructions

JSON ONLY. """
            }]
        )
        
        # Accumulate token usage for final recipe generation
        if hasattr(response, 'usage'):
            if "gpt-4.1" not in token_accumulator:
                token_accumulator["gpt-4.1"] = {"input": 0, "output": 0}
            token_accumulator["gpt-4.1"]["input"] += response.usage.input_tokens
            token_accumulator["gpt-4.1"]["output"] += response.usage.output_tokens
        
        recipe_json = response.output_text.replace("`", "").replace("json", "").replace("\n", "").replace("\\n", "").replace("\\\"", '"')
        
        if recipe_json.startswith('"') and recipe_json.endswith('"'):
            recipe_json = recipe_json[1:-1]
        
        recipe_json = json.loads(recipe_json)
        frame_refs = get_frame_references(recipe_json)
        replace_frame_image_references(recipe_json, video_id, frame_server_base_url)
        sanitize_recipe_images(recipe_json)
        if 'image' not in recipe_json or not recipe_json['image']:
            first_image = find_first_valid_image_url(recipe_json)
            if first_image:
                recipe_json['image'] = first_image
        persist_frame_files(video_id, temp_dir, frame_refs)
        
        print("Final Recipe JSON generated.")
        print(f"Attempting to upload to Mealie URL: {MEALIE_URL}")
        print(f"Using token: {MEALIE_TOKEN[:20]}..." if MEALIE_TOKEN else "No token set")
        
        # Send to external API
        api_response = "Recipe generated but not uploaded to Mealie"
        try:
            print("Starting recipe upload...")
            yield {"video_id": video_id, "status": "uploading", "message": f"Uploading recipe to Mealie at {MEALIE_URL}..."}
            sys.stdout.flush()
            
            x = requests.post(
                f"{MEALIE_URL}/api/recipes/create/html-or-json",
                json={
                    "includeTags": True,
                    "data": json.dumps(recipe_json)
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {MEALIE_TOKEN}"
                },
                timeout=30
            )
            
            print(f"Recipe upload status: {x.status_code}")
            print(f"Recipe upload response: {x.text}")
            
            if x.status_code in [200, 201]:
                api_response = f"Recipe uploaded successfully (Status: {x.status_code})"
                yield {"video_id": video_id, "status": "uploading", "message": "Recipe uploaded successfully!"}
                
                # Upload thumbnail if available
                if thumbnail_url:
                    try:
                        slug = recipe_json["name"].lower().replace(" ", "-")
                        yield {"video_id": video_id, "status": "uploading", "message": "Uploading recipe thumbnail..."}
                        
                        y = requests.post(
                            f"{MEALIE_URL}/api/recipes/{slug}/image",
                            json={
                                "includeTags": True,
                                "url": thumbnail_url
                            },
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {MEALIE_TOKEN}"
                            },
                            timeout=30
                        )
                        print(f"Image upload status: {y.status_code}")
                        print(f"Image upload response: {y.text}")
                        
                        if y.status_code in [200, 201]:
                            yield {"video_id": video_id, "status": "uploading", "message": "Thumbnail uploaded successfully!"}
                        else:
                            yield {"video_id": video_id, "status": "uploading", "message": f"Thumbnail upload failed (Status: {y.status_code})"}
                    except Exception as img_error:
                        print(f"Image upload error: {str(img_error)}")
                        yield {"video_id": video_id, "status": "uploading", "message": f"Thumbnail upload error: {str(img_error)}"}
                else:
                    yield {"video_id": video_id, "status": "uploading", "message": "No thumbnail available for this video"}
            else:
                api_response = f"Error uploading recipe (Status: {x.status_code}): {x.text}"
                yield {"video_id": video_id, "status": "error", "message": f"Recipe upload failed: {api_response}"}
                
        except requests.exceptions.Timeout:
            api_response = f"Timeout connecting to Mealie at {MEALIE_URL}"
            print(f"API Error: {api_response}")
            yield {"video_id": video_id, "status": "error", "message": api_response}
            sys.stdout.flush()
        except requests.exceptions.ConnectionError as e:
            api_response = f"Connection error to Mealie at {MEALIE_URL}: {str(e)}"
            print(f"API Error: {api_response}")
            yield {"video_id": video_id, "status": "error", "message": f"Cannot connect to Mealie. Check MEALIE_URL and network settings."}
            sys.stdout.flush()
        except Exception as e:
            api_response = f"Error posting to API: {str(e)}"
            print(f"API Error: {api_response}")
            import traceback
            traceback.print_exc()
            yield {"video_id": video_id, "status": "error", "message": api_response}
            sys.stdout.flush()
        
        print(f"Final API response: {api_response}")
        yield {
            "video_id": video_id,
            "status": "complete",
            "message": "Recipe generated successfully!",
            "recipe": json.dumps(recipe_json, indent=2),
            "api_response": api_response
        }
        sys.stdout.flush()
        print("Process completed successfully")
        
        # Log accumulated tokens for this video at the end
        if token_accumulator:
            log_video_token_usage(user_id, video_id, token_accumulator)
        
    except Exception as e:
        print(f"FATAL ERROR in process_video: {str(e)}")
        import traceback
        traceback.print_exc()
        yield {
            "video_id": video_id,
            "status": "error",
            "message": f"Error: {str(e)}"
        }
    
    finally:
        os.chdir(original_dir)
        active_frame_dirs.pop(video_id, None)
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('index.html', user_name=session.get('user_name'))

@app.route('/process', methods=['POST'])
@login_required
def process():
    data = request.json
    video_url = data.get('url')
    
    if not video_url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400
    
    # Check rate limit
    user_id = session['user_id']
    conn = get_db()
    user = conn.execute('SELECT daily_limit FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    
    usage_today = get_usage_today(user_id)
    if usage_today >= user['daily_limit']:
        return jsonify({
            "status": "error",
            "message": f"Daily limit reached ({user['daily_limit']} requests). Try again tomorrow."
        }), 429
    
    # Log the usage
    log_usage(user_id, video_url, 'started')
    
    video_id = str(uuid.uuid4())
    base_url = request.host_url
    
    def generate():
        # Send initial event with video_id
        yield f"data: {json.dumps({'video_id': video_id, 'status': 'queued', 'message': 'Video queued for processing', 'url': video_url})}\n\n"
        sys.stdout.flush()
        for update in process_video(video_url, video_id, user_id, base_url):
            yield f"data: {json.dumps(update)}\n\n"
            sys.stdout.flush()
    
    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json if request.is_json else request.form
        name = data.get('name')
        pin = data.get('pin')
        
        user = verify_pin(name, pin)
        if user:
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['is_admin'] = bool(user['is_admin'])
            return jsonify({"status": "success", "redirect": url_for('index')})
        else:
            return jsonify({"status": "error", "message": "Invalid name or PIN"}), 401
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
@login_required
def admin():
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    conn = get_db()
    
    # Get all users with their usage stats and costs
    users = conn.execute('''
        SELECT u.id, u.name, u.is_admin, u.daily_limit, u.active, u.created_at,
               COUNT(DISTINCT DATE(ug.timestamp)) as days_active,
               COUNT(ug.id) as total_requests,
               MAX(ug.timestamp) as last_used,
               SUM(tu.cost) as total_cost,
               COUNT(DISTINCT tu.request_id) as total_videos
        FROM users u
        LEFT JOIN usage ug ON u.id = ug.user_id
        LEFT JOIN token_usage tu ON u.id = tu.user_id
        GROUP BY u.id
        ORDER BY u.created_at DESC
    ''').fetchall()
    
    # Get today's usage per user
    today = datetime.now().strftime('%Y-%m-%d')
    today_usage = conn.execute('''
        SELECT u.name, COUNT(*) as count, SUM(tu.cost) as cost
        FROM usage ug
        JOIN users u ON ug.user_id = u.id
        LEFT JOIN token_usage tu ON ug.user_id = tu.user_id AND DATE(tu.timestamp) = ?
        WHERE DATE(ug.timestamp) = ?
        GROUP BY u.id
    ''', (today, today)).fetchall()
    
    # Get recent activity with costs
    recent = conn.execute('''
        SELECT u.name, ug.url, ug.status, ug.timestamp,
               SUM(tu.cost) as cost, SUM(tu.total_tokens) as tokens, tu.request_id
        FROM usage ug
        JOIN users u ON ug.user_id = u.id
        LEFT JOIN token_usage tu ON u.id = tu.user_id AND ug.url = (
            SELECT url FROM usage WHERE id = ug.id
        )
        GROUP BY ug.id
        ORDER BY ug.timestamp DESC
        LIMIT 50
    ''').fetchall()
    
    conn.close()
    
    return render_template('admin.html',
                         users=[dict(u) for u in users],
                         today_usage=[dict(t) for t in today_usage],
                         recent=[dict(r) for r in recent])

@app.route('/admin/create_user', methods=['POST'])
@login_required
def admin_create_user():
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    data = request.json
    name = data.get('name')
    pin = data.get('pin')
    is_admin = data.get('is_admin', False)
    daily_limit = data.get('daily_limit', 10)
    
    if not name or not pin:
        return jsonify({"status": "error", "message": "Name and PIN required"}), 400
    
    if create_user(name, pin, is_admin, daily_limit):
        return jsonify({"status": "success", "message": f"User {name} created"})
    else:
        return jsonify({"status": "error", "message": "User already exists"}), 400

@app.route('/admin/toggle_user/<int:user_id>', methods=['POST'])
@login_required
def admin_toggle_user(user_id):
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    conn = get_db()
    user = conn.execute('SELECT active FROM users WHERE id = ?', (user_id,)).fetchone()
    if user:
        new_status = 0 if user['active'] else 1
        conn.execute('UPDATE users SET active = ? WHERE id = ?', (new_status, user_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "active": bool(new_status)})
    conn.close()
    return jsonify({"status": "error", "message": "User not found"}), 404

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    # Prevent deleting the only admin
    if user_id == session.get('user_id'):
        return jsonify({"status": "error", "message": "Cannot delete your own account"}), 400
    
    conn = get_db()
    try:
        # Delete usage logs first (foreign key constraint)
        conn.execute('DELETE FROM usage WHERE user_id = ?', (user_id,))
        # Delete the user
        conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "User deleted successfully"})
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/api/request-cost/<request_id>', methods=['GET'])
@login_required
def get_request_cost(request_id):
    """Get cost and token usage for a specific request"""
    user_id = session['user_id']
    cost_data = get_request_cost_and_tokens(user_id, request_id)
    return jsonify(cost_data)

@app.route('/api/user-cost-summary', methods=['GET'])
@login_required
def get_user_cost_summary_endpoint():
    """Get cost summary for current user"""
    user_id = session['user_id']
    days = request.args.get('days', 30, type=int)
    summary = get_user_cost_summary(user_id, days)
    return jsonify(summary)

@app.route('/admin/cost-overview', methods=['GET'])
@login_required
def admin_cost_overview():
    """Get cost overview across all users (admin only)"""
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    conn = get_db()
    
    # Get cost summary per user (last 30 days)
    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    cost_per_user = conn.execute('''
        SELECT u.name, 
               COUNT(DISTINCT tu.request_id) as request_count,
               SUM(tu.input_tokens) as total_input_tokens,
               SUM(tu.output_tokens) as total_output_tokens,
               SUM(tu.total_tokens) as total_tokens,
               SUM(tu.cost) as total_cost,
               GROUP_CONCAT(DISTINCT tu.model) as models_used
        FROM users u
        LEFT JOIN token_usage tu ON u.id = tu.user_id AND DATE(tu.timestamp) >= ?
        GROUP BY u.id
        ORDER BY total_cost DESC NULLS LAST
    ''', (thirty_days_ago,)).fetchall()
    
    # Get overall stats
    overall = conn.execute('''
        SELECT COUNT(DISTINCT request_id) as total_requests,
               SUM(input_tokens) as total_input_tokens,
               SUM(output_tokens) as total_output_tokens,
               SUM(total_tokens) as total_tokens,
               SUM(cost) as total_cost
        FROM token_usage
        WHERE DATE(timestamp) >= ?
    ''', (thirty_days_ago,)).fetchone()
    
    # Get cost breakdown by model
    model_breakdown = conn.execute('''
        SELECT model,
               COUNT(*) as usage_count,
               SUM(input_tokens) as total_input_tokens,
               SUM(output_tokens) as total_output_tokens,
               SUM(total_tokens) as total_tokens,
               SUM(cost) as total_cost,
               AVG(cost) as avg_cost
        FROM token_usage
        WHERE DATE(timestamp) >= ?
        GROUP BY model
        ORDER BY total_cost DESC
    ''', (thirty_days_ago,)).fetchall()
    
    conn.close()
    
    return jsonify({
        "cost_per_user": [dict(row) for row in cost_per_user],
        "overall_stats": dict(overall) if overall else {},
        "model_breakdown": [dict(row) for row in model_breakdown]
    })

@app.route('/api/daily-usage', methods=['GET'])
@login_required
def get_daily_usage():
    """Get daily token usage and cost for the current user (last 30 days)"""
    user_id = session['user_id']
    days = request.args.get('days', 30, type=int)
    
    conn = get_db()
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    daily_stats = conn.execute('''
        SELECT DATE(timestamp) as date,
               SUM(total_tokens) as tokens,
               SUM(cost) as cost,
               COUNT(DISTINCT request_id) as videos
        FROM token_usage
        WHERE user_id = ? AND DATE(timestamp) >= ?
        GROUP BY DATE(timestamp)
        ORDER BY date
    ''', (user_id, cutoff_date)).fetchall()
    
    conn.close()
    
    return jsonify([dict(row) for row in daily_stats])

@app.route('/admin/api/daily-usage', methods=['GET'])
@login_required
def admin_get_daily_usage():
    """Get daily token usage and cost across all users (admin only)"""
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    days = request.args.get('days', 30, type=int)
    
    conn = get_db()
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    daily_stats = conn.execute('''
        SELECT DATE(timestamp) as date,
               SUM(total_tokens) as tokens,
               SUM(cost) as cost,
               COUNT(DISTINCT request_id) as videos
        FROM token_usage
        WHERE DATE(timestamp) >= ?
        GROUP BY DATE(timestamp)
        ORDER BY date
    ''', (cutoff_date,)).fetchall()
    
    conn.close()
    
    return jsonify([dict(row) for row in daily_stats])

@app.route('/admin/api/user-cost-breakdown', methods=['GET'])
@login_required
def admin_get_user_cost_breakdown():
    """Get cost breakdown by user (admin only)"""
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    days = request.args.get('days', 30, type=int)
    
    conn = get_db()
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    user_costs = conn.execute('''
        SELECT u.name,
               SUM(tu.cost) as cost,
               SUM(tu.total_tokens) as tokens,
               COUNT(DISTINCT tu.request_id) as videos
        FROM users u
        LEFT JOIN token_usage tu ON u.id = tu.user_id AND DATE(tu.timestamp) >= ?
        WHERE u.active = 1
        GROUP BY u.id
        ORDER BY cost DESC
    ''', (cutoff_date,)).fetchall()
    
    conn.close()
    
    return jsonify([dict(row) for row in user_costs])

@app.route('/admin/api/model-usage', methods=['GET'])
@login_required
def admin_get_model_usage():
    """Get model usage breakdown (admin only)"""
    if not session.get('is_admin'):
        return jsonify({"status": "error", "message": "Admin access required"}), 403
    
    days = request.args.get('days', 30, type=int)
    
    conn = get_db()
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    # Get all token usage entries with model breakdown
    entries = conn.execute('''
        SELECT model_breakdown
        FROM token_usage
        WHERE DATE(timestamp) >= ? AND model_breakdown IS NOT NULL
    ''', (cutoff_date,)).fetchall()
    
    conn.close()
    
    # Parse model breakdowns and aggregate
    model_stats_dict = {}
    for entry in entries:
        try:
            breakdown = json.loads(entry['model_breakdown'])
            for model, data in breakdown.items():
                if model not in model_stats_dict:
                    model_stats_dict[model] = {
                        "usage_count": 0,
                        "total_tokens": 0,
                        "total_cost": 0,
                        "avg_cost": 0
                    }
                model_stats_dict[model]["usage_count"] += 1
                model_stats_dict[model]["total_tokens"] += data.get("total_tokens", 0)
                model_stats_dict[model]["total_cost"] += data.get("cost", 0)
        except (json.JSONDecodeError, KeyError):
            continue
    
    # Calculate average cost
    for model in model_stats_dict:
        if model_stats_dict[model]["usage_count"] > 0:
            model_stats_dict[model]["avg_cost"] = model_stats_dict[model]["total_cost"] / model_stats_dict[model]["usage_count"]
    
    # Convert to sorted list
    model_stats = sorted(
        [{"model": k, **v} for k, v in model_stats_dict.items()],
        key=lambda x: x["total_cost"],
        reverse=True
    )
    
    return jsonify(model_stats)

if __name__ == '__main__':
    # Create default admin user if none exists
    conn = get_db()
    admin_exists = conn.execute('SELECT id FROM users WHERE is_admin = 1').fetchone()
    conn.close()
    
    if not admin_exists:
        print("\n" + "="*50)
        print("No admin user found. Creating default admin...")
        print("Name: admin")
        print("PIN: 1234")
        print("PLEASE CHANGE THIS PIN IMMEDIATELY!")
        print("="*50 + "\n")
        create_user('admin', '1234', is_admin=True, daily_limit=999)
    
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
