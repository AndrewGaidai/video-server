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

# Create folders for temp files
os.makedirs('temp_images', exist_ok=True)
os.makedirs('output_videos', exist_ok=True)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "Video server is running"})

@app.route('/create-video', methods=['POST'])
def create_video():
    try:
        # Get data from Buildship
        data = request.json
        
        image_urls = data.get('image_urls', [])  # List of image URLs
        music_url = data.get('music_url', '')     # Music URL
        beat_timings = data.get('beat_timings', [])  # List of durations [1.2, 0.8, 1.5, ...]
        caption = data.get('caption', '')         # Caption text
        
        # Validate inputs
        if not image_urls or not beat_timings:
            return jsonify({"error": "Missing image_urls or beat_timings"}), 400
        
        # Make sure we have same number of images and timings
        if len(image_urls) != len(beat_timings):
            # If more images than timings, trim images
            # If more timings than images, trim timings
            min_length = min(len(image_urls), len(beat_timings))
            image_urls = image_urls[:min_length]
            beat_timings = beat_timings[:min_length]
        
        print(f"Creating video with {len(image_urls)} images...")
        
        # Create video
        video_path = create_slideshow_video(
            image_urls=image_urls,
            music_url=music_url,
            beat_timings=beat_timings,
            caption=caption
        )
        
        # Return the video file URL (you'll need to serve this)
        video_filename = os.path.basename(video_path)
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
    """
    Create a slideshow video from images with music and caption
    """
    clips = []
    video_id = str(uuid.uuid4())[:8]
    
    # Download and create clips for each image
    for idx, (img_url, duration) in enumerate(zip(image_urls, beat_timings)):
        try:
            print(f"Processing image {idx + 1}/{len(image_urls)}...")
            
            # Download image
            response = requests.get(img_url, timeout=10)
            response.raise_for_status()
            
            # Open image
            img = Image.open(BytesIO(response.content))
            
            # Convert to RGB if necessary
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Resize to 1080x1920 (9:16 aspect ratio for reels/shorts)
            # You can change this to 1920x1080 for landscape
            target_width = 1080
            target_height = 1920
            
            img = resize_and_crop(img, target_width, target_height)
            
            # Save temp image
            temp_img_path = f'temp_images/{video_id}_img_{idx}.jpg'
            img.save(temp_img_path)
            
            # Create clip
            clip = ImageClip(temp_img_path).set_duration(duration)
            clips.append(clip)
            
        except Exception as e:
            print(f"Error processing image {idx}: {e}")
            continue
    
    if not clips:
        raise Exception("No valid images could be processed")
    
    # Concatenate all clips
    print("Concatenating clips...")
    video = concatenate_videoclips(clips, method="compose")
    
    # Add caption if provided
    if caption:
        print("Adding caption...")
        try:
            txt_clip = TextClip(
                caption,
                fontsize=60,
                color='white',
                font='Arial-Bold',
                stroke_color='black',
                stroke_width=2,
                method='caption',
                size=(1000, None)  # Width, height auto
            )
            txt_clip = txt_clip.set_position(('center', 100)).set_duration(video.duration)
            video = CompositeVideoClip([video, txt_clip])
        except Exception as e:
            print(f"Error adding caption: {e}")
    
    # Add music if provided
    if music_url:
        print("Adding music...")
        try:
            # Download music
            music_response = requests.get(music_url, timeout=30)
            music_response.raise_for_status()
            
            temp_music_path = f'temp_images/{video_id}_music.mp3'
            with open(temp_music_path, 'wb') as f:
                f.write(music_response.content)
            
            # Add audio
            audio = AudioFileClip(temp_music_path)
            
            # Trim audio to video length or loop if too short
            if audio.duration < video.duration:
                # Loop audio
                num_loops = int(video.duration / audio.duration) + 1
                audio = concatenate_audioclips([audio] * num_loops)
            
            audio = audio.subclip(0, video.duration)
            video = video.set_audio(audio)
            
        except Exception as e:
            print(f"Error adding music: {e}")
    
    # Write final video
    print("Rendering video...")
    output_path = f'output_videos/video_{video_id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.mp4'
    
    video.write_videofile(
        output_path,
        fps=24,
        codec='libx264',
        audio_codec='aac',
        preset='medium',  # Use 'ultrafast' for faster render, 'slow' for better quality
        threads=4
    )
    
    # Cleanup temp files
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
    """
    Resize image to fill target dimensions and crop excess
    """
    img_width, img_height = img.size
    target_ratio = target_width / target_height
    img_ratio = img_width / img_height
    
    if img_ratio > target_ratio:
        # Image is wider, fit height and crop width
        new_height = target_height
        new_width = int(target_height * img_ratio)
    else:
        # Image is taller, fit width and crop height
        new_width = target_width
        new_height = int(target_width / img_ratio)
    
    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
    
    # Crop to exact dimensions
    left = (new_width - target_width) // 2
    top = (new_height - target_height) // 2
    right = left + target_width
    bottom = top + target_height
    
    img = img.crop((left, top, right, bottom))
    
    return img

# Endpoint to serve videos
@app.route('/videos/<filename>', methods=['GET'])
def serve_video(filename):
    video_path = os.path.join('output_videos', filename)
    if os.path.exists(video_path):
        return send_file(video_path, mimetype='video/mp4')
    return jsonify({"error": "Video not found"}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))