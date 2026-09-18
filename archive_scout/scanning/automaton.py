from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

try:
    import ahocorasick_rs
except Exception:  # pragma: no cover - source checkouts can run without optional wheel
    ahocorasick_rs = None


@dataclass(slots=True)
class _Node:
    transitions: dict[str, int] = field(default_factory=dict)
    failure: int = 0
    outputs: tuple[str, ...] = ()


class LiteralAutomaton:
    """Compact Aho-Corasick matcher for normalized literal keyword rules.

    Archive Scout keeps the full regular-expression matcher for rules that need
    case sensitivity, whole-word boundaries, or regex semantics. This matcher is
    only the fast gate for ordinary normalized literals, so one pass over a field
    replaces an increasingly expensive giant alternation regex.
    """

    __slots__ = ("_nodes", "_native", "patterns")

    def __init__(self, patterns: Iterable[str]) -> None:
        unique = tuple(dict.fromkeys(value for value in patterns if value))
        self.patterns = unique
        self._native = None
        if unique and ahocorasick_rs is not None:
            # Rust-backed matching releases the GIL for strings, allowing the
            # download/scanning worker pool to keep multiple pages moving.
            self._native = ahocorasick_rs.AhoCorasick(
                unique, implementation=ahocorasick_rs.Implementation.DFA, store_patterns=True
            )
        self._nodes: list[_Node] = [_Node()]
        if self._native is not None:
            return
        for pattern in unique:
            state = 0
            for character in pattern:
                next_state = self._nodes[state].transitions.get(character)
                if next_state is None:
                    next_state = len(self._nodes)
                    self._nodes[state].transitions[character] = next_state
                    self._nodes.append(_Node())
                state = next_state
            self._nodes[state].outputs = (*self._nodes[state].outputs, pattern)
        self._build_failures()

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def _build_failures(self) -> None:
        queue: deque[int] = deque()
        for state in self._nodes[0].transitions.values():
            self._nodes[state].failure = 0
            queue.append(state)
        while queue:
            state = queue.popleft()
            for character, next_state in self._nodes[state].transitions.items():
                queue.append(next_state)
                failure = self._nodes[state].failure
                while failure and character not in self._nodes[failure].transitions:
                    failure = self._nodes[failure].failure
                fallback = self._nodes[failure].transitions.get(character, 0)
                self._nodes[next_state].failure = fallback
                inherited = self._nodes[fallback].outputs
                if inherited:
                    self._nodes[next_state].outputs = tuple(
                        dict.fromkeys((*self._nodes[next_state].outputs, *inherited))
                    )

    def search_any(self, text: str) -> bool:
        if not text or not self.patterns:
            return False
        if self._native is not None:
            return bool(self._native.find_matches_as_indexes(text))
        state = 0
        nodes = self._nodes
        for character in text:
            while state and character not in nodes[state].transitions:
                state = nodes[state].failure
            state = nodes[state].transitions.get(character, 0)
            if nodes[state].outputs:
                return True
        return False

    def find_into(self, text: str, matches: set[str]) -> None:
        """Add matches to an existing set without allocating one set per field."""
        if not text or not self.patterns:
            return
        if self._native is not None:
            for pattern_index, _start, _end in self._native.find_matches_as_indexes(text, overlapping=True):
                matches.add(self.patterns[pattern_index])
            return
        state = 0
        nodes = self._nodes
        for character in text:
            while state and character not in nodes[state].transitions:
                state = nodes[state].failure
            state = nodes[state].transitions.get(character, 0)
            if nodes[state].outputs:
                matches.update(nodes[state].outputs)


    def find_matches(self, text: str, *, overlapping: bool = True) -> list[tuple[str, int, int]]:
        """Return literal, start, end matches for scoring/snippets.

        Native Aho returns every overlapping match.  The Python fallback emits
        the same representation so callers can implement regex-equivalent
        per-pattern non-overlapping counts without a second document scan.
        """
        if not text or not self.patterns:
            return []
        if self._native is not None:
            return [
                (self.patterns[index], int(start), int(end))
                for index, start, end in self._native.find_matches_as_indexes(text, overlapping=overlapping)
            ]
        found: list[tuple[str, int, int]] = []
        state = 0
        nodes = self._nodes
        for offset, character in enumerate(text):
            while state and character not in nodes[state].transitions:
                state = nodes[state].failure
            state = nodes[state].transitions.get(character, 0)
            for pattern in nodes[state].outputs:
                end = offset + 1
                found.append((pattern, end - len(pattern), end))
        if overlapping:
            return found
        accepted: list[tuple[str, int, int]] = []
        last_end = -1
        for item in sorted(found, key=lambda value: (value[1], value[2])):
            if item[1] >= last_end:
                accepted.append(item)
                last_end = item[2]
        return accepted

    def find(self, text: str) -> set[str]:
        matches: set[str] = set()
        self.find_into(text, matches)
        return matches

    def count_non_overlapping(self, text: str, chunk_size: int = 65536) -> dict[str, int]:
        """Count per-pattern matches with bounded native-result allocation.

        Carry enough source overlap to retain phrases at chunk boundaries. Each
        match is owned by the chunk containing its end; per-pattern end offsets
        give the same counts as literal regex finditer without sorting/storing
        all occurrences in a multi-megabyte capture.
        """
        if not text or not self.patterns:
            return {}
        overlap = max(map(len, self.patterns)) - 1
        width = max(1, int(chunk_size), overlap + 1)
        counts: dict[str, int] = {}
        last_end: dict[str, int] = {}
        for boundary in range(0, len(text), width):
            start = max(0, boundary - overlap)
            for pattern, left, right in self.find_matches(text[start:boundary + width]):
                left += start
                right += start
                if right <= boundary or left < last_end.get(pattern, -1):
                    continue
                counts[pattern] = counts.get(pattern, 0) + 1
                last_end[pattern] = right
        return counts
