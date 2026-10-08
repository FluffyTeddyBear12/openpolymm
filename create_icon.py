import os
from PIL import Image

src_img = r"C:\Users\redwi\.gemini\antigravity\brain\c5f64199-c1fe-498d-9d34-d0ccbcd80f5c\polymarket_bot_icon_1790860982397.jpg"
dst_ico = r"d:\neststock\scripts\polymarket_bot\polymarket_icon.ico"

img = Image.open(src_img)
# Resize to a common icon size just in case, though save as ICO handles multiple sizes or one
img = img.resize((256, 256), Image.Resampling.LANCZOS)
img.save(dst_ico, format="ICO", sizes=[(256, 256)])

print(f"Saved {dst_ico}")
