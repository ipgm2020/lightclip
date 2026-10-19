Lightclip revised progress build

Run from this folder:
  pip install -r requirements.txt
  python app.py

Open http://127.0.0.1:5000

Progress is polled from Flask and reports yt-dlp download bytes/speed plus FFmpeg processing progress when compact encoding is used.

Important: a website cannot force the browser's download folder. The app automatically triggers the file download when ready; Chrome/Edge save to the configured Downloads folder if "Ask where to save each file" is disabled. Configure that in browser Settings > Downloads.
