from flask import Flask, render_template, request, jsonify, Response
import base64
from openai import OpenAI
import os
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

load_dotenv()

app = Flask(__name__)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Mealie configuration
MEALIE_URL = os.getenv("MEALIE_URL", "http://localhost:9925")
MEALIE_TOKEN = os.getenv("MEALIE_TOKEN", "")

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

def download_instagram_reel(url, temp_dir):
    """Download Instagram Reel and return video path and caption"""
    L = instaloader.Instaloader(dirname_pattern=temp_dir)
    
    # Extract shortcode from URL
    shortcode_match = re.search(r'/reel/([^/?]+)', url)
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

def process_video(video_url, video_id):
    """Process a TikTok or Instagram Reel video URL and return the recipe"""
    responses = []
    temp_dir = tempfile.mkdtemp()
    
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
            
            yield {
                "video_id": video_id,
                "status": "complete",
                "message": "Recipe generated successfully!",
                "recipe": response.output_text
            }
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
        subprocess.run([
            'ffmpeg', '-i', video,
            '-vn',
            '-acodec', 'pcm_s16le',
            '-ar', '16000',
            '-ac', '1',
            audio_path
        ], check=True, capture_output=True)
        
        with open(audio_path, 'rb') as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en",
                timestamp_granularities=["segment"],
                response_format="verbose_json"
            )
        
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
            temp_path = os.path.join(temp_dir, f"temp_frame_{i}.jpg")
            cv2.imwrite(temp_path, frame)
            base64_image = encode_image(temp_path)
            os.remove(temp_path)
            
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
- Output valid JSON only
- Use Schema.org Recipe
- Do not hallucinate information not in the inputs. Especially for ingredients and quantities and times that where not specified.
- Include:
  name, description, recipeIngredient, recipeInstructions
- Use HowToStep for instructions
- Use ISO 8601 durations (PT#M)
- Do not include unsupported fields
- Do not include names in recipeInstructions

JSON ONLY. """
            }]
        )
        
        recipe_json = response.output_text.replace("`", "").replace("json", "").replace("\n", "").replace("\\n", "").replace("\\\"", '"')
        
        if recipe_json.startswith('"') and recipe_json.endswith('"'):
            recipe_json = recipe_json[1:-1]
        
        recipe_json = json.loads(recipe_json)
        
        print("Final Recipe JSON generated.")
        
        # Send to external API
        api_response = "Recipe generated but not uploaded to Mealie"
        try:
            yield {"video_id": video_id, "status": "uploading", "message": f"Uploading recipe to Mealie at {MEALIE_URL}..."}
            
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
        except requests.exceptions.ConnectionError as e:
            api_response = f"Connection error to Mealie at {MEALIE_URL}: {str(e)}"
            print(f"API Error: {api_response}")
            yield {"video_id": video_id, "status": "error", "message": f"Cannot connect to Mealie. Check MEALIE_URL and network settings."}
        except Exception as e:
            api_response = f"Error posting to API: {str(e)}"
            print(f"API Error: {api_response}")
            yield {"video_id": video_id, "status": "error", "message": api_response}
        
        yield {
            "video_id": video_id,
            "status": "complete",
            "message": "Recipe generated successfully!",
            "recipe": json.dumps(recipe_json, indent=2),
            "api_response": api_response
        }
        
    except Exception as e:
        yield {
            "video_id": video_id,
            "status": "error",
            "message": f"Error: {str(e)}"
        }
    
    finally:
        os.chdir(original_dir)
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    data = request.json
    video_url = data.get('url')
    
    if not video_url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400
    
    video_id = str(uuid.uuid4())
    
    def generate():
        # Send initial event with video_id
        yield f"data: {json.dumps({'video_id': video_id, 'status': 'queued', 'message': 'Video queued for processing', 'url': video_url})}\n\n"
        sys.stdout.flush()
        for update in process_video(video_url, video_id):
            yield f"data: {json.dumps(update)}\n\n"
            sys.stdout.flush()
    
    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
