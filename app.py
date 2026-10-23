"""
KDP Formatter — local Flask web app that merges chapter PDFs into one
Amazon KDP-ready interior PDF. Everything runs on your machine; no cloud APIs.

Run:  python app.py            (production server via waitress, http://127.0.0.1:5000)
      python app.py --debug    (Flask development server with auto-reload)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List

from flask import (Flask, abort, jsonify, render_template, request, send_from_directory,
                   session)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from kdp_processor import (TRIM_SIZES, KDPProcessingError, KDPSettings, inspect_pdf,
                           process_chapters, render_thumbnail)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("KDP_DATA_DIR", os.path.join(BASE_DIR, "instance")))
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
MAX_UPLOAD_MB = int(os.environ.get("KDP_MAX_UPLOAD_MB", "500"))
MAX_FILES = int(os.environ.get("KDP_MAX_FILES", "200"))
SESSION_TTL_HOURS = float(os.environ.get("KDP_SESSION_TTL_HOURS", "24"))

ID_RE = re.compile(r"^[a-f0-9]{32}$")
OUTPUT_RE = re.compile(r"^[A-Za-z0-9._-]{1,180}\.pdf$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kdp_formatter")


def _load_secret_key() -> str:
    """Persist a random secret key so sessions survive restarts."""
    env_key = os.environ.get("KDP_SECRET_KEY")
    if env_key:
        return env_key
    os.makedirs(DATA_DIR, exist_ok=True)
    key_file = os.path.join(DATA_DIR, "secret_key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=int(SESSION_TTL_HOURS * 3600),
)

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_last_cleanup = 0.0


# --------------------------------------------------------------------------- #
# Session storage helpers (server-side folder keyed by a session UUID)
# --------------------------------------------------------------------------- #

def _session_id() -> str:
    sid = session.get("sid")
    if not sid or not ID_RE.match(sid):
        sid = uuid.uuid4().hex
        session["sid"] = sid
        session.permanent = True
    return sid


def _session_dir(sid: str) -> str:
    path = os.path.join(SESSIONS_DIR, sid)
    os.makedirs(os.path.join(path, "chapters"), exist_ok=True)
    os.makedirs(os.path.join(path, "output"), exist_ok=True)
    return path


def _lock_for(sid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(sid, threading.Lock())


def _manifest_path(sid: str) -> str:
    return os.path.join(_session_dir(sid), "manifest.json")


def _load_manifest(sid: str) -> dict:
    path = _manifest_path(sid)
    if not os.path.exists(path):
        return {"files": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("files"), list):
            return data
    except (OSError, ValueError):
        log.warning("Corrupt manifest for session %s; starting fresh", sid)
    return {"files": []}


def _save_manifest(sid: str, manifest: dict) -> None:
    path = _manifest_path(sid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, path)


def _chapter_path(sid: str, file_id: str) -> str:
    return os.path.join(_session_dir(sid), "chapters", f"{file_id}.pdf")


def _public_files(manifest: dict) -> List[dict]:
    return [{k: f[k] for k in ("id", "name", "pages", "size", "width_in", "height_in")}
            for f in manifest["files"]]


def _cleanup_expired_sessions() -> None:
    """Remove session folders untouched for longer than SESSION_TTL_HOURS (runs at most every 10 min)."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 600:
        return
    _last_cleanup = now
    if not os.path.isdir(SESSIONS_DIR):
        return
    cutoff = now - SESSION_TTL_HOURS * 3600
    for name in os.listdir(SESSIONS_DIR):
        path = os.path.join(SESSIONS_DIR, name)
        try:
            if ID_RE.match(name) and os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                log.info("Removed expired session %s", name)
        except OSError:
            continue


def _touch(sid: str) -> None:
    try:
        os.utime(_session_dir(sid), None)
    except OSError:
        pass


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.before_request
def _before_request():
    _cleanup_expired_sessions()


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_exc):
    return _error(f"Upload too large. The limit is {MAX_UPLOAD_MB} MB per request.", 413)


@app.errorhandler(HTTPException)
def _http_error(exc: HTTPException):
    if request.path.startswith(("/api", "/upload", "/reorder", "/generate", "/files", "/download")):
        return _error(exc.description or exc.name, exc.code or 500)
    return exc


@app.errorhandler(Exception)
def _unhandled(exc: Exception):
    log.exception("Unhandled error: %s", exc)
    return _error("Unexpected server error. Check the terminal log for details.", 500)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    _session_id()
    return render_template(
        "index.html",
        trim_sizes=list(TRIM_SIZES.keys()),
        defaults=KDPSettings(),
        max_upload_mb=MAX_UPLOAD_MB,
    )


@app.get("/files")
def list_files():
    sid = _session_id()
    with _lock_for(sid):
        return jsonify({"ok": True, "files": _public_files(_load_manifest(sid))})


@app.post("/upload")
def upload():
    sid = _session_id()
    incoming = request.files.getlist("files")
    if not incoming:
        return _error("No files received. Choose one or more PDF chapters.")

    added, rejected = [], []
    with _lock_for(sid):
        manifest = _load_manifest(sid)
        for storage in incoming:
            name = os.path.basename((storage.filename or "chapter.pdf").replace("\\", "/")).strip() or "chapter.pdf"
            name = name[:200]
            if len(manifest["files"]) >= MAX_FILES:
                rejected.append({"name": name, "error": f"Maximum of {MAX_FILES} chapters reached."})
                continue
            if not name.lower().endswith(".pdf"):
                rejected.append({"name": name, "error": "Only .pdf files are accepted."})
                continue
            head = storage.stream.read(1024)
            storage.stream.seek(0)
            if b"%PDF-" not in head:
                rejected.append({"name": name, "error": "File is not a valid PDF."})
                continue

            file_id = uuid.uuid4().hex
            dest = _chapter_path(sid, file_id)
            storage.save(dest)
            try:
                facts = inspect_pdf(dest, name)
            except KDPProcessingError as exc:
                os.remove(dest)
                rejected.append({"name": name, "error": str(exc)})
                continue
            entry = {
                "id": file_id,
                "name": name,
                "size": os.path.getsize(dest),
                "uploaded_at": datetime.now().isoformat(timespec="seconds"),
                **facts,
            }
            manifest["files"].append(entry)
            added.append(entry["id"])
        _save_manifest(sid, manifest)
        files = _public_files(manifest)
    _touch(sid)
    log.info("Session %s: %d uploaded, %d rejected", sid[:8], len(added), len(rejected))
    return jsonify({"ok": True, "files": files, "added": added, "rejected": rejected})


@app.post("/reorder")
def reorder():
    sid = _session_id()
    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
        return _error("Invalid order payload; expected {\"order\": [ids...]}.")
    with _lock_for(sid):
        manifest = _load_manifest(sid)
        by_id = {f["id"]: f for f in manifest["files"]}
        if len(order) != len(by_id) or set(order) != set(by_id):
            return _error("Chapter list is out of date. Refresh the page and try again.", 409)
        manifest["files"] = [by_id[i] for i in order]
        _save_manifest(sid, manifest)
        return jsonify({"ok": True, "files": _public_files(manifest)})


@app.delete("/files/<file_id>")
def delete_file(file_id: str):
    if not ID_RE.match(file_id):
        abort(404)
    sid = _session_id()
    with _lock_for(sid):
        manifest = _load_manifest(sid)
        before = len(manifest["files"])
        manifest["files"] = [f for f in manifest["files"] if f["id"] != file_id]
        if len(manifest["files"]) == before:
            return _error("Chapter not found.", 404)
        for path in (_chapter_path(sid, file_id), _chapter_path(sid, file_id)[:-4] + ".png"):
            if os.path.exists(path):
                os.remove(path)
        _save_manifest(sid, manifest)
        return jsonify({"ok": True, "files": _public_files(manifest)})


@app.post("/files/clear")
def clear_files():
    sid = _session_id()
    with _lock_for(sid):
        shutil.rmtree(os.path.join(SESSIONS_DIR, sid), ignore_errors=True)
        _save_manifest(sid, {"files": []})
    return jsonify({"ok": True, "files": []})


@app.get("/files/<file_id>/thumbnail")
def thumbnail(file_id: str):
    if not ID_RE.match(file_id):
        abort(404)
    sid = _session_id()
    pdf_path = _chapter_path(sid, file_id)
    if not os.path.exists(pdf_path):
        abort(404)
    png_path = pdf_path[:-4] + ".png"
    if not os.path.exists(png_path):
        try:
            render_thumbnail(pdf_path, png_path)
        except Exception:
            abort(404)
    response = send_from_directory(os.path.dirname(png_path), os.path.basename(png_path), mimetype="image/png")
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


def _parse_settings(payload: dict) -> KDPSettings:
    def num(key: str, default: float) -> float:
        value = payload.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            raise KDPProcessingError(f"'{key}' must be a number.")

    def flag(key: str, default: bool) -> bool:
        value = payload.get(key, default)
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    def text(key: str) -> str:
        return str(payload.get(key, "") or "").strip()

    d = KDPSettings()
    trim = text("trim_size") or "6x9"
    if trim not in TRIM_SIZES:
        raise KDPProcessingError(f"Unsupported trim size '{trim}'.")
    width, height = TRIM_SIZES[trim]
    return KDPSettings(
        trim_width_in=width,
        trim_height_in=height,
        margin_top_in=num("margin_top_in", d.margin_top_in),
        margin_bottom_in=num("margin_bottom_in", d.margin_bottom_in),
        margin_inner_in=num("margin_inner_in", d.margin_inner_in),
        margin_outer_in=num("margin_outer_in", d.margin_outer_in),
        trim_whitespace=flag("trim_whitespace", d.trim_whitespace),
        remove_blank_pages=flag("remove_blank_pages", d.remove_blank_pages),
        chapters_start_recto=flag("chapters_start_recto", d.chapters_start_recto),
        add_page_numbers=flag("add_page_numbers", d.add_page_numbers),
        allow_upscale=flag("allow_upscale", d.allow_upscale),
        title=text("title"),
        author=text("author"),
        subject=text("subject"),
        keywords=text("keywords"),
        language=text("language") or d.language,
    )


def _safe_filename(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:80]
    return slug or "Book"


@app.post("/generate")
def generate():
    sid = _session_id()
    payload = request.get_json(silent=True) or {}
    lock = _lock_for(sid)
    if not lock.acquire(timeout=5):
        return _error("Another operation is in progress for this session. Please wait.", 409)
    try:
        manifest = _load_manifest(sid)
        if not manifest["files"]:
            return _error("Upload at least one chapter PDF first.")
        try:
            settings = _parse_settings(payload)
            out_dir = os.path.join(_session_dir(sid), "output")
            for old in os.listdir(out_dir):
                os.remove(os.path.join(out_dir, old))
            filename = f"{_safe_filename(settings.title)}_KDP_{settings.trim_width_in:g}x{settings.trim_height_in:g}_" \
                       f"{datetime.now():%Y%m%d-%H%M%S}.pdf"
            chapters = [(_chapter_path(sid, f["id"]), f["name"]) for f in manifest["files"]]
            started = time.perf_counter()
            report = process_chapters(chapters, os.path.join(out_dir, filename), settings)
            elapsed = time.perf_counter() - started
        except KDPProcessingError as exc:
            return _error(str(exc), 422)
    finally:
        lock.release()
    _touch(sid)
    log.info("Session %s: generated %s (%d pages) in %.1fs", sid[:8], filename, report.page_count, elapsed)
    return jsonify({
        "ok": True,
        "filename": filename,
        "download_url": f"/download/{filename}",
        "elapsed_seconds": round(elapsed, 2),
        "report": report.to_dict(),
    })


@app.get("/download/<path:filename>")
def download(filename: str):
    if not OUTPUT_RE.match(filename):
        abort(404)
    sid = _session_id()
    out_dir = os.path.join(_session_dir(sid), "output")
    if not os.path.exists(os.path.join(out_dir, filename)):
        abort(404, description="File expired or not found. Generate the PDF again.")
    return send_from_directory(out_dir, filename, as_attachment=True, download_name=filename,
                               mimetype="application/pdf")


@app.get("/health")
def health():
    return jsonify({"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="KDP Formatter — local KDP interior PDF builder")
    parser.add_argument("--host", default=os.environ.get("KDP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KDP_PORT", "5000")))
    parser.add_argument("--debug", action="store_true", help="use the Flask dev server with auto-reload")
    args = parser.parse_args()

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
        return
    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress not installed; falling back to the Flask development server.")
        app.run(host=args.host, port=args.port, threaded=True)
        return
    log.info("KDP Formatter running at %s  (Ctrl+C to stop)", url)
    serve(app, host=args.host, port=args.port, threads=8, max_request_body_size=app.config["MAX_CONTENT_LENGTH"])


if __name__ == "__main__":
    main()
