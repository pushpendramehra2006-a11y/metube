# MeTube

Link paste karo (YouTube / Instagram / Facebook etc.), MP4 (max se min quality, 4K/8K tak) ya MP3 download karo.
Image links se image, aur gallery video se MP3 bhi. Laptop + mobile, dark/light mode.

## Local chalane ke liye
1. Python aur ffmpeg install karo (YouTube ke liye Deno bhi: `winget install DenoLand.Deno`)
2. `pip install -r requirements.txt`
3. `python -m uvicorn server:app --host 0.0.0.0 --port 8000`
4. Browser: http://localhost:8000

## Structure
- `server.py` - FastAPI backend (yt-dlp + ffmpeg, background jobs)
- `static/index.html` - frontend
- `Dockerfile` - hosting ke liye (ffmpeg + Deno ke saath)

## Note
Sirf personal use ke liye. Sites ke terms download allow nahi karte.
