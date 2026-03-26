#!/usr/bin/env python3
"""
OrpheusDL Web UI — Flask backend
Place this file in the root of your OrpheusDL folder (next to orpheus.py).
Run: python webui.py
Then open: http://localhost:5000
"""

import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, send_file

app = Flask(__name__, static_folder=".", static_url_path="")

ORPHEUS_DIR = Path(__file__).parent
SETTINGS_FILE = ORPHEUS_DIR / "config" / "settings.json"
ORPHEUS_PY = ORPHEUS_DIR / "orpheus.py"

# In-memory job store  {job_id: {"status": ..., "log": [...], "progress": 0}}
jobs: dict[str, dict] = {}
# Active process tracking {job_id: subprocess.Popen}
active_procs: dict[str, subprocess.Popen] = {}


# ── HELPERS ──────────────────────────────────────────────────────────────────

def run_orpheus(args: list[str], job_id: str):
    """Run orpheus.py in a background thread and stream output to job log."""
    job = jobs[job_id]
    job["status"] = "running"
    cmd = ["python", "-u", str(ORPHEUS_PY)] + args + ["--progress"]
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    progress_re = re.compile(r'(\d+)%')
    # tqdm progress bar lines look like: "33%|####2  | 6.29M/19.2M [00:00<00:01, 8.31MB/s]"
    tqdm_re = re.compile(r'^\d+%\|')

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ORPHEUS_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        active_procs[job_id] = proc

        for line in proc.stdout:
            # Handle carriage returns from tqdm or other progress indicators
            if '\r' in line:
                # Split by \r and take the last part that has content
                parts = line.split('\r')
                line = parts[-1] if parts[-1].strip() else (parts[-2] if len(parts) > 1 else parts[0])
            
            line = ansi_escape.sub('', line)
            line = line.strip('\r\n').strip()
            if not line: continue

            # TIDAL auto-login handling: Detect prompt and send "1" (TV)
            if "login type: (1) TV, (2) Mobile" in line:
                try:
                    proc.stdin.write("1\n")
                    proc.stdin.flush()
                    job["log"].append("TIDAL: Automatically selected (1) TV mode...")
                except:
                    pass

            # Whitelist search result lines: if it has metadata tags, it's NOT a logo
            if '|PLATFORM|' in line and '|ID|' in line:
                pass
            else:
                # Filter out the ASCII logo
                logo_markers = ['____', '/  \\', '|  |', '|__|', '\\____']
                if any(marker in line for marker in logo_markers):
                    continue
           
            # Simple progress parsing
            # Look for percentage (tqdm style: " 10%|#   |")
            pm = progress_re.search(line)
            if pm:
                jobs[job_id]["progress"] = int(pm.group(1))
            elif "Downloading" in line:
                if jobs[job_id]["progress"] < 10:
                    jobs[job_id]["progress"] = 10
            elif "Done" in line or "Success" in line:
                jobs[job_id]["progress"] = 100

            # Skip tqdm progress bar lines from the visible log (they clutter output)
            # Progress percentage is already extracted above.
            if tqdm_re.match(line):
                continue

            jobs[job_id]["log"].append(line)

            
        proc.wait()
        if proc.returncode == 0:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
        else:
            jobs[job_id]["status"] = "error"
    except Exception as e:
        job["log"].append(f"ERROR: {e}")
        job["status"] = "error"
    finally:
        if job_id in active_procs:
            del active_procs[job_id]


# ── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Disable caching so UI updates (HTML/CSS/JS) show immediately.
    resp = send_from_directory(".", "orpheusdl-webui.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ── DOWNLOAD BY URL ──

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    quality = data.get("quality", "").strip()  # e.g. LOSSLESS, HIFI, ATMOS …

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    args = []
    if quality:
        args += ["--quality", quality.lower()]
    args.append(url)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "log": [], "progress": 0}
    thread = threading.Thread(target=run_orpheus, args=(args, job_id), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


# ── SEARCH ──

@app.route("/api/job/stop/<job_id>", methods=["POST"])
def api_job_stop(job_id):
    """Terminate an active job."""
    if job_id in active_procs:
        try:
            proc = active_procs[job_id]
            proc.terminate() # Try graceful SIGTERM
            # If it doesn't die in 2 seconds, kill it
            def force_kill():
                import time
                time.sleep(2)
                if proc.poll() is None: proc.kill()
            threading.Thread(target=force_kill).start()
            
            if job_id in jobs:
                jobs[job_id]["status"] = "stopped"
                jobs[job_id]["log"].append("--- JOB STOPPED BY USER ---")
            
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "error", "message": "Job not active"}), 404


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.json or {}
    platform = data.get("platform", "").strip().lower()
    search_type = data.get("type", "track").strip().lower()
    query = data.get("query", "").strip()

    if not platform or not query:
        return jsonify({"error": "platform and query are required"}), 400

    # orpheus.py search <platform> <type> <query> --non-interactive
    args = ["search", platform, search_type, query, "--non-interactive"]

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "log": [], "progress": 0, "search_type": search_type}
    thread = threading.Thread(target=run_orpheus, args=(args, job_id), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/search/download", methods=["POST"])
def api_search_download():
    """Download a result returned from a search job by index."""
    data = request.json or {}
    search_job_id = data.get("search_job_id", "")
    index = data.get("index", 1)  # 1-based
    quality = data.get("quality", "").strip()

    if search_job_id not in jobs:
        return jsonify({"error": "Search job not found"}), 404

    job_info = jobs[search_job_id]
    log = job_info.get("log", [])
    search_type = job_info.get("search_type", "track")

    # Parse URLs from search log — orpheus prints them as numbered list
    results = []
    for line in log:
        line = line.strip()
        if line and line[0].isdigit() and ". " in line:
            result_id = ""
            platform = ""
            if '|ID|' in line:
                result_id = line.split('|ID|')[1].split('|')[0].strip()
            
            if '|PLATFORM|' in line:
                platform = line.split('|PLATFORM|')[1].split('|')[0].strip()

            if not result_id:
                parts = line.split()
                for part in parts:
                    if part.startswith("http"):
                        result_id = part
                        break
            
            if result_id:
                results.append({"id": result_id, "platform": platform})

    if not results:
        # Fallback: pass index directly to orpheus search-download
        return jsonify({"error": "Could not parse search results"}), 400

    try:
        result = results[int(index) - 1]
    except IndexError:
        return jsonify({"error": "Index out of range"}), 400

    url = result["id"]
    platform = result["platform"]

    args = []
    if quality:
        args += ["--quality", quality.lower()]
    
    if url.startswith("http") or not platform:
        args.append(url)
    else:
        args.extend(["download", platform, search_type, url])

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "log": [], "progress": 0, "search_type": search_type}
    thread = threading.Thread(target=run_orpheus, args=(args, job_id), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


# ── BROWSE ──

@app.route("/api/browse", methods=["GET"])
def api_browse():
    """List subdirectories of a given path."""
    rel_path = request.args.get("path", ".").strip()
    try:
        # Resolve path relative to ORPHEUS_DIR or use absolute
        target_dir = Path(rel_path).expanduser()
        if not target_dir.is_absolute():
            target_dir = (ORPHEUS_DIR / rel_path).resolve()

        # Auto-create the directory if it doesn't exist yet
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)

        if not target_dir.is_dir():
            return jsonify({"error": "Path is not a directory"}), 400
            
        items = []
        # Add parent directory
        items.append({"name": "..", "path": str(target_dir.parent), "is_dir": True})
        
        MUSIC_EXTS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.wma', '.alac'}
        for p in sorted(target_dir.iterdir()):
            if p.is_dir():
                items.append({"name": p.name, "path": str(p), "is_dir": True})
            elif p.suffix.lower() in MUSIC_EXTS:
                items.append({"name": p.name, "path": str(p), "is_dir": False, "size": p.stat().st_size})
        
        return jsonify({"current_path": str(target_dir), "items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/view", methods=["GET"])
def api_view():
    """Serve a file for playback/download."""
    file_path = request.args.get("path", "").strip()
    if not file_path:
        return "No path provided", 400
    try:
        p = Path(file_path).expanduser()
        if not p.is_absolute():
            p = (ORPHEUS_DIR / p).resolve()
        
        if not p.exists() or not p.is_file():
            return "File not found", 404
            
        return send_file(p)
    except Exception as e:
        return str(e), 500


# ── JOB STATUS ──

@app.route("/api/job/<job_id>", methods=["GET"])
def api_job_status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(jobs[job_id])


# ── SETTINGS ──

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    if not SETTINGS_FILE.exists():
        return jsonify({"error": "settings.json not found"}), 404
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/raw", methods=["GET"])
def api_settings_raw():
    """Return settings.json as raw text for the textarea editor."""
    if not SETTINGS_FILE.exists():
        return jsonify({"error": "settings.json not found"}), 404
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return jsonify({"raw": f.read()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/raw", methods=["POST"])
def api_settings_raw_save():
    """Save raw JSON text from the textarea editor."""
    data = request.json or {}
    raw = data.get("raw", "")
    try:
        parsed = json.loads(raw)  # validate JSON first
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=4)
        return jsonify({"ok": True})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("OrpheusDL Web UI")
    print(f"  OrpheusDL dir : {ORPHEUS_DIR}")
    print(f"  Settings file : {SETTINGS_FILE}")
    print(f"  Open browser  : http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
