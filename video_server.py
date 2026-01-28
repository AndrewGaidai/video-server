"""
Video Server for Instagram Reels/TikTok Slideshow Generation

Instagram Reels UI Safe Zones (1080x1920):
- Top 300px: Username, audio, camera button - AVOID
- Bottom 350px: Like, comment, share buttons - AVOID
- Safe zone for captions: 1200-1600px from top
- Our caption position: 1344px (70% from top = 30% from bottom)
"""

from flask import Flask, request, jsonify, send_file
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip, TextClip
from PIL import Image
import requests
from io import BytesIO
import numpy as np
import os
import uuid
from datetime import datetime

app = Flask(__name__)

os.makedirs('temp_images', exist_ok=True)
os.makedirs('output_videos', exist_ok=True)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "Video server is running"})

@app.route('/create-video', methods=['POST'])
def create_video():
    try:
        data = request.json
        
        image_urls = data.get('image_urls', [])
        music_url = data.get('music_url', '')
        beat_timings = data.get('beat_timings', [])
        caption = data.get('caption', '')
        
        if not image_urls or not beat_timings:
            return jsonify({"error": "Missing image_urls or beat_timings"}), 400
        
        min_length = min(len(image_urls), len(beat_timings))
        image_urls = image_urls[:min_length]
        beat_timings = beat_timings[:min_length]
        
        print(f"Creating video with {len(image_urls)} images...")
        
        video_path = create_slideshow_video(
            image_urls=image_urls,
            music_url=music_url,
            beat_timings=beat_timings,
            caption=caption
        )
        
        video_filename = os.path.basename(video_path)
        # Update with your actual server URL after deployment
        video_url = f"https://your-server.railway.app/videos/{video_filename}"
        
        return jsonify({
            "success": True,
            "video_url": video_url,
            "message": f"Video created successfully with {len(image_urls)} images"
        })
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

def create_slideshow_video(image_urls, music_url, beat_timings, caption):
    clips = []
    video_id = str(uuid.uuid4())[:8]
    
    # Process images
    for idx, (img_url, duration) in enumerate(zip(image_urls, beat_timings)):
        try:
            print(f"Processing image {idx + 1}/{len(image_urls)}...")
            
            response = requests.get(img_url, timeout=10)
            response.raise_for_status()
            
            img = Image.open(BytesIO(response.content))
            
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # 9:16 for Instagram Reels/TikTok
            target_width = 1080
            target_height = 1920
            
            img = resize_and_crop(img, target_width, target_height)
            
            temp_img_path = f'temp_images/{video_id}_img_{idx}.jpg'
            img.save(temp_img_path)
            
            clip = ImageClip(temp_img_path).set_duration(duration)
            clips.append(clip)
            
        except Exception as e:
            print(f"Error processing image {idx}: {e}")
            continue
    
    if not clips:
        raise Exception("No valid images could be processed")
    
    print("Concatenating clips...")
    video = concatenate_videoclips(clips, method="compose")
    
    # CAPTION STYLING - MATCHES CAPCUT SYSTEM FONT STYLE
    # CAPTION STYLING - TEMPORARILY DISABLED FOR TESTING
    if False:  # TODO: Change back to 'if caption:' after fonts are fixed
        print("Adding caption...")
        try:
            # Dynamic font size based on caption length
            caption_length = len(caption)
            if caption_length < 30:
                fontsize = 85
                stroke_width = 4
            elif caption_length < 50:
                fontsize = 75
                stroke_width = 3
            else:
                fontsize = 65
                stroke_width = 3
            
            # Try system fonts in order of availability
            # Segoe UI (Windows), SF Pro (Mac), Helvetica Neue, Arial (fallback)
            fonts_to_try = ['Segoe-UI', 'SF-Pro-Display', 'Helvetica-Neue', 'Arial']
            selected_font = 'Arial'  # fallback
            
            for font in fonts_to_try:
                try:
                    # Test if font works
                    test = TextClip("test", font=font, fontsize=20)
                    selected_font = font
                    test.close()
                    break
                except:
                    continue
            
            txt_clip = TextClip(
                caption,
                fontsize=fontsize,
                color='white',
                font=selected_font,
                stroke_color='black',
                stroke_width=stroke_width,
                method='caption',
                size=(950, None),      # Max width with side margins
                align='center'
            )
            
            # Position at 30% from bottom = 70% from top
            # 1920 * 0.70 = 1344px from top
            txt_clip = txt_clip.set_position(('center', 1344)).set_duration(video.duration)
            
            video = CompositeVideoClip([video, txt_clip])
        except Exception as e:
            print(f"Error adding caption: {e}")
    
    # Add music
    if music_url:
        print("Adding music...")
        try:
            music_response = requests.get(music_url, timeout=30)
            music_response.raise_for_status()
            
            temp_music_path = f'temp_images/{video_id}_music.mp3'
            with open(temp_music_path, 'wb') as f:
                f.write(music_response.content)
            
            audio = AudioFileClip(temp_music_path)
            
            if audio.duration < video.duration:
                num_loops = int(video.duration / audio.duration) + 1
                audio = concatenate_audioclips([audio] * num_loops)
            
            audio = audio.subclip(0, video.duration)
            video = video.set_audio(audio)
            
        except Exception as e:
            print(f"Error adding music: {e}")
    
    # Render
    print("Rendering video...")
    output_path = f'output_videos/video_{video_id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.mp4'
    
    video.write_videofile(
        output_path,
        fps=24,
        codec='libx264',
        audio_codec='aac',
        preset='medium',
        threads=4
    )
    
    # Cleanup
    print("Cleaning up...")
    for idx in range(len(image_urls)):
        temp_img = f'temp_images/{video_id}_img_{idx}.jpg'
        if os.path.exists(temp_img):
            os.remove(temp_img)
    
    if music_url and os.path.exists(f'temp_images/{video_id}_music.mp3'):
        os.remove(f'temp_images/{video_id}_music.mp3')
    
    video.close()
    if music_url:
        audio.close()
    
    print(f"Video created: {output_path}")
    return output_path

def resize_and_crop(img, target_width, target_height):
    """Resize and crop image to exact dimensions"""
    img_width, img_height = img.size
    target_ratio = target_width / target_height
    img_ratio = img_width / img_height
    
    if img_ratio > target_ratio:
        new_height = target_height
        new_width = int(target_height * img_ratio)
    else:
        new_width = target_width
        new_height = int(target_width / img_ratio)
    
    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
    
    left = (new_width - target_width) // 2
    top = (new_height - target_height) // 2
    right = left + target_width
    bottom = top + target_height
    
    img = img.crop((left, top, right, bottom))
    
    return img

@app.route('/videos/<filename>', methods=['GET'])
def serve_video(filename):
    video_path = os.path.join('output_videos', filename)
    if os.path.exists(video_path):
        return send_file(video_path, mimetype='video/mp4')
    return jsonify({"error": "Video not found"}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))