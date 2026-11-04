import os
import re
import json
import uuid
import shutil
import tempfile
import threading
import subprocess
from flask import Flask, render_template, request, jsonify, send_file, after_this_request

app = Flask(__name__)

TASKS = {}
TEMP_BASE = os.path.join(tempfile.gettempdir(), "yt_dubbed_cache")
os.makedirs(TEMP_BASE, exist_ok=True)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/get-tracks", methods=["POST"])
def get_tracks():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL empty hai"}), 400

    try:
        cmd = ["yt-dlp", "-J", "--no-warnings", "--no-playlist", url]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        
        tracks = []
        seen = set()
        for f in info.get("formats", []):
            if f.get("vcodec") == "none" and f.get("acodec") != "none":
                lang = f.get("language") or "und"
                label = f.get("format_note") or f.get("format_id")
                track_id = f"{lang}-{label}"
                if track_id not in seen:
                    seen.add(track_id)
                    tracks.append({
                        "id": f.get("format_id"),
                        "lang": lang,
                        "label": f"{lang.upper()} ({label})"
                    })

        return jsonify({
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "tracks": tracks
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def run_download_worker(task_id, cmd, temp_dir):
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    # Progress regex to catch both standard and fragmented downloads
    progress_regex = re.compile(r'\[download\]\s+(\d+\.?\d*)%\s+of\s+~?(\S+)\s+at\s+(\S+)\s+ETA\s+(\S+)')

    for line in iter(process.stdout.readline, ''):
        if not line:
            break
        
        match = progress_regex.search(line)
        if match:
            percent, size, speed, eta = match.groups()
            TASKS[task_id].update({
                "percent": float(percent),
                "speed": speed,
                "eta": eta,
                "status": "downloading"
            })
        elif "Destination:" in line:
            TASKS[task_id]["status"] = "downloading"
        elif "Merging formats" in line or "[Fixup" in line or "ffmpeg" in line.lower():
            TASKS[task_id]["status"] = "processing"
            TASKS[task_id]["percent"] = 99.0

    process.stdout.close()
    return_code = process.wait()

    if return_code == 0:
        files = [f for f in os.listdir(temp_dir) if not f.endswith('.part') and not f.endswith('.ytdl')]
        if files:
            TASKS[task_id]["file_path"] = os.path.join(temp_dir, files[0])
            TASKS[task_id]["status"] = "finished"
            TASKS[task_id]["percent"] = 100.0
        else:
            TASKS[task_id]["status"] = "error"
            TASKS[task_id]["error"] = "Output file compile nahi hui"
    else:
        TASKS[task_id]["status"] = "error"
        TASKS[task_id]["error"] = "Download fail hua ya YouTube ne stream reject ki"

@app.route("/start-task", methods=["POST"])
def start_task():
    data = request.json or {}
    url = data.get("url")
    mode = data.get("mode")
    format_id = data.get("format_id")
    quality = data.get("quality", "1080")

    task_id = str(uuid.uuid4())
    task_temp_dir = os.path.join(TEMP_BASE, task_id)
    os.makedirs(task_temp_dir, exist_ok=True)

    output_template = os.path.join(task_temp_dir, "%(title).60s.%(ext)s")

    if mode == "all":
        cmd = [
            "yt-dlp",
            "--newline",
            "--no-playlist",
            "-N", "4",
            "--audio-multitrack",
            "--remux-video", "mkv",
            "-o", output_template,
            url
        ]
    else:
        # Badi video me best video + specific audio ko clean merge karein
        cmd = [
            "yt-dlp",
            "--newline",
            "--no-playlist",
            "-N", "4",
            "-f", f"bv*[height<={quality}]+{format_id}/b[height<={quality}]",
            "--merge-output-format", "mp4",
            "-o", output_template,
            url
        ]

    TASKS[task_id] = {
        "status": "starting",
        "percent": 0.0,
        "speed": "Connecting...",
        "eta": "Calculating...",
        "temp_dir": task_temp_dir,
        "file_path": None,
        "error": None
    }

    thread = threading.Thread(target=run_download_worker, args=(task_id, cmd, task_temp_dir))
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id})

@app.route("/task-status/<task_id>")
def task_status(task_id):
    task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task nahi mila"}), 404
    return jsonify(task)

@app.route("/get-file/<task_id>")
def get_file(task_id):
    task = TASKS.get(task_id)
    if not task or not task.get("file_path"):
        return "File nahi mili", 404

    file_path = task["file_path"]
    temp_dir = task["temp_dir"]

    @after_this_request
    def cleanup(response):
        def remove_cache():
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                TASKS.pop(task_id, None)
            except Exception:
                pass
        threading.Timer(10.0, remove_cache).start()
        return response

    filename = os.path.basename(file_path)
    return send_file(file_path, as_attachment=True, download_name=filename)

if __name__ == "__main__":
    app.run(debug=True, port=5000)