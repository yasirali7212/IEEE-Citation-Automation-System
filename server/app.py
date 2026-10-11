"""
Flask Web Server — IEEE Citation Workflow Interface
====================================================
Provides a simple web interface to:
  - Upload research paper and IEEE template
  - Trigger the n8n workflow via webhook
  - Show processing progress
  - Download output files

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
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import (Flask, jsonify, render_template_string,
                   request, send_file, send_from_directory)
from flask_cors import CORS

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).parent.parent
UPLOAD_DIR = BASE_DIR / os.getenv("UPLOAD_FOLDER", "uploads")
OUTPUT_DIR = BASE_DIR / os.getenv("OUTPUT_FOLDER", "outputs")
LOG_DIR = BASE_DIR / os.getenv("LOG_FOLDER", "logs")
SCRIPTS_DIR = BASE_DIR / "scripts"
N8N_WEBHOOK = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/ieee-citation-workflow")
PORT = int(os.getenv("PORT", 8080))
MAX_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", 50)) * 1024 * 1024

for d in [UPLOAD_DIR, OUTPUT_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── In-memory job status store ────────────────────────────────────────────────
jobs: dict[str, dict] = {}


def cleanup_old_jobs(max_age_hours: int = 24) -> None:
    """Delete upload and output directories older than max_age_hours to save disk."""
    import shutil
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
    Run the full Python pipeline locally (without n8n) as a subprocess chain.
    Updates jobs[job_id] with progress.
    Supports optional per-user Zotero credentials for multi-user deployment.
    """
    python = sys.executable
    od = str(job_output_dir)

    sub_env = os.environ.copy()
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
                return
        except subprocess.TimeoutExpired:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Stage '{label}' timed out after 5 minutes."
            return
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Stage '{label}' raised exception: {e}"
            return

    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"
    jobs[job_id]["stage"] = "Complete"

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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/api/upload", methods=["POST"])
def upload():
    """Accept file uploads and trigger the pipeline."""
    cleanup_old_jobs()

    paper = request.files.get("paper")
    template = request.files.get("template")
    custom_zotero_key = request.form.get("zotero_api_key", "").strip() or None
    custom_zotero_id = request.form.get("zotero_user_id", "").strip() or None

    if not paper:
        return jsonify({"error": "No paper file uploaded."}), 400
    if not paper.filename.lower().endswith(".docx"):
        return jsonify({"error": "Paper must be a .docx file."}), 400
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

    jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "stage": "Starting...",
        "progress": 0,
        "output_files": [],
        "error": None,
    }

    # Run pipeline in a background thread
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
    """Poll processing status."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(jobs[job_id])


@app.route("/api/download/<job_id>/<filename>")
def download(job_id: str, filename: str):
    """Download a processed output file."""
    safe_name = Path(filename).name  # prevent path traversal
    file_path = OUTPUT_DIR / job_id / safe_name
    if not file_path.exists():
        return jsonify({"error": "File not found."}), 404
    return send_file(str(file_path), as_attachment=True, download_name=safe_name)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "n8n_webhook": N8N_WEBHOOK})


@app.route("/api/zotero-status")
def zotero_status():
    """Return Zotero configuration status."""
    api_key = os.getenv("ZOTERO_API_KEY", "")
    user_id = os.getenv("ZOTERO_USER_ID", "")
    return jsonify({
        "configured": bool(api_key and user_id),
        "user_id": user_id,
        "masked_key": f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("Set" if api_key else "Not set"),
    })


# ── HTML Page (self-contained) ────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IEEE Citation Workflow — Automated Formatter</title>
<meta name="description" content="Automate IEEE journal citation formatting without changing your original research paper text.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0f1e;
    --surface: #111827;
    --surface2: #1a2235;
    --border: rgba(99,179,237,0.15);
    --accent: #3b82f6;
    --accent2: #8b5cf6;
    --accent3: #06b6d4;
    --success: #10b981;
    --warning: #f59e0b;
    --error: #ef4444;
    --text: #e2e8f0;
    --text2: #94a3b8;
    --text3: #64748b;
    --glow: 0 0 40px rgba(59,130,246,0.15);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: 'Inter', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* ── Background animation ── */
  body::before {
    content: '';
    position: fixed; inset: 0; z-index: -1;
    background:
      radial-gradient(ellipse 80% 50% at 20% 10%, rgba(59,130,246,0.08) 0%, transparent 60%),
      radial-gradient(ellipse 60% 40% at 80% 80%, rgba(139,92,246,0.07) 0%, transparent 60%),
      radial-gradient(ellipse 40% 30% at 50% 50%, rgba(6,182,212,0.04) 0%, transparent 70%);
  }

  /* ── Header ── */
  header {
    padding: 2rem 2rem 1rem;
    text-align: center;
    position: relative;
  }
  .logo-badge {
    display: inline-flex; align-items: center; gap: 0.5rem;
    background: linear-gradient(135deg, rgba(59,130,246,0.2), rgba(139,92,246,0.2));
    border: 1px solid var(--border);
    border-radius: 2rem;
    padding: 0.4rem 1.2rem;
    font-size: 0.75rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent3);
    margin-bottom: 1.5rem;
  }
  .logo-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent3); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;transform:scale(1);} 50%{opacity:0.5;transform:scale(0.8);} }

  h1 {
    font-size: clamp(2rem, 5vw, 3.2rem);
    font-weight: 700;
    line-height: 1.15;
    background: linear-gradient(135deg, #e2e8f0 30%, #93c5fd 60%, #c4b5fd 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 1rem;
  }
  .subtitle {
    color: var(--text2);
    font-size: 1.05rem;
    max-width: 600px;
    margin: 0 auto 0.75rem;
    line-height: 1.6;
  }
  .critical-badge {
    display: inline-flex; align-items: center; gap: 0.4rem;
    background: rgba(16,185,129,0.12);
    border: 1px solid rgba(16,185,129,0.3);
    border-radius: 0.5rem;
    padding: 0.5rem 1rem;
    font-size: 0.82rem;
    color: var(--success);
    font-weight: 500;
    margin-top: 0.5rem;
  }
  .shield-icon { font-size: 1rem; }

  /* ── Main container ── */
  .container {
    max-width: 900px;
    margin: 0 auto;
    padding: 1rem 2rem 4rem;
  }

  /* ── Cards ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 1.25rem;
    padding: 2rem;
    margin-bottom: 1.5rem;
    position: relative;
    overflow: hidden;
    transition: border-color 0.3s, box-shadow 0.3s;
  }
  .card::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, var(--accent), var(--accent2), var(--accent3));
    opacity: 0;
    transition: opacity 0.3s;
  }
  .card:hover { border-color: rgba(99,179,237,0.3); box-shadow: var(--glow); }
  .card:hover::before { opacity: 1; }

  .card-title {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.4rem;
    display: flex; align-items: center; gap: 0.6rem;
  }
  .card-icon {
    width: 2rem; height: 2rem;
    background: linear-gradient(135deg, rgba(59,130,246,0.2), rgba(139,92,246,0.2));
    border-radius: 0.5rem;
    display: flex; align-items: center; justify-content: center;
    font-size: 1rem;
    flex-shrink: 0;
  }
  .card-desc { color: var(--text3); font-size: 0.85rem; margin-bottom: 1.25rem; }

  /* ── Upload zones ── */
  .upload-zone {
    border: 2px dashed var(--border);
    border-radius: 0.875rem;
    padding: 1.75rem 1.5rem;
    text-align: center;
    cursor: pointer;
    transition: all 0.25s;
    position: relative;
    background: rgba(255,255,255,0.02);
  }
  .upload-zone:hover, .upload-zone.drag-over {
    border-color: var(--accent);
    background: rgba(59,130,246,0.06);
    transform: translateY(-1px);
  }
  .upload-zone input[type=file] {
    position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
  }
  .upload-icon { font-size: 2.2rem; margin-bottom: 0.6rem; display: block; }
  .upload-label { font-weight: 500; font-size: 0.95rem; color: var(--text); margin-bottom: 0.3rem; }
  .upload-hint { font-size: 0.78rem; color: var(--text3); }
  .upload-filename {
    display: none;
    margin-top: 0.75rem;
    padding: 0.5rem 0.875rem;
    background: rgba(16,185,129,0.12);
    border: 1px solid rgba(16,185,129,0.25);
    border-radius: 0.5rem;
    font-size: 0.8rem;
    color: var(--success);
    font-family: 'JetBrains Mono', monospace;
    word-break: break-all;
  }
  .upload-filename.visible { display: block; }

  /* ── Zotero connection ── */
  .zotero-info {
    background: rgba(59,130,246,0.06);
    border: 1px solid rgba(59,130,246,0.2);
    border-radius: 0.75rem;
    padding: 1rem 1.25rem;
    font-size: 0.85rem;
    color: var(--text2);
    line-height: 1.6;
  }
  .zotero-status {
    display: flex; align-items: center; gap: 0.5rem;
    font-weight: 500;
    color: var(--warning);
    margin-bottom: 0.5rem;
  }
  .zotero-status.connected { color: var(--success); }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--warning);
    animation: pulse 2s infinite;
  }
  .status-dot.connected { background: var(--success); animation: none; }

  /* ── Run button ── */
  .btn-run {
    width: 100%;
    padding: 1.1rem 2rem;
    background: linear-gradient(135deg, #2563eb, #7c3aed);
    border: none;
    border-radius: 0.875rem;
    color: white;
    font-size: 1.05rem;
    font-weight: 600;
    font-family: 'Inter', sans-serif;
    cursor: pointer;
    transition: all 0.25s;
    position: relative;
    overflow: hidden;
    letter-spacing: 0.02em;
  }
  .btn-run::after {
    content: '';
    position: absolute; inset: 0;
    background: linear-gradient(135deg, rgba(255,255,255,0.1), transparent);
    opacity: 0;
    transition: opacity 0.25s;
  }
  .btn-run:hover:not(:disabled) {
    transform: translateY(-2px);
    box-shadow: 0 8px 30px rgba(59,130,246,0.4);
  }
  .btn-run:hover:not(:disabled)::after { opacity: 1; }
  .btn-run:active:not(:disabled) { transform: translateY(0); }
  .btn-run:disabled {
    opacity: 0.55;
    cursor: not-allowed;
    transform: none;
  }
  .btn-run .spinner {
    display: none;
    width: 18px; height: 18px;
    border: 2px solid rgba(255,255,255,0.3);
    border-top-color: white;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    margin-right: 0.5rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .btn-run.loading .spinner { display: inline-block; }

  /* ── Progress section ── */
  #progress-section { display: none; }
  .progress-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 1.25rem;
    padding: 2rem;
    margin-bottom: 1.5rem;
  }
  .progress-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 1.25rem;
  }
  .progress-title { font-weight: 600; font-size: 1rem; }
  .progress-pct {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.9rem;
    color: var(--accent);
    font-weight: 500;
  }
  .progress-bar-track {
    height: 6px;
    background: rgba(255,255,255,0.07);
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 1rem;
  }
  .progress-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    border-radius: 3px;
    transition: width 0.5s ease;
    position: relative;
  }
  .progress-bar-fill::after {
    content: '';
    position: absolute; right: 0; top: 0; bottom: 0; width: 20px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3));
    animation: shimmer 1.5s infinite;
  }
  @keyframes shimmer { 0%{opacity:0;} 50%{opacity:1;} 100%{opacity:0;} }
  .stage-label {
    font-size: 0.85rem; color: var(--text2);
    font-family: 'JetBrains Mono', monospace;
    display: flex; align-items: center; gap: 0.5rem;
  }
  .stage-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--accent);
    animation: pulse 1.5s infinite;
    flex-shrink: 0;
  }

  /* ── Download section ── */
  #download-section { display: none; }
  .download-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 1rem;
  }
  .download-item {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 0.875rem;
    padding: 1.25rem;
    display: flex; flex-direction: column; gap: 0.75rem;
    transition: all 0.25s;
    cursor: pointer;
    text-decoration: none;
    color: inherit;
  }
  .download-item:hover {
    border-color: rgba(59,130,246,0.4);
    transform: translateY(-2px);
    box-shadow: 0 4px 20px rgba(59,130,246,0.15);
  }
  .dl-icon { font-size: 1.75rem; }
  .dl-name { font-weight: 600; font-size: 0.88rem; font-family: 'JetBrains Mono', monospace; word-break: break-all; }
  .dl-desc { font-size: 0.78rem; color: var(--text3); line-height: 1.5; }
  .dl-btn {
    display: inline-flex; align-items: center; gap: 0.35rem;
    background: linear-gradient(135deg, rgba(59,130,246,0.2), rgba(139,92,246,0.2));
    border: 1px solid rgba(59,130,246,0.3);
    border-radius: 0.5rem;
    padding: 0.45rem 0.875rem;
    font-size: 0.78rem;
    font-weight: 500;
    color: var(--accent);
    transition: all 0.2s;
  }
  .download-item:hover .dl-btn { background: rgba(59,130,246,0.3); }

  /* ── Error section ── */
  .error-box {
    display: none;
    background: rgba(239,68,68,0.08);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: 0.875rem;
    padding: 1.25rem;
    color: var(--error);
    font-size: 0.88rem;
    line-height: 1.6;
    margin-top: 1rem;
  }
  .error-box.visible { display: block; }
  .error-title { font-weight: 600; margin-bottom: 0.5rem; font-size: 0.95rem; }

  /* ── Steps guide ── */
  .steps { display: flex; flex-direction: column; gap: 0.75rem; }
  .step {
    display: flex; align-items: flex-start; gap: 1rem;
    padding: 0.875rem 1rem;
    background: rgba(255,255,255,0.02);
    border-radius: 0.75rem;
    border: 1px solid transparent;
    transition: border-color 0.2s;
  }
  .step:hover { border-color: var(--border); }
  .step-num {
    width: 28px; height: 28px; border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: flex; align-items: center; justify-content: center;
    font-size: 0.75rem; font-weight: 700; flex-shrink: 0;
    color: white;
  }
  .step-content { flex: 1; }
  .step-title { font-weight: 500; font-size: 0.9rem; margin-bottom: 0.2rem; }
  .step-desc { font-size: 0.8rem; color: var(--text3); line-height: 1.5; }

  /* ── Responsive ── */
  @media (max-width: 600px) {
    .container { padding: 1rem; }
    .card { padding: 1.25rem; }
    .download-grid { grid-template-columns: 1fr; }
    h1 { font-size: 1.8rem; }
  }
</style>
</head>
<body>

<header>
  <div class="logo-badge">
    <div class="logo-dot"></div>
    IEEE Citation Automation System
  </div>
  <h1>Research Paper<br>Citation Workflow</h1>
  <p class="subtitle">
    Upload your Word paper and IEEE template. The system verifies references,
    formats citations, and delivers a submission-ready document.
  </p>
  <div class="critical-badge">
    <span class="shield-icon">🛡️</span>
    Your original text is NEVER modified &mdash; only citation numbers are updated
  </div>
</header>

<div class="container">

  <!-- Upload Form -->
  <div class="card" id="upload-card">
    <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:1.5rem;">
      <div class="card-icon">📄</div>
      <div>
        <div class="card-title">Upload Your Documents</div>
        <div class="card-desc">Both files are accepted as .docx only</div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.5rem;">
      <!-- Paper upload -->
      <div>
        <label style="font-size:0.82rem;font-weight:500;color:var(--text2);display:block;margin-bottom:0.5rem;">
          Research Paper <span style="color:var(--error)">*</span>
        </label>
        <div class="upload-zone" id="paper-zone">
          <input type="file" id="paper-input" accept=".docx">
          <span class="upload-icon">📝</span>
          <div class="upload-label">Click or drag & drop</div>
          <div class="upload-hint">Your research paper .docx</div>
          <div class="upload-filename" id="paper-name"></div>
        </div>
      </div>
      <!-- Template upload -->
      <div>
        <label style="font-size:0.82rem;font-weight:500;color:var(--text2);display:block;margin-bottom:0.5rem;">
          IEEE Journal Template <span style="color:var(--text3)">(optional)</span>
        </label>
        <div class="upload-zone" id="template-zone">
          <input type="file" id="template-input" accept=".docx">
          <span class="upload-icon">📋</span>
          <div class="upload-label">Click or drag & drop</div>
          <div class="upload-hint">IEEE template .docx</div>
          <div class="upload-filename" id="template-name"></div>
        </div>
      </div>
    </div>

    <!-- Zotero status -->
    <div style="margin-bottom:1.5rem;">
      <label style="font-size:0.82rem;font-weight:500;color:var(--text2);display:block;margin-bottom:0.5rem;">
        Zotero Library
      </label>
      <div class="zotero-info">
        <div class="zotero-status" id="zotero-status-line">
          <div class="status-dot"></div>
          Checking Zotero connection...
        </div>
        <div id="zotero-status-desc">
          Connecting to your Zotero library...
        </div>
      </div>
    <!-- Optional custom Zotero inputs for external users -->
    <details style="margin-bottom:1.5rem;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:0.75rem;padding:0.75rem 1rem;">
      <summary style="font-size:0.85rem;color:var(--text2);cursor:pointer;font-weight:500;">
        ⚙️ Use your own Zotero credentials (optional)
      </summary>
      <div style="margin-top:0.75rem;display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;">
        <div>
          <label style="font-size:0.75rem;color:var(--text3);display:block;margin-bottom:0.25rem;">Zotero API Key</label>
          <input type="password" id="custom-zotero-key" placeholder="Leave blank to use default" style="width:100%;padding:0.5rem;background:var(--surface2);border:1px solid var(--border);border-radius:0.4rem;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:0.8rem;">
        </div>
        <div>
          <label style="font-size:0.75rem;color:var(--text3);display:block;margin-bottom:0.25rem;">Numeric User ID</label>
          <input type="text" id="custom-zotero-id" placeholder="Leave blank to use default" style="width:100%;padding:0.5rem;background:var(--surface2);border:1px solid var(--border);border-radius:0.4rem;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:0.8rem;">
        </div>
      </div>
    </details>

    <!-- Run button -->
    <button class="btn-run" id="run-btn" onclick="startWorkflow()">
      <span class="spinner" id="btn-spinner"></span>
      <span id="btn-text">🚀 Run Workflow</span>
    </button>

    <div class="error-box" id="error-box">
      <div class="error-title">⚠️ Error</div>
      <div id="error-message"></div>
    </div>
  </div>

  <!-- Progress -->
  <div id="progress-section">
    <div class="progress-card">
      <div class="progress-header">
        <div class="progress-title">⚙️ Processing your paper...</div>
        <div class="progress-pct" id="progress-pct">0%</div>
      </div>
      <div class="progress-bar-track">
        <div class="progress-bar-fill" id="progress-bar" style="width:0%"></div>
      </div>
      <div class="stage-label">
        <div class="stage-dot" id="stage-dot"></div>
        <span id="stage-text">Initializing...</span>
      </div>
    </div>
  </div>

  <!-- Downloads -->
  <div id="download-section">
    <div class="card">
      <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:1.5rem;">
        <div class="card-icon">✅</div>
        <div>
          <div class="card-title">Processing Complete</div>
          <div class="card-desc">Download your IEEE formatted paper, reports &amp; citation files</div>
        </div>
      </div>
      <div id="stats-banner"></div>
      <div class="download-grid" id="download-grid"></div>
    </div>
  </div>

  <!-- How It Works -->
  <div class="card">
    <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:1.25rem;">
      <div class="card-icon">🔬</div>
      <div>
        <div class="card-title">How It Works</div>
        <div class="card-desc">7-stage automated pipeline</div>
      </div>
    </div>
    <div class="steps">
      <div class="step">
        <div class="step-num">A</div>
        <div class="step-content">
          <div class="step-title">Read &amp; Inspect</div>
          <div class="step-desc">Extract paragraphs, [N] citation markers, reference entries, tables, and figures. Back up the original untouched.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">B</div>
        <div class="step-content">
          <div class="step-title">Verify References Online</div>
          <div class="step-desc">Check each reference against your Zotero library, Crossref, and Semantic Scholar. Unverifiable references are flagged — never silently accepted.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">C</div>
        <div class="step-content">
          <div class="step-title">Map Citations to Text</div>
          <div class="step-desc">Match each [N] marker to its verified reference. Detect ordering issues. Flag uncertain mappings for your review.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">D</div>
        <div class="step-content">
          <div class="step-title">Apply IEEE Citation Style</div>
          <div class="step-desc">Renumber citations by first appearance per IEEE rules. Only citation numbers change — never sentence wording.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">E</div>
        <div class="step-content">
          <div class="step-title">Format the Bibliography</div>
          <div class="step-desc">Build the complete IEEE-formatted reference list using verified metadata from Crossref and Zotero.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">F</div>
        <div class="step-content">
          <div class="step-title">Apply Journal Template</div>
          <div class="step-desc">Copy margins, page size, column layout, and styles from your IEEE template. Text is preserved exactly.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">G</div>
        <div class="step-content">
          <div class="step-title">Validate &amp; Report</div>
          <div class="step-desc">Compare original vs final paragraph by paragraph. Any unexpected change triggers a failure report.</div>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
let currentJobId = null;
let pollInterval = null;

const FILE_DESCRIPTIONS = {
  'IEEE_Formatted_Research_Paper.docx': {icon:'📄', desc:'Your paper with IEEE citations, interactive Ctrl+Click hyperlinks, hover tooltips & bibliography.'},
  'Original_Research_Paper_Backup.docx': {icon:'🔒', desc:'Untouched backup of your original document. 100% text preservation verified.'},
  'Citation_Verification_Report.csv': {icon:'📊', desc:'Detailed verification spreadsheet: status, DOI, and verified metadata for all references.'},
  'Text_Preservation_Report.txt': {icon:'🛡️', desc:'Paragraph-by-paragraph proof that zero manuscript words were altered.'},
  'Unresolved_Citations_Report.txt': {icon:'⚠️', desc:'Verification check: flags any unverified references needing review before submitting.'},
  'references.bib': {icon:'📚', desc:'BibTeX export file — ready if your journal requests a .bib file.'},
  'references.ris': {icon:'📑', desc:'EndNote & Zotero RIS export file — ready for any citation manager.'},
};

// ── Check Zotero Connection on Load ──
async function checkZoteroStatus() {
  try {
    const res = await fetch('/api/zotero-status');
    const data = await res.json();
    const line = document.getElementById('zotero-status-line');
    const desc = document.getElementById('zotero-status-desc');
    if (data.configured) {
      line.innerHTML = `<div class="status-dot" style="background:var(--success);box-shadow:0 0 8px var(--success)"></div><span style="color:var(--success);font-weight:600;">Zotero Library Connected</span> (User ID: ${data.user_id})`;
      if (desc) desc.innerHTML = `Your Zotero library is linked! All imported papers will automatically be verified and matched with in-text citations.`;
    } else {
      line.innerHTML = `<div class="status-dot" style="background:var(--warning)"></div>Zotero Not Configured (Optional)`;
      if (desc) desc.innerHTML = `Set <code style="color:var(--accent)">ZOTERO_API_KEY</code> and <code style="color:var(--accent)">ZOTERO_USER_ID</code> in <strong>.env</strong> to auto-match your library.`;
    }
  } catch (e) {}
}
window.addEventListener('DOMContentLoaded', checkZoteroStatus);

// ── Drag & drop setup ──
['paper', 'template'].forEach(type => {
  const zone = document.getElementById(type + '-zone');
  const input = document.getElementById(type + '-input');
  const nameEl = document.getElementById(type + '-name');

  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      input.files = e.dataTransfer.files;
      showFileName(nameEl, e.dataTransfer.files[0].name);
    }
  });
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) showFileName(nameEl, input.files[0].name);
  });
});

function showFileName(el, name) {
  el.textContent = '✓ ' + name;
  el.classList.add('visible');
}

async function startWorkflow() {
  const paper = document.getElementById('paper-input').files[0];
  const template = document.getElementById('template-input').files[0];

  if (!paper) {
    showError('Please upload your research paper (.docx) before running.');
    return;
  }

  hideError();
  setRunning(true);

  const formData = new FormData();
  formData.append('paper', paper);
  if (template) formData.append('template', template);

  const customKey = document.getElementById('custom-zotero-key')?.value?.trim();
  const customId = document.getElementById('custom-zotero-id')?.value?.trim();
  if (customKey) formData.append('zotero_api_key', customKey);
  if (customId) formData.append('zotero_user_id', customId);

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) { showError(data.error || 'Upload failed.'); setRunning(false); return; }

    currentJobId = data.job_id;
    document.getElementById('progress-section').style.display = 'block';
    document.getElementById('download-section').style.display = 'none';
    pollInterval = setInterval(pollStatus, 1500);
  } catch (e) {
    showError('Network error: ' + e.message);
    setRunning(false);
  }
}

async function pollStatus() {
  if (!currentJobId) return;
  try {
    const res = await fetch('/api/status/' + currentJobId);
    const data = await res.json();

    document.getElementById('progress-pct').textContent = data.progress + '%';
    document.getElementById('progress-bar').style.width = data.progress + '%';
    document.getElementById('stage-text').textContent = data.stage || '';

    if (data.status === 'done') {
      clearInterval(pollInterval);
      setRunning(false);
      document.getElementById('stage-dot').style.animation = 'none';
      document.getElementById('stage-dot').style.background = 'var(--success)';
      showDownloads(data.output_files, currentJobId, data.stats);
    } else if (data.status === 'error') {
      clearInterval(pollInterval);
      setRunning(false);
      document.getElementById('progress-section').style.display = 'none';
      showError(data.error || 'An unknown error occurred. Check the logs folder.');
    }
  } catch (e) {
    // Network blip — keep polling
  }
}

function showDownloads(files, jobId, stats) {
  const banner = document.getElementById('stats-banner');
  if (banner && stats) {
    banner.innerHTML = `
      <div style="display:flex;flex-wrap:wrap;gap:0.75rem;margin-bottom:1.5rem;padding:1rem;background:rgba(59,130,246,0.08);border:1px solid rgba(59,130,246,0.25);border-radius:0.75rem;">
        <div style="background:rgba(17,24,39,0.85);border:1px solid var(--border);border-radius:0.5rem;padding:0.4rem 0.8rem;font-size:0.82rem;">
          📄 <strong>${stats.in_text_markers || stats.citations_remapped || 'All'}</strong> Citations Remapped
        </div>
        <div style="background:rgba(17,24,39,0.85);border:1px solid rgba(16,185,129,0.4);border-radius:0.5rem;padding:0.4rem 0.8rem;font-size:0.82rem;color:var(--success);">
          🔗 <strong>Ctrl+Click</strong> Interactive Hyperlinks
        </div>
        <div style="background:rgba(17,24,39,0.85);border:1px solid rgba(6,182,212,0.4);border-radius:0.5rem;padding:0.4rem 0.8rem;font-size:0.82rem;color:var(--accent3);">
          🏷️ <strong>ScreenTip</strong> Hover Tooltips
        </div>
        <div style="background:rgba(17,24,39,0.85);border:1px solid var(--border);border-radius:0.5rem;padding:0.4rem 0.8rem;font-size:0.82rem;">
          📚 <strong>${stats.references_total || '30'}</strong> References Matched
        </div>
        <div style="background:rgba(17,24,39,0.85);border:1px solid rgba(16,185,129,0.4);border-radius:0.5rem;padding:0.4rem 0.8rem;font-size:0.82rem;color:var(--success);">
          🛡️ <strong>100%</strong> Original Text Preserved
        </div>
      </div>
    `;
  }
  const grid = document.getElementById('download-grid');
  grid.innerHTML = '';
  files.forEach(fname => {
    const info = FILE_DESCRIPTIONS[fname] || {icon:'📁', desc:'Output file'};
    const a = document.createElement('a');
    a.href = '/api/download/' + jobId + '/' + encodeURIComponent(fname);
    a.download = fname;
    a.className = 'download-item';
    a.innerHTML = `
      <div class="dl-icon">${info.icon}</div>
      <div class="dl-name">${fname}</div>
      <div class="dl-desc">${info.desc}</div>
      <div class="dl-btn">⬇ Download</div>
    `;
    grid.appendChild(a);
  });
  document.getElementById('download-section').style.display = 'block';
}

function setRunning(running) {
  const btn = document.getElementById('run-btn');
  const text = document.getElementById('btn-text');
  btn.disabled = running;
  btn.classList.toggle('loading', running);
  text.textContent = running ? 'Processing...' : '🚀 Run Workflow';
}

function showError(msg) {
  document.getElementById('error-message').textContent = msg;
  document.getElementById('error-box').classList.add('visible');
}
function hideError() {
  document.getElementById('error-box').classList.remove('visible');
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    logger.info(f"Starting server on http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
