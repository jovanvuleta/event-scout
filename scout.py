#!/usr/bin/env python3
"""Event scout: public event pages (schema.org JSON-LD) for the configured cities ->
Claude picks what fits the profile -> one Telegram digest. Sources: Songkick, Meetup. Stdlib only.

Config (profile + cities, see config.example.json) comes from the EVENT_SCOUT_CONFIG environment variable
(a GitHub secret in Actions) or, locally, from config.json, which is git-ignored because it's personal.

  python3 scout.py           fetch, pick, send, remember what was sent
  python3 scout.py --dry     print the digest instead of sending; don't update seen.json
  python3 scout.py --no-ai   skip the Claude step; send a plain list grouped by day

The Claude step runs the Claude Code CLI (`claude -p`) on a subscription: locally it uses the logged-in
CLI, in GitHub Actions the CLAUDE_CODE_OAUTH_TOKEN secret (from `claude setup-token`).
"""
import hashlib, html, json, os, re, subprocess, sys, urllib.parse, urllib.request, concurrent.futures as cf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DRY = '--dry' in sys.argv
NO_AI = '--no-ai' in sys.argv
ENV = getattr(os, 'environ')
CFG = json.loads(ENV.get('EVENT_SCOUT_CONFIG') or open(f'{HERE}/config.json').read())
CI = bool(ENV.get('GITHUB_ACTIONS'))  # public repo: run logs are public, so they must not name cities or events
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
      'Accept-Language': 'en'}
MODEL = 'claude-opus-5-5'

SOURCES = {
    'songkick': lambda v: f'https://www.songkick.com/metro-areas/{v}',
    'meetup': lambda v: f'https://www.meetup.com/find/?location={urllib.parse.quote(v)}&source=EVENTS',
}
ICON = {'songkick': '🎵', 'meetup': '🤝'}

PICK_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['picks'],
               'properties': {'picks': {'type': 'array', 'items': {
                   'type': 'object', 'additionalProperties': False, 'required': ['id', 'why'],
                   'properties': {'id': {'type': 'integer'}, 'why': {'type': 'string'}}}}}}
PICK_SYSTEM = ('You pick events for one person from a numbered list of upcoming events. Choose the ones he would '
               'most likely enjoy given his profile, best first, and give each a short reason (under 15 words: '
               'genre, what he can learn, who he can meet). Skip anything that clashes with his dislikes, and '
               'favour variety over several similar nights. Use only ids from the list.')


def text(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'replace')


def when(v, tz):
    """(start in the city's local time, all-day?). Times without an offset are already local (Songkick);
    Meetup's are UTC. A bare date means an all-day or time-unknown event."""
    try:
        t = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None, False
    allday = len(str(v)) <= 10
    return (t.astimezone(tz) if t.tzinfo else t.replace(tzinfo=tz)), allday


def first(v):
    return v[0] if isinstance(v, list) and v else v


def ld_events(h):
    """Every schema.org *Event object in the page's JSON-LD blocks."""
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get('@type')
            if any(str(x).endswith('Event') for x in (t if isinstance(t, list) else [t])):
                out.append(o)
            for k in ('@graph', 'itemListElement', 'item', 'subEvent'):
                if k in o:
                    walk(o[k])

    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S):
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return out


def price(e):
    o = first(e.get('offers'))
    o = o if isinstance(o, dict) else {}
    if e.get('isAccessibleForFree') is True or str(o.get('price')) in ('0', '0.0', '0.00'):
        return 'free'
    p = o.get('lowPrice') or o.get('price')
    return f"{p} {o.get('priceCurrency', '')}".strip() if p else ''


def clean(v):
    return re.sub(r'\s+', ' ', html.unescape(str(v or ''))).strip()


def fetch(task):
    city, src, val, tz = task
    try:
        events = []
        for e in ld_events(text(SOURCES[src](val))):
            start, allday = when(e.get('startDate'), tz)
            if not start or 'Online' in str(e.get('eventAttendanceMode', '')):
                continue
            loc = first(e.get('location'))
            loc = loc if isinstance(loc, dict) else {'name': loc or ''}
            addr = loc.get('address')
            locality = addr.get('addressLocality', '') if isinstance(addr, dict) else ''
            events.append(dict(
                name=clean(e.get('name')), start=start, allday=allday, url=str(e.get('url', '')).split('?')[0],
                venue=clean(loc.get('name')), place=clean(locality) or city, city=city, price=price(e),
                source=src, about=clean(e.get('description'))[:240]))
        return task, events, None
    except Exception as ex:
        return task, [], f'{type(ex).__name__}: {ex}'[:120]


def pick(events):
    """Claude's picks as [(event, why)], via the Claude Code CLI on the subscription. None on any failure."""
    listing = '\n'.join(
        f"{i}. {e['start']:%a %d %b}{'' if e['allday'] else e['start'].strftime(' %H:%M')} | {e['city']} | "
        f"{e['name']} | {e['venue']} | {e['price'] or 'price n/a'} | {e['source']} | {e['about']}"
        for i, e in enumerate(events))
    prompt = f"Profile: {CFG['profile']}\n\nPick at most {CFG['max_picks']} events.\n\n{listing}"
    r = subprocess.run(
        ['claude', '-p', prompt, '--model', MODEL, '--system-prompt', PICK_SYSTEM,
         '--json-schema', json.dumps(PICK_SCHEMA), '--output-format', 'json', '--tools', '',
         '--setting-sources', '', '--strict-mcp-config', '--no-session-persistence'],
        capture_output=True, text=True, timeout=600, cwd=HERE)
    out = json.loads(r.stdout or '{}')
    if r.returncode or out.get('is_error') or not isinstance(out.get('structured_output'), dict):
        raise RuntimeError(f"claude exit {r.returncode}: {(out.get('result') or r.stderr or '')[:120]}")
    picks = [(events[p['id']], p['why']) for p in out['structured_output'].get('picks', [])
             if isinstance(p.get('id'), int) and 0 <= p['id'] < len(events)]
    return picks[:CFG['max_picks']]


def block(e, why=''):
    """One event in Telegram HTML."""
    esc = html.escape
    t = 'all day' if e['allday'] else f"{e['start']:%H:%M}"
    s = f"{ICON[e['source']]} <b>{t}</b>  <a href=\"{esc(e['url'], quote=True)}\">{esc(e['name'])}</a>"
    where = ', '.join(x for x in (e['venue'], e['place']) if x and x.lower() not in e['name'].lower())
    meta = ' · '.join(x for x in (where, e['price']) if x)
    if meta:
        s += f'\n      📍 {esc(meta)}'
    if why:
        s += f'\n      💡 <i>{esc(why)}</i>'
    return s


def digest(title, items):
    """Telegram messages (HTML): items grouped by day, split between events to stay under Telegram's 4096 limit;
    a day that continues in the next message repeats its heading."""
    msgs, cur, day = [], title, None
    for e, why in sorted(items, key=lambda x: (x[0]['start'].date(), x[0]['allday'] is False, x[0]['start'])):
        b = block(e, why)
        head = f"<b>📅 {e['start']:%A %d %b}</b>"
        piece = b if e['start'].date() == day else f'{head}\n{b}'
        if len(cur) + len(piece) + 2 > 3900:
            msgs.append(cur)
            cur, piece = '', f'{head}\n{b}'
        cur = f'{cur}\n\n{piece}' if cur else piece
        day = e['start'].date()
    return msgs + [cur]


def tg(msgs):
    for m in msgs:
        if DRY:
            print(re.sub(r'<[^>]+>', '', html.unescape(m)) + '\n' + '-' * 40)
            continue
        d = urllib.parse.urlencode({'chat_id': ENV['TELEGRAM_CHAT_ID'], 'text': m, 'parse_mode': 'HTML',
                                    'disable_web_page_preview': 'true'}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{ENV['TELEGRAM_BOT_TOKEN']}/sendMessage", d, timeout=20)


def fingerprint(url):
    """What seen.json stores per sent event: a short hash of its link, so the file doesn't reveal which events."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def explain(err):
    """A readable Telegram line (HTML) for one collected error, with the fix when it's a known one."""
    low = err.lower()
    if err.startswith('claude') and ('not logged in' in low or '401' in low or 'authenticat' in low):
        return ("🔑 Claude isn't logged in: the <code>CLAUDE_CODE_OAUTH_TOKEN</code> secret is missing or expired.\n"
                "     Fix: run <code>claude setup-token</code> and update the secret (or <code>/install-github-app</code>).")
    if err.startswith('claude') and ('limit' in low or '429' in low):
        return '⏳ Claude usage limit reached; picks resume on the next run.'
    if err.startswith('claude'):
        return f'🤖 Claude step failed: {html.escape(err.split(": ", 2)[-1][:150])}'
    where, _, what = err.partition(': ')
    code = re.search(r'HTTP Error (\d{3})', what)
    if code and code.group(1) in ('403', '405', '429'):
        return f'🚫 {html.escape(where)}: blocked the request (HTTP {code.group(1)}).'
    return f'🌐 {html.escape(where)}: {html.escape(what[:120])}'


def main():
    seen_p = f'{HERE}/seen.json'
    seen = set(json.load(open(seen_p))) if os.path.exists(seen_p) else set()
    seen = {fingerprint(x) if x.startswith('http') else x for x in seen}  # older files stored the links themselves
    tasks = [(c['name'], s, c[s], ZoneInfo(c['tz'])) for c in CFG['cities'] for s in SOURCES if c.get(s)]

    events, errs, urls = [], [], set()
    with cf.ThreadPoolExecutor(6) as ex:
        for (city, src, _, tz), found, err in ex.map(fetch, tasks):
            if err:
                errs.append(f'{city}/{src}: {err}')
            now = datetime.now(tz)
            horizon = now + timedelta(days=CFG['days_ahead'])
            for e in found:
                # all-day events count until the end of their day; timed ones until they start
                upcoming = e['start'].date() >= now.date() if e['allday'] else e['start'] >= now
                if upcoming and e['start'] <= horizon and e['url'] and fingerprint(e['url']) not in seen and e['url'] not in urls:
                    urls.add(e['url'])
                    events.append(e)
    events.sort(key=lambda e: e['start'])

    picks = None
    if events and not NO_AI:
        try:
            picks = pick(events)
        except Exception as ex:
            errs.append(f'claude: {type(ex).__name__}: {ex}'[:200])
    cities = ' + '.join(c['name'] for c in CFG['cities'])
    if picks:
        sent = [e for e, _ in picks]
        tg(digest(f"🗓 <b>{html.escape(cities)}</b> · next {CFG['days_ahead']} days\n"
                  f"<i>{len(picks)} picks from {len(events)} events</i>", picks))
    elif events:
        # ponytail: a fallback list isn't remembered (unless --no-ai), so the next run with Claude picks from it again
        sent = events[:25] if NO_AI else []
        shown = events[:25]
        note = ' (Claude unavailable, plain list)' if not NO_AI else ''
        tg(digest(f"🗓 <b>{html.escape(cities)}</b> · next {CFG['days_ahead']} days\n"
                  f"<i>{len(shown)} of {len(events)} events{note}</i>", [(e, '') for e in shown]))
    else:
        sent = []
        tg([f"🗓 event scout OK: {len(tasks) - len([e for e in errs if not e.startswith('claude')])}/{len(tasks)} "
            f"sources, nothing new in the next {CFG['days_ahead']} days"])
    fetch_errs = [e for e in errs if not e.startswith('claude')]
    if len(fetch_errs) > len(tasks) // 3 or len(fetch_errs) < len(errs):
        head = '⚠️ <b>Event scout:</b> ' + ('sent a plain list instead of picks' if picks is None and events and not NO_AI
                                             else 'some sources failed')
        tg([head + '\n\n' + '\n'.join(explain(e) for e in errs[:8])])
    if not DRY:
        seen.update(fingerprint(e['url']) for e in sent)
        json.dump(sorted(seen), open(seen_p, 'w'), indent=0)
    print(f'sources ok {len(tasks) - len(fetch_errs)}/{len(tasks)}, events {len(events)}, sent {len(sent)}, '
          f'errors {len(errs)} (details go to Telegram)')
    for e in [] if CI else errs:
        print('ERR', e)


if __name__ == '__main__':
    main()
