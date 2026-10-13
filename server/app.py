"""
Flask Web Server — IEEE Citation Workflow Interface
====================================================
Provides an enterprise-grade academic automation web interface to:
  - Securely authenticate users (Signup, Login, Sessions)
  - Safely isolate personal Zotero API credentials per researcher
  - Upload research manuscripts (.docx) and optional IEEE templates
  - Trigger multi-database reference verification & citation renumbering
  - Track live progress with real-time stage milestones
  - Download submission-ready Word manuscripts, reports & ZIP bundles

Runs on http://localhost:8080 by default.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import (Flask, jsonify, render_template, request,
                   send_file, session)
from flask_cors import CORS

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from server.db import (init_db, create_user, authenticate_user,
                           get_user_by_id, update_user_zotero,
                           record_user_job, update_job_status, get_user_jobs)
except ImportError:
    from db import (init_db, create_user, authenticate_user,
                    get_user_by_id, update_user_zotero,
                    record_user_job, update_job_status, get_user_jobs)

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TEMPLATE_DIR = BASE_DIR / "server" / "templates"
STATIC_DIR = BASE_DIR / "server" / "static"
UPLOAD_DIR = BASE_DIR / os.getenv("UPLOAD_FOLDER", "uploads")
OUTPUT_DIR = BASE_DIR / os.getenv("OUTPUT_FOLDER", "outputs")
LOG_DIR = BASE_DIR / os.getenv("LOG_FOLDER", "logs")
SCRIPTS_DIR = BASE_DIR / "scripts"
N8N_WEBHOOK = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/ieee-citation-workflow")
PORT = int(os.getenv("PORT", 8080))
MAX_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", 50)) * 1024 * 1024

for d in [UPLOAD_DIR, OUTPUT_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Initialize persistent SQLite database
init_db()

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
CORS(app)

# Session Security Configuration
app.secret_key = os.getenv("SECRET_KEY", "ieee-citation-workflow-session-secret-token-2026")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ── In-memory active job store ────────────────────────────────────────────────
jobs: dict[str, dict] = {}


def cleanup_old_jobs(max_age_hours: int = 24) -> None:
    """Delete upload and output directories older than max_age_hours to save disk."""
    now = time.time()
    for base in [UPLOAD_DIR, OUTPUT_DIR, LOG_DIR]:
        if not base.exists():
            continue
        for item in base.iterdir():
            try:
                if item.is_dir() and (now - item.stat().st_mtime) > (max_age_hours * 3600):
                    shutil.rmtree(item, ignore_errors=True)
                elif item.is_file() and (now - item.stat().st_mtime) > (max_age_hours * 3600):
                    item.unlink(missing_ok=True)
            except Exception:
                pass


def run_pipeline_locally(job_id: str, paper_path: str,
                          template_path: str | None,
                          job_output_dir: Path,
                          custom_zotero_key: str | None = None,
                          custom_zotero_id: str | None = None) -> None:
    """
    Run the full Python pipeline locally as an isolated subprocess chain.
    Updates jobs[job_id] with progress and updates database job status.
    Supports per-user private Zotero credentials with complete cross-user isolation.
    """
    python = sys.executable
    od = str(job_output_dir)

    sub_env = os.environ.copy()
    # Strip any global server Zotero credentials for complete multi-user isolation
    sub_env.pop("ZOTERO_API_KEY", None)
    sub_env.pop("ZOTERO_USER_ID", None)
    if custom_zotero_key:
        sub_env["ZOTERO_API_KEY"] = custom_zotero_key
    if custom_zotero_id:
        sub_env["ZOTERO_USER_ID"] = custom_zotero_id

    stages = [
        ("Extracting document...",
         [python, str(SCRIPTS_DIR / "extract_document.py"), paper_path, od]),
        ("Fetching Zotero library...",
         [python, str(SCRIPTS_DIR / "zotero_client.py"), od]),
        ("Verifying references online...",
         [python, str(SCRIPTS_DIR / "verify_references.py"), od]),
        ("Mapping and renumbering citations...",
         [python, str(SCRIPTS_DIR / "match_citations.py"), od]),
        ("Applying IEEE citation format...",
         [python, str(SCRIPTS_DIR / "format_citations.py"), od, paper_path]),
        ("Applying journal template...",
         [python, str(SCRIPTS_DIR / "apply_template.py"), od] +
         ([template_path] if template_path else [])),
        ("Validating text preservation...",
         [python, str(SCRIPTS_DIR / "validate_document.py"), od]),
        ("Generating reports...",
         [python, str(SCRIPTS_DIR / "report_generator.py"), od]),
    ]

    total = len(stages)
    for i, (label, cmd) in enumerate(stages):
        jobs[job_id]["stage"] = label
        jobs[job_id]["progress"] = int((i / total) * 100)
        logger.info(f"[{job_id}] Stage {i+1}/{total}: {label}")

        log_file = LOG_DIR / f"{job_id}_stage_{i+1}.log"
        try:
            with open(log_file, 'w', encoding='utf-8') as lf:
                result = subprocess.run(
                    cmd, stdout=lf, stderr=subprocess.STDOUT,
                    timeout=300, check=False, env=sub_env,
                )
            if result.returncode != 0:
                with open(log_file, encoding='utf-8') as lf:
                    last_lines = lf.readlines()[-20:]
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = (
                    f"Stage '{label}' failed (exit {result.returncode}). "
                    f"Last log: {''.join(last_lines)}"
                )
                update_job_status(job_id, "error")
                return
        except subprocess.TimeoutExpired:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Stage '{label}' timed out after 5 minutes."
            update_job_status(job_id, "error")
            return
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Stage '{label}' raised exception: {e}"
            update_job_status(job_id, "error")
            return

    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"
    jobs[job_id]["stage"] = "Complete"
    update_job_status(job_id, "done")

    # Collect output files
    output_files = []
    expected = [
        "IEEE_Formatted_Research_Paper.docx",
        "Original_Research_Paper_Backup.docx",
        "Citation_Verification_Report.csv",
        "Text_Preservation_Report.txt",
        "Unresolved_Citations_Report.txt",
        "references.bib",
        "references.ris",
    ]
    for fname in expected:
        fp = job_output_dir / fname
        if fp.exists():
            output_files.append(fname)
    jobs[job_id]["output_files"] = output_files

    # Collect summary stats for UI
    stats = {}
    try:
        cmap_file = job_output_dir / "citation_map.json"
        ext_file = job_output_dir / "extraction.json"
        ver_file = job_output_dir / "verified_references.json"
        if cmap_file.exists():
            with open(cmap_file, encoding="utf-8") as f:
                cm = json.load(f)
                stats["citations_remapped"] = len(cm.get("first_appearance_order", []))
                stats["references_total"] = len(cm.get("new_to_ref", {}))
                stats["unresolved"] = len(cm.get("unresolved", []))
        if ext_file.exists():
            with open(ext_file, encoding="utf-8") as f:
                ex = json.load(f)
                stats["paragraphs_total"] = len(ex.get("paragraphs", []))
                stats["in_text_markers"] = len(ex.get("citation_markers", []))
        if ver_file.exists():
            with open(ver_file, encoding="utf-8") as f:
                vr = json.load(f)
                stats["verified_zotero"] = sum(1 for r in vr if r.get("verification_source") == "zotero")
                stats["verified_total"] = sum(1 for r in vr if r.get("verified"))
    except Exception as e:
        logger.warning(f"Could not load stats: {e}")
    jobs[job_id]["stats"] = stats
    logger.info(f"[{job_id}] Pipeline complete. Files: {output_files}, Stats: {stats}")


# ── Web Application & API Routes ──────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the modern IEEE Citation Automation suite interface."""
    return render_template("index.html")


# ── User Authentication Endpoints ────────────────────────────────────────────

@app.route("/api/auth/signup", methods=["POST"])
def auth_signup():
    """Register a new researcher account."""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    email = data.get("email", "").strip() or None
    zotero_key = data.get("zotero_api_key", "").strip() or None
    zotero_id = data.get("zotero_user_id", "").strip() or None

    if not username:
        return jsonify({"error": "Username is required."}), 400
    if not password:
        return jsonify({"error": "Password is required."}), 400

    # Auto-resolve numeric Zotero ID if key provided but no ID
    if zotero_key and not zotero_id:
        try:
            res = requests.get(
                f"https://api.zotero.org/keys/{zotero_key}",
                headers={"Zotero-API-Version": "3"},
                timeout=8
            )
            if res.status_code == 200:
                zotero_id = str(res.json().get("userID", ""))
        except Exception:
            pass

    try:
        user = create_user(
            username=username,
            password=password,
            email=email,
            zotero_api_key=zotero_key,
            zotero_user_id=zotero_id,
        )
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return jsonify({"user": user, "message": "Account created successfully."}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Signup error: {e}")
        return jsonify({"error": "Internal server error during registration."}), 500


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Authenticate researcher credentials and establish session."""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Both username and password are required."}), 400

    user = authenticate_user(username, password)
    if not user:
        return jsonify({"error": "Invalid username or password."}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return jsonify({"user": user, "message": "Login successful."})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Clear session and log user out."""
    session.clear()
    return jsonify({"message": "Logged out successfully."})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    """Return authenticated researcher profile with masked credentials."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"logged_in": False, "user": None})

    user = get_user_by_id(user_id)
    if not user:
        session.clear()
        return jsonify({"logged_in": False, "user": None})

    return jsonify({"logged_in": True, "user": user})


@app.route("/api/auth/update-zotero", methods=["POST"])
def auth_update_zotero():
    """Update private Zotero credentials for the currently logged in researcher."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    data = request.get_json(silent=True) or {}
    key = data.get("zotero_api_key", "").strip() or None
    uid = data.get("zotero_user_id", "").strip() or None

    # Auto-resolve numeric User ID if key provided but no ID
    if key and (not uid or not uid.isdigit()):
        try:
            res = requests.get(
                f"https://api.zotero.org/keys/{key}",
                headers={"Zotero-API-Version": "3"},
                timeout=8
            )
            if res.status_code == 200:
                uid = str(res.json().get("userID", ""))
        except Exception:
            pass

    success = update_user_zotero(user_id, key, uid)
    if not success:
        return jsonify({"error": "User account not found."}), 404

    updated_user = get_user_by_id(user_id)
    return jsonify({
        "message": "Zotero credentials updated successfully.",
        "user": updated_user
    })


@app.route("/api/user/jobs", methods=["GET"])
def user_jobs_list():
    """Return past jobs submitted by the authenticated user."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"jobs": []})

    return jsonify({"jobs": get_user_jobs(user_id)})


# ── Citation Workflow Execution Endpoints ────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def upload():
    """Accept manuscript uploads, resolve user Zotero keys, and trigger processing."""
    cleanup_old_jobs()

    paper = request.files.get("paper")
    template = request.files.get("template")
    custom_zotero_key = request.form.get("zotero_api_key", "").strip() or None
    custom_zotero_id = request.form.get("zotero_user_id", "").strip() or None

    # If user is logged in and didn't provide a temporary key, load their saved account credentials
    user_id = session.get("user_id")
    if user_id and not custom_zotero_key:
        user_record = get_user_by_id(user_id, include_secret=True)
        if user_record and user_record.get("zotero_api_key"):
            custom_zotero_key = user_record["zotero_api_key"]
            custom_zotero_id = user_record.get("zotero_user_id")

    if not paper:
        return jsonify({"error": "No research paper uploaded."}), 400
    if not paper.filename.lower().endswith(".docx"):
        return jsonify({"error": "Research paper must be a .docx file."}), 400
    if paper.stream.seek(0, 2) > MAX_SIZE:
        return jsonify({"error": f"File too large (max {MAX_SIZE // 1024 // 1024}MB)."}), 400
    paper.stream.seek(0)

    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / job_id
    job_output_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job_output_dir.mkdir(parents=True, exist_ok=True)

    paper_path = str(job_dir / "paper.docx")
    paper.save(paper_path)

    template_path = None
    if template and template.filename.lower().endswith(".docx"):
        template_path = str(job_dir / "template.docx")
        template.save(template_path)

    # Record job in database for logged in user
    record_user_job(user_id, job_id, paper.filename, status="running")

    jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "stage": "Starting...",
        "progress": 0,
        "output_files": [],
        "error": None,
    }

    # Run pipeline in a background worker thread
    import threading
    t = threading.Thread(
        target=run_pipeline_locally,
        args=(job_id, paper_path, template_path, job_output_dir,
              custom_zotero_key, custom_zotero_id),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id: str):
    """Poll real-time processing status and pipeline statistics."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(jobs[job_id])


@app.route("/api/download/<job_id>/<filename>")
def download(job_id: str, filename: str):
    """Download an individual processed output deliverable."""
    safe_name = Path(filename).name  # prevent path traversal
    file_path = OUTPUT_DIR / job_id / safe_name
    if not file_path.exists():
        return jsonify({"error": "File not found."}), 404
    return send_file(str(file_path), as_attachment=True, download_name=safe_name)


@app.route("/api/download-all/<job_id>")
def download_all_zip(job_id: str):
    """Package all output deliverables for a job into a convenient ZIP bundle."""
    target_dir = OUTPUT_DIR / job_id
    if not target_dir.exists():
        return jsonify({"error": "Job outputs not found."}), 404

    zip_filename = f"IEEE_Submission_Bundle_{job_id}.zip"
    zip_path = target_dir / zip_filename

    # Build ZIP archive of all valid deliverables
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for f in target_dir.iterdir():
            if f.is_file() and f.name != zip_filename and not f.name.endswith('.tmp'):
                zipf.write(f, arcname=f.name)

    return send_file(str(zip_path), as_attachment=True, download_name=zip_filename)


@app.route("/api/health")
def health():
    """Health check for deployment platforms."""
    return jsonify({
        "status": "ok",
        "version": "2.2.0",
        "n8n_webhook": N8N_WEBHOOK,
        "database": "sqlite_ready"
    })


@app.route("/api/validate-zotero", methods=["POST"])
def validate_zotero():
    """Verify a user's private Zotero key and return their user ID & library username."""
    body = request.get_json(silent=True) or {}
    key = body.get("api_key", "").strip()
    if not key:
        return jsonify({"valid": False, "error": "Please enter an API key to test."})

    try:
        res = requests.get(
            f"https://api.zotero.org/keys/{key}",
            headers={"Zotero-API-Version": "3"},
            timeout=8
        )
        if res.status_code == 200:
            data = res.json()
            user_id = str(data.get("userID", ""))
            username = data.get("username", "")
            return jsonify({
                "valid": True,
                "user_id": user_id,
                "username": username,
                "message": f"Verified! Connected to Zotero (User ID: {user_id})"
            })
        elif res.status_code in (401, 403):
            return jsonify({
                "valid": False,
                "error": "Invalid API key or key lacks library read permission."
            })
        else:
            return jsonify({
                "valid": False,
                "error": f"Zotero API error (status {res.status_code})"
            })
    except Exception as e:
        return jsonify({"valid": False, "error": f"Connection error: {str(e)}"})


if __name__ == "__main__":
    logger.info(f"Starting IEEE Citation Automation Server on http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
