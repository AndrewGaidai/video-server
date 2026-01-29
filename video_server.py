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
        video_url = f"https://video-server-qcs9.onrender.com/videos/{video_filename}"
        
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
            print(f"Downloading from: {img_url[:100]}...")  # Print URL (truncated)
            
            response = requests.get(img_url, timeout=10)
            print(f"Download status: {response.status_code}")
            response.raise_for_status()
            
            print(f"Opening image, size: {len(response.content)} bytes")
            img = Image.open(BytesIO(response.content))
            print(f"Image mode: {img.mode}, size: {img.size}")
            
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # 9:16 for Instagram Reels/TikTok
            target_width = 1080
            target_height = 1920
            
            print(f"Resizing to {target_width}x{target_height}...")
            img = resize_and_crop(img, target_width, target_height)
            
            temp_img_path = f'temp_images/{video_id}_img_{idx}.jpg'
            img.save(temp_img_path)
            print(f"Saved to {temp_img_path}")
            
            clip = ImageClip(temp_img_path).set_duration(duration)
            clips.append(clip)
            print(f"Clip created successfully")
            
        except Exception as e:
            print(f"!!! ERROR processing image {idx}: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            continue
    
    if not clips:
        raise Exception("No valid images could be processed")
    
    print("Concatenating clips...")
    video = concatenate_videoclips(clips, method="compose")
    
    # CAPTION STYLING WITH EMOJI SUPPORT
    if caption:
        print(f"Adding caption: '{caption}'")
        try:
            from PIL import Image, ImageDraw, ImageFont
            import textwrap
            
            # Dynamic font size based on caption length
            caption_length = len(caption)
            if caption_length < 30:
                fontsize = 80
                stroke_width = 3
            elif caption_length < 50:
                fontsize = 70
                stroke_width = 2
            else:
                fontsize = 60
                stroke_width = 2
            
            print(f"Font size: {fontsize}, stroke: {stroke_width}")
            
            # Create transparent image for text
            text_img = Image.new('RGBA', (1080, 600), (0, 0, 0, 0))
            draw = ImageDraw.Draw(text_img)
            
            # Try to load system font with emoji support
            try:
                # Linux: DejaVu Sans or fallback to default
                font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', fontsize)
            except:
                try:
                    # Try a different path or use default
                    font = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf', fontsize)
                except:
                    font = ImageFont.load_default()
            
            # Wrap text to fit width (~800px)
            max_width = 800
            lines = []
            words = caption.split()
            current_line = []
            
            for word in words:
                test_line = ' '.join(current_line + [word])
                bbox = draw.textbbox((0, 0), test_line, font=font)
                if bbox[2] - bbox[0] <= max_width:
                    current_line.append(word)
                else:
                    if current_line:
                        lines.append(' '.join(current_line))
                    current_line = [word]
            if current_line:
                lines.append(' '.join(current_line))
            
            # Calculate total height
            line_height = fontsize + 10
            total_height = len(lines) * line_height
            
            # Draw text with stroke (outline)
            y_offset = (600 - total_height) // 2
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                text_width = bbox[2] - bbox[0]
                x = (1080 - text_width) // 2
                
                # Draw stroke (black outline)
                for adj_x in range(-stroke_width, stroke_width+1):
                    for adj_y in range(-stroke_width, stroke_width+1):
                        if adj_x != 0 or adj_y != 0:
                            draw.text((x + adj_x, y_offset + adj_y), line, font=font, fill='black')
                
                # Draw main text (white)
                draw.text((x, y_offset), line, font=font, fill='white')
                y_offset += line_height
            
            # Save temporary image
            temp_text_path = f'temp_images/{video_id}_text.png'
            text_img.save(temp_text_path)
            
            # Create MoviePy clip from image
            txt_clip = ImageClip(temp_text_path, transparent=True)
            txt_clip = txt_clip.set_position(('center', 1200)).set_duration(video.duration)
            
            print("Text clip with emoji support created")
            
            video = CompositeVideoClip([video, txt_clip])
            print("Caption composited into video")
            
            # Cleanup temp text image after compositing
            if os.path.exists(temp_text_path):
                os.remove(temp_text_path)
            
        except Exception as e:
            print(f"!!! CAPTION ERROR: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
    
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