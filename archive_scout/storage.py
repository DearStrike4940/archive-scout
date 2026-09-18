from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

_PORTABLE_ILLEGAL = set('<>:"/\\|?*')


def _escape_component(value: str) -> str:
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if ch in _PORTABLE_ILLEGAL or code < 32:
            for byte in ch.encode('utf-8', 'surrogatepass'):
                out.append(f'%{byte:02X}')
        else:
            out.append(ch)
    return ''.join(out).rstrip(' .')


def url_filename(original_url: str, fallback: str = 'capture', max_utf8_bytes: int = 235) -> str:
    """Return a deterministic, recognizable filename derived from the full URL.

    Existing percent escapes are preserved. Only characters that cannot be one
    portable filename component are percent-escaped. If the complete URL cannot
    fit a conservative cross-platform component length, the readable prefix is
    retained and an 12-hex SHA-256 suffix disambiguates the shortened name.
    """
    raw = str(original_url or '').strip() or fallback
    name = _escape_component(raw) or fallback
    encoded = name.encode('utf-8', 'surrogatepass')
    if len(encoded) <= max_utf8_bytes:
        return name
    digest = hashlib.sha256(raw.encode('utf-8', 'surrogatepass')).hexdigest()[:12]
    suffix = '~' + digest
    budget = max(24, max_utf8_bytes - len(suffix.encode('ascii')))
    prefix = encoded[:budget]
    while prefix:
        try:
            readable = prefix.decode('utf-8', 'strict')
            break
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    else:
        readable = fallback
    return readable.rstrip(' .') + suffix


def with_timestamp_disambiguator(filename: str, timestamp: str) -> str:
    stamp = ''.join(ch for ch in str(timestamp or '') if ch.isdigit())[:14] or 'capture'
    path = Path(filename)
    if path.suffix and len(path.suffix) <= 12:
        return f'{path.stem}~{stamp}{path.suffix}'
    return f'{filename}~{stamp}'


def capture_path(root: Path, timestamp: str, original_url: str, *, disambiguate: bool = False) -> Path:
    # Keep the original URL recognizable while making the research corpus safe
    # to open in text tools. Bytes are not decoded/re-encoded or stripped here.
    # Reserve space for .txt, a timestamp and the temporary .part suffix.
    filename = url_filename(original_url, 'capture', max_utf8_bytes=225)
    if not filename.casefold().endswith('.txt'):
        filename += '.txt'
    if disambiguate:
        filename = with_timestamp_disambiguator(filename, timestamp)
    year = str(timestamp or '')[:4] if len(str(timestamp or '')) >= 4 else 'unknown'
    month = str(timestamp or '')[4:6] if len(str(timestamp or '')) >= 6 else '00'
    return Path(root) / 'captures' / year / month / filename


def media_path(root: Path, kind: str, original_url: str, timestamp: str = '', *, disambiguate: bool = False) -> Path:
    folder = 'images' if str(kind).casefold() == 'image' else 'videos'
    filename = url_filename(original_url, 'media')
    if disambiguate:
        filename = with_timestamp_disambiguator(filename, timestamp)
    return Path(root) / 'media' / folder / filename


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _clone_file(source: Path, destination: Path) -> bool:
    """Best-effort copy-on-write clone. Never hard-link mutable user files."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == 'darwin':
            subprocess.run(['cp', '-c', str(source), str(destination)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if sys.platform.startswith('linux'):
            subprocess.run(['cp', '--reflink=always', '--preserve=mode,timestamps', str(source), str(destination)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except (OSError, subprocess.SubprocessError):
        destination.unlink(missing_ok=True)
    return False


def deduplicate_exact_file(destination: Path, canonical: Path) -> str:
    """Replace *destination* with a safe CoW clone of identical *canonical* when possible.

    Returns ``clone`` when physical blocks can be shared, ``copy`` when a normal
    copy was necessary, and ``same`` if both paths already identify the same file.
    The fallback deliberately avoids hard links so a user editing one URL-named
    capture can never mutate another historical capture.
    """
    destination = Path(destination)
    canonical = Path(canonical)
    if not destination.exists() or not canonical.exists():
        return 'copy'
    try:
        if os.path.samefile(destination, canonical):
            return 'same'
    except OSError:
        pass
    temp = destination.with_name(destination.name + '.dedupe')
    temp.unlink(missing_ok=True)
    if _clone_file(canonical, temp):
        os.replace(temp, destination)
        return 'clone'
    temp.unlink(missing_ok=True)
    return 'copy'


def clone_or_copy(source: Path, destination: Path) -> str:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + '.copying')
    temp.unlink(missing_ok=True)
    if _clone_file(source, temp):
        os.replace(temp, destination)
        return 'clone'
    shutil.copy2(source, temp)
    os.replace(temp, destination)
    return 'copy'
