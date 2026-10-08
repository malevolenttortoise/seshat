"""httpx's request lines keep coming, with secret query values masked
(2026-10 audit L2-08; Mark, G34)."""
import logging

from app.config import apply_logging, redact_url_secrets


def test_redaction_rules():
    line = ('HTTP Request: GET https://www.googleapis.com/books/v1/volumes'
            '?q=inauthor%3AX&key=AIzaPLACEHOLDER "HTTP/1.1 200 OK"')
    assert redact_url_secrets(line) == (
        'HTTP Request: GET https://www.googleapis.com/books/v1/volumes'
        '?q=inauthor%3AX&key=*** "HTTP/1.1 200 OK"'
    )
    assert redact_url_secrets("https://x/a?token=t1&page=2&Passkey=p&mam_id=m") == (
        "https://x/a?token=***&page=2&Passkey=***&mam_id=***"
    )
    assert redact_url_secrets("https://x/a?q=keyboard&monkey=1") == "https://x/a?q=keyboard&monkey=1"


def test_the_httpx_logger_masks_its_request_lines(caplog):
    apply_logging(verbose=False)
    apply_logging(verbose=False)            # idempotent: one filter, not two
    httpx_logger = logging.getLogger("httpx")
    assert sum(type(f).__name__ == "_RedactUrlSecrets" for f in httpx_logger.filters) == 1

    with caplog.at_level(logging.INFO, logger="httpx"):
        httpx_logger.info(
            'HTTP Request: %s %s "%s %d %s"', "GET",
            "https://www.googleapis.com/books/v1/volumes?q=x&key=AIzaPLACEHOLDER",
            "HTTP/1.1", 200, "OK",
        )
    [record] = [r for r in caplog.records if r.name == "httpx"]
    assert "AIzaPLACEHOLDER" not in record.getMessage()
    assert "key=***" in record.getMessage()
    assert record.getMessage().startswith("HTTP Request: GET https://www.googleapis.com/")
