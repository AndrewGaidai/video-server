from moviepy.editor import ImageClip, TextClip, CompositeVideoClip
import numpy as np

# Create blank 1080x1920 canvas
canvas = np.zeros((1920, 1080, 3), dtype=np.uint8) + 50  # Dark gray
canvas_clip = ImageClip(canvas).set_duration(5)

# Test caption
caption = "they said 'get over here' i said 'nah i'm booked'"

txt_clip = TextClip(
    caption,
    fontsize=70,
    color='white',
    font='DejaVu-Sans-Bold',
    stroke_color='black',
    stroke_width=2.0,
    method='caption',
    size=(800, None),
    align='center'
)

txt_clip = txt_clip.set_position(('center', 1344)).set_duration(5)
video = CompositeVideoClip([canvas_clip, txt_clip])

video.write_videofile("test_caption.mp4", fps=24)
print("Preview saved as test_caption.mp4")