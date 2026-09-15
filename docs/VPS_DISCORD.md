# VPS and Discord deployment

This deployment can run several independent Archive Scout projects at once. It never runs two
operations against the same project database concurrently. Project databases, downloaded captures,
and reports live in the Docker volume `archive-scout-data` and survive image rebuilds and container
restarts.
Interrupted Archive Scout queues remain resumable with `/scout run project:NAME mode:resume`.

## 1. Create the Discord application

1. Open the Discord Developer Portal and create an application.
2. Open **Bot**, create/reset the token, and copy it into `.env` on the VPS.
3. In **OAuth2 > URL Generator**, select `bot` and `applications.commands`.
4. Grant only **View Channels**, **Send Messages**, and **Attach Files** in the bot's channel.
5. Use the generated URL to install the bot in your server.
6. Enable Discord Developer Mode and copy the server, user, and/or operator-role IDs.

The bot does not need Message Content Intent or administrator permission.

## 2. Prepare an Ubuntu VPS

Install Docker Engine and its Compose plugin using Docker's instructions for your Ubuntu release.
Then clone your fork and create its private configuration:

```bash
git clone https://github.com/CocoaCockatiel/archive-scout-VPS.git
cd archive-scout-VPS
cp .env.example .env
nano .env
chmod 600 .env
```

Set `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, and either `DISCORD_ALLOWED_USER_IDS` or
`DISCORD_ALLOWED_ROLE_IDS`. Set `ARCHIVE_SCOUT_MAX_CONCURRENT_JOBS` to the desired concurrency
limit. Start at `3` for an 8-core, 24 GB VPS; Wayback throttling is usually the limiting resource.
Never commit `.env`.

## 3. Start it 24/7

```bash
docker compose up -d --build
docker compose logs -f archive-scout-bot
```

The Compose restart policy starts the bot again after a crash or VPS reboot, provided Docker is
enabled at boot. Verify that with `sudo systemctl enable --now docker`.

Updates are applied with:

```bash
git pull --ff-only
docker compose up -d --build
```

Back up the `archive-scout-data` Docker volume; it contains all durable project state and
downloaded material. For example:

```bash
docker compose exec -T archive-scout-bot tar -C /data -czf - projects > archive-scout-backup.tar.gz
```

## Commands

- `/scout help` shows an in-Discord quick-start guide, including how to run several searches at once.
- `/scout create` creates a project with one or more targets and comma-separated keyword rules.
  Put extra targets in `additional_targets`, separated by semicolons.
- `/scout projects` lists available projects.
- `/scout run` starts an operation such as `all`, `resume`, `report`, or `integrity`.
- `/scout status` shows all active jobs, or one named project's latest progress.
- `/scout matches` shows the newest qualifying matches from a project's current scan without
  pausing or modifying the running job. Set `limit` from 1 to 10.
- `/scout stop` safely stops a named project after its current network operation.
- `/scout reports` lists generated report files.
- `/scout get-report` defaults to `all_matches_ranked.md`; project and report fields provide
  autocomplete, so downloading the readable combined report only requires choosing a project.
  Choose `all_matches_ranked.csv` for Excel or Google Sheets. The bot backfills these reports for
  older projects and ZIP-compresses them automatically if they exceed Discord's upload limit.
- `all_matches_ranked.md`, `all_matches_ranked.csv`, and the compatibility
  `all_matches_ranked.txt` combine every qualifying match across original, interrupted, and
  resumed scan runs. Per-scan `matches_ranked.txt` files remain available.

Long jobs announce completion with a normal channel message rather than relying on the temporary
slash-command interaction token.

## Operational notes

- Run the bot in a private channel when project targets or results are sensitive.
- Concurrent operation is across separate projects. Create a separate project for each independent
  search, then invoke `/scout run` for each one.
- Archive Scout respects Wayback exclusions and rate limits; do not increase concurrency simply to
  work around archive restrictions.
- `import_folder` and `merge_project` are intentionally unavailable from Discord because they
  accept server filesystem paths. Run those from an authenticated VPS shell when needed.
- A container restart safely interrupts the current process, but it does not automatically restart
  the operation. Use the project's `resume` mode after the bot reconnects.
