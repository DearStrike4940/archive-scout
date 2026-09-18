from __future__ import annotations

VERSION = "1.0.7.1"
SCHEMA_VERSION = 8
APP_NAME = "Archive Scout"
CDX_URL = "https://web.archive.org/cdx/search/cdx"
CDX_TIMEMAP_URL = "https://web.archive.org/web/timemap/cdx"
CDX_TIMEMAP_JSON_URL = "https://web.archive.org/web/timemap/json"
CDX_ENDPOINTS = (CDX_URL, CDX_TIMEMAP_URL, CDX_TIMEMAP_JSON_URL)
REPLAY_URL = "https://web.archive.org/web"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
TEXT_EXTENSIONS = {
    ".asp", ".aspx", ".cfm", ".cgi", ".css", ".dat", ".dhtm", ".htm", ".html", ".inc",
    ".js", ".json", ".jsp", ".dhtml", ".phtml", ".phtm", ".php", ".php3", ".php4", ".php5", ".shtm", ".shtml", ".text", ".txt",
    ".xhtm", ".xhtml", ".xml"
}
IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jfif", ".jpeg",
    ".jpg", ".jxl", ".png", ".svg", ".tif", ".tiff", ".webp"
}
VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".f4v", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpe", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv",
    ".qt", ".rm", ".rmvb", ".swf", ".ts", ".vob", ".webm", ".wmv"
}
AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".flac", ".m4a", ".mid", ".mp3", ".oga", ".ogg", ".wav", ".wma"
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
BINARY_EXTENSIONS = MEDIA_EXTENSIONS | AUDIO_EXTENSIONS | {
    ".7z", ".ace", ".bin", ".bz2", ".cab", ".class", ".dmg", ".doc", ".docx",
    ".exe", ".gz", ".iso", ".jar", ".pdf", ".ppt", ".pptx", ".rar", ".tar",
    ".torrent", ".xls", ".xlsx", ".zip"
}
ARCHIVE_EXTENSIONS = {".7z", ".ace", ".cab", ".gz", ".rar", ".tar", ".tgz", ".zip"}
DEFAULT_IMAGE_EXTENSIONS = sorted(IMAGE_EXTENSIONS - {".ico", ".heic", ".heif", ".jxl"})
DEFAULT_VIDEO_EXTENSIONS = sorted(VIDEO_EXTENSIONS)
REVIEW_STATUSES = (
    "unreviewed", "relevant", "possibly_relevant", "false_positive", "duplicate", "dead_end", "needs_follow_up"
)
OPERATION_MODES = {
    "Index, download, scan, and report": "all",
    "Index and download only (no scanning)": "download_only",
    "Index, download, scan, then download external embedded media": "external_media_after_scan",
    "Index URLs only": "index",
    "Download and scan pending URLs": "download",
    "Resume interrupted work": "resume",
    "Rescan existing downloads with selected keyword sets": "rescan",
    "Retry only errored URLs": "retry_errors",
    "Regenerate reports only": "report",
    "Check project integrity": "integrity",
    "Repair project and rebuild indexes": "repair",
    "Create project backup": "backup",
    "Export diagnostic package": "diagnostics",
    "Import an existing archive folder": "import_folder",
    "Index and download selected media": "media_all",
    "Index media URLs only": "media_index",
    "Download pending media": "media_download",
    "Retry only errored media": "media_retry",
    "Run archive recovery and analysis": "analysis",
    "Build or refresh Research Intelligence": "research_index",
    "Rebuild forum threads only": "forum_rebuild",
    "Merge another Archive Scout project": "merge_project",
    "Search with Hitlist": "hitlist",
    "Compact project storage": "compact",
}
SCOPE_LABELS = {
    "All archived text pages (thorough)": "all_text",
    "Only URLs containing a keyword (fast)": "keyword_urls",
    "Index only; download nothing": "index_only",
}
