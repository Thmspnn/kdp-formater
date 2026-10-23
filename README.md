# KDP Formatter — Local Chapter Merger for Amazon KDP

KDP Formatter is a self-contained Flask web app that turns a set of chapter PDFs into one print-ready interior PDF for **Amazon KDP paperbacks**.
All the work happens on your own computer with the open-source PyMuPDF library. It needs no cloud APIs, accounts or paid credits, and works offline once installed.

```
kdp_formatter/
├── app.py              # Flask backend: upload, reorder, delete, generate, download
├── kdp_processor.py    # PDF engine: trimming, re-fitting, margins, merge, metadata, preflight
├── font_embedder.py    # embeds every font in the output (installed fonts / built-in base-14 / raster fallback)
├── requirements.txt    # Flask, PyMuPDF, waitress
├── README.md
└── templates/
    └── index.html      # Single-page UI (HTML/CSS/JS, no CDN, no build step)
```

Runtime data such as uploads, generated PDFs and the session key goes into `kdp_formatter/instance/`. The app creates this folder itself and deletes session data after 24 hours.

---

## 1. Setup

You need **Python 3.9 or newer** on Windows, macOS or Linux.

```bash
cd kdp_formatter

# (recommended) create an isolated environment
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt
```

## 2. Run

```bash
python app.py
```

Then open **http://localhost:5000** in your browser. To stop the app, press `Ctrl+C`.

| Option | Example | Purpose |
|---|---|---|
| `--port` | `python app.py --port 8080` | Use a different port |
| `--host` | `python app.py --host 0.0.0.0` | Make the app reachable from other devices on your network |
| `--debug` | `python app.py --debug` | Flask dev server with auto-reload |

The app runs on the multi-threaded **waitress** server by default. It can also be configured with environment variables:
`KDP_PORT`, `KDP_HOST`, `KDP_MAX_UPLOAD_MB` (default 500), `KDP_MAX_FILES` (default 200),
`KDP_SESSION_TTL_HOURS` (default 24), `KDP_DATA_DIR`, `KDP_SECRET_KEY`.

## 3. Usage

1. **Upload chapters.** Drag PDFs onto the drop zone or click it to browse. You can add several files at once, and a progress bar shows the upload. Each chapter is listed with a first-page thumbnail, its page count, file size and original page size.
2. **Reorder.** Drag a chapter by its grip handle, thumbnail or name. You can also use the ▲/▼ buttons, or focus a row and press `Alt+↑` / `Alt+↓`. The trash icon removes a chapter. The server saves the order straight away, so reloading the page keeps it.
   **Read a chapter:** click the eye icon ("Read PDF") to open the built-in reader. Use Prev/Next or the `←`/`→` keys to turn pages, pick a zoom of 75–150% (100% is the real printed size), and click **Copy page text** to copy the current page's text. Press `Esc` or × to close.
3. **Settings.** Enter a title, author and language; these are embedded as PDF metadata. Pick a trim size (6″ × 9″ by default) and set the margins. A live preview shows the recto and verso live areas, and a hint tells you whether your gutter meets KDP's minimum for the current page count.
4. **Click "Generate KDP-Ready PDF".** A spinner shows while the book is processed. When it finishes, the file downloads automatically and a direct download link stays on the page. The page also shows a **preflight report** with the page count, where each chapter starts, the scale applied, and any KDP problems found.
5. In KDP, upload the file as your paperback **manuscript** and choose the same trim size with **No bleed**.

## 4. What the engine does

For each chapter, in the order you set:

- **Normalizes rotation.** Pages with a `/Rotate` flag are converted to how they actually look on screen.
- **Detects the real text block.** Each page is rendered at low resolution to find its ink bounding box. This works for text, vector art and scanned pages, and it ignores invisible white background boxes.
- **Cleans up the layout.** All pages of a chapter share one crop box, the union of their text blocks. This removes the old margins while keeping text size and position the same from page to page. The content is then re-fitted, **as vectors with no rasterizing**, into the KDP live area: centered horizontally and aligned to the top margin.
- **Mirrors margins.** Odd (recto) pages have the gutter on the left and even (verso) pages have it on the right. With the defaults on a 6″ × 9″ book: top 0.75″, bottom 0.75″, inside 0.875″, outside 0.75″.
- **Removes blank pages** (optional) left over from exports.
- **Starts chapters on right-hand pages** (optional), adding a blank verso where needed.
- **Adds page numbers** (optional), centered in the bottom margin using an embedded font.
- **Embeds all fonts** automatically, which KDP requires. Fonts missing from a chapter are added from fonts installed on your computer, or from built-in metric-identical versions of the standard fonts (Helvetica, Times, Courier, Symbol, ZapfDingbats). If a font isn't installed, a metric-compatible or similar font is used and the app tells you. Pages that use an uninstalled *composite* (CID) font are converted to 400-DPI images, the only safe option. The finished file is re-checked to confirm every font is embedded.
- **Keeps original type size** by default. Content is only scaled down to fit, never enlarged, unless you turn on *Allow enlarging*.

The finished file:

- Has every page at the exact trim size, with TrimBox = MediaBox (the no-bleed layout KDP expects).
- Contains page content only, with no annotations, links, form fields or JavaScript, which KDP may flag.
- Carries Info dictionary and XMP metadata (title, author, subject, keywords, language, dates).
- Is compressed and garbage-collected, with no password or encryption.

### Preflight checks

**Errors** mean KDP would reject the file or it is not ready to print:

- The inside margin is below KDP's minimum gutter for the final page count (0.375″ up to 150 pages, 0.5″ up to 300, 0.625″ up to 500, 0.75″ up to 700, 0.875″ up to 828).
- The book has more than 828 pages.
- **A font could not be embedded** (rare, because fonts are embedded automatically). Re-export the listed chapters from Word, Google Docs, LibreOffice or InDesign with "embed fonts" / "PDF/X" turned on.

**Warnings:**

- Fewer than 24 pages (the paperback minimum).
- Images below 300 DPI at their printed size.
- Content shrunk below 85%.
- Landscape pages that look like two-page spreads.

### Tips for the best result

- Export chapters at the trim size you will publish, e.g. 6″ × 9″. If you export at Letter or A4, the engine has to shrink the text to fit.
- Embed all fonts when exporting (in Word: *Options → Save → Embed fonts in the file*, then *Save As → PDF → Options → PDF/A*).
- Images should be at least 300 DPI.

## 5. API (used by the UI)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard |
| `GET` | `/files` | List the chapters in the current session |
| `POST` | `/upload` | Multipart field `files` (one or more PDFs) |
| `POST` | `/reorder` | JSON `{"order": ["<id>", ...]}`, which must contain every chapter id |
| `DELETE` | `/files/<id>` | Remove a chapter |
| `POST` | `/files/clear` | Remove all chapters |
| `GET` | `/files/<id>/thumbnail` | PNG of the first page |
| `GET` | `/files/<id>/info` | Chapter name, page count and page size |
| `GET` | `/files/<id>/page/<n>` | Page `n` (1-based) rendered as a 150 DPI PNG; 404 if out of range |
| `GET` | `/files/<id>/text/<n>` | `{"ok": true, "text": "..."}` with the plain text of page `n` |
| `POST` | `/generate` | JSON settings (`title`, `author`, `trim_size`, `margin_*_in`, toggles). Returns the report and `download_url` |
| `GET` | `/download/<filename>` | Downloads the generated PDF |

Each browser gets its own server-side folder, identified by a random UUID stored in a signed session cookie.

## 6. Troubleshooting

- **"is password-protected"**: remove the password in your PDF tool and upload the file again.
- **Upload too large**: raise the limit, e.g. `KDP_MAX_UPLOAD_MB=1500 python app.py`.
- **Port already in use**: run `python app.py --port 5001`.
- **Text looks small**: the chapter was exported at a larger page size. Re-export it at the trim size, or reduce the margins within KDP's limits.
