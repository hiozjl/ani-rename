"""Text normalization, romaji conversion, and fuzzy matching utilities."""

from __future__ import annotations

import difflib
import re

# ── Junk patterns for clean_text ─────────────────────────────────────
JUNK_PATTERNS = [
    r"\[[^\]]+\]",
    r"\([^)]*\)",
    r"（[^）]*）",
    r"\b(?:1080p|720p|2160p|x264|x265|hevc|aac|flac|web[- ]?dl|bd|bdrip)\b",
]

# ── Kana → Romaji (Hepburn) ──────────────────────────────────────────
_KANA_TO_ROMAJI: dict[str, str] = {
    # Hiragana basic
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "wo", "ん": "n",
    # Hiragana small (yōon)
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "っ": "",
    # Katakana basic
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヲ": "wo", "ン": "n",
    # Katakana small (yōon)
    "ャ": "ya", "ュ": "yu", "ョ": "yo", "ッ": "",
    # Chōonpu
    "ー": "",
    # Punctuation
    "・": " ", "？": "?", "！": "!", "〜": "~", "～": "~",
}

# Yōon: (full romaji, short romaji for palatal consonants)
_YOON_MAP: dict[str, tuple[str, str]] = {
    "ゃ": ("ya", "a"), "ゅ": ("yu", "u"), "ょ": ("yo", "o"),
    "ャ": ("ya", "a"), "ュ": ("yu", "u"), "ョ": ("yo", "o"),
}
# Palatal consonant stems drop the 'y' in yōon: sh+ya→sha, ch+yu→chu, j+yo→jo
_PALATAL_STEMS: set[str] = {"sh", "ch", "j"}


def _is_kana(ch: str) -> bool:
    return ch in _KANA_TO_ROMAJI


def to_romaji(text: str) -> str:
    """Convert Japanese kana to Hepburn romaji. Non-kana characters pass through."""
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        # Yōon (small ya/yu/yo): merge with previous character
        yoon_pair = _YOON_MAP.get(ch)
        if yoon_pair and result:
            prev = result[-1]
            yoon_full, yoon_short = yoon_pair
            if len(prev) >= 2 and prev[-1] in "aiueo":
                stem = prev[:-1]
                if stem in _PALATAL_STEMS:
                    result[-1] = stem + yoon_short
                else:
                    result[-1] = stem + yoon_full
            else:
                result[-1] = prev + yoon_full
            i += 1
            continue

        # Sokuon (っ/ッ): double the next consonant
        if ch in ("っ", "ッ"):
            if i + 1 < n:
                next_ch = text[i + 1]
                next_roma = _KANA_TO_ROMAJI.get(next_ch, "")
                if next_roma and len(next_roma) >= 2 and next_roma[0] not in "aiueon":
                    result.append(next_roma[0])
            i += 1
            continue

        # Chōonpu (ー): double previous vowel
        if ch == "ー":
            if result:
                prev = result[-1]
                if prev and prev[-1] in "aiueo":
                    result.append(prev[-1])
            i += 1
            continue

        roma = _KANA_TO_ROMAJI.get(ch)
        if roma is not None:
            result.append(roma)
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def levenshtein_ratio(s1: str, s2: str) -> float:
    """Normalized Levenshtein similarity in [0, 1]."""
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    len1, len2 = len(s1), len(s2)
    prev = list(range(len2 + 1))
    curr = [0] * (len2 + 1)
    for i in range(1, len1 + 1):
        curr[0] = i
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    distance = prev[len2]
    return 1.0 - distance / max(len1, len2)


def fuzzy_match_score(left: str, right: str) -> float:
    """Hybrid text similarity: max of SequenceMatcher, Levenshtein, and substring bonus."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    seq_ratio = difflib.SequenceMatcher(None, left, right).ratio()
    lev_ratio = levenshtein_ratio(left, right)
    sub_bonus = 0.0
    if left in right:
        sub_bonus = min(len(left), len(right)) / max(len(left), len(right))
    elif right in left:
        sub_bonus = min(len(left), len(right)) / max(len(left), len(right))
    return max(seq_ratio, lev_ratio, sub_bonus)


def extract_episode_token(name: str):
    return re.search(r"(S\d{2}E\d{2}(?:\.5)?)", name, re.IGNORECASE)


def clean_text(value: str) -> str:
    value = value.strip()
    for pat in JUNK_PATTERNS:
        value = re.sub(pat, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -._")
    return value


def strip_series_and_episode_markers(value: str, series_title: str, episode_index: int) -> str:
    value = clean_text(value)
    candidates = [series_title, normalize_for_match(series_title)]
    for candidate in candidates:
        if candidate:
            value = re.sub(re.escape(candidate), " ", value, flags=re.IGNORECASE)
    patterns = [
        rf"S\d{{1,2}}E{episode_index:02d}(?:\.5)?",
        rf"#\s*{episode_index}\b",
        rf"＃\s*{episode_index}\b",
        rf"第\s*{episode_index}\s*[話话卷集]",
        rf"\b{episode_index}\b",
    ]
    for pat in patterns:
        value = re.sub(pat, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"^[\s._\-–—~]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -._")
    return value


def normalize_for_match(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"[^\w\sア-ヶぁ-ん一-龯ー]", " ", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def _has_kana(text: str) -> bool:
    """Check if text contains any hiragana or katakana."""
    return bool(re.search(r"[぀-ゟ゠-ヿ]", text))
