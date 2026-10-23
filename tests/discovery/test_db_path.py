"""`get_db_path` opens nothing but `seshat_<slug>.db` in DATA_DIR (CodeQL #69).

Slugs reach it from requests (`?slug=`, the library switch) and through the
active library, so a slug `config.slugify` couldn't have made is refused
instead of becoming a path.
"""
import pytest

from app.config import slugify
from app.discovery import database as disco_db


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    yield tmp_path
    disco_db.set_active_library(None)


@pytest.mark.parametrize("slug", ["calibre-library", "abs-audio-library", "default", "lib2"])
def test_a_library_slug_is_its_file_in_data_dir(data_dir, slug):
    assert disco_db.get_db_path(slug) == data_dir / f"seshat_{slug}.db"


@pytest.mark.parametrize("name", ["Calibre Library", "ABS: Audio  Library!", "---", "Ünïcode Books"])
def test_every_slug_slugify_makes_is_accepted(data_dir, name):
    slug = slugify(name)
    assert disco_db.get_db_path(slug) == data_dir / f"seshat_{slug}.db"


@pytest.mark.parametrize("slug", [
    "../escape", "a/b", "..", "Calibre", "lib.db", "lib-", "-lib", "lib--x",
    "lib\n", "lib name", "ｌｉｂ",
])
def test_anything_slugify_could_not_make_is_refused(data_dir, slug):
    with pytest.raises(ValueError):
        disco_db.get_db_path(slug)


def test_the_active_library_is_checked_too(data_dir):
    disco_db.set_active_library("../escape")
    with pytest.raises(ValueError):
        disco_db.get_db_path()


def test_no_slug_and_no_active_library_is_the_default_db(data_dir):
    disco_db.set_active_library(None)
    assert disco_db.get_db_path() == data_dir / "seshat_default.db"


def test_a_cleanup_preview_copy_is_still_opened(data_dir, tmp_path):
    copy = tmp_path / "copies" / "seshat_calibre-library.db"
    with disco_db.using_copies({"calibre-library": str(copy)}):
        assert disco_db.get_db_path("calibre-library") == copy
