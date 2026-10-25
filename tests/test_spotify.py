"""
تست‌های واحد برای utils/spotify_downloader.py - پارس متاتگ صفحه‌ی ترک

اجرا: python -m pytest tests/test_spotify.py -v
"""
from utils.spotify_downloader import _parse_track_meta, build_search_query

_SAMPLE_HTML = """<!doctype html><html><head>
<meta property="og:title" content="کوچه باغ‌ها">
<meta name="music:musician_description" content="محسن چاوشی">
<meta property="og:image" content="https://i.scdn.co/image/cover123">
<meta property="og:description" content="محسن چاوشی · شاهراه · Song · 2024">
<script>window.__data={"audioPreview":{"url":"https:\\/\\/p.scdn.co\\/mp3-preview\\/abc123?cid=x"}}</script>
</head></html>"""

_NO_PREVIEW_HTML = """<html><head>
<meta property="og:title" content="Track Without Preview">
</head></html>"""


def test_parse_full_track():
    track = _parse_track_meta(_SAMPLE_HTML, "https://open.spotify.com/track/abc")
    assert track is not None
    assert track.title == "کوچه باغ‌ها"
    assert track.artist == "محسن چاوشی"
    # «Song» جزو کلمات غیر-آلبومیه، پس parts[1] = شاهراه آلبوم حساب می‌شه
    assert track.album == "شاهراه"
    assert track.cover_url == "https://i.scdn.co/image/cover123"


def test_preview_url_extracted_with_escaped_slashes():
    # JSON اسپاتیفای اسلش‌ها رو \/ می‌نویسه - باید نرمالایز بشه
    track = _parse_track_meta(_SAMPLE_HTML, "https://open.spotify.com/track/abc")
    assert track is not None
    assert track.preview_url == "https://p.scdn.co/mp3-preview/abc123?cid=x"


def test_preview_missing_is_none():
    track = _parse_track_meta(_NO_PREVIEW_HTML, "https://open.spotify.com/track/xyz")
    assert track is not None
    assert track.title == "Track Without Preview"
    assert track.preview_url is None


def test_no_title_means_no_track():
    assert _parse_track_meta("<html><head></head></html>", "https://open.spotify.com/track/q") is None


def test_artist_fallback_from_description():
    html = (
        '<html><head><meta property="og:title" content="T">'
        '<meta property="og:description" content="Some Artist · Some Album · Song · 2021">'
        "</head></html>"
    )
    track = _parse_track_meta(html, "https://open.spotify.com/track/q")
    assert track is not None
    assert track.artist == "Some Artist"
    assert track.album == "Some Album"


def test_search_query_combines_artist_and_title():
    from utils.spotify_downloader import SpotifyTrack

    track = SpotifyTrack(
        title="کوچه باغ‌ها", artist="محسن چاوشی", album="شاهراه",
        cover_url=None, spotify_url="https://open.spotify.com/track/abc",
    )
    assert build_search_query(track) == "محسن چاوشی کوچه باغ‌ها"

    no_artist = SpotifyTrack(
        title="Solo Track", artist=None, album=None,
        cover_url=None, spotify_url="https://open.spotify.com/track/def",
    )
    assert build_search_query(no_artist) == "Solo Track"
