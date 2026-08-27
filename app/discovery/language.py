"""Language-value normalization for discovered books.

`books.language` is written by eight sources that do not agree on a
format. Measured on the reference install:

  - OpenLibrary  → ISO 639-1 two-letter (``"en"``, ``"es"``), already
    mapped from MARC by `sources/openlibrary.py::_OL_LANGUAGE_MAP`.
  - Hardcover    → the literal ``"English"`` for eng/en, and the raw
    ISO 639-2 code (``"fre"``, ``"ger"``) for everything else.
  - Kobo         → scraped page text, so whatever the product page says.
  - Amazon       → ``"English"`` (the `_amazon_entry` default).

So the same language arrives as ``"en"``, ``"eng"`` and ``"English"``
depending on who found the book, and a sweep written as
``language NOT LIKE 'en%'`` silently disagrees with itself across
sources. Everything that compares languages goes through here.

Deliberately **fails open**: an unrecognized value normalizes to None,
and callers treat None as "unknown", never as "foreign". A language
lookup that can't parse its input must not cause a book to be dropped
or flagged — the same contract `_lang_ok` and `_extract_language`
already follow.
"""
from __future__ import annotations

from typing import Optional

# ISO 639-2/B + /T and other 3-letter forms → ISO 639-1. Superset of
# `sources/openlibrary.py::_OL_LANGUAGE_MAP`, which only had to cover
# what OL emits; this has to cover every source.
_THREE_TO_TWO = {
    "eng": "en",
    "spa": "es",
    "fre": "fr", "fra": "fr",
    "ger": "de", "deu": "de",
    "ita": "it",
    "jpn": "ja",
    "rus": "ru",
    "chi": "zh", "zho": "zh",
    "por": "pt",
    "dut": "nl", "nld": "nl",
    "kor": "ko",
    "pol": "pl",
    "swe": "sv",
    "tur": "tr",
    "ara": "ar",
    "heb": "he",
    "hin": "hi",
    "tha": "th",
    "vie": "vi",
    "ind": "id",
    "dan": "da",
    "nor": "no", "nob": "no", "nno": "no",
    "fin": "fi",
    "ces": "cs", "cze": "cs",
    "gre": "el", "ell": "el",
    "hun": "hu",
    "ron": "ro", "rum": "ro",
    "ukr": "uk",
    "bul": "bg",
    "cat": "ca",
    "slk": "sk", "slo": "sk",
    "hrv": "hr",
    "srp": "sr",
    "lat": "la",
    "fas": "fa", "per": "fa",
    "ben": "bn",
    "tam": "ta",
    "urd": "ur",
    "mal": "ml",
    "tel": "te",
    "mar": "mr",
    "guj": "gu",
    "afr": "af",
    "est": "et",
    "lav": "lv",
    "lit": "lt",
    "slv": "sl",
    "isl": "is", "ice": "is",
    "gle": "ga",
    "cym": "cy", "wel": "cy",
    "eus": "eu", "baq": "eu",
    "glg": "gl",
    "mya": "my", "bur": "my",
    "khm": "km",
    "lao": "lo",
    "sin": "si",
    "nep": "ne",
    "swa": "sw",
    "zul": "zu",
    "amh": "am",
    "fil": "tl", "tgl": "tl",
    "msa": "ms", "may": "ms",
}

# English language names → ISO 639-1. Sources emit the display form
# ("English", "German"); some emit the endonym ("Deutsch").
_NAME_TO_TWO = {
    "english": "en",
    "spanish": "es", "espanol": "es", "español": "es", "castellano": "es",
    "french": "fr", "francais": "fr", "français": "fr",
    "german": "de", "deutsch": "de",
    "italian": "it", "italiano": "it",
    "japanese": "ja",
    "russian": "ru",
    "chinese": "zh",
    "simplified chinese": "zh", "traditional chinese": "zh",
    "portuguese": "pt", "portugues": "pt", "português": "pt",
    "dutch": "nl", "nederlands": "nl",
    "korean": "ko",
    "polish": "pl", "polski": "pl",
    "swedish": "sv", "svenska": "sv",
    "turkish": "tr",
    "arabic": "ar",
    "hebrew": "he",
    "hindi": "hi",
    "thai": "th",
    "vietnamese": "vi",
    "indonesian": "id",
    "danish": "da", "dansk": "da",
    "norwegian": "no", "norsk": "no",
    "finnish": "fi", "suomi": "fi",
    "czech": "cs",
    "greek": "el",
    "hungarian": "hu", "magyar": "hu",
    "romanian": "ro",
    "ukrainian": "uk",
    "bulgarian": "bg",
    "catalan": "ca",
    "slovak": "sk",
    "croatian": "hr",
    "serbian": "sr",
    "latin": "la",
    "persian": "fa", "farsi": "fa",
    "bengali": "bn",
    "tamil": "ta",
    "urdu": "ur",
    "icelandic": "is",
    "irish": "ga",
    "welsh": "cy",
    "basque": "eu",
    "galician": "gl",
    "burmese": "my",
    "khmer": "km",
    "sinhala": "si",
    "nepali": "ne",
    "swahili": "sw",
    "afrikaans": "af",
    "estonian": "et",
    "latvian": "lv",
    "lithuanian": "lt",
    "slovenian": "sl",
    "filipino": "tl", "tagalog": "tl",
    "malay": "ms",
}

# Two-letter codes we accept as-is. Derived from the maps above so the
# three tables can't drift apart.
_KNOWN_TWO = set(_THREE_TO_TWO.values()) | set(_NAME_TO_TWO.values())


def normalize_language(raw: Optional[str]) -> Optional[str]:
    """Normalize a source-reported language to an ISO 639-1 code.

    Handles the three shapes actually present in the DB — two-letter
    codes, three-letter codes and English display names — plus locale
    tags (``"en-US"``, ``"pt_BR"``) and parenthesized qualifiers
    (``"English (US)"``), which appear in scraped Kobo values.

    Returns None for anything unrecognized, empty, or explicitly
    unknown (``"und"``, ``"mul"``, ``"unknown"``). **None means
    "unknown", not "foreign"** — callers must not treat it as a
    positive signal.

    >>> normalize_language("English"), normalize_language("eng")
    ('en', 'en')
    >>> normalize_language("en-GB"), normalize_language("  DEU ")
    ('en', 'de')
    >>> normalize_language("Klingon") is None
    True
    """
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None

    # "English (US)" / "Chinese [Simplified]" → drop the qualifier.
    for opener, closer in (("(", ")"), ("[", "]")):
        if opener in s:
            s = s.split(opener, 1)[0].strip()
    if not s:
        return None

    # Explicit unknowns. "mul" (multiple) and "und" (undetermined) are
    # real MARC codes and must not be guessed at.
    if s in ("und", "mul", "unknown", "unspecified", "none", "n/a", "zxx"):
        return None

    # Locale tag: "en-US", "pt_BR", "zh-Hans".
    if len(s) > 2 and s[2] in ("-", "_"):
        s = s[:2]
    elif len(s) > 3 and s[3] in ("-", "_"):
        s = s[:3]

    if s in _NAME_TO_TWO:
        return _NAME_TO_TWO[s]
    if len(s) == 3 and s in _THREE_TO_TWO:
        return _THREE_TO_TWO[s]
    if len(s) == 2 and s in _KNOWN_TWO:
        return s
    return None


def is_english(raw: Optional[str]) -> bool:
    """True only when `raw` normalizes to English.

    Unknown values are **not** English — but they aren't foreign either,
    so callers deciding whether to flag a book must test
    `is_foreign()`, not `not is_english()`.
    """
    return normalize_language(raw) == "en"


def is_foreign(raw: Optional[str]) -> bool:
    """True only when `raw` normalizes to a language that is NOT English.

    This is the sweep predicate. It is deliberately narrower than
    ``not is_english()``: an unrecognized or missing language returns
    False here, so a book is never flagged on the strength of a value
    we failed to parse. On the reference install that distinction covers
    99.3% of unowned rows, which have no language at all.
    """
    code = normalize_language(raw)
    return code is not None and code != "en"
