FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHIVE_SCOUT_PROJECTS_ROOT=/data/projects

WORKDIR /app

RUN addgroup --system scout && adduser --system --ingroup scout scout

COPY pyproject.toml README.md LICENSE ./
COPY archive_scout ./archive_scout
RUN python -m pip install --no-cache-dir ".[discord]"

RUN mkdir -p /data/projects && chown -R scout:scout /data /app
USER scout

VOLUME ["/data"]
CMD ["archive-scout-discord"]
