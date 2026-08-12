import logging
import os
import re
import sys
import tempfile
import uuid

import anthropic
from flask import Flask, abort, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from document_parser import extract_pages, parse_blocks
from assemble import blocks_to_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Under PyInstaller, __file__ resolves inside a bundle dir that's read-only
# (--onedir) or a fresh temp extraction wiped after every run (--onefile) --
# neither is a place to persist a user's uploads/converted documents across
# runs, so those go to the OS's normal per-user app-data directory instead.
# Templates are read-only either way, so they're fine loaded straight out of
# sys._MEIPASS (the bundle's extraction root PyInstaller sets at runtime).
_FROZEN = getattr(sys, "frozen", False)

if _FROZEN:
    template_folder = os.path.join(sys._MEIPASS, "templates")
    app = Flask(__name__, template_folder=template_folder)

    if sys.platform == "darwin":
        _data_dir = os.path.expanduser("~/Library/Application Support/STEM-Access")
    elif sys.platform == "win32":
        _data_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "STEM-Access")
    else:
        _data_dir = os.path.join(
            os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "STEM-Access"
        )
    UPLOAD_DIR = os.path.join(_data_dir, "uploads")
else:
    app = Flask(__name__)
    UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(UPLOAD_DIR, exist_ok=True)

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


@app.route("/upload", methods=["GET"])
def upload_form():
    return render_template("upload.html")


@app.route("/upload", methods=["POST"])
def upload_submit():
    uploaded = request.files.get("pdf")
    if not uploaded or not uploaded.filename:
        return render_template("upload.html", error="Please choose a PDF file to upload.")

    if not uploaded.filename.lower().endswith(".pdf"):
        return render_template("upload.html", error="Only PDF files are supported.")

    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return render_template("upload.html", error="Please enter your Anthropic API key.")

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    pdf_path = os.path.join(job_dir, uploaded.filename)
    uploaded.save(pdf_path)

    # Cropped figures only need to exist transiently, to feed the Claude
    # vision calls below -- the final saved HTML embeds Claude's
    # description of each figure, never the image file itself, so these
    # never needed to live under the persistent per-user data dir. A plain
    # OS temp dir also sidesteps a pymupdf4llm bug: it builds its Markdown
    # image reference by sanitizing the image path's spaces/parens/brackets,
    # then reuses that *same sanitized string* as the real filesystem write
    # target -- so a space anywhere in image_dir (e.g. macOS's own
    # "Application Support" folder name, or any username containing one)
    # corrupts the actual write path and crashes with FileNotFoundError.
    image_dir = tempfile.mkdtemp(prefix=f"stem-access-{job_id}-")

    try:
        pages = extract_pages(pdf_path, image_dir=image_dir)
    except Exception:
        logger.exception("Failed to parse PDF")
        return render_template(
            "upload.html",
            error="Could not read that PDF. Make sure it's a valid, non-corrupted PDF file.",
        )

    try:
        # parse_blocks may call Claude (transcribe_page) for pages where
        # local text extraction produced broken math -- handled separately
        # from the PDF-read failure above.
        blocks = parse_blocks(pages, pdf_path=pdf_path, api_key=api_key)
    except anthropic.AuthenticationError:
        return render_template(
            "upload.html",
            error="That Anthropic API key was rejected. Double-check it and try again.",
        )
    except anthropic.APIError:
        logger.exception("Anthropic API call failed while transcribing a page")
        return render_template(
            "upload.html",
            error="The document description service returned an error. Please try again.",
        )
    except Exception:
        logger.exception("Failed to parse PDF")
        return render_template(
            "upload.html",
            error="Could not read that PDF. Make sure it's a valid, non-corrupted PDF file.",
        )

    logger.info("Parsed %d blocks", len(blocks))

    try:
        content_html = blocks_to_html(blocks, api_key=api_key)
    except anthropic.AuthenticationError:
        return render_template(
            "upload.html",
            error="That Anthropic API key was rejected. Double-check it and try again.",
        )
    except anthropic.APIError:
        logger.exception("Anthropic API call failed while describing a figure")
        return render_template(
            "upload.html",
            error="The document description service returned an error. Please try again.",
        )
    except Exception:
        logger.exception("Failed to assemble HTML from parsed blocks")
        return render_template(
            "upload.html",
            error="Something went wrong while building the accessible document. Please try again.",
        )

    download_name = secure_filename(os.path.splitext(uploaded.filename)[0]) + "_accessible.html"
    rendered = render_template(
        "result.html",
        content=content_html,
        filename=uploaded.filename,
        job_id=job_id,
        download_name=download_name,
    )

    output_path = os.path.join(job_dir, download_name)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    logger.info("Saved converted document to %s", output_path)

    return rendered


@app.route("/download/<job_id>/<path:filename>")
def download_file(job_id, filename):
    if not _JOB_ID_RE.match(job_id):
        abort(404)
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    return send_from_directory(job_dir, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)
