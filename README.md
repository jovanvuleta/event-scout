# event-scout

Twice a week (Mon and Thu evening), collects upcoming events for your cities,
lets Claude pick the ones that fit the profile, and sends one Telegram digest.

- **Sources:** Songkick (concerts) and Meetup (meetups, socializing, learning). Both publish schema.org `Event`
  JSON-LD, so no fragile HTML parsing. (Eventbrite was dropped: it blocks GitHub Actions with HTTP 405.)
- **Picking:** Claude Opus 5.5 through the Claude Code CLI (`claude -p`, structured JSON output) on the Claude
  subscription, no API key. If the step fails (or with `--no-ai`), it sends a plain list grouped by day instead.
- **Message:** Telegram HTML, grouped by day, local times per city, venue, price and the reason for each pick.
- **Memory:** `seen.json` holds short hashes of the links already sent, so nothing repeats and the file doesn't reveal which events.

## Configure

Profile and cities are personal, so the real config isn't in the repo. Copy `config.example.json` to `config.json`
(git-ignored), fill it in, and store it as the `EVENT_SCOUT_CONFIG` secret for GitHub Actions:

```bash
cp config.example.json config.json   # edit: profile, cities (name, tz, songkick metro id, meetup location)
gh secret set EVENT_SCOUT_CONFIG < config.json
```

Run the second line again after every change. Run logs only show counts, never cities or events, since a public
repo's logs are public; error details go to Telegram.

## Run locally

```bash
python3 scout.py --dry --no-ai   # print the plain list, send nothing
python3 scout.py --dry           # with Claude's picks (uses the logged-in `claude` CLI)
```

## Secrets (GitHub Actions)

`EVENT_SCOUT_CONFIG`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token` once; it's tied to
the Claude subscription).
