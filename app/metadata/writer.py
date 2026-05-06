"""
Epub metadata writer.

Modifies the OPF metadata inside an epub file to fix author/title
before handing the file to a sink (CWA, Calibre, etc.).

IMPORTANT: This ONLY modifies copies of files — never the original
in the download directory. The original must stay byte-identical for
qBit seeding. The pipeline copies to staging or the ingest directory
first, then calls this module to fix the copy.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Optional

_log = logging.getLogger("seshat.metadata")

_DC = "http://purl.org/dc/elements/1.1/"
_OPF = "http://www.idpf.org/2007/opf"
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Register namespaces so ET.tostring doesn't mangle the prefixes.
ET.register_namespace("dc", _DC)
ET.register_namespace("opf", _OPF)
ET.register_namespace("", _OPF)


def patch_epub_metadata(
    epub_path: str | Path,
    *,
    title: Optional[str] = None,
    authors: Optional[list[str]] = None,
    series: Optional[str] = None,
    series_index: Optional[str] = None,
    language: Optional[str] = None,
    description: Optional[str] = None,
) -> bool:
    """Patch metadata fields inside an epub file in-place.

    Only modifies fields that are explicitly passed (non-None).
    Returns True on success, False on any error.

    This function modifies the file at `epub_path` directly —
    the caller is responsible for ensuring it's a COPY, not the
    seeding original.
    """
    path = Path(epub_path)
    if not path.exists() or path.suffix.lower() != ".epub":
        return False

    try:
        return _patch_opf(path, title=title, authors=authors,
                          series=series, series_index=series_index,
                          language=language, description=description)
    except Exception:
        _log.exception("failed to patch epub metadata: %s", path)
        return False


def _patch_opf(
    epub_path: Path,
    *,
    title: Optional[str],
    authors: Optional[list[str]],
    series: Optional[str],
    series_index: Optional[str],
    language: Optional[str],
    description: Optional[str] = None,
) -> bool:
    """Read the epub zip, patch the OPF, write it back."""
    import shutil
    import tempfile

    opf_path = None
    opf_content = None

    # Read the OPF from the epub.
    with zipfile.ZipFile(str(epub_path), "r") as zf:
        opf_path = _find_opf_path(zf)
        if not opf_path:
            _log.warning("no OPF found in %s", epub_path)
            return False
        opf_content = zf.read(opf_path)

    # Parse and modify the OPF.
    tree = ET.ElementTree(ET.fromstring(opf_content))
    root = tree.getroot()
    md = root.find(f"{{{_OPF}}}metadata")
    if md is None:
        md = root.find("metadata")
    if md is None:
        _log.warning("no <metadata> element in OPF")
        return False

    changed = False

    if title is not None:
        changed |= _set_dc(md, "title", title)

    if authors is not None:
        changed |= _set_dc_creators(md, authors)

    if language is not None:
        changed |= _set_dc(md, "language", language)

    if description is not None:
        changed |= _set_dc(md, "description", description)

    if series is not None:
        changed |= _set_meta(md, "calibre:series", series)

    if series_index is not None:
        changed |= _set_meta(md, "calibre:series_index", series_index)

    if not changed:
        return True  # nothing to change

    # Write the modified OPF back into the epub.
    new_opf = ET.tostring(root, encoding="unicode", xml_declaration=True)

    # Rebuild the zip with the modified OPF. NamedTemporaryFile reserves
    # a unique name via O_EXCL, avoiding the create/use race that the
    # deprecated tempfile.mktemp() exposes.
    with tempfile.NamedTemporaryFile(
        suffix=".epub", dir=epub_path.parent, delete=False
    ) as _tmpf:
        tmp = Path(_tmpf.name)
    try:
        with zipfile.ZipFile(str(epub_path), "r") as zf_in, \
             zipfile.ZipFile(str(tmp), "w") as zf_out:
            for item in zf_in.infolist():
                if item.filename == opf_path:
                    zf_out.writestr(item, new_opf)
                else:
                    zf_out.writestr(item, zf_in.read(item.filename))

        # Atomic replace.
        shutil.move(str(tmp), str(epub_path))
        _log.info("patched epub metadata: %s", epub_path.name)
        return True
    except Exception:
        # Clean up temp file on error.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _find_opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    """Locate the OPF file inside the epub zip."""
    try:
        with zf.open("META-INF/container.xml") as f:
            tree = ET.parse(f)
        for rf in tree.iter(f"{{{_CONTAINER_NS}}}rootfile"):
            path = rf.get("full-path")
            if path:
                return path
    except (KeyError, ET.ParseError):
        pass
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    return None


def _set_dc(md_element, tag: str, value: str) -> bool:
    """Set a Dublin Core element, creating it if needed."""
    el = md_element.find(f"{{{_DC}}}{tag}")
    if el is None:
        el = ET.SubElement(md_element, f"{{{_DC}}}{tag}")
    if el.text == value:
        return False
    el.text = value
    return True


def _set_dc_creators(md_element, authors: list[str]) -> bool:
    """Replace all dc:creator elements with the given authors."""
    # Remove existing creators.
    existing = md_element.findall(f"{{{_DC}}}creator")
    for el in existing:
        md_element.remove(el)

    # Add new ones.
    for author in authors:
        el = ET.SubElement(md_element, f"{{{_DC}}}creator")
        el.text = author

    return True


def _set_meta(md_element, name: str, content) -> bool:
    """Set a <meta name="..." content="..."> element.

    Coerces `content` to str up front — Python's XML writer raises
    `TypeError: argument of type 'float' is not iterable` in
    `_escape_attrib` if you pass a non-string attribute value.
    The enricher yields `series_index` as a float, and the source-
    metadata handoff path can land similar float/int values, so
    stringifying here covers every caller instead of forcing each to
    remember.
    """
    if content is None:
        return False
    content_str = str(content)
    # Check both namespaced and non-namespaced meta elements.
    for ns in [f"{{{_OPF}}}", ""]:
        for meta in md_element.findall(f"{ns}meta"):
            if meta.get("name") == name:
                if meta.get("content") == content_str:
                    return False
                meta.set("content", content_str)
                return True

    # Not found — create it.
    meta = ET.SubElement(md_element, "meta")
    meta.set("name", name)
    meta.set("content", content_str)
    return True
