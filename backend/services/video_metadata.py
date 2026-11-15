"""EthOS NAS — Videos filename metadata parser.

Pure string processing (no I/O, no deps) that turns messy release filenames
into structured library metadata, Jellyfin-style but conservative: when a name
doesn't match known patterns the result degrades to a tidied version of the
filename rather than a wrong guess.

Shared by the web UI and the ethos-video Android app via the API — both get
the same fields from `GET /api/videos/library`.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger("ethos-nas.services.video_metadata")

# S01E02, s1e2, S01 E02, S01-E02 … (season 1–99, episode 1–999)
_EP_RE = re.compile(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})\b", re.IGNORECASE)

# Standalone year 1900–2035, not embedded in a longer number and not a
# resolution tag ("1920p"); release names usually dot-separate it:
# Movie.Name.2024.1080p
_YEAR_TOKEN_RE = re.compile(r"(?<!\d)(19\d{2}|20[0-3]\d)(?!\d|p)")

# Leading tracker/site prefixes ("DDLValley.me_83_Digger…") — the site name +
# numeric release ID are not part of the title. The TLD anchor keeps real
# titles safe: only a leading `word.<tld>` followed by digits is stripped.
_SITE_PREFIX_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*\.(?:me|com|net|org|io|tv|cc|co|si|to|xyz|info|biz)(?![a-z])"
    r"[_ .]*\d{1,6}(?:[_ .]+(?=\S)|$)")

# Codec tags that survive tokenization as a stray letter ("H.264" → "H") and
# audio channel counts ("5.1", "2.0") — removed as units before splitting.
_CODEC_TAG_RE = re.compile(r"(?i)\bh\.?\d{3}\b")
_CHANNEL_TAG_RE = re.compile(r"\d{1,2}\.\d(?!\d|[a-z])")

# A trailing release group after the final dash ("…x264-KyoGo"): pure
# alphanumerics only, so "X-Men.2017.BluRay" keeps its title half intact.
_GROUP_TAIL_RE = re.compile(r"^[A-Za-z0-9]{1,24}$")

_YEAR_LIKE_RE = re.compile(r"(?:19\d{2}|20[0-3]\d)")
_RES_NUMS = {"240", "360", "480", "576", "720", "1080", "1440", "2160", "4320"}

_QUALITY_WORDS = {
    "4k": "4K", "uhd": "4K", "8k": "8K", "7k": "7K", "5k": "5K",
    "2160p": "4K", "3840p": "4K", "1440p": "1440p", "1920p": "1080p",
    "1080p": "1080p", "720p": "720p", "576p": "576p", "480p": "480p",
    "360p": "360p", "4096p": "8K",
}

# WxH resolution pairs → quality label (VR content is full of 7680x3840 etc.)
_RES_PAIRS = {
    "7680x3840": "8K", "3840x2160": "4K", "4096x2160": "4K",
    "2560x1440": "1440p", "1920x1080": "1080p", "1280x720": "720p",
}

# Quality tags are usually glued to underscores (…_4096p_8K_LR…), so plain \b
# word boundaries fail — use lookarounds that only exclude alphanumerics.
_QUALITY_TAG_RE = re.compile(r"(?<![a-z0-9])(\d{3,4}p|4k|5k|7k|8k|uhd)(?![a-z0-9])")

# Tokens that carry no title information — dropped from the display title.
_JUNK_TOKENS = {
    # codecs / encodes
    "h264", "h265", "hevc", "avc", "x264", "x265", "x26d", "vp9", "avs",
    "10bit", "8bit", "hi10p", "hdr10", "hdr", "dv", "dolby", "atmos",
    # release tags
    "bluray", "blu-ray", "brrip", "webrip", "web-dl", "webdl", "hdtv",
    "dvdrip", "remux", "proper", "repack", "internal", "limited", "extended",
    "unrated", "directorscut", "dc", "multi", "dual", "audio", "subbed",
    "imax", "imaxenhanced",
    # capture / source tags (bare forms — dashes/underscores become spaces)
    "cam", "cams", "telesync", "ts", "hdrip", "screener", "scr", "pdtv",
    "web", "dl", "dvd",
    # streaming sources
    "amzn", "nf", "disney", "dsny", "hmax", "prime", "peacock", "wbd",
    "mxf", "tubitv", "crint", "plutotv",
    # VR / capture tags seen in the real library
    "lr", "3dh", "fisheye190", "uhq", "vrdesktophd", "oculusrift",
    "original", "highquality", "hq", "sd", "hd", "fhd",

    # well-known release groups ("…x264-DEMAND")
    "demand", "fgt", "yify", "rarbg", "rartv", "ettv",
}

# Numbers that are never title words: 3+ digits (release IDs like "180",
# "94421", unix timestamps). 1–2 digit numbers stay — real titles use them
# ("Rocky 3"). Guarded in _clean_title so a numeric-only name survives.
# Quality/resolution/fps tokens that leak into titles ("… 4096p 8K 180 …").
_QUALITY_TOKEN_RE = re.compile(
    r"^(?:\d{3,4}p|4k|5k|7k|8k|uhd|\d{3,5}x\d{3,5}|\d+fps)$", re.IGNORECASE)

# Audio codec / channel tags: aac2.0, ac3, eac3, dts-hd ma 5.1, dd5.1, truehd…
_AUDIO_TAG_RE = re.compile(
    r"^(?:aac|ac[-_]?3|eac[-_]?3|dts(?:-?hd)?|ddp?|truehd|flac|mp3|opus|mlp)"
    r"[\s._-]*\d{0,2}[\.\s]*\d{0,1}(?:ch|c)?$", re.IGNORECASE)


def _clean_title(raw: str) -> str:
    """Underscores/hyphens → spaces, drop junk tokens, collapse whitespace.

    Also strips release-name scaffolding that is not part of the title:
    a leading tracker/site prefix (``DDLValley.me_83_``), a trailing release
    group after the final dash (``…x264-KyoGo`` → ``KyoGo`` dropped), codec
    tags like ``H.264``, and audio channel counts (``5.1``). Short numbers
    that are not years/resolutions are kept — they are usually sequel parts
    ("Practical Magic 2", "Lust Stories 3").
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    s = _SITE_PREFIX_RE.sub("", s)
    if "-" in s:
        head, _, tail = s.rpartition("-")
        if _GROUP_TAIL_RE.match(tail):
            s = head
    s = s.replace("_", " ").replace("-", " ")
    s = _CODEC_TAG_RE.sub(" ", s)
    s = _CHANNEL_TAG_RE.sub(" ", s)
    tokens = [t for t in re.split(r"[\s.]+", s) if t]
    # A leading site name + TLD token pair without a release ID
    # ("DDLValley me The Uprising") is not part of the title.
    if len(tokens) >= 3 and tokens[1].lower() in ("me", "com", "net", "org",
                                                    "io", "tv", "cc", "co", "si"):
        tokens = tokens[2:]
    kept = []
    for t in tokens:
        low = t.lower().replace(" ", "")
        if low in _JUNK_TOKENS or _QUALITY_TOKEN_RE.match(low) or _AUDIO_TAG_RE.match(low):
            continue
        # Drop years, long numeric IDs and resolution numbers; keep short
        # non-year numbers — usually sequel parts ("Practical Magic 2").
        if re.fullmatch(r"\d+", t) and (
                len(t) >= 4 or _YEAR_LIKE_RE.fullmatch(t) or t in _RES_NUMS):
            continue
        kept.append(t)
    out = " ".join(kept).strip(" .")
    return re.sub(r"\s{2,}", " ", out)


def parse_video_name(filename: str, folder: Optional[str] = None) -> dict:
    """Parse a video filename into library metadata.

    `folder` (parent directory basename) is used as the series name fallback
    when an SxxEyy pattern has no title prefix. Never raises; unknown shapes
    yield kind="movie" with a cleaned title.
    """
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", filename)  # strip extension
    # Drop a leading tracker/site prefix ("DDLValley.me_83_") before any other
    # parsing so year/episode extraction never sees the release ID.
    stem = _SITE_PREFIX_RE.sub("", stem).strip()

    def _is_tag(tok: str) -> bool:
        low = tok.lower()
        return (low in _JUNK_TOKENS or bool(_QUALITY_TOKEN_RE.match(low))
                or bool(_AUDIO_TAG_RE.match(low)))

    first_clean = next((t for t in re.split(r"[\s._-]+", stem) if not _is_tag(t)), None)

    result: dict = {
        "title": _clean_title(stem) or (first_clean or stem or filename),
        "year": None,
        "season": None,
        "episode": None,
        "show": None,
        "kind": "movie",
        "quality": None,
    }

    # ── quality (word tags + WxH pairs) ────────────────────────────────
    low = stem.lower()
    for tag in _QUALITY_TAG_RE.findall(low):
        q = _QUALITY_WORDS.get(tag)
        if q:
            result["quality"] = q
            break
    if not result["quality"]:
        for pair, label in _RES_PAIRS.items():
            if pair in low.replace(" ", ""):
                result["quality"] = label
                break

    # ── season / episode ───────────────────────────────────────────────
    m = _EP_RE.search(stem)
    if m:
        result["kind"] = "episode"
        result["season"] = int(m.group(1))
        result["episode"] = int(m.group(2))
        prefix = stem[:m.start()].strip(" ._-")
        suffix = stem[m.end():].strip(" ._-")
        if prefix:
            result["show"] = _clean_title(prefix) or None
        elif folder:
            result["show"] = _clean_title(folder) or None
        ep_title = _clean_title(suffix) if suffix else ""
        result["title"] = ep_title or f"Episode {result['episode']}"

    # ── year (only for movies — episodes rarely carry one) ─────────────
    if result["kind"] != "episode":
        ym = _YEAR_TOKEN_RE.search(stem)
        if ym:
            result["year"] = int(ym.group(1))

    # Special case: the whole name is a year ("1917.IMAX.mkv") — that number
    # IS the title, not a release-year tag.
    core = [t for t in re.split(r"[\s._-]+", stem)
            if t and t.lower() not in _JUNK_TOKENS and not _QUALITY_TOKEN_RE.match(t.lower())]
    if len(core) == 1 and re.fullmatch(r"(19\d{2}|20[0-3]\d)", core[0]):
        result["title"] = core[0]
        result["year"] = None

    if result["kind"] == "movie" and result["year"] is None:
        logger.debug("No year found in %r — parsed as %r", filename, result["title"])

    return result


def parse_video_item(name: str, folder_path: Optional[str] = None) -> dict:
    """Convenience wrapper for library items: adds fields to a copy of the item."""
    folder_name = None
    if folder_path:
        folder_name = folder_path.rstrip("/").rsplit("/", 1)[-1] or None
    return parse_video_name(name, folder=folder_name)
