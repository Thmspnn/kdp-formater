"""
kdp_processor.py — local Amazon KDP interior-PDF engine built on PyMuPDF.

Pipeline for every chapter (in the user-defined order):
  1. Open the source PDF (empty-password encrypted files are unlocked).
  2. Render each page at low resolution to detect the real ink bounding box
     and blank pages (robust against white background rectangles, scans, etc.).
  3. Compute ONE crop box per chapter (union of the ink boxes of all pages of
     the same size) so text keeps a consistent size and position across pages.
  4. Place each page as a vector Form XObject on a fresh trim-size page, scaled
     to fit inside the live area defined by the KDP margins. Inside/outside
     margins alternate for recto (odd) and verso (even) pages so the gutter is
     always on the binding edge.
  5. Optionally drop blank pages, start chapters on a right-hand page and add
     page numbers using an embedded font.

The finished document gets Info + XMP metadata, TrimBox == MediaBox (no bleed),
no annotations / links / form fields (only page content is transferred), and is
saved with garbage collection and compression.

A preflight report is returned with KDP-relevant warnings: page-count limits,
the minimum gutter for the final page count, non-embedded fonts, low-resolution
images, heavy down-scaling and probable two-page spreads.
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as _xml_escape

import fitz  # PyMuPDF

PT_PER_INCH = 72.0

# Common KDP paperback trim sizes (inches, width x height).
TRIM_SIZES: Dict[str, Tuple[float, float]] = {
    "5x8": (5.0, 8.0),
    "5.06x7.81": (5.06, 7.81),
    "5.25x8": (5.25, 8.0),
    "5.5x8.5": (5.5, 8.5),
    "6x9": (6.0, 9.0),
    "6.14x9.21": (6.14, 9.21),
    "7x10": (7.0, 10.0),
    "8x10": (8.0, 10.0),
    "8.5x11": (8.5, 11.0),
}

# KDP minimum inside (gutter) margin by page count, no-bleed paperback.
GUTTER_TABLE: Sequence[Tuple[int, float]] = (
    (150, 0.375),
    (300, 0.5),
    (500, 0.625),
    (700, 0.75),
    (828, 0.875),
)
KDP_MIN_PAGES = 24
KDP_MAX_PAGES = 828
KDP_MIN_OUTSIDE_IN = 0.25          # top / bottom / outside, no bleed
RECOMMENDED_IMAGE_DPI = 300
SCALE_WARNING_THRESHOLD = 0.85     # warn when content shrinks below 85 %

# Ink-detection parameters.
_ANALYSIS_DPI = 60                 # render resolution for bbox / blank detection
_INK_THRESHOLD = 245               # gray value below which a pixel is "ink"
_BBOX_PADDING_PT = 2.0             # safety padding around detected ink


class KDPProcessingError(Exception):
    """Raised for problems the user can fix (bad input, invalid settings)."""


def required_gutter_in(page_count: int) -> float:
    """Minimum inside margin (inches) KDP requires for a given page count."""
    for max_pages, gutter in GUTTER_TABLE:
        if page_count <= max_pages:
            return gutter
    return GUTTER_TABLE[-1][1]


@dataclass
class KDPSettings:
    trim_width_in: float = 6.0
    trim_height_in: float = 9.0
    margin_top_in: float = 0.75
    margin_bottom_in: float = 0.75
    margin_inner_in: float = 0.875
    margin_outer_in: float = 0.75
    trim_whitespace: bool = True
    remove_blank_pages: bool = True
    chapters_start_recto: bool = False
    add_page_numbers: bool = False
    allow_upscale: bool = False
    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""
    language: str = "en-US"

    def validate(self) -> None:
        if not (2.0 <= self.trim_width_in <= 12.0 and 2.0 <= self.trim_height_in <= 14.0):
            raise KDPProcessingError("Trim size is outside the supported range.")
        margins = {
            "Top": self.margin_top_in,
            "Bottom": self.margin_bottom_in,
            "Inside (gutter)": self.margin_inner_in,
            "Outside": self.margin_outer_in,
        }
        for name, value in margins.items():
            if value < KDP_MIN_OUTSIDE_IN:
                raise KDPProcessingError(
                    f"{name} margin must be at least {KDP_MIN_OUTSIDE_IN}\" for KDP (got {value}\")."
                )
            if value > 3.0:
                raise KDPProcessingError(f"{name} margin of {value}\" is unrealistically large.")
        live_w = self.trim_width_in - self.margin_inner_in - self.margin_outer_in
        live_h = self.trim_height_in - self.margin_top_in - self.margin_bottom_in
        if live_w < 1.5 or live_h < 2.0:
            raise KDPProcessingError("Margins leave too little live area on the page.")
        for name in ("title", "author", "subject", "keywords", "language"):
            value = getattr(self, name)
            if len(value) > 500:
                raise KDPProcessingError(f"{name.capitalize()} is too long (max 500 characters).")


@dataclass
class ChapterReport:
    name: str
    source_pages: int
    output_pages: int
    start_page: int
    scale_percent: float
    blank_pages_removed: int = 0


@dataclass
class ProcessReport:
    output_path: str
    page_count: int
    file_size_bytes: int
    trim_size: str
    chapters: List[ChapterReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    required_gutter_in: float = 0.0
    kdp_ready: bool = True

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("output_path", None)
        return data


@dataclass
class _PageInfo:
    ink: Optional[fitz.Rect]      # None => blank
    size_key: Tuple[int, int, int]


# --------------------------------------------------------------------------- #
# Analysis helpers
# --------------------------------------------------------------------------- #

_INK_TABLE = bytes(0x00 if v < _INK_THRESHOLD else 0xFF for v in range(256))


def _analyze_page(page: fitz.Page) -> _PageInfo:
    """Find the ink bounding box of a page (None if the page is blank)."""
    rect = page.rect
    size_key = (round(rect.width), round(rect.height), page.rotation % 360)
    zoom = _ANALYSIS_DPI / PT_PER_INCH
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    width, height, stride = pix.width, pix.height, pix.stride
    data = pix.samples.translate(_INK_TABLE)  # ink -> 0x00, paper -> 0xFF

    top = bottom = None
    column_acc = -1  # all bits set; AND-ing keeps 0x00 bytes where any row has ink
    for y in range(height):
        row = data[y * stride: y * stride + width]
        if row.count(0xFF) == width:
            continue
        if top is None:
            top = y
        bottom = y
        column_acc &= int.from_bytes(row, "big")

    if top is None:
        return _PageInfo(ink=None, size_key=size_key)

    columns = column_acc.to_bytes(width, "big")
    left = width - len(columns.lstrip(b"\xff"))
    right = len(columns.rstrip(b"\xff")) - 1

    ink = fitz.Rect(left / zoom, top / zoom, (right + 1) / zoom, (bottom + 1) / zoom)
    ink.x0 -= _BBOX_PADDING_PT
    ink.y0 -= _BBOX_PADDING_PT
    ink.x1 += _BBOX_PADDING_PT
    ink.y1 += _BBOX_PADDING_PT
    ink &= rect  # page.rect always starts at (0, 0) in PyMuPDF coordinates
    if ink.is_empty or ink.width < 1 or ink.height < 1:
        return _PageInfo(ink=None, size_key=size_key)
    return _PageInfo(ink=ink, size_key=size_key)


def _non_embedded_fonts(doc: fitz.Document) -> List[str]:
    names = set()
    for pno in range(doc.page_count):
        try:
            for font in doc.get_page_fonts(pno, full=False):
                ext, ftype, basefont = font[1], font[2], font[3]
                if ext == "n/a" and ftype != "Type3":
                    names.add(basefont.split("+")[-1] or "unnamed font")
        except Exception:  # malformed font dictionaries must not abort processing
            continue
    return sorted(names)


def _open_source(path: str, display_name: str) -> fitz.Document:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise KDPProcessingError(f"\"{display_name}\" could not be opened as a PDF: {exc}") from exc
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise KDPProcessingError(f"\"{display_name}\" is password-protected. Remove the password and re-upload.")
    if doc.page_count == 0:
        doc.close()
        raise KDPProcessingError(f"\"{display_name}\" contains no pages.")
    # Bake /Rotate into the page content (in memory only) so every later step
    # works in the same visual coordinate space the reader sees.
    for page in doc:
        if page.rotation % 360:
            try:
                page.remove_rotation()
            except Exception:
                pass
    return doc


def _build_xmp(settings: KDPSettings, now_iso: str, producer: str) -> str:
    title = _xml_escape(settings.title or "Untitled")
    author = _xml_escape(settings.author or "")
    subject = _xml_escape(settings.subject or "")
    keywords = _xml_escape(settings.keywords or "")
    lang = _xml_escape(settings.language or "x-default")
    producer = _xml_escape(producer)
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '    xmlns:pdf="http://ns.adobe.com/pdf/1.3/">\n'
        '   <dc:format>application/pdf</dc:format>\n'
        f'   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{title}</rdf:li></rdf:Alt></dc:title>\n'
        f'   <dc:creator><rdf:Seq><rdf:li>{author}</rdf:li></rdf:Seq></dc:creator>\n'
        f'   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{subject}</rdf:li></rdf:Alt></dc:description>\n'
        f'   <dc:language><rdf:Bag><rdf:li>{lang}</rdf:li></rdf:Bag></dc:language>\n'
        f'   <pdf:Keywords>{keywords}</pdf:Keywords>\n'
        f'   <pdf:Producer>{producer}</pdf:Producer>\n'
        '   <xmp:CreatorTool>KDP Formatter (local)</xmp:CreatorTool>\n'
        f'   <xmp:CreateDate>{now_iso}</xmp:CreateDate>\n'
        f'   <xmp:ModifyDate>{now_iso}</xmp:ModifyDate>\n'
        f'   <xmp:MetadataDate>{now_iso}</xmp:MetadataDate>\n'
        '  </rdf:Description>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def process_chapters(
    chapters: Sequence[Tuple[str, str]],
    output_path: str,
    settings: KDPSettings,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ProcessReport:
    """
    Merge and format chapters into one KDP-ready interior PDF.

    :param chapters: ordered list of (file_path, display_name)
    :param output_path: where to write the finished PDF
    :param settings: formatting options
    :param progress: optional callback(done_chapters, total_chapters)
    """
    if not chapters:
        raise KDPProcessingError("Upload at least one chapter PDF first.")
    settings.validate()

    page_w = settings.trim_width_in * PT_PER_INCH
    page_h = settings.trim_height_in * PT_PER_INCH
    top = settings.margin_top_in * PT_PER_INCH
    bottom = settings.margin_bottom_in * PT_PER_INCH
    inner = settings.margin_inner_in * PT_PER_INCH
    outer = settings.margin_outer_in * PT_PER_INCH

    def live_area(page_index: int) -> fitz.Rect:
        # page_index 0 is page 1, a recto (right-hand) page: gutter on the left.
        recto = page_index % 2 == 0
        left, right = (inner, outer) if recto else (outer, inner)
        return fitz.Rect(left, top, page_w - right, page_h - bottom)

    out = fitz.open()
    warnings: List[str] = []
    chapter_reports: List[ChapterReport] = []
    numbered_pages: List[int] = []      # output page indexes that receive a folio
    low_dpi_images = 0
    min_dpi_seen: Optional[float] = None
    fonts_missing: Dict[str, List[str]] = {}

    try:
        for c_index, (path, name) in enumerate(chapters):
            if not os.path.isfile(path):
                raise KDPProcessingError(f"\"{name}\" is missing on the server. Please re-upload it.")
            src = _open_source(path, name)
            try:
                if settings.chapters_start_recto and out.page_count % 2 == 1:
                    out.new_page(width=page_w, height=page_h)  # blank verso

                infos = [_analyze_page(src[p]) for p in range(src.page_count)]

                # One crop box per page-size group keeps scale consistent in a chapter.
                group_clip: Dict[Tuple[int, int, int], fitz.Rect] = {}
                for p, info in enumerate(infos):
                    if settings.trim_whitespace and info.ink is not None:
                        current = group_clip.get(info.size_key)
                        group_clip[info.size_key] = fitz.Rect(info.ink) if current is None else current | info.ink
                for p, info in enumerate(infos):
                    if info.size_key not in group_clip:
                        group_clip[info.size_key] = fitz.Rect(src[p].rect)

                live_ref = live_area(0)
                group_scale: Dict[Tuple[int, int, int], float] = {}
                for key, clip in group_clip.items():
                    scale = min(live_ref.width / clip.width, live_ref.height / clip.height)
                    if not settings.allow_upscale:
                        scale = min(scale, 1.0)
                    group_scale[key] = scale

                start_page = out.page_count + 1
                removed = 0
                spread_detected = False
                for p, info in enumerate(infos):
                    src_page = src[p]
                    if info.ink is None and settings.remove_blank_pages:
                        removed += 1
                        continue

                    clip = group_clip[info.size_key]
                    scale = group_scale[info.size_key]
                    if src_page.rect.width > src_page.rect.height * 1.15:
                        spread_detected = True

                    page = out.new_page(width=page_w, height=page_h)
                    if info.ink is None:
                        continue  # intentionally kept blank page

                    live = live_area(page.number)
                    draw_w, draw_h = clip.width * scale, clip.height * scale
                    x0 = live.x0 + (live.width - draw_w) / 2.0
                    target = fitz.Rect(x0, live.y0, x0 + draw_w, live.y0 + draw_h)
                    try:
                        page.show_pdf_page(target, src, p, clip=clip, keep_proportion=True)
                    except ValueError:
                        continue  # source page has no content stream
                    numbered_pages.append(page.number)

                    for img in src_page.get_image_info():
                        bbox = fitz.Rect(img.get("bbox", (0, 0, 0, 0)))
                        if bbox.width < 36 or not img.get("width"):
                            continue
                        dpi = img["width"] / (bbox.width * scale / PT_PER_INCH)
                        min_dpi_seen = dpi if min_dpi_seen is None else min(min_dpi_seen, dpi)
                        if dpi < RECOMMENDED_IMAGE_DPI - 1:
                            low_dpi_images += 1

                if out.page_count < start_page:
                    warnings.append(f"\"{name}\" produced no pages (all pages were blank).")
                    start_page = 0

                min_scale = min(group_scale.values()) if group_scale else 1.0
                chapter_reports.append(ChapterReport(
                    name=name,
                    source_pages=src.page_count,
                    output_pages=max(0, out.page_count - start_page + 1) if start_page else 0,
                    start_page=start_page,
                    scale_percent=round(min_scale * 100, 1),
                    blank_pages_removed=removed,
                ))
                if min_scale < SCALE_WARNING_THRESHOLD:
                    warnings.append(
                        f"\"{name}\": content was reduced to {min_scale * 100:.0f}% to fit the margins. "
                        f"For larger text, export this chapter at {settings.trim_width_in:g}\" x "
                        f"{settings.trim_height_in:g}\" from your word processor."
                    )
                if spread_detected:
                    warnings.append(
                        f"\"{name}\" contains landscape pages that look like two-page spreads. "
                        "KDP requires single pages; export them as individual pages."
                    )
                missing = _non_embedded_fonts(src)
                if missing:
                    fonts_missing[name] = missing
            finally:
                src.close()
            if progress:
                progress(c_index + 1, len(chapters))

        if out.page_count == 0:
            raise KDPProcessingError("The merged document has no printable pages.")

        if settings.add_page_numbers and numbered_pages:
            font = fitz.Font("tiro")  # MuPDF's built-in Nimbus Roman, embedded on write
            font_size = 9.0
            for pno in numbered_pages:
                page = out[pno]
                live = live_area(pno)
                text = str(pno + 1)
                width = font.text_length(text, fontsize=font_size)
                baseline_y = page_h - bottom / 2.0 + font_size / 3.0
                writer = fitz.TextWriter(page.rect)
                writer.append(fitz.Point(live.x0 + (live.width - width) / 2.0, baseline_y), text,
                              font=font, fontsize=font_size)
                writer.write_text(page)

        for page in out:
            page.set_trimbox(page.mediabox)

        # ---------------- metadata ----------------
        now = _dt.datetime.now(_dt.timezone.utc).astimezone()
        now_iso = now.isoformat(timespec="seconds")
        producer = f"PyMuPDF {fitz.VersionBind} / KDP Formatter"
        out.set_metadata({
            "title": settings.title or "Untitled",
            "author": settings.author,
            "subject": settings.subject,
            "keywords": settings.keywords,
            "creator": "KDP Formatter (local)",
            "producer": producer,
            "creationDate": fitz.get_pdf_now(),
            "modDate": fitz.get_pdf_now(),
        })
        out.set_xml_metadata(_build_xmp(settings, now_iso, producer))
        if settings.language:
            try:
                out.set_language(settings.language)
            except Exception:
                warnings.append("Document language could not be set; continuing without it.")

        page_count = out.page_count
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        out.save(output_path, garbage=4, deflate=True, deflate_images=True,
                 deflate_fonts=True, clean=True)
    finally:
        out.close()

    # ---------------- KDP preflight ----------------
    errors: List[str] = []
    gutter = required_gutter_in(page_count)
    if settings.margin_inner_in + 1e-9 < gutter:
        errors.append(
            f"Inside margin {settings.margin_inner_in}\" is below KDP's minimum of {gutter}\" "
            f"for a {page_count}-page book. Increase the inside margin and regenerate."
        )
    if page_count < KDP_MIN_PAGES:
        warnings.append(
            f"The book has {page_count} pages; KDP paperbacks need at least {KDP_MIN_PAGES} "
            "(eBook/hardcover rules differ)."
        )
    if page_count > KDP_MAX_PAGES:
        errors.append(f"The book has {page_count} pages; KDP paperbacks allow at most {KDP_MAX_PAGES}.")
    if fonts_missing:
        details = "; ".join(f"\"{n}\": {', '.join(f[:5])}{'…' if len(f) > 5 else ''}"
                            for n, f in fonts_missing.items())
        errors.append(
            "Some fonts are not embedded (KDP rejects these). Re-export the affected chapters "
            f"with 'embed all fonts' enabled — {details}."
        )
    if low_dpi_images:
        warnings.append(
            f"{low_dpi_images} image(s) print below {RECOMMENDED_IMAGE_DPI} DPI "
            f"(lowest ≈ {min_dpi_seen:.0f} DPI) and may look blurry."
        )

    return ProcessReport(
        output_path=output_path,
        page_count=page_count,
        file_size_bytes=os.path.getsize(output_path),
        trim_size=f"{settings.trim_width_in:g}\" x {settings.trim_height_in:g}\"",
        chapters=chapter_reports,
        warnings=warnings,
        errors=errors,
        required_gutter_in=gutter,
        kdp_ready=not errors,
    )


def inspect_pdf(path: str, display_name: str) -> dict:
    """Quick facts about an uploaded chapter (raises KDPProcessingError if unusable)."""
    doc = _open_source(path, display_name)
    try:
        first = doc[0].rect
        return {
            "pages": doc.page_count,
            "width_in": round(first.width / PT_PER_INCH, 2),
            "height_in": round(first.height / PT_PER_INCH, 2),
        }
    finally:
        doc.close()


def render_thumbnail(path: str, out_png: str, width_px: int = 96) -> None:
    """Render the first page of a PDF to a small PNG thumbnail."""
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            doc.authenticate("")
        page = doc[0]
        zoom = width_px / max(page.rect.width, 1)
        page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).save(out_png)
    finally:
        doc.close()
