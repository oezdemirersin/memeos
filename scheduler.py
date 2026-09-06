"""
scheduler.py – Automatik für MemeOS (Phase B5)

Ein Daemon-Thread tickt alle 30 Sekunden und führt Tagesjobs zur eingestellten
Uhrzeit (Europe/Berlin) genau einmal pro Tag aus. Zustand liegt in AppSettings:

    master_pause            '0' | '1'   – hält alle Tagesjobs an (Poll läuft weiter,
                                          er reagiert nur auf Knopfdrücke und kostet nichts)
    auto_<job>_enabled      '0' | '1'   – Schalter je Job (Standardwerte s. JOBS)
    <job>_time              'HH:MM'     – Uhrzeit je Tagesjob
    sched_last_<job>        ISO-Zeit    – letzter Lauf (Berlin, mit Offset)
    sched_result_<job>      Kurztext    – Ergebnis des letzten Laufs
    telegram_poll_offset    int         – getUpdates-Offset
    telegram_token / telegram_chat_id   – kommen aus der Telegram-Card der Einstellungen
    alert_threshold_days                – Warnschwelle "ohne geplanten Post" (Einstellungen)

Jobs: rss, trending, inspo, events, nopost, overdue, weather, digest (täglich)
und poll (alle 30 s).

Regeln:
- app.py wird NIE auf Modulebene importiert (zirkulärer Import). Helfer aus app.py
  werden zur Laufzeit über _appmod() geholt (funktioniert auch bei `python app.py`,
  wo das Modul '__main__' heißt).
- Jeder Netzaufruf hat einen Timeout, jede Job-Ausführung steckt in try/except.
- Der Thread startet nicht bei TESTING oder MEMEOS_SCHEDULER=0. Eine Dateisperre
  in <DATA_ROOT>/scheduler.lock sorgt dafür, dass bei mehreren Gunicorn-Workern nur
  EIN Prozess die Schleife fährt (sonst doppelte Digests und 409 beim Poll).
"""
import os
import re
import sys
import json
import time
import html
import calendar
import logging
import threading
from datetime import datetime, timedelta, date, time as dtime, timezone
from functools import wraps

import requests
from flask import Blueprint, request, jsonify, session, redirect, current_app

from models import (db, City, MemePost, MemeEvent, MemeTemplate, NewsItem,
                    TrendingTopic, AppSettings)

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo('Europe/Berlin')
except Exception:                                   # pragma: no cover – Notnagel ohne tzdata
    BERLIN = timezone(timedelta(hours=1), 'MEZ')

log = logging.getLogger(__name__)
bp = Blueprint('automation', __name__)

TICK_SECONDS = 30
POLL_SUSPEND_SECONDS = 600          # nach HTTP 409 (Webhook aktiv) Poll aussetzen
HEARTBEAT_STALE_SECONDS = 120       # danach gilt der Scheduler als "nicht lebendig"
WATCHDOG_ALARM_SECONDS = 600        # Wächter meldet Stillstand einmal pro Vorfall
WEATHER_EVENT_GAP_DAYS = 3          # pro Regel und Stadt höchstens ein Event alle 3 Tage
FIRST_SNOW_GAP_DAYS = 60
OVERDUE_HOURS = 24                  # ab wann ein geplanter Post als überfällig gilt
OVERDUE_LIST_MAX = 20               # so viele Posts stehen einzeln in der Telegram-Meldung
INSPO_SORT_DEFAULT = 20             # KI-Deckel je Lauf (AppSettings inspo_sort_limit)
INSPO_SORT_MAX = 200
EVENT_RENOTIFY_HOURS = 20
TELEGRAM_CAPTION_MAX = 1000
TRENDING_MODEL = 'claude-haiku-4-5-20251001'   # wie /api/trending/refresh in app.py
_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_UNSET = object()

# ── Job-Register ───────────────────────────────────────────────────────────────
JOBS = [
    {'key': 'rss',      'label': 'RSS-Feeds holen',
     'description': 'Holt die Feeds aller aktiven Städte mit RSS-URL, neue Artikel landen im News-Radar. KI-Bewertung nur, wenn ANTHROPIC_API_KEY gesetzt ist.',
     'default_enabled': True,  'default_time': '07:00', 'needs_telegram': False, 'costs_ai': False},
    {'key': 'trending', 'label': 'Trending-Themen (KI)',
     'description': 'Analysiert die Schlagzeilen jeder Stadt mit Claude und legt Trending-Themen an. Kostet KI-Tokens, deshalb standardmäßig aus.',
     'default_enabled': False, 'default_time': '07:15', 'needs_telegram': False, 'costs_ai': True},
    {'key': 'inspo',    'label': 'Inspiration abholen (KI)',
     'description': 'Holt neue Beiträge aller Inspirationsquellen über RapidAPI und lässt Claude sie einordnen. Kostet RapidAPI-Aufrufe und KI-Tokens, deshalb standardmäßig aus.',
     'default_enabled': False, 'default_time': '06:00', 'needs_telegram': False, 'costs_ai': True},
    {'key': 'events',   'label': 'Event-Erinnerung',
     'description': 'Eine Telegram-Sammelnachricht zu Events im Vorlauf oder laufend, mit den Städten, die noch keinen passenden Post im Vorrat haben.',
     'default_enabled': True,  'default_time': '08:00', 'needs_telegram': True,  'costs_ai': False},
    {'key': 'nopost',   'label': 'Warnung ohne geplanten Post',
     'description': 'Meldet Städte, die in den nächsten Tagen (Schwelle aus den Einstellungen) keinen geplanten Post haben.',
     'default_enabled': True,  'default_time': '09:00', 'needs_telegram': True,  'costs_ai': False},
    {'key': 'overdue',  'label': 'Überfällige Posts melden',
     'description': 'Meldet Posts, die noch auf „geplant" stehen, deren Termin aber über 24 Stunden zurückliegt. Nur eine Telegram-Sammelliste, es wird nichts automatisch geändert.',
     'default_enabled': True,  'default_time': '09:15', 'needs_telegram': True,  'costs_ai': False},
    {'key': 'weather',  'label': 'Wetter-Events (Open-Meteo)',
     'description': 'Prüft die Vorhersage je Stadt und legt Wetter-Events an (Hitzewelle, Erster Schnee, Starkregen, Sturmwarnung). Telegram-Hinweis, falls konfiguriert.',
     'default_enabled': True,  'default_time': '06:30', 'needs_telegram': False, 'costs_ai': False},
    {'key': 'digest',   'label': 'Tages-Digest',
     'description': 'Schickt alle heute geplanten Posts mit Bild und Knöpfen (Gepostet / Überspringen) an Telegram.',
     'default_enabled': True,  'default_time': '08:30', 'needs_telegram': True,  'costs_ai': False},
    {'key': 'poll',     'label': 'Telegram-Rückmeldungen',
     'description': 'Fragt alle 30 Sekunden die Knopfdrücke aus Telegram ab und markiert Posts als veröffentlicht.',
     'default_enabled': True,  'default_time': None,    'needs_telegram': True,  'costs_ai': False},
]
JOB_BY_KEY = {j['key']: j for j in JOBS}
DAILY_JOBS = [j['key'] for j in JOBS if j['default_time']]

# Wetterregeln – Kategorien und Emoji entsprechen den Seed-Events in app.py.
# 'setting' nennt die AppSettings-Schlüssel, über die der Nutzer die Schwelle bzw. die
# Sperrfrist verstellen kann; 'threshold'/'min_gap_days' sind nur noch die Standardwerte.
WEATHER_RULES = [
    {'name': 'Hitzewelle',    'field': 'temperature_2m_max', 'op': '>=', 'threshold': 32, 'unit': '°C',
     'relevance': 5, 'cats': ['hitze'],            'emoji': '🌡️',
     'setting': 'weather_heat_c'},
    {'name': 'Erster Schnee', 'field': 'snowfall_sum',       'op': '>',  'threshold': 0,  'unit': 'cm',
     'relevance': 5, 'cats': ['schnee', 'winter'], 'emoji': '☃️', 'min_gap_days': FIRST_SNOW_GAP_DAYS,
     'gap_setting': 'weather_snow_gap_days'},
    {'name': 'Starkregen',    'field': 'precipitation_sum',  'op': '>=', 'threshold': 25, 'unit': 'mm',
     'relevance': 4, 'cats': ['regen', 'gewitter'], 'emoji': '⛈️',
     'setting': 'weather_rain_mm'},
    {'name': 'Sturmwarnung',  'field': 'wind_gusts_10m_max', 'op': '>=', 'threshold': 75, 'unit': 'km/h',
     'relevance': 4, 'cats': ['regen'],            'emoji': '🌪️',
     'setting': 'weather_wind_kmh'},
]
# Einstellbare Wetter-Schwellen: Schlüssel → (Standard, Untergrenze, Obergrenze, Beschriftung)
WEATHER_SETTINGS = {
    'weather_heat_c':        (32, 20, 50,  'Hitzewelle ab (°C)'),
    'weather_rain_mm':       (25, 1, 200,  'Starkregen ab (mm/Tag)'),
    'weather_wind_kmh':      (75, 30, 250, 'Sturmwarnung ab (km/h Böen)'),
    'weather_snow_gap_days': (FIRST_SNOW_GAP_DAYS, 1, 365, 'Erster Schnee: Sperrfrist (Tage)'),
}
GEOCODE_URL = 'https://geocoding-api.open-meteo.com/v1/search'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
FORECAST_DAILY = 'temperature_2m_max,precipitation_sum,snowfall_sum,wind_gusts_10m_max'

CITY_GEO_MIGRATIONS = (
    'ALTER TABLE city ADD COLUMN lat FLOAT',
    'ALTER TABLE city ADD COLUMN lon FLOAT',
)

# Laufzeitzustand (pro Prozess)
_state = {
    'app': None, 'thread': None, 'started': False, 'lock_fh': None,
    'heartbeat': 0.0, 'phase': '',
    'poll_suspended_until': 0.0, 'poll_409_logged': False,
    'poll_last': None, 'poll_result': '',
    'watchdog_reported': False,
}


# ══════════════════════════════════════════════════════════════════════════════
# Grundhelfer
# ══════════════════════════════════════════════════════════════════════════════

def _appmod():
    """Das app-Modul zur Laufzeit holen – ohne Import auf Modulebene.
    Bei `python app.py` heißt das Modul '__main__'; ein `import app` würde dort die
    ganze App ein zweites Mal aufbauen. Deshalb zuerst über import_name suchen."""
    fa = _state.get('app')
    if fa is None:
        try:
            fa = current_app._get_current_object()
        except Exception:
            fa = None
    if fa is not None:
        mod = sys.modules.get(getattr(fa, 'import_name', '') or '')
        if mod is not None and hasattr(mod, '_DATA_ROOT'):
            return mod
    # Ohne Flask-App (z. B. beim Start): gleiche Reihenfolge wie in den übrigen Modulen
    main = sys.modules.get('__main__')
    if main is not None and hasattr(main, '_DATA_ROOT'):
        return main
    mod = sys.modules.get('app')
    if mod is not None:
        return mod
    import app as appmod   # noqa: WPS433 – bewusst erst hier
    return appmod


def _flask_app():
    fa = _state.get('app')
    if fa is not None:
        return fa
    return current_app._get_current_object()


def data_root():
    try:
        root = getattr(_appmod(), '_DATA_ROOT', None)
        if root:
            return root
    except Exception:
        pass
    base = os.path.dirname(os.path.abspath(__file__))
    return os.getenv('MEMEOS_DATA_ROOT') or os.path.join(base, 'instance')


def _media_dirs():
    root = data_root()
    return [os.path.join(root, 'renders'), os.path.join(root, 'uploads')]


def _local_media(name):
    """Dateiname, relativer Pfad (/uploads/x.png) oder URL → lokaler Pfad, wenn vorhanden."""
    if not name:
        return None
    base = os.path.basename(str(name).split('?')[0])
    if not base:
        return None
    for folder in _media_dirs():
        candidate = os.path.join(folder, base)
        if os.path.isfile(candidate):
            return candidate
    return None


def _get(key, default=None):
    v = AppSettings.get(key)
    return default if v is None else v


def _set(key, value):
    AppSettings.set(key, value)


def _int_setting(key, default):
    try:
        return int(str(_get(key, default)).strip())
    except Exception:
        return default


def _truthy(v):
    return v in (True, 1, '1', 'true', 'True', 'on', 'yes')


def _h(text):
    return html.escape(str(text or ''), quote=False)


def is_master_paused():
    return _get('master_pause', '0') == '1'


def job_enabled(key):
    v = _get(f'auto_{key}_enabled')
    if v is None:
        return bool(JOB_BY_KEY[key]['default_enabled'])
    return v == '1'


def job_time(key):
    default = JOB_BY_KEY[key]['default_time']
    if not default:
        return None
    v = (_get(f'{key}_time') or '').strip()
    return v if _TIME_RE.match(v) else default


def now_berlin():
    return datetime.now(BERLIN)


def today_berlin():
    return now_berlin().date()


def _to_berlin(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BERLIN)
    return dt.astimezone(BERLIN)


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip())
    except Exception:
        return None


def due(job, now=None, last_run=_UNSET, at=None):
    """Ist der Tagesjob jetzt fällig?
    - poll ist immer fällig (die Schleife taktet ohnehin nur alle 30 s).
    - Sonst: eingestellte Uhrzeit erreicht UND heute (Berlin) noch nicht gelaufen.
    last_run/at sind optional für Unit-Tests; ohne Angabe werden sie aus AppSettings gelesen."""
    if job == 'poll':
        return True
    if job not in JOB_BY_KEY:
        return False
    now = _to_berlin(now or now_berlin())
    at = at or job_time(job)
    if not at or not _TIME_RE.match(at):
        return False
    hh, mm = int(at[:2]), int(at[3:])
    if (now.hour, now.minute) < (hh, mm):
        return False
    if last_run is _UNSET:
        last_run = _get(f'sched_last_{job}')
    last = _parse_iso(last_run) if isinstance(last_run, str) else last_run
    last = _to_berlin(last) if last is not None else None
    if last is not None and last.date() == now.date():
        return False
    return True


def _offline():
    return os.getenv('MEMEOS_OFFLINE', '') == '1'


def _anthropic_key():
    return (os.getenv('ANTHROPIC_API_KEY') or '').strip()


# ── Login (eigener Decorator, wie login_required in app.py) ────────────────────
def _login_required(f):
    @wraps(f)
    def inner(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Nicht angemeldet'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return inner


# ══════════════════════════════════════════════════════════════════════════════
# Telegram
# ══════════════════════════════════════════════════════════════════════════════

def _tg_config():
    token = (_get('telegram_token', '') or '').strip()
    chat = (_get('telegram_chat_id', '') or '').strip()
    if token and chat:
        return token, chat
    return None, None


def telegram_configured():
    token, chat = _tg_config()
    return bool(token and chat)


def _tg_call(method, payload=None, files=None, timeout=15):
    """→ (ok, antwort_json_oder_None, http_status). Ohne Konfiguration (False, None, 0)."""
    token, _chat = _tg_config()
    if not token:
        return False, None, 0
    url = f'https://api.telegram.org/bot{token}/{method}'
    try:
        if files:
            r = requests.post(url, data=payload or {}, files=files, timeout=timeout)
        else:
            r = requests.post(url, json=payload or {}, timeout=timeout)
        try:
            data = r.json()
        except Exception:
            data = None
        if not r.ok:
            desc = (data or {}).get('description') if isinstance(data, dict) else None
            log.warning('Telegram %s: HTTP %s %s', method, r.status_code, desc or r.text[:160])
        return bool(r.ok and isinstance(data, dict) and data.get('ok')), data, r.status_code
    except requests.RequestException as ex:
        log.warning('Telegram %s nicht erreichbar: %s', method, ex)
        return False, None, 0


def _keyboard(buttons):
    """[[('Text', 'callback'), …], …] → Telegram-InlineKeyboard."""
    if not buttons:
        return None
    return {'inline_keyboard': [[{'text': t, 'callback_data': d} for (t, d) in row] for row in buttons]}


def send_text(text, buttons=None):
    """Textnachricht (HTML-Modus, Aufrufer escaped mit _h). Ohne Konfiguration still False."""
    token, chat = _tg_config()
    if not token:
        return False
    payload = {'chat_id': chat, 'text': str(text)[:4096], 'parse_mode': 'HTML',
               'disable_web_page_preview': True}
    kb = _keyboard(buttons)
    if kb:
        payload['reply_markup'] = kb
    ok, _data, _status = _tg_call('sendMessage', payload)
    return ok


def send_photo(path_or_url, caption, buttons=None, video=False):
    """Bild (oder Video) mit Caption und Inline-Knöpfen. Lokale Datei per Upload, sonst URL.
    Ohne Konfiguration still False."""
    token, chat = _tg_config()
    if not token or not path_or_url:
        return False
    method = 'sendVideo' if video else 'sendPhoto'
    field = 'video' if video else 'photo'
    payload = {'chat_id': chat, 'caption': str(caption or '')[:1024], 'parse_mode': 'HTML'}
    kb = _keyboard(buttons)
    if kb:
        payload['reply_markup'] = json.dumps(kb)
    local = path_or_url if os.path.isfile(str(path_or_url)) else None
    if local:
        try:
            with open(local, 'rb') as fh:
                ok, _d, _s = _tg_call(method, payload, files={field: (os.path.basename(local), fh)}, timeout=60)
            return ok
        except OSError as ex:
            log.warning('Telegram %s: Datei nicht lesbar: %s', method, ex)
            return False
    if kb:
        payload['reply_markup'] = kb      # als JSON-Body geht das Objekt direkt
    payload[field] = str(path_or_url)
    ok, _d, _s = _tg_call(method, payload, timeout=30)
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# Job 1: RSS
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_feed(url):
    """Feed über requests (mit Timeout) laden und feedparser parsen lassen –
    feedparser selbst hat keinen Timeout."""
    import feedparser
    resp = requests.get(url, timeout=20, headers={'User-Agent': 'MemeOS/1.0 (+RSS-Reader)'})
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def job_rss(manual=False):
    cities = (City.query.filter_by(active=True)
              .filter(City.rss_url.isnot(None), City.rss_url != '').all())
    feeds = total = failed = 0
    for city in cities:
        try:
            feed = _fetch_feed(city.rss_url)
            feeds += 1
            for entry in feed.entries[:20]:
                url = (entry.get('link') or '').strip()
                if not url or NewsItem.query.filter_by(url=url).first():
                    continue
                pub = None
                pp = entry.get('published_parsed')
                if pp:
                    try:
                        pub = datetime.fromtimestamp(calendar.timegm(pp))
                    except Exception:
                        pub = None
                db.session.add(NewsItem(
                    city_id=city.id,
                    headline=(entry.get('title') or '')[:500],
                    url=url[:1000],
                    source_name=(feed.feed.get('title') or '')[:200],
                    published_at=pub,
                ))
                total += 1
            db.session.commit()
        except Exception as ex:
            db.session.rollback()
            failed += 1
            log.warning('RSS [%s]: %s', city.name, ex)
    result = f'{feeds} Feeds, {total} neue Artikel'
    if failed:
        result += f', {failed} Feeds fehlgeschlagen'
    if total and _anthropic_key():
        try:
            appmod = _appmod()
            threading.Thread(target=appmod._score_news_items, args=(_flask_app(),), daemon=True).start()
            result += ', KI-Bewertung gestartet'
        except Exception as ex:
            log.warning('KI-Bewertung nicht gestartet: %s', ex)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Job 2: Trending (KI, Standard AUS)
# ══════════════════════════════════════════════════════════════════════════════

def _trending_prompt(city_name, headlines):
    return f"""Du analysierst aktuelle Schlagzeilen aus {city_name} auf ihr Meme-Potenzial für Instagram-Stadtmemes.

Schlagzeilen:
{chr(10).join(f'- {h}' for h in headlines)}

Extrahiere die Top 5 Trending-Themen die sich am besten für virale Stadtmemes eignen.
Antworte NUR mit validem JSON (kein Markdown, kein Text davor/danach):
{{"topics":[{{"keyword":"kurzes prägnantes Schlagwort (max 4 Wörter)","description":"1-2 Sätze Kontext warum das trending ist","trend_score":85}},{{"keyword":"...","description":"...","trend_score":70}}]}}

trend_score: 0-100, wie gut geeignet für einen viralen Stadtmeme."""


def _trending_for_city(city, client):
    feed = _fetch_feed(city.rss_url)
    headlines = [e.title for e in feed.entries[:20] if getattr(e, 'title', None)]
    if not headlines:
        return 0, 0
    resp = client.messages.create(
        model=TRENDING_MODEL, max_tokens=800,
        messages=[{'role': 'user', 'content': _trending_prompt(city.name, headlines)}],
    )
    try:
        _appmod()._log_ai_usage('trending_auto', TRENDING_MODEL,
                                resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception as ex:
        log.warning('AI-Usage-Log fehlgeschlagen: %s', ex)
    raw = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text').strip()
    start, end = raw.find('{'), raw.rfind('}') + 1
    data = json.loads(raw[start:end]) if start >= 0 and end > start else {}
    cutoff = datetime.utcnow() - timedelta(hours=48)
    added = skipped = 0
    for t in data.get('topics', []) or []:
        kw = (t.get('keyword') or '').strip()[:200]
        if not kw:
            continue
        already = db.session.query(TrendingTopic).filter(
            TrendingTopic.city_id == city.id,
            db.func.lower(TrendingTopic.keyword) == kw.lower(),
            TrendingTopic.created_at >= cutoff,
        ).first()
        if already:
            skipped += 1
            continue
        try:
            score = max(0, min(100, int(t.get('trend_score', 50))))
        except Exception:
            score = 50
        db.session.add(TrendingTopic(
            city_id=city.id, keyword=kw, description=t.get('description', ''),
            trend_score=score, source='rss', fetched_at=datetime.utcnow(),
        ))
        added += 1
    db.session.commit()
    return added, skipped


def job_trending(manual=False):
    key = _anthropic_key()
    if not key:
        return 'kein ANTHROPIC_API_KEY, übersprungen'
    cities = (City.query.filter_by(active=True)
              .filter(City.rss_url.isnot(None), City.rss_url != '').all())
    if not cities:
        return '0 Städte mit RSS-URL'
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1)
    done = added = skipped = failed = 0
    for city in cities:
        try:
            a, s = _trending_for_city(city, client)
            added += a
            skipped += s
            done += 1
        except Exception as ex:
            db.session.rollback()
            failed += 1
            log.warning('Trending [%s]: %s', city.name, ex)
    result = f'{done} Städte, {added} neue Themen, {skipped} Duplikate'
    if failed:
        result += f', {failed} fehlgeschlagen'
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Job 2b: Inspiration abholen + KI-Sortierung (Standard AUS – kostet RapidAPI und KI)
# ══════════════════════════════════════════════════════════════════════════════

def inspo_sort_limit():
    """Deckel für die KI-Sortierung je Lauf (AppSettings 'inspo_sort_limit')."""
    return max(0, min(INSPO_SORT_MAX, _int_setting('inspo_sort_limit', INSPO_SORT_DEFAULT)))


def _call_app_json(fn_name, payload):
    """Eine Routenfunktion aus app.py direkt aufrufen – ohne HTTP, ohne CSRF, ohne Login.
    → (daten_dict_oder_None, status, fehlertext_oder_None)."""
    fn = getattr(_appmod(), fn_name, None)
    if fn is None:
        return None, 0, f'{fn_name} gibt es in app.py nicht mehr'
    inner = getattr(fn, '__wrapped__', None)      # login_required nutzt functools.wraps
    flask_app = _flask_app()
    with flask_app.test_request_context(f'/intern/{fn_name}', method='POST', json=payload):
        if inner is None:                         # kein Decorator zum Auspacken → Sitzung setzen
            inner = fn
            try:
                session['logged_in'] = True
            except Exception as ex:
                return None, 0, f'{fn_name} nicht aufrufbar ({ex})'
        rv = inner()
    status = 200
    if isinstance(rv, tuple):
        rv, status = rv[0], (rv[1] if len(rv) > 1 else 200)
    data = None
    try:
        data = rv.get_json(silent=True) if hasattr(rv, 'get_json') else rv
    except Exception:
        data = None
    if not isinstance(data, dict):
        return None, status, f'{fn_name} lieferte keine verwertbare Antwort'
    return data, status, None


def job_inspo(manual=False):
    if _offline():
        return 'offline übersprungen'
    try:
        import inspiration_fetch_bp as insp
        from models import MemoInspirationSource
    except Exception as ex:
        return f'Fehler: Inspiration-Modul nicht ladbar ({ex})'

    key, _quelle = insp.get_rapidapi_key()
    if not key:
        return 'kein RapidAPI-Schlüssel hinterlegt (Einstellungen → Integrationen)'
    sources = MemoInspirationSource.query.order_by(MemoInspirationSource.username).all()
    if not sources:
        return 'keine Inspirationsquellen angelegt'
    if not insp._fetch_lock.acquire(blocking=False):
        return 'ein Abruf läuft bereits, übersprungen'
    limit = insp.get_fetch_limit()
    neu = geholt = fehler = 0
    fehlertexte = []
    try:
        for i, src in enumerate(sources):
            if i and insp.FETCH_ALL_PAUSE:
                time.sleep(insp.FETCH_ALL_PAUSE)
            try:
                r = insp.fetch_source(src, limit, None, key)
                neu += r['new']
                geholt += r['fetched']
            except insp.FetchError as ex:
                db.session.rollback()
                fehler += 1
                fehlertexte.append(f'@{src.username}: {ex}')
            except Exception as ex:
                db.session.rollback()
                fehler += 1
                fehlertexte.append(f'@{src.username}: {type(ex).__name__}')
                log.warning('Inspiration-Abruf @%s: %s', src.username, ex)
    finally:
        insp._fetch_lock.release()

    teile = [f'{neu} neue Beiträge aus {len(sources) - fehler} von {len(sources)} Quellen',
             f'{geholt} geprüft']
    if fehler:
        teile.append(f'{fehler} Quelle(n) mit Fehler: ' + '; '.join(fehlertexte[:3]))

    deckel = inspo_sort_limit()
    if deckel <= 0:
        teile.append('KI-Sortierung aus (inspo_sort_limit 0)')
        return ', '.join(teile)
    if not _anthropic_key():
        teile.append('KI-Sortierung übersprungen (kein ANTHROPIC_API_KEY)')
        return ', '.join(teile)
    data, status, err = _call_app_json('api_inspo_ai_sort', {'count': deckel})
    if err:
        teile.append(f'KI-Sortierung: {err}')
    elif status >= 400 or not data.get('ok'):
        teile.append('KI-Sortierung: ' + str(data.get('error') or f'HTTP {status}'))
    else:
        teile.append(f'{data.get("processed", 0)} von höchstens {deckel} KI-sortiert')
        if data.get('knowledge_added'):
            teile.append(f'{data["knowledge_added"]} Wissenseinträge')
    return ', '.join(teile)


# ══════════════════════════════════════════════════════════════════════════════
# Job 3: Events
# ══════════════════════════════════════════════════════════════════════════════

def _active_cities():
    return City.query.filter_by(active=True).order_by(City.name).all()


def _cities_without_matching_post(event, cities):
    """Städte im Scope des Events, die keinen Post (bereit/geplant) mit passender
    Template-Kategorie (suggested_cats) im Vorrat haben. None, wenn das Event keine
    Kategorien vorschlägt."""
    cats = [c for c in event.get_suggested_cats() if c]
    if not cats:
        return None
    scope = set(event.get_city_scope() or [])
    relevant = [c for c in cities if not scope or c.id in scope]
    have = {row[0] for row in db.session.query(MemePost.city_id)
            .join(MemeTemplate, MemePost.template_id == MemeTemplate.id)
            .filter(MemePost.status.in_(('bereit', 'geplant')),
                    MemeTemplate.category.in_(cats))
            .distinct().all()}
    return [c for c in relevant if c.id not in have]


def _due_events(now_utc=None):
    """[(event, days_until)] – im Vorlauf (lead_days) oder heute aktiv, zuletzt vor mehr
    als 20 h gemeldet."""
    now_utc = now_utc or datetime.utcnow()
    out = []
    for e in MemeEvent.query.filter_by(active=True).all():
        try:
            d = e.days_until()
        except Exception:
            d = None
        if d is None:
            continue
        if not (e.is_active_today() or 0 <= d <= (e.lead_days or 0)):
            continue
        if e.notified_at and (now_utc - e.notified_at) < timedelta(hours=EVENT_RENOTIFY_HOURS):
            continue
        out.append((e, d))
    out.sort(key=lambda x: (0 if x[0].is_active_today() else 1, x[1], x[0].name))
    return out


def _name_list(cities, limit=8):
    names = [c.name for c in cities]
    if len(names) > limit:
        return ', '.join(names[:limit]) + f' und {len(names) - limit} weitere'
    return ', '.join(names)


def _events_message(items):
    cities = _active_cities()
    lines = [f'<b>Anstehende Events ({len(items)})</b>']
    for e, d in items:
        if e.is_active_today():
            when = 'läuft gerade'
        elif d == 0:
            when = 'heute'
        elif d == 1:
            when = 'morgen'
        else:
            when = f'in {d} Tagen'
        head = f"{e.emoji or ''} <b>{_h(e.name)}</b> – {when}".strip()
        lines.append('')
        lines.append(head)
        if e.description:
            lines.append(_h(e.description)[:300])
        cats = e.get_suggested_cats()
        if cats:
            lines.append('Kategorien: ' + _h(', '.join(cats)))
        missing = _cities_without_matching_post(e, cities)
        if missing is None:
            continue
        if missing:
            lines.append('Ohne passenden Post im Vorrat: ' + _h(_name_list(missing)))
        else:
            lines.append('Alle Städte haben einen passenden Post im Vorrat ✓')
    return '\n'.join(lines)


def job_events(manual=False):
    items = _due_events()
    if not telegram_configured():
        return f'Telegram nicht konfiguriert ({len(items)} Events anstehend)'
    if not items:
        return 'keine anstehenden Events'
    text = _events_message(items)
    if not send_text(text):
        return 'Fehler: Telegram-Versand fehlgeschlagen'
    now = datetime.utcnow()
    for e, _d in items:
        e.notified_at = now
    db.session.commit()
    return f'{len(items)} Events gemeldet'


# ══════════════════════════════════════════════════════════════════════════════
# Job 4: Ohne geplanten Post
# ══════════════════════════════════════════════════════════════════════════════

def _cities_without_planned_post(days):
    start = datetime.combine(today_berlin(), dtime.min)     # scheduled_at ist Berlin-naiv
    end = start + timedelta(days=days)
    covered = {row[0] for row in db.session.query(MemePost.city_id)
               .filter(MemePost.status == 'geplant',
                       MemePost.scheduled_at.isnot(None),
                       MemePost.scheduled_at >= start,
                       MemePost.scheduled_at < end)
               .distinct().all()}
    return [c for c in _active_cities() if c.id not in covered]


def job_nopost(manual=False):
    days = max(1, _int_setting('alert_threshold_days', 3))
    missing = _cities_without_planned_post(days)
    if not telegram_configured():
        return f'Telegram nicht konfiguriert ({len(missing)} Städte ohne geplanten Post)'
    total = City.query.filter_by(active=True).count()
    if not missing:
        return f'alle {total} Städte haben geplante Posts (nächste {days} Tage)'
    text = (f'<b>Ohne geplanten Post</b> (nächste {days} Tage, {len(missing)} von {total}): '
            + _h(', '.join(c.name for c in missing)))
    if not send_text(text):
        return 'Fehler: Telegram-Versand fehlgeschlagen'
    return f'{len(missing)} von {total} Städten ohne geplanten Post gemeldet'


# ══════════════════════════════════════════════════════════════════════════════
# Job 4b: Überfällige Posts (nur melden, nichts ändern)
# ══════════════════════════════════════════════════════════════════════════════

def overdue_posts(now=None, hours=OVERDUE_HOURS):
    """Posts, die noch auf 'geplant' stehen, deren Termin aber länger als `hours` zurückliegt.
    scheduled_at ist Berlin-naiv, deshalb wird auch die Grenze Berlin-naiv gebildet."""
    jetzt = (now or now_berlin()).replace(tzinfo=None)
    grenze = jetzt - timedelta(hours=hours)
    return (MemePost.query
            .filter(MemePost.status == 'geplant',
                    MemePost.scheduled_at.isnot(None),
                    MemePost.scheduled_at < grenze)
            .order_by(MemePost.scheduled_at, MemePost.id).all())


def _overdue_message(posts):
    lines = [f'<b>Überfällig ({len(posts)})</b> – Termin über {OVERDUE_HOURS} Stunden her, '
             f'Status weiterhin „geplant“']
    for p in posts[:OVERDUE_LIST_MAX]:
        stadt = p.city.name if p.city else '?'
        wann = p.scheduled_at.strftime('%d.%m. %H:%M') if p.scheduled_at else '—'
        titel = (p.title or p.caption or '').strip()
        zeile = f'● {_h(stadt)} · {wann}'
        if titel:
            zeile += f' · {_h(titel[:60])}'
        lines.append(zeile)
    if len(posts) > OVERDUE_LIST_MAX:
        lines.append(f'… und {len(posts) - OVERDUE_LIST_MAX} weitere')
    lines.append('')
    lines.append('Es wurde nichts geändert. Im Dashboard als veröffentlicht markieren '
                 'oder neu terminieren.')
    return '\n'.join(lines)


def job_overdue(manual=False):
    posts = overdue_posts()
    if not telegram_configured():
        return f'Telegram nicht konfiguriert ({len(posts)} überfällige Posts)'
    if not posts:
        return 'keine überfälligen Posts'
    if not send_text(_overdue_message(posts)):
        return f'Fehler: Telegram-Versand fehlgeschlagen ({len(posts)} überfällige Posts)'
    return f'{len(posts)} überfällige Posts gemeldet'


# ══════════════════════════════════════════════════════════════════════════════
# Job 5: Wetter (Open-Meteo)
# ══════════════════════════════════════════════════════════════════════════════

def _num(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def _geocode(name):
    """→ (lat, lon) oder (None, None). Open-Meteo Geocoding, auf Deutschland begrenzt."""
    r = requests.get(GEOCODE_URL, params={'name': name, 'count': 1, 'language': 'de',
                                          'format': 'json', 'country': 'DE', 'countryCode': 'DE'},
                     timeout=15)
    r.raise_for_status()
    results = (r.json() or {}).get('results') or []
    for res in results:
        if (res.get('country_code') or 'DE').upper() != 'DE':
            continue
        lat, lon = _num(res.get('latitude')), _num(res.get('longitude'))
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


def _ensure_coords(city):
    """lat/lon der Stadt sicherstellen (Geocoding einmalig, Ergebnis wird gespeichert)."""
    if city.lat is not None and city.lon is not None:
        return city.lat, city.lon
    lat, lon = _geocode(city.name)
    if lat is None:
        return None, None
    city.lat, city.lon = lat, lon
    db.session.commit()
    return lat, lon


def _fetch_forecast(lat, lon):
    r = requests.get(FORECAST_URL, params={
        'latitude': lat, 'longitude': lon, 'daily': FORECAST_DAILY,
        'timezone': 'Europe/Berlin', 'forecast_days': 2,
    }, timeout=15)
    r.raise_for_status()
    return r.json() or {}


def forecast_days(fc):
    """Open-Meteo-Antwort → [{'date', 'temperature_2m_max', 'precipitation_sum', 'snowfall_sum',
    'wind_gusts_10m_max'}, …] (ein Eintrag je Tag)."""
    daily = fc.get('daily') or {}
    times = daily.get('time') or []
    fields = FORECAST_DAILY.split(',')
    out = []
    for i, day in enumerate(times):
        row = {'date': day}
        for f in fields:
            vals = daily.get(f) or []
            row[f] = _num(vals[i]) if i < len(vals) else None
        out.append(row)
    return out


def _rule_hit(rule, value):
    if value is None:
        return False
    return value >= rule['threshold'] if rule['op'] == '>=' else value > rule['threshold']


def weather_setting(key):
    """Eine Wetter-Schwelle aus den Einstellungen, auf ihren erlaubten Bereich begrenzt."""
    default, lo, hi, _label = WEATHER_SETTINGS[key]
    return max(lo, min(hi, _int_setting(key, default)))


def weather_rules():
    """WEATHER_RULES mit den Schwellen aus den Einstellungen (Standard, falls nichts gesetzt)."""
    out = []
    for rule in WEATHER_RULES:
        r = dict(rule)
        if r.get('setting') in WEATHER_SETTINGS:
            r['threshold'] = weather_setting(r['setting'])
        if r.get('gap_setting') in WEATHER_SETTINGS:
            r['min_gap_days'] = weather_setting(r['gap_setting'])
        out.append(r)
    return out


def evaluate_weather_rules(days, rules=None):
    """→ Liste ausgelöster Regeln: {'rule', 'date', 'value', 'unit', 'cats', 'relevance', 'emoji'};
    je Regel der erste Tag, an dem sie zutrifft. Ohne `rules` gelten die eingestellten Schwellen."""
    hits = []
    for rule in (rules if rules is not None else weather_rules()):
        for day in days:
            val = day.get(rule['field'])
            if _rule_hit(rule, val):
                hits.append({'rule': rule['name'], 'date': day.get('date'), 'value': val,
                             'unit': rule['unit'], 'cats': rule['cats'],
                             'relevance': rule['relevance'], 'emoji': rule['emoji'],
                             'threshold': rule['threshold'],
                             'min_gap_days': rule.get('min_gap_days', WEATHER_EVENT_GAP_DAYS)})
                break
    return hits


def _weather_event_name(rule_name, city):
    return f'{rule_name} {city.name}'


def _weather_event_blocked(city, hit, now_utc=None):
    """Grund (Text), warum für diese Regel und Stadt gerade KEIN Event angelegt wird, sonst None.
    Pro Regel und Stadt höchstens ein Event alle 3 Tage; 'Erster Schnee' erst wieder nach 60 Tagen."""
    now_utc = now_utc or datetime.utcnow()
    name = _weather_event_name(hit['rule'], city)
    gap = int(hit.get('min_gap_days') or WEATHER_EVENT_GAP_DAYS)
    last = (MemeEvent.query.filter_by(name=name, event_type='wetter')
            .order_by(MemeEvent.created_at.desc()).first())
    if last and last.created_at and (now_utc - last.created_at) < timedelta(days=gap):
        age = (now_utc - last.created_at).days
        return f'bereits angelegt vor {age} Tag(en), Sperrfrist {gap} Tage'
    return None


def _create_weather_event(city, hit):
    ev = MemeEvent(
        name=_weather_event_name(hit['rule'], city),
        description=f"{hit['rule']} laut Vorhersage: {hit['value']:g} {hit['unit']} am {hit['date']}",
        event_type='wetter',
        date_from=hit['date'], date_to=hit['date'],
        recurring=False, lead_days=0,
        city_scope=json.dumps([city.id]),
        meme_relevance=hit['relevance'],
        suggested_cats=json.dumps(hit['cats']),
        emoji=hit['emoji'],
        notes='automatisch erkannt (Open-Meteo)',
    )
    db.session.add(ev)
    return ev


def _weather_day_label(iso_day):
    try:
        d = date.fromisoformat(iso_day)
    except Exception:
        return iso_day or ''
    today = today_berlin()
    if d == today:
        return 'heute'
    if d == today + timedelta(days=1):
        return 'morgen'
    return d.strftime('%d.%m.')


def _weather_message(created):
    lines = [f'<b>Wetter-Events erkannt ({len(created)})</b>']
    for city, hit in created:
        lines.append(f"● {hit['emoji']} {_h(hit['rule'])} {_h(city.name)} – {_weather_day_label(hit['date'])}"
                     f" ({hit['value']:g} {hit['unit']})")
    lines.append('')
    lines.append('Events-Seite → Filter Wetter. Passende Template-Kategorien stehen am Event.')
    return '\n'.join(lines)


def job_weather(manual=False):
    if _offline():
        return 'offline übersprungen'
    cities = _active_cities()
    if not cities:
        return '0 aktive Städte'
    checked = net_errors = errors = no_geo = 0
    created = []
    for city in cities:
        try:
            lat, lon = _ensure_coords(city)
            if lat is None:
                no_geo += 1
                continue
            days = forecast_days(_fetch_forecast(lat, lon))
            checked += 1
            for hit in evaluate_weather_rules(days):
                if _weather_event_blocked(city, hit):
                    continue
                _create_weather_event(city, hit)
                created.append((city, hit))
            db.session.commit()
        except requests.RequestException as ex:
            db.session.rollback()
            net_errors += 1
            log.warning('Wetter [%s]: Open-Meteo nicht erreichbar: %s', city.name, ex)
        except Exception as ex:
            db.session.rollback()
            errors += 1
            log.warning('Wetter [%s]: %s', city.name, ex)
    if checked == 0 and net_errors and net_errors + no_geo >= len(cities):
        return 'offline übersprungen (Open-Meteo nicht erreichbar)'
    result = f'{checked} Städte geprüft, {len(created)} neue Wetter-Events'
    if no_geo:
        result += f', {no_geo} ohne Koordinaten'
    if net_errors:
        result += f', {net_errors} Netzfehler'
    if errors:
        result += f', {errors} Fehler'
    if created:
        if telegram_configured():
            if not send_text(_weather_message(created)):
                result += ', Telegram-Versand fehlgeschlagen'
        else:
            result += ', Telegram nicht konfiguriert'
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Job 6: Digest
# ══════════════════════════════════════════════════════════════════════════════

def _digest_posts(day=None):
    day = day or today_berlin()
    start = datetime.combine(day, dtime.min)
    end = start + timedelta(days=1)
    return (MemePost.query
            .filter(MemePost.status == 'geplant',
                    MemePost.scheduled_at.isnot(None),
                    MemePost.scheduled_at >= start,
                    MemePost.scheduled_at < end)
            .order_by(MemePost.scheduled_at, MemePost.id).all())


def _is_video(name):
    base = (str(name or '').split('?')[0]).lower()
    return base.rsplit('.', 1)[-1] in ('mp4', 'mov', 'webm') if '.' in base else False


def _post_media(post):
    """→ {'local': Pfad|None, 'url': http-URL|None, 'video': bool, 'slides': int}
    Karussell: erstes Bild. Lokale Datei aus <DATA_ROOT>/renders bzw. uploads bevorzugt."""
    paths = post.get_carousel_paths() if post.post_type == 'carousel' else []
    first = paths[0] if paths else (post.image_path or post.image_url or '')
    candidates = [first, post.image_path, post.image_url]
    local = None
    for c in candidates:
        local = _local_media(c)
        if local:
            break
    url = None
    for c in candidates:
        if c and str(c).startswith(('http://', 'https://')):
            url = str(c)
            break
    return {'local': local, 'url': url,
            'video': _is_video(local or url or first),
            'slides': len(paths) if paths else (1 if (local or url) else 0)}


def _digest_caption(post, media=None):
    media = media or _post_media(post)
    when = post.scheduled_at.strftime('%H:%M') if post.scheduled_at else '—'
    city = post.city.name if post.city else '?'
    lines = [f'<b>{_h(city)} · {when}</b>']
    body = (post.caption or '').strip() or (post.title or '').strip()
    if body:
        lines.append(_h(body))
    if (post.hashtags or '').strip():
        lines.append(_h(post.hashtags.strip()))
    if post.post_type == 'carousel' and media['slides'] > 1:
        lines.append(f"Karussell, {media['slides']} Slides")
    text = '\n'.join(lines)
    if len(text) > TELEGRAM_CAPTION_MAX:
        text = text[:TELEGRAM_CAPTION_MAX - 1] + '…'
    return text


SKIP_DAYS = 1        # Knopf "Überspringen" → einen Tag später, gleiche Uhrzeit
LATER_DAYS = 7       # Knopf "Später" → eine Woche später, gleiche Uhrzeit


def _digest_buttons(post):
    return [
        [('Gepostet ✓', f'posted:{post.id}'), ('Überspringen', f'skip:{post.id}')],
        [('Später', f'later:{post.id}')],
    ]


def _digest_preview_item(post):
    media = _post_media(post)
    return {
        'id': post.id, 'city': post.city.name if post.city else '',
        'time': post.scheduled_at.strftime('%H:%M') if post.scheduled_at else None,
        'scheduled_at': post.scheduled_at.isoformat() if post.scheduled_at else None,
        'title': post.title or '', 'post_type': post.post_type,
        'caption_preview': (post.caption or '')[:140],
        'has_image': bool(media['local'] or media['url']),
        'image_source': 'lokal' if media['local'] else ('url' if media['url'] else None),
        'is_video': media['video'], 'slide_count': media['slides'],
        'telegram_caption': _digest_caption(post, media),
    }


def job_digest(manual=False):
    posts = _digest_posts()
    if not telegram_configured():
        return f'Telegram nicht konfiguriert ({len(posts)} Posts heute)'
    if not posts:
        return 'keine geplanten Posts heute'
    sent = 0
    for p in posts:
        try:
            media = _post_media(p)
            cap = _digest_caption(p, media)
            btn = _digest_buttons(p)
            ok = False
            if media['local'] or media['url']:
                ok = send_photo(media['local'] or media['url'], cap, btn, video=media['video'])
                if not ok:
                    cap = cap + '\n(Bild konnte nicht gesendet werden)'
            else:
                cap = cap + '\n(kein Bild hinterlegt)'
            if not ok:
                ok = send_text(cap, btn)
            sent += 1 if ok else 0
        except Exception as ex:
            log.warning('Digest Post %s: %s', p.id, ex)
    return f'{sent} von {len(posts)} Posts gesendet'


# ══════════════════════════════════════════════════════════════════════════════
# Job 7: Poll (Telegram getUpdates, Knopfdrücke)
# ══════════════════════════════════════════════════════════════════════════════

def _answer_callback(cb_id, text=''):
    if not cb_id:
        return
    _tg_call('answerCallbackQuery', {'callback_query_id': cb_id, 'text': text[:200]}, timeout=10)


def _clear_buttons(chat_id, message_id):
    if chat_id is None or message_id is None:
        return
    _tg_call('editMessageReplyMarkup', {'chat_id': chat_id, 'message_id': message_id,
                                        'reply_markup': {'inline_keyboard': []}}, timeout=10)


def _shift_post(raw_id, days, antwort, cb_id, chat_id, message_id):
    """Geplanten Post um `days` Tage nach hinten schieben (gleiche Uhrzeit), Knöpfe der
    Nachricht entfernen und den Grund in post.notes vermerken. → Kurztext für das Log."""
    try:
        pid = int(str(raw_id).strip())
    except ValueError:
        _answer_callback(cb_id, 'Ungültig')
        return 'ungültige ID'
    post = db.session.get(MemePost, pid)
    if not post:
        _answer_callback(cb_id, 'Post nicht gefunden')
        return f'Post {pid} nicht gefunden'
    # scheduled_at ist Berlin-naiv (siehe _digest_posts). Ohne Termin zählt ab jetzt.
    basis = post.scheduled_at or now_berlin().replace(tzinfo=None, second=0, microsecond=0)
    neu = basis + timedelta(days=days)
    post.scheduled_at = neu
    vermerk = (f'{now_berlin():%d.%m.%Y %H:%M} Telegram: um {days} Tag(e) verschoben '
               f'auf {neu:%d.%m.%Y %H:%M}')
    post.notes = f'{(post.notes or "").rstrip()}\n{vermerk}'.strip()
    db.session.commit()
    _answer_callback(cb_id, antwort)
    _clear_buttons(chat_id, message_id)
    return f'Post {pid} um {days} Tag(e) verschoben auf {neu:%d.%m. %H:%M}'


def _handle_callback(cbq):
    """Verarbeitet eine callback_query. → Kurztext für das Log."""
    cb_id = cbq.get('id', '')
    data = (cbq.get('data') or '').strip()
    msg = cbq.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    message_id = msg.get('message_id')
    _token, allowed_chat = _tg_config()
    if allowed_chat and chat_id is not None and str(chat_id) != str(allowed_chat):
        _answer_callback(cb_id, 'Nicht erlaubt')
        return 'fremder Chat ignoriert'
    if data.startswith('posted:'):
        try:
            pid = int(data.split(':', 1)[1])
        except ValueError:
            _answer_callback(cb_id, 'Ungültig')
            return 'ungültige ID'
        post = db.session.get(MemePost, pid)
        if not post:
            _answer_callback(cb_id, 'Post nicht gefunden')
            return f'Post {pid} nicht gefunden'
        if post.status != 'veroeffentlicht':
            post.status = 'veroeffentlicht'
            post.published_at = datetime.utcnow()
            db.session.commit()
        _answer_callback(cb_id, 'Als veröffentlicht markiert')
        _clear_buttons(chat_id, message_id)
        return f'Post {pid} veröffentlicht'
    for prefix, days, antwort in (('skip:', SKIP_DAYS, 'Auf morgen verschoben'),
                                  ('later:', LATER_DAYS, 'Um sieben Tage verschoben')):
        if data.startswith(prefix):
            return _shift_post(data[len(prefix):], days, antwort, cb_id, chat_id, message_id)
    _answer_callback(cb_id, '')
    return 'unbekannt'


def job_poll(manual=False):
    token, _chat = _tg_config()
    if not token:
        return 'Telegram nicht konfiguriert'
    if time.time() < _state['poll_suspended_until']:
        rest = int(_state['poll_suspended_until'] - time.time())
        return f'ausgesetzt ({rest}s, Webhook aktiv?)'
    offset = _int_setting('telegram_poll_offset', 0)
    params = {'timeout': 0, 'allowed_updates': ['callback_query']}
    if offset:
        params['offset'] = offset
    ok, data, status = _tg_call('getUpdates', params, timeout=15)
    if status == 409:
        _state['poll_suspended_until'] = time.time() + POLL_SUSPEND_SECONDS
        if not _state['poll_409_logged']:
            log.warning('Telegram getUpdates: 409 – Webhook aktiv oder zweiter Poller. '
                        'Poll für %s s ausgesetzt.', POLL_SUSPEND_SECONDS)
            _state['poll_409_logged'] = True
        return 'HTTP 409 – Poll 10 Minuten ausgesetzt'
    if not ok or not isinstance(data, dict):
        return f'getUpdates fehlgeschlagen (HTTP {status})'
    _state['poll_409_logged'] = False
    handled = 0
    new_offset = offset
    notes = []
    for upd in data.get('result') or []:
        uid = upd.get('update_id')
        if isinstance(uid, int):
            new_offset = max(new_offset, uid + 1)
        cbq = upd.get('callback_query')
        if not cbq:
            continue
        try:
            notes.append(_handle_callback(cbq))
            handled += 1
        except Exception as ex:
            db.session.rollback()
            log.warning('Callback-Verarbeitung: %s', ex)
    if new_offset != offset:
        _set('telegram_poll_offset', str(new_offset))
    if handled:
        return f'{handled} Rückmeldungen: ' + '; '.join(notes)[:200]
    return '0 Rückmeldungen'


# ══════════════════════════════════════════════════════════════════════════════
# Ausführung, Schleife, Wächter
# ══════════════════════════════════════════════════════════════════════════════

_JOB_FUNCS = {
    'rss': job_rss, 'trending': job_trending, 'inspo': job_inspo, 'events': job_events,
    'nopost': job_nopost, 'overdue': job_overdue, 'weather': job_weather,
    'digest': job_digest, 'poll': job_poll,
}


def run_job(key, manual=False):
    """Job synchron ausführen; Ergebnis und Zeitpunkt merken. Wirft nie."""
    fn = _JOB_FUNCS[key]
    started = now_berlin()
    _state['phase'] = key
    try:
        result = fn(manual=manual) or 'ok'
    except Exception as ex:
        db.session.rollback()
        result = f'Fehler: {type(ex).__name__}: {ex}'[:300]
        log.exception('Job %s fehlgeschlagen', key)
    if key == 'poll':
        _state['poll_last'] = started.isoformat()
        _state['poll_result'] = result
        if manual or result != '0 Rückmeldungen':
            # nicht bei jedem 30-s-Tick in die DB schreiben
            try:
                _set('sched_last_poll', started.isoformat())
                _set('sched_result_poll', result)
            except Exception:
                db.session.rollback()
    else:
        try:
            _set(f'sched_last_{key}', started.isoformat())
            _set(f'sched_result_{key}', result)
        except Exception:
            db.session.rollback()
    return result


def _heartbeat_path():
    return os.path.join(data_root(), 'scheduler.heartbeat')


def _touch_heartbeat():
    _state['heartbeat'] = time.time()
    try:
        with open(_heartbeat_path(), 'w') as fh:
            fh.write(datetime.utcnow().isoformat())
    except Exception:
        pass


def scheduler_alive():
    """Lebt die Schleife (in diesem oder einem anderen Worker-Prozess)?"""
    last = _state['heartbeat']
    try:
        last = max(last, os.path.getmtime(_heartbeat_path()))
    except Exception:
        pass
    return bool(last) and (time.time() - last) < HEARTBEAT_STALE_SECONDS


def tick(flask_app=None):
    """Ein Durchlauf: Poll, dann fällige Tagesjobs (außer bei Master-Pause)."""
    flask_app = flask_app or _flask_app()
    _touch_heartbeat()
    with flask_app.app_context():
        try:
            now = now_berlin()
            paused = is_master_paused()
            if job_enabled('poll') and telegram_configured():
                run_job('poll')
            if not paused:
                for key in DAILY_JOBS:
                    if job_enabled(key) and due(key, now):
                        run_job(key)
            _state['phase'] = 'wartet'
        finally:
            db.session.remove()


def _loop(flask_app):
    time.sleep(10)   # App fertig laden lassen
    log.info('MemeOS-Automatik gestartet (Tick %s s, Zeitzone Europe/Berlin)', TICK_SECONDS)
    while True:
        try:
            tick(flask_app)
        except Exception as ex:
            log.error('Scheduler-Tick fehlgeschlagen: %s', ex)
            try:
                db.session.remove()
            except Exception:
                pass
        time.sleep(TICK_SECONDS)


def _watchdog(flask_app):
    """Meldet einmal pro Vorfall, wenn die Schleife stehen bleibt (Lehre aus CityBot:
    ein Hänger sieht von außen wie Ruhe aus)."""
    time.sleep(300)
    while True:
        try:
            still = time.time() - _state['heartbeat']
            if still > WATCHDOG_ALARM_SECONDS and not _state['watchdog_reported']:
                msg = (f'MemeOS: Scheduler steht seit {still / 60:.0f} Minuten still '
                       f'(zuletzt: {_state.get("phase") or "?"}). Digest und Poll laufen gerade nicht.')
                log.error(msg)
                with flask_app.app_context():
                    try:
                        send_text(_h(msg))
                    finally:
                        db.session.remove()
                _state['watchdog_reported'] = True
            elif still <= WATCHDOG_ALARM_SECONDS and _state['watchdog_reported']:
                log.info('Scheduler läuft wieder.')
                _state['watchdog_reported'] = False
        except Exception as ex:
            log.debug('Scheduler-Wächter: %s', ex)
        time.sleep(300)


def _acquire_lock():
    """Exklusive Dateisperre – nur ein Prozess je Datenpfad fährt die Schleife."""
    try:
        import fcntl
    except ImportError:            # pragma: no cover – Windows
        return True
    path = os.path.join(data_root(), 'scheduler.lock')
    try:
        fh = open(path, 'a+')
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _state['lock_fh'] = fh
        return True
    except OSError:
        return False


def _migrate_city_geo():
    """City.lat / City.lon nachziehen (idempotent). WICHTIG: Für eine bestehende DB muss
    dieselbe Migration auch in app.py in der _col_sql-Liste stehen, weil app.py vor
    register_extensions() schon City-Abfragen ausführt (siehe integration/scheduler.md)."""
    for sql in CITY_GEO_MIGRATIONS:
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
        except Exception:
            db.session.rollback()


def init_app(flask_app):
    _state['app'] = flask_app
    if 'automation' not in flask_app.blueprints:
        flask_app.register_blueprint(bp)
    with flask_app.app_context():
        _migrate_city_geo()
    if flask_app.config.get('TESTING') or os.getenv('MEMEOS_SCHEDULER', '1') == '0':
        log.info('MemeOS-Automatik: Thread nicht gestartet (TESTING oder MEMEOS_SCHEDULER=0)')
        return
    if _state['thread'] is not None:
        return
    if not _acquire_lock():
        log.info('MemeOS-Automatik läuft bereits in einem anderen Prozess – dieser Worker bleibt passiv')
        return
    t = threading.Thread(target=_loop, args=(flask_app,), name='memeos-scheduler', daemon=True)
    t.start()
    _state['thread'] = t
    _state['started'] = True
    threading.Thread(target=_watchdog, args=(flask_app,), name='memeos-scheduler-watchdog',
                     daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# Routen
# ══════════════════════════════════════════════════════════════════════════════

def _weather_settings_payload():
    """Wetter-Schwellen mit Wert, erlaubtem Bereich, Standard und Beschriftung."""
    out = {}
    for key, (default, lo, hi, label) in WEATHER_SETTINGS.items():
        out[key] = {'value': weather_setting(key), 'default': default,
                    'min': lo, 'max': hi, 'label': label}
    return out


def _status_payload():
    jobs = []
    for j in JOBS:
        key = j['key']
        if key == 'poll':
            last = _state['poll_last'] or _get('sched_last_poll')
            result = _state['poll_result'] or _get('sched_result_poll', '')
        else:
            last = _get(f'sched_last_{key}')
            result = _get(f'sched_result_{key}', '')
        jobs.append({
            'key': key, 'label': j['label'], 'description': j['description'],
            'enabled': job_enabled(key),
            'time': job_time(key),
            'last_run': last, 'last_result': result or '',
            'needs_telegram': j['needs_telegram'], 'costs_ai': j['costs_ai'],
            'default_enabled': j['default_enabled'],
        })
    return {
        'master_pause': is_master_paused(),
        'jobs': jobs,
        'telegram_configured': telegram_configured(),
        'scheduler_alive': scheduler_alive(),
        'scheduler_started': _state['started'],
        'poll_suspended': time.time() < _state['poll_suspended_until'],
        'alert_threshold_days': _int_setting('alert_threshold_days', 3),
        'overdue_hours': OVERDUE_HOURS,
        'inspo_sort_limit': inspo_sort_limit(),
        'inspo_sort_limit_max': INSPO_SORT_MAX,
        'weather_settings': _weather_settings_payload(),
        'now_berlin': now_berlin().strftime('%Y-%m-%d %H:%M'),
    }


@bp.route('/api/automation', methods=['GET'])
@_login_required
def api_automation_get():
    return jsonify(_status_payload())


@bp.route('/api/automation', methods=['POST'])
@_login_required
def api_automation_save():
    d = request.get_json(silent=True) or {}
    if 'master_pause' in d:
        _set('master_pause', '1' if _truthy(d['master_pause']) else '0')
    for key, cfg in (d.get('jobs') or {}).items():
        if key not in JOB_BY_KEY:
            return jsonify({'error': f'Unbekannter Job: {key}'}), 400
        cfg = cfg or {}
        if 'enabled' in cfg:
            _set(f'auto_{key}_enabled', '1' if _truthy(cfg['enabled']) else '0')
        if 'time' in cfg and JOB_BY_KEY[key]['default_time']:
            t = str(cfg['time'] or '').strip()
            if not _TIME_RE.match(t):
                return jsonify({'error': f'Ungültige Uhrzeit für {key}: "{t}" (HH:MM)'}), 400
            _set(f'{key}_time', t)
    # Wetter-Schwellen: {'weather': {'weather_heat_c': 30, …}}
    for key, raw in (d.get('weather') or {}).items():
        if key not in WEATHER_SETTINGS:
            return jsonify({'error': f'Unbekannte Wetter-Einstellung: {key}'}), 400
        default, lo, hi, label = WEATHER_SETTINGS[key]
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError):
            return jsonify({'error': f'{label}: "{raw}" ist keine Zahl'}), 400
        if not (lo <= val <= hi):
            return jsonify({'error': f'{label}: erlaubt ist {lo} bis {hi}'}), 400
        _set(key, str(val))
    if 'inspo_sort_limit' in d:
        try:
            val = int(str(d['inspo_sort_limit']).strip())
        except (TypeError, ValueError):
            return jsonify({'error': 'KI-Deckel Inspiration: keine Zahl'}), 400
        if not (0 <= val <= INSPO_SORT_MAX):
            return jsonify({'error': f'KI-Deckel Inspiration: erlaubt ist 0 bis {INSPO_SORT_MAX}'}), 400
        _set('inspo_sort_limit', str(val))
    if 'alert_threshold_days' in d:
        try:
            val = int(str(d['alert_threshold_days']).strip())
        except (TypeError, ValueError):
            return jsonify({'error': 'Warnschwelle: keine Zahl'}), 400
        if not (1 <= val <= 30):
            return jsonify({'error': 'Warnschwelle: erlaubt ist 1 bis 30 Tage'}), 400
        _set('alert_threshold_days', str(val))
    payload = _status_payload()
    payload['ok'] = True
    return jsonify(payload)


@bp.route('/api/automation/run/<job>', methods=['POST'])
@_login_required
def api_automation_run(job):
    if job not in JOB_BY_KEY:
        return jsonify({'ok': False, 'error': f'Unbekannter Job: {job}'}), 404
    result = run_job(job, manual=True)
    ok = not result.startswith('Fehler')
    return jsonify({'ok': ok, 'job': job, 'result': result,
                    'last_run': _state['poll_last'] if job == 'poll' else _get(f'sched_last_{job}')}), (200 if ok else 500)


@bp.route('/api/automation/weather/<int:city_id>/preview', methods=['GET'])
@_login_required
def api_automation_weather_preview(city_id):
    city = City.query.get_or_404(city_id)
    if _offline():
        return jsonify({'error': 'offline (MEMEOS_OFFLINE=1)', 'city': city.name}), 503
    try:
        lat, lon = _ensure_coords(city)
        if lat is None:
            return jsonify({'error': f'Keine Koordinaten für {city.name} gefunden', 'city': city.name}), 404
        days = forecast_days(_fetch_forecast(lat, lon))
    except requests.RequestException as ex:
        db.session.rollback()
        return jsonify({'error': f'Open-Meteo nicht erreichbar: {ex}', 'city': city.name}), 503
    hits = evaluate_weather_rules(days)
    rules = []
    for h in hits:
        blocked = _weather_event_blocked(city, h)
        rules.append({**h, 'would_create': blocked is None, 'blocked_reason': blocked})
    return jsonify({'city': city.name, 'city_id': city.id, 'lat': lat, 'lon': lon,
                    'days': days, 'rules': rules,
                    'thresholds': [{'rule': r['name'], 'field': r['field'], 'op': r['op'],
                                    'threshold': r['threshold'], 'unit': r['unit'],
                                    'setting': r.get('setting') or r.get('gap_setting')}
                                   for r in weather_rules()],
                    'weather_settings': _weather_settings_payload()})


@bp.route('/api/automation/digest/preview', methods=['POST'])
@_login_required
def api_automation_digest_preview():
    day = today_berlin()
    posts = _digest_posts(day)
    return jsonify({'date': day.isoformat(), 'count': len(posts),
                    'telegram_configured': telegram_configured(),
                    'digest_time': job_time('digest'),
                    'posts': [_digest_preview_item(p) for p in posts]})
