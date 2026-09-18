from __future__ import annotations

import bisect
import re
from collections import Counter
from itertools import combinations

from ..constants import ARCHIVE_EXTENSIONS, MEDIA_EXTENSIONS
from ..content import safe_urlsplit
from ..utils import normalize_search
from .keywords import CompiledRule, KeywordPrefilter, keyword_url_match
from .snippets import make_snippets

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+")
PARAGRAPH_SPLIT = re.compile(r"(?:\r?\n){2,}|</p\s*>|<br\s*/?>\s*<br\s*/?>", re.IGNORECASE)
WORD_PATTERN = re.compile(r"\S+")


def link_is_interesting(
    link: str,
    patterns: list[CompiledRule],
    prefilter: KeywordPrefilter | None = None,
) -> bool:
    parsed = safe_urlsplit(link)
    if parsed:
        filename = parsed.path.rsplit("/", 1)[-1]
        dot = filename.rfind(".")
        extension = filename[dot:].lower() if dot > 0 else ""
    else:
        extension = ""
    if extension in MEDIA_EXTENSIONS or extension in ARCHIVE_EXTENSIONS:
        return True
    if prefilter is not None:
        if not prefilter.has_positive_rules:
            return False
        normalized = normalize_search(link)
        return prefilter.matches({"url": link}, {"url": normalized})
    return keyword_url_match(link, patterns)


def _matches(item: CompiledRule, value: str, normalized_value: str):
    """Yield regex matches without materializing a per-rule list."""
    haystack = value if item.rule.case_sensitive else normalized_value
    return item.pattern.finditer(haystack)


def _match_count(item: CompiledRule, value: str, normalized_value: str) -> int:
    return sum(1 for _ in _matches(item, value, normalized_value))


def _non_overlapping_count(spans: list[tuple[int, int]]) -> int:
    """Match Python regex finditer semantics for one literal pattern."""
    count = 0
    last_end = -1
    for start, end in sorted(spans):
        if start >= last_end:
            count += 1
            last_end = end
    return count


def _compact_markup_text(raw: str) -> str:
    # Preserve script/comment/noscript contents while removing only markup.
    # This catches a phrase split by tags without replacing the raw-source view.
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]*>", " ", raw or "")).strip()


def _matched_labels_in_segments(text: str, patterns: list[CompiledRule], splitter: re.Pattern[str]) -> int:
    bonus = 0
    for segment in splitter.split(text):
        if not segment.strip():
            continue
        normalized_segment = normalize_search(segment)
        labels = {
            item.rule.label
            for item in patterns
            if item.rule.kind != "excluded" and item.pattern.search(segment if item.rule.case_sensitive else normalized_segment)
        }
        if len(labels) >= 2:
            bonus += len(labels) * (len(labels) - 1)
    return bonus


def _proximity_bonus(text: str, patterns: list[CompiledRule], window_words: int = 25) -> tuple[int, dict]:
    normalized = normalize_search(text)
    word_spans = list(WORD_PATTERN.finditer(normalized))
    if not word_spans:
        return 0, {"window_words": window_words, "pairs": 0}
    positions: dict[str, list[int]] = {}
    starts = [span.start() for span in word_spans]
    for item in patterns:
        if item.rule.kind == "excluded":
            continue
        bucket = positions.setdefault(item.rule.label, [])
        for match in item.pattern.finditer(normalized):
            bucket.append(max(0, bisect.bisect_right(starts, match.start()) - 1))
    close_pairs = 0
    minimum_distance: int | None = None
    for left, right in combinations(positions, 2):
        left_positions = positions[left]
        right_positions = positions[right]
        i = j = 0
        distance = len(word_spans)
        # Both lists are generated in document order. A two-pointer minimum is
        # linear instead of the old Cartesian product for frequent keywords.
        while i < len(left_positions) and j < len(right_positions):
            a, b = left_positions[i], right_positions[j]
            distance = min(distance, abs(a - b))
            if a < b:
                i += 1
            else:
                j += 1
            if distance == 0:
                break
        minimum_distance = distance if minimum_distance is None else min(minimum_distance, distance)
        if distance <= window_words:
            close_pairs += 1
    return close_pairs * 6, {
        "window_words": window_words,
        "pairs": close_pairs,
        "minimum_distance": minimum_distance,
    }


def prepare_analysis_fields(
    original: str,
    title: str,
    visible: str,
    raw: str,
    links: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    fields = {
        "url": original,
        "title": title,
        "body": visible,
        # v1.0.5 truncated source at 500,000 characters.  Maximum-recall scans
        # must search the complete archived response.
        "source": raw,
        "compact": _compact_markup_text(raw),
        "links": "\n".join(links),
    }
    return fields, {name: normalize_search(value) for name, value in fields.items()}

def analyze_content(
    original: str,
    title: str,
    visible: str,
    raw: str,
    links: list[str],
    patterns: list[CompiledRule],
    prefilter: KeywordPrefilter | None = None,
    prepared_fields: dict[str, str] | None = None,
    prepared_normalized_fields: dict[str, str] | None = None,
    *,
    include_hit_fields: bool = True,
    include_snippets: bool = True,
    include_interesting_links: bool = True,
) -> dict:
    if prepared_fields is None or prepared_normalized_fields is None:
        fields, normalized_fields = prepare_analysis_fields(original, title, visible, raw, links)
    else:
        fields = prepared_fields
        normalized_fields = prepared_normalized_fields
    multipliers = {"url": 6.0, "title": 5.0, "body": 1.0, "source": 0.75, "compact": 0.9, "links": 2.5}
    hits: Counter[str] = Counter()
    hit_fields: dict[str, set[str]] = {}
    score = 0.0
    matched_rules: dict[str, CompiledRule] = {}
    excluded_labels: set[str] = set()
    required_labels = {item.rule.label for item in patterns if item.rule.kind == "required"}

    # Ordinary normalized literals are counted directly from one Aho-Corasick
    # traversal per field.  Regex/case-sensitive/whole-word rules retain the
    # exact legacy regex path.
    literal_map = prefilter.literal_rules if prefilter is not None else {}
    literal_auto = prefilter.candidate_automaton if prefilter is not None else None
    slow_patterns = prefilter.slow_patterns if prefilter is not None else patterns
    positive_found = not any(item.rule.kind != "excluded" for item in patterns)

    for field_name, value in fields.items():
        normalized_value = normalized_fields[field_name]
        if literal_auto is not None:
            for expression, count in literal_auto.count_non_overlapping(normalized_value).items():
                for item in literal_map.get(expression, ()):
                    label = item.rule.label
                    hits[label] += count
                    if include_hit_fields:
                        hit_fields.setdefault(label, set()).add(field_name)
                    matched_rules[label] = item
                    if item.rule.kind == "excluded":
                        excluded_labels.add(label)
                        continue
                    positive_found = True
                    exact_bonus = 2.0 if item.rule.kind == "exact" else 1.0
                    score += min(count, 10) * multipliers[field_name] * item.rule.weight * exact_bonus
        for item in slow_patterns:
            count = _match_count(item, value, normalized_value)
            if not count:
                continue
            label = item.rule.label
            hits[label] += count
            if include_hit_fields:
                hit_fields.setdefault(label, set()).add(field_name)
            matched_rules[label] = item
            if item.rule.kind == "excluded":
                excluded_labels.add(label)
                continue
            positive_found = True
            exact_bonus = 2.0 if item.rule.kind == "exact" else 1.0
            score += min(count, 10) * multipliers[field_name] * item.rule.weight * exact_bonus

    if not positive_found and any(item.rule.kind != "excluded" for item in patterns):
        return {
            "score": 0, "hits": dict(sorted(hits.items())),
            "hit_fields": {key: sorted(value) for key, value in hit_fields.items()},
            "snippets": [], "interesting_links": [],
            "excluded": bool(excluded_labels), "excluded_labels": sorted(excluded_labels),
            "required_missing": bool(required_labels),
            "missing_required_labels": sorted(required_labels),
            "proximity": {"window_words": 25, "pairs": 0, "minimum_distance": None, "sentence_bonus": 0, "paragraph_bonus": 0, "score_bonus": 0},
        }

    missing_required = sorted(required_labels - set(hits))
    excluded = bool(excluded_labels)
    distinct = len([label for label in hits if label not in excluded_labels])
    if distinct >= 2:
        score += distinct * 3

    positive_matched_rules = [item for item in matched_rules.values() if item.rule.kind != "excluded"]
    # Sentence, paragraph, and word-distance bonuses require at least two
    # positive labels by definition. Most archive hits contain only one keyword;
    # skipping three whole-document passes in that common case is a substantial
    # scan-speed win with identical scores.
    if len(positive_matched_rules) >= 2:
        sentence_bonus = _matched_labels_in_segments(visible, positive_matched_rules, SENTENCE_SPLIT) * 4
        paragraph_bonus = _matched_labels_in_segments(raw, positive_matched_rules, PARAGRAPH_SPLIT) * 2
        proximity_bonus, proximity = _proximity_bonus(visible, positive_matched_rules)
    else:
        sentence_bonus = paragraph_bonus = proximity_bonus = 0
        proximity = {"window_words": 25, "pairs": 0, "minimum_distance": None}
    score += sentence_bonus + paragraph_bonus + proximity_bonus
    proximity.update({"sentence_bonus": sentence_bonus, "paragraph_bonus": paragraph_bonus, "score_bonus": proximity_bonus})

    if excluded or missing_required:
        score = 0
    interesting_links = (
        sorted({link for link in links if link_is_interesting(link, patterns, prefilter)})
        if include_interesting_links
        else []
    )
    snippets = (
        make_snippets(visible or raw, positive_matched_rules)
        if include_snippets and positive_matched_rules
        else []
    )
    return {
        "score": int(round(score)),
        "hits": dict(sorted(hits.items())),
        "hit_fields": {key: sorted(value) for key, value in hit_fields.items()},
        "snippets": snippets,
        "interesting_links": interesting_links,
        "excluded": excluded,
        "excluded_labels": sorted(excluded_labels),
        "required_missing": bool(missing_required),
        "missing_required_labels": missing_required,
        "proximity": proximity,
    }
