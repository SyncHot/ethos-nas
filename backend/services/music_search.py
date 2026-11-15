"""Search ranking and Polish text normalisation for the Music app.

YouTube (via yt-dlp ``ytsearchN:``) returns results in *its* relevance order and
repeats the same song across uploads, covers and hour-long compilations. That was
acceptable for a prototype; with 150 radio stations and a podcast catalog it is
not. This module is the single place that decides what a search result is worth:

- :func:`fold`      — Polish diacritics → ASCII (``żółw`` → ``zolw``), lowercase,
                      punctuation collapsed. Both clients use the same rules for
                      their offline caches, so keep the algorithm in sync with
                      ``ethos-music/data/SearchNormalizer.kt``.
- :func:`split_artist_title` — ``"Artist - Title (Official Video)"`` →
                      ``("Artist", "Title")``.
- :func:`rank_tracks` — score, de-duplicate and re-sort yt-dlp results.
- :func:`search_catalog` — folded substring/prefix/fuzzy match over the radio and
                      podcast catalogs (and the user's own library/playlists).

The module is pure Python (no FastAPI, no yt-dlp, no network) so it is cheap to
unit-test and safe to import from scripts that build the catalogs.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Iterable, Sequence

logger = logging.getLogger("ethos-nas.services.music_search")

# Characters NFKD does not decompose into ASCII (ł/Ł are single code points).
_PL_MAP = str.maketrans({"ł": "l", "Ł": "l"})

_NON_WORD_RE = re.compile(r"[^a-z0-9]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Bracketed noise: "(Official Video)", "[4K]", "(Official Audio)", "(prod. ...)".
_BRACKET_NOISE_RE = re.compile(
    r"[\(\[]\s*(?:[^\)\]]*?\b(?:"
    r"official|oficjaln\w*|audio|video|lyric\w*|tekst|teledysk|remaster\w*|"
    r"hd|hq|4k|8k|mv|m/v|na\s+żywo|live|radio\s+edit|extended|sped\s+up|slowed|"
    r"nightcore|8d|instrumental|karaoke|cover|remix|prod\.?"
    r")\b[^\)\]]*)[\)\]]",
    re.IGNORECASE,
)

# Trailing "- Official Video" / "| Official Music Video" without brackets.
_TRAILING_NOISE_RE = re.compile(
    r"\s*[-–—|]\s*(?:official\s*(?:music\s*)?(?:video|audio)|oficjalny\s*(?:teledysk|klip)|"
    r"teledysk|lyrics?|tekst)\s*$",
    re.IGNORECASE,
)

_ARTIST_SEPARATORS = (" - ", " – ", " — ", " | ", " • ")

# Words that make a result a worse fit for a plain song query.
_LOW_VALUE_WORDS = (
    "cover", "karaoke", "instrumental", "backing track", "reaction", "nightcore",
    "8d audio", "sped up", "slowed", "1 hour", "10 hours", "loop", "tutorial",
    "nauka gry", "how to play", "sheet music", "nuty",
)

# Results dropped entirely unless the query asks for them. A 10-hour loop or a
# karaoke backing track is never what “find me this song” means.
_HARD_DROP_WORDS = (
    "karaoke", "instrumental", "backing track", "10 hours", "8 hours", "1 hour",
    "nightcore", "8d audio", "speed up", "slowed",
)

# Channels that publish the official recording (YouTube Music "Topic" channels).
_OFFICIAL_ARTIST_SUFFIX = "topic"


def fold(text: Any) -> str:
    """Lowercase, strip Polish diacritics and punctuation.

    ``"Żółw Śpiewa – Łódź!"`` → ``"zolw spiewa lodz"``.
    """
    if text is None:
        return ""
    s = str(text)
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).translate(_PL_MAP).lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _NON_WORD_RE.sub(" ", s)
    return _WHITESPACE_RE.sub(" ", s).strip()


def tokens(text: Any) -> list[str]:
    """Folded word tokens (empty list for blank input)."""
    folded = fold(text)
    return folded.split(" ") if folded else []


def strip_noise(title: Any) -> str:
    """Remove ``(Official Video)`` / ``- Lyrics`` decorations from a title."""
    s = str(title or "").strip()
    if not s:
        return ""
    s = _BRACKET_NOISE_RE.sub(" ", s)
    s = _TRAILING_NOISE_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip(" -–—|·•")
    return s


def split_artist_title(title: Any) -> tuple[str, str]:
    """``"Doda - Nie żałuję (Official Video)"`` → ``("Doda", "Nie żałuję")``.

    Returns ``("", cleaned_title)`` when the upload has no ``Artist - Title`` shape.
    """
    raw = str(title or "").strip()
    for sep in _ARTIST_SEPARATORS:
        if sep in raw:
            left, right = raw.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, strip_noise(right)
    return "", strip_noise(raw)


def track_signature(title: Any, artist: Any) -> tuple[str, str]:
    """Normalised ``(core_title, artist)`` used to collapse duplicate uploads."""
    artist_in_title, core = split_artist_title(title)
    core_norm = fold(core)
    # "Song feat. X" and "Song" are the same recording.
    core_norm = re.sub(r"\b(?:feat|ft|featuring)\b.*$", "", core_norm).strip()
    artist_norm = fold(artist or artist_in_title)
    for suffix in (" - topic", " topic"):
        if artist_norm.endswith(suffix):
            artist_norm = artist_norm[: -len(suffix)].strip()
    # YouTube often prefixes the channel with "Radio"/"TV" — irrelevant here.
    return core_norm, artist_norm


def _levenshtein_within(a: str, b: str, max_distance: int = 1) -> bool:
    """True when ``a`` and ``b`` differ by at most ``max_distance`` edits."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > max_distance:
        return False
    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i]
        row_min = i
        for j, ch_b in enumerate(b, start=1):
            cost = 0 if ch_a == ch_b else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
            row_min = min(row_min, current[j])
        if row_min > max_distance:
            return False
        previous = current
    return previous[-1] <= max_distance


def _query_terms(query: Any) -> tuple[str, list[str]]:
    q_fold = fold(query)
    return q_fold, (q_fold.split(" ") if q_fold else [])


def _duration_penalty(duration: Any) -> float:
    try:
        seconds = int(duration or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return 0.0
    if seconds < 30:
        return -3.0            # snippets, previews
    if seconds > 15 * 60:
        return -4.0            # mixes/compilations
    if seconds > 8 * 60:
        return -1.5
    return 0.0


def score_track(track: dict, q_fold: str, q_tokens: Sequence[str]) -> float:
    """Relevance score for one yt-dlp result (higher = better)."""
    title = track.get("title") or ""
    artist = track.get("artist") or ""
    title_fold = fold(title)
    artist_fold = fold(artist)
    core_artist, core_title = split_artist_title(title)
    core_fold = fold(core_title)

    score = 0.0
    if q_fold:
        if title_fold == q_fold or core_fold == q_fold:
            score += 12.0
        elif title_fold.startswith(q_fold):
            score += 8.0
        elif core_fold.startswith(q_fold):
            score += 7.0
        elif q_fold in title_fold:
            score += 5.0
        elif q_fold in artist_fold:
            score += 3.5

        # Every query word present in the title is a strong signal.
        title_tokens = set(title_fold.split()) | set(core_fold.split())
        matched = sum(1 for t in q_tokens if t in title_tokens)
        score += 2.0 * matched
        if q_tokens and matched == len(q_tokens):
            score += 3.0
        score += 1.0 * sum(1 for t in q_tokens if t in set(artist_fold.split()))

        # "Artist - Title" shape with the queried artist in either slot.
        if core_artist and fold(core_artist) == q_fold:
            score += 2.0
        if artist_fold and q_fold in artist_fold:
            score += 1.5

        # Prefix tolerance: "kryminator" → "kryminatorium" (typos, truncation).
        for token in q_tokens:
            if len(token) >= 5 and token not in title_tokens:
                if any(t.startswith(token) or _levenshtein_within(token, t) for t in title_tokens if len(t) >= 5):
                    score += 1.0

    # Official uploads (Topic channels, VEVO, label channels) are the recording
    # the user most likely wants, not a re-upload with a noisy title.
    if artist_fold.endswith(_OFFICIAL_ARTIST_SUFFIX) or "- topic" in artist_fold:
        score += 3.0
    if "vevo" in artist_fold:
        score += 2.5
    if "official" in artist_fold or "oficjalny" in artist_fold:
        score += 1.5

    for word in _LOW_VALUE_WORDS:
        if word in title_fold:
            score -= 4.0

    score += _duration_penalty(track.get("duration"))

    # Covers/karaoke are only acceptable when explicitly requested.
    requested_noise = any(word in q_fold for word in ("cover", "karaoke", "instrumental", "remix", "live"))
    if not requested_noise and any(word in title_fold for word in ("cover", "karaoke", "instrumental")):
        score -= 2.0

    return score


def rank_tracks(
    query: str,
    items: Iterable[dict],
    *,
    limit: int | None = None,
) -> list[dict]:
    """Score, de-duplicate and re-sort yt-dlp search results.

    Duplicates are collapsed first by video id, then by ``(core title, artist)``,
    then by identical folded title — the highest-scoring variant wins, so an
    official Topic upload beats an unofficial re-upload of the same song.
    Original yt-dlp order is the tie-breaker (it still reflects YouTube's data).
    """
    q_fold, q_tokens = _query_terms(query)
    wants_noise = any(word in q_fold for word in _HARD_DROP_WORDS + ("cover", "remix", "live", "mix"))
    scored: list[tuple[float, int, dict]] = []
    for index, item in enumerate(items or ()):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        title_fold = fold(item.get("title"))
        if not wants_noise and any(word in title_fold for word in _HARD_DROP_WORDS):
            continue
        scored.append((score_track(item, q_fold, q_tokens), index, item))

    scored.sort(key=lambda row: (-row[0], row[1]))

    # Covers are only dropped when a clearly better non-cover result exists —
    # searching for “cover piosenki” must still find covers.
    if scored and not wants_noise:
        best = scored[0][0]
        if best >= 6.0:
            scored = [
                row for row in scored
                if not ("cover" in fold(row[2].get("title")) and row[0] < best * 0.7)
            ]

    seen_ids: set[str] = set()
    seen_signatures: set[tuple[str, str]] = set()
    seen_titles: set[str] = set()
    out: list[dict] = []
    for score, _index, item in scored:
        vid = str(item.get("id") or "")
        if not vid or vid in seen_ids:
            continue
        signature = track_signature(item.get("title"), item.get("artist"))
        title_fold = fold(item.get("title"))
        if signature[0] and signature in seen_signatures:
            continue
        if title_fold and title_fold in seen_titles:
            continue
        seen_ids.add(vid)
        if signature[0]:
            seen_signatures.add(signature)
        if title_fold:
            seen_titles.add(title_fold)
        out.append(item)
        if limit is not None and len(out) >= limit:
            break
    return out


def score_catalog_item(
    item: dict,
    q_fold: str,
    q_tokens: Sequence[str],
    *,
    name_keys: Sequence[str] = ("name",),
    field_weights: dict[str, float] | None = None,
) -> float:
    """Folded match score for one catalog entry (radio station, podcast, playlist)."""
    weights = field_weights or {"name": 20.0, "author": 6.0, "category": 4.0, "tags": 3.0}
    # `name_keys` may be ("title",) for the library; the primary field weight is
    # whatever the caller attached to "name" (or the largest weight).
    name_weight = float(weights.get("name") or max(weights.values(), default=20.0))

    def value_for(key: str) -> str:
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            return " ".join(str(v) for v in value)
        return str(value or "")

    name_fold = ""
    for key in name_keys:
        name_fold = fold(value_for(key))
        if name_fold:
            break

    score = 0.0
    if q_fold and name_fold:
        if name_fold == q_fold:
            score += name_weight
        elif name_fold.startswith(q_fold):
            score += name_weight * 0.6
        elif q_fold in name_fold:
            score += name_weight * 0.4
        name_tokens = name_fold.split()
        if q_tokens:
            matched = sum(1 for t in q_tokens if t in name_tokens)
            score += 2.0 * matched
            if matched == len(q_tokens):
                score += 3.0
            # Word-start match ("troj" → "trojka") and single-edit typos.
            for token in q_tokens:
                if len(token) >= 4 and token not in name_tokens:
                    if any(t.startswith(token) for t in name_tokens):
                        score += 2.0
                    elif any(len(t) >= 4 and _levenshtein_within(token, t) for t in name_tokens):
                        score += 1.5

    for key, weight in weights.items():
        if key in name_keys or key == "name":
            continue
        field_fold = fold(value_for(key))
        if field_fold and q_fold and q_fold in field_fold:
            score += weight
        elif field_fold and q_tokens:
            field_tokens = set(field_fold.split())
            score += min(weight, 1.0 * sum(1 for t in q_tokens if t in field_tokens))

    return score


def search_catalog(
    items: Iterable[dict],
    query: str,
    *,
    name_keys: Sequence[str] = ("name",),
    field_weights: dict[str, float] | None = None,
    limit: int | None = None,
    min_score: float = 0.5,
) -> list[dict]:
    """Filter + rank catalog entries by a user query.

    Empty query returns the input unchanged (callers paginate the full catalog).
    Entries below ``min_score`` are dropped — unlike YouTube results, a catalog
    entry that does not match any token is simply not a match.
    """
    q_fold, q_tokens = _query_terms(query)
    source = [item for item in (items or ()) if isinstance(item, dict)]
    if not q_fold:
        return source[:limit] if limit is not None else source

    scored = [
        (score_catalog_item(item, q_fold, q_tokens, name_keys=name_keys, field_weights=field_weights), index, item)
        for index, item in enumerate(source)
    ]
    scored = [row for row in scored if row[0] >= min_score]
    scored.sort(key=lambda row: (-row[0], row[1]))
    ranked = [row[2] for row in scored]
    return ranked[:limit] if limit is not None else ranked


def query_variants(query: str, *, max_variants: int = 2) -> list[str]:
    """Queries worth sending to yt-dlp, most promising first.

    The original text is always first (YouTube handles diacritics well); a folded
    variant is appended only when it differs, which helps with misspellings and
    titles that are catalogued without Polish characters.
    """
    raw = str(query or "").strip()
    if not raw:
        return []
    variants = [raw]
    folded = fold(raw)
    if folded and folded != raw.lower() and folded not in variants:
        variants.append(folded)
    return variants[:max_variants]


def dedupe_keep_order(items: Iterable[Any], key) -> list[Any]:
    """Small helper shared by the catalog builders (first entry wins)."""
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out
