"""
Embed every font in a PDF so it passes KDP's "all fonts must be embedded" check.

100 % local, PyMuPDF only. Strategy for each font that has no font program:

1. Exact match  - a font with the same name installed on this computer
                  (Windows/macOS/Linux font folders) is embedded as-is.
2. Metric twin  - simple fonts only: a metric-compatible font is embedded
                  (Arial -> Liberation Sans/Arimo, Times New Roman -> Liberation Serif/Tinos, ...).
3. Base-14      - simple fonts only: MuPDF's built-in URW fonts, which are metric-identical
                  to Helvetica / Times / Courier / Symbol / ZapfDingbats (what every viewer
                  shows for these fonts anyway).
4. Rasterize    - composite (CID) fonts whose glyph ids only match the original file cannot be
                  substituted safely; the pages using them are converted to high-resolution
                  images so the book contains no unembedded font.

Type3 fonts are defined inside the PDF and count as embedded.
"""
from __future__ import annotations

import glob
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz  # type: ignore

RASTER_DPI = 400

_REF = re.compile(r"(\d+) 0 R")
_SUBSET = re.compile(r"^[A-Z]{6}\+")

# Metric-compatible substitutes (normalized family -> candidate installed families).
_METRIC_TWINS = {
    "arial": ["liberationsans", "arimo", "helvetica", "nimbussans"],
    "arialmt": ["liberationsans", "arimo", "nimbussans"],
    "helvetica": ["liberationsans", "arimo", "nimbussans"],
    "timesnewroman": ["liberationserif", "tinos", "nimbusroman"],
    "timesnewromanps": ["liberationserif", "tinos", "nimbusroman"],
    "timesnewromanpsmt": ["liberationserif", "tinos", "nimbusroman"],
    "times": ["liberationserif", "tinos", "nimbusroman"],
    "timesroman": ["liberationserif", "tinos", "nimbusroman"],
    "couriernew": ["liberationmono", "cousine", "nimbusmono", "nimbusmonops"],
    "couriernewpsmt": ["liberationmono", "cousine", "nimbusmonops"],
    "courier": ["liberationmono", "cousine", "nimbusmonops"],
    "calibri": ["carlito"],
    "cambria": ["caladea"],
}
_SERIF_HINTS = ("times", "roman", "serif", "georgia", "garamond", "book", "cambria", "caslon",
                "palatino", "baskerville", "minion", "bodoni", "century", "caladea", "tinos")
_MONO_HINTS = ("courier", "mono", "consol", "code", "typewriter", "cousine")


@dataclass
class EmbedResult:
    embedded: List[str] = field(default_factory=list)       # "Name -> how"
    rasterized_pages: List[int] = field(default_factory=list)  # 0-based
    unresolved_fonts: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Font inventory
# --------------------------------------------------------------------------- #

def _key(doc, xref, key):
    try:
        return doc.xref_get_key(xref, key)
    except Exception:
        return ("null", "null")


def _ref_of(val: Tuple[str, str]) -> Optional[int]:
    if val[0] == "xref":
        return int(val[1].split()[0])
    if val[0] == "array":
        m = _REF.search(val[1])
        return int(m.group(1)) if m else None
    return None


def _font_xrefs(doc) -> List[int]:
    out = []
    for xref in range(1, doc.xref_length()):
        if _key(doc, xref, "Type") == ("name", "/Font"):
            out.append(xref)
    return out


def _descriptor_has_file(doc, desc_xref: Optional[int], desc_inline: Optional[str]) -> bool:
    keys = ("FontFile", "FontFile2", "FontFile3")
    if desc_xref:
        return any(_key(doc, desc_xref, k)[0] == "xref" for k in keys)
    if desc_inline:
        return any(f"/{k} " in desc_inline or f"/{k}\n" in desc_inline for k in keys)
    return False


def _font_info(doc, xref) -> dict:
    sub = _key(doc, xref, "Subtype")[1].lstrip("/")
    base = _key(doc, xref, "BaseFont")[1].lstrip("/")
    info = {"xref": xref, "subtype": sub, "base": base, "name": _SUBSET.sub("", base) or "unnamed",
            "desc": None, "cid": None, "cid_subtype": None, "embedded": False}
    if sub == "Type3":
        info["embedded"] = True
        return info
    target = xref
    if sub == "Type0":
        cid = _ref_of(_key(doc, xref, "DescendantFonts"))
        if not cid:
            return info
        info["cid"] = cid
        info["cid_subtype"] = _key(doc, cid, "Subtype")[1].lstrip("/")
        target = cid
    fd = _key(doc, target, "FontDescriptor")
    info["desc"] = _ref_of(fd)
    info["embedded"] = _descriptor_has_file(doc, info["desc"], fd[1] if fd[0] == "dict" else None)
    return info


def list_fonts(doc) -> List[dict]:
    """Top-level fonts; CID descendants are covered by their Type0 parent."""
    infos = [_font_info(doc, x) for x in _font_xrefs(doc)]
    return [f for f in infos if f["subtype"] not in ("CIDFontType0", "CIDFontType2")]


def non_embedded_font_names(doc) -> List[str]:
    return sorted({f["name"] for f in list_fonts(doc) if not f["embedded"]})


# --------------------------------------------------------------------------- #
# Installed system fonts (indexed once per process)
# --------------------------------------------------------------------------- #

_sys_index: Optional[Dict[str, str]] = None
_sys_lock = threading.Lock()


def _norm(name: str) -> str:
    name = _SUBSET.sub("", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _split_style(name: str) -> Tuple[str, bool, bool]:
    """'Arial,BoldItalic' / 'TimesNewRomanPS-BoldMT' -> ('arial', bold, italic)."""
    n = _norm(name)
    bold = any(s in n for s in ("bold", "black", "heavy", "semibold", "demi"))
    italic = any(s in n for s in ("italic", "oblique"))
    fam = re.sub(r"(semibold|demibold|bold|black|heavy|italic|oblique|regular|medium)", "", n)
    changed = True
    while changed:
        changed = False
        for suf in ("psmt", "mt", "ps", "book"):
            if fam.endswith(suf) and len(fam) > len(suf) + 2:
                fam, changed = fam[: -len(suf)], True
    return fam or n, bold, italic


def _font_dirs() -> List[str]:
    home = os.path.expanduser("~")
    dirs = []
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs += [os.path.join(windir, "Fonts"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")]
    elif sys.platform == "darwin":
        dirs += ["/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts")]
    else:
        dirs += ["/usr/share/fonts", "/usr/local/share/fonts", os.path.join(home, ".fonts"),
                 os.path.join(home, ".local", "share", "fonts")]
    return [d for d in dirs if d and os.path.isdir(d)]


def _system_index() -> Dict[str, str]:
    global _sys_index
    with _sys_lock:
        if _sys_index is not None:
            return _sys_index
        idx: Dict[str, str] = {}
        for d in _font_dirs():
            for ext in ("ttf", "otf", "TTF", "OTF"):
                for path in glob.glob(os.path.join(d, "**", f"*.{ext}"), recursive=True):
                    try:
                        f = fitz.Font(fontfile=path)
                    except Exception:
                        continue
                    for nm in (f.name, os.path.splitext(os.path.basename(path))[0]):
                        idx.setdefault(_norm(nm), path)
                    fam, b, i = _split_style(f.name)
                    idx.setdefault(f"{fam}|{int(b)}{int(i)}", path)
        _sys_index = idx
        return idx


def _find_system_font(name: str, allow_twins: bool) -> Tuple[Optional[str], str]:
    idx = _system_index()
    exact = idx.get(_norm(name))
    fam, bold, italic = _split_style(name)
    style = f"{int(bold)}{int(italic)}"
    if not exact:
        exact = idx.get(f"{fam}|{style}")
    if exact:
        return exact, "installed font"
    if allow_twins:
        for twin in _METRIC_TWINS.get(fam, []):
            p = idx.get(f"{twin}|{style}")
            if p:
                return p, "metric-compatible font"
    return None, ""


def _base14_for(name: str, flags: int) -> str:
    n = _norm(name)
    if "symbol" in n:
        return "Symbol"
    if "dingbat" in n or "zapf" in n:
        return "ZapfDingbats"
    fam, bold, italic = _split_style(name)
    bold = bold or bool(flags & (1 << 18))
    italic = italic or bool(flags & 64)
    if any(h in n for h in _MONO_HINTS) or flags & 1:
        return "Courier" + {(0, 0): "", (1, 0): "-Bold", (0, 1): "-Oblique", (1, 1): "-BoldOblique"}[(bold, italic)]
    if any(h in n for h in _SERIF_HINTS) or (flags & 2 and "sans" not in n):
        return {(0, 0): "Times-Roman", (1, 0): "Times-Bold", (0, 1): "Times-Italic",
                (1, 1): "Times-BoldItalic"}[(bold, italic)]
    return "Helvetica" + {(0, 0): "", (1, 0): "-Bold", (0, 1): "-Oblique", (1, 1): "-BoldOblique"}[(bold, italic)]


# --------------------------------------------------------------------------- #
# Writing font programs into the PDF
# --------------------------------------------------------------------------- #

_stream_cache: Dict[Tuple[int, str], int] = {}


def _new_stream(doc, data: bytes, extra: str = "", cache_key: str = "") -> int:
    """Create a font-file stream; identical font programs are stored only once per document."""
    key = (id(doc), cache_key)
    if cache_key and key in _stream_cache:
        return _stream_cache[key]
    x = doc.get_new_xref()
    doc.update_object(x, f"<<{extra}>>")
    doc.update_stream(x, data, compress=True)
    if cache_key:
        _stream_cache[key] = x
    return x


def _code_unicodes(doc, xref) -> Dict[int, int]:
    """Map character codes 0-255 to Unicode using the font's /Encoding."""
    enc = _key(doc, xref, "Encoding")
    base, diffs = "StandardEncoding", ""
    if enc[0] == "name":
        base = enc[1].lstrip("/")
    elif enc[0] in ("dict", "xref"):
        src = enc[1] if enc[0] == "dict" else doc.xref_object(int(enc[1].split()[0]))
        m = re.search(r"/BaseEncoding\s*/(\w+)", src)
        if m:
            base = m.group(1)
        m = re.search(r"/Differences\s*\[(.*?)\]", src, re.S)
        diffs = m.group(1) if m else ""
    codec = {"WinAnsiEncoding": "cp1252", "MacRomanEncoding": "mac_roman"}.get(base, "latin-1")
    table = {}
    for c in range(32, 256):
        try:
            table[c] = ord(bytes([c]).decode(codec))
        except Exception:
            continue
    if base == "StandardEncoding":
        table[39], table[96] = 0x2019, 0x2018
    code = 0
    for tok in re.findall(r"/[^\s/\[\]]+|\d+", diffs):
        if tok[0] == "/":
            u = fitz.mupdf.fz_unicode_from_glyph_name(tok[1:])
            if u:
                table[code] = u
            code += 1
        else:
            code = int(tok)
    return table


def _widths(doc, xref, font: "fitz.Font", base14: str) -> Tuple[int, int, List[int]]:
    if base14 in ("Symbol", "ZapfDingbats"):
        g = fitz.symbol_glyphs if base14 == "Symbol" else fitz.zapf_glyphs
        return 32, 255, [round(g[c][1] * 1000) for c in range(32, 256)]
    uni = _code_unicodes(doc, xref)
    w = [round(font.glyph_advance(uni[c]) * 1000) if c in uni else 0 for c in range(32, 256)]
    return 32, 255, w


def _descriptor_fields(font: "fitz.Font", name: str, flags: int) -> str:
    b = font.bbox
    italic = flags & 64
    return (f"/Type/FontDescriptor/FontName/{re.sub(r'[^A-Za-z0-9+,-]', '', name) or 'Font'}"
            f"/Flags {flags}/FontBBox[{round(b.x0 * 1000)} {round(b.y0 * 1000)} {round(b.x1 * 1000)} {round(b.y1 * 1000)}]"
            f"/ItalicAngle {-12 if italic else 0}/Ascent {round(font.ascender * 1000)}"
            f"/Descent {round(font.descender * 1000)}/CapHeight {round(font.ascender * 700)}/StemV 80")


def _embed_simple(doc, info: dict) -> str:
    xref, name = info["xref"], info["name"]
    desc = info["desc"]
    flags = 0
    if desc:
        try:
            flags = int(_key(doc, desc, "Flags")[1])
        except ValueError:
            flags = 0
    has_widths = _key(doc, xref, "Widths")[0] in ("array", "xref")
    is_std14 = _norm(name) in {_norm(v) for v in fitz.Base14_fontdict.values()}

    path, how = (None, "") if is_std14 else _find_system_font(name, allow_twins=True)
    if path:
        data = open(path, "rb").read()
        font = fitz.Font(fontfile=path)
        if data[:4] == b"OTTO":
            ffkey, ff = "FontFile3", _new_stream(doc, data, "/Subtype/OpenType", path)
        else:
            ffkey, ff = "FontFile2", _new_stream(doc, data, f"/Length1 {len(data)}", path)
            doc.xref_set_key(xref, "Subtype", "/TrueType")
        base14 = ""
        label = f"{how} {font.name}"
    else:
        base14 = _base14_for(name, flags)
        font = fitz.Font(base14)
        ffkey, ff = "FontFile3", _new_stream(doc, font.buffer, "/Subtype/Type1C", "base14:" + base14)
        doc.xref_set_key(xref, "Subtype", "/Type1")
        label = f"built-in {font.name}" + (" (metric-identical)" if is_std14 else " (substitute)")

    symbolic = base14 in ("Symbol", "ZapfDingbats")
    if not desc:
        fl = 4 if symbolic else 32
        if base14.startswith("Times"):
            fl |= 2
        if base14.startswith("Courier"):
            fl |= 1
        if "Italic" in base14 or "Oblique" in base14:
            fl |= 64
        desc = doc.get_new_xref()
        doc.update_object(desc, f"<<{_descriptor_fields(font, name, fl)}>>")
        doc.xref_set_key(xref, "FontDescriptor", f"{desc} 0 R")
    elif base14 and not symbolic:
        doc.xref_set_key(desc, "Flags", str((flags & ~4) | 32))  # text font: use the /Encoding
    for k in ("FontFile", "FontFile2", "FontFile3"):
        doc.xref_set_key(desc, k, "null")
    doc.xref_set_key(desc, ffkey, f"{ff} 0 R")
    if not has_widths:
        first, last, w = _widths(doc, xref, font, base14)
        doc.xref_set_key(xref, "FirstChar", str(first))
        doc.xref_set_key(xref, "LastChar", str(last))
        doc.xref_set_key(xref, "Widths", "[" + " ".join(map(str, w)) + "]")
    return f"{name} → {label}"


def _embed_cid(doc, info: dict) -> Optional[str]:
    """Composite fonts: only an installed font with the same name keeps glyph ids valid."""
    if info["cid_subtype"] != "CIDFontType2":
        return None
    path, how = _find_system_font(info["name"], allow_twins=False)
    if not path:
        return None
    data = open(path, "rb").read()
    if data[:4] == b"OTTO":
        return None
    desc = info["desc"]
    if not desc:
        font = fitz.Font(fontfile=path)
        desc = doc.get_new_xref()
        doc.update_object(desc, f"<<{_descriptor_fields(font, info['name'], 32)}>>")
        doc.xref_set_key(info["cid"], "FontDescriptor", f"{desc} 0 R")
    ff = _new_stream(doc, data, f"/Length1 {len(data)}", path)
    doc.xref_set_key(desc, "FontFile2", f"{ff} 0 R")
    return f"{info['name']} → {how} {os.path.basename(path)}"


# --------------------------------------------------------------------------- #
# Rasterize fallback
# --------------------------------------------------------------------------- #

def _page_refs(doc, page) -> Set[int]:
    """All objects reachable from a page's resources (fonts, nested forms, ...)."""
    res = _key(doc, page.xref, "Resources")
    stack = [int(m) for m in _REF.findall(res[1])] if res[0] in ("dict", "xref") else []
    seen: Set[int] = set()
    while stack:
        x = stack.pop()
        if x in seen or x <= 0 or x >= doc.xref_length():
            continue
        seen.add(x)
        if doc.xref_is_stream(x) and _key(doc, x, "Subtype")[1] not in ("/Form",):
            continue  # images / font files: leaves
        try:
            stack.extend(int(m) for m in _REF.findall(doc.xref_object(x, compressed=True)))
        except Exception:
            continue
    return seen


def _rasterize_page(doc, page, dpi: int) -> None:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    for x in page.get_contents():
        doc.update_stream(x, b" ")
    doc.xref_set_key(page.xref, "Resources", "<<>>")
    page.insert_image(page.rect, pixmap=pix)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def embed_all_fonts(doc, raster_dpi: int = RASTER_DPI) -> EmbedResult:
    result = EmbedResult()
    for k in [k for k in _stream_cache if k[0] == id(doc)]:
        del _stream_cache[k]
    unresolved: Dict[int, str] = {}
    for info in list_fonts(doc):
        if info["embedded"]:
            continue
        try:
            if info["subtype"] == "Type0":
                msg = _embed_cid(doc, info)
            elif info["subtype"] in ("Type1", "MMType1", "TrueType"):
                msg = _embed_simple(doc, info)
            else:
                msg = None
        except Exception:
            msg = None
        if msg:
            result.embedded.append(msg)
        else:
            unresolved[info["xref"]] = info["name"]

    if unresolved:
        bad = set(unresolved)
        for page in doc:
            if _page_refs(doc, page) & bad:
                _rasterize_page(doc, page, raster_dpi)
                result.rasterized_pages.append(page.number)
        result.unresolved_fonts = sorted(set(unresolved.values()))
    for k in [k for k in _stream_cache if k[0] == id(doc)]:
        del _stream_cache[k]
    return result
