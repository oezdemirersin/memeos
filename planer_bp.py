# -*- coding: utf-8 -*-
"""
planer_bp – Bundesland-Planer und Kampagnen (Phase E3)

Zweck: Die Stadtseiten sollen nicht als zusammengehörig erkennbar sein. Gleiche oder ähnliche
Memes dürfen in benachbarten Städten nicht zur selben Zeit laufen. Bisher gab es nur eine
Konfliktliste ohne Wirkung (/api/scheduling/state-conflicts); dieses Modul prüft, schlägt einen
freien Termin vor und verteilt ganze Kampagnen konfliktfrei.

Blueprint 'planer'. Registrierung in app.py (register_extensions):
    'planer_bp',              # E3 Bundesland-Planer und Kampagnen

Routen (alle login-geschützt, /api/-Routen liefern 401 JSON statt Redirect; schreibende Routen
sind über den globalen CSRF-Schutz von app.py abgesichert):
    GET  /api/planer/check?city_id=&template_id=&category=&when=ISO[&post_id=]
    GET  /api/planer/calendar-conflicts?days=14[&from=ISO]
    POST /api/planer/kampagne      {batch_id | post_ids, start_date, time, spread, dry_run}
    GET  /api/planer/settings
    POST /api/planer/settings      {radius_km, gap_template_days, gap_category_days, same_state_gap_days}
    POST /api/planer/geocode       {city_ids?, limit?}   – füllt City.lat/lon über Open-Meteo
    GET  /api/planer/neighbors?city_id=[&radius_km=]     – Nachbarliste (für die Einstellungen-Card)

Öffentliche Funktionen (auch für andere Module nutzbar):
    haversine_km(lat1, lon1, lat2, lon2)
    neighbors(city, radius_km=None)
    conflicts_for(city_id, template_id, category, when)
    next_free_slot(city_id, template_id, category, from_dt, time_of_day)
    plan_campaign(post_ids, start_dt, spread, dry_run)

Regeln:
- Kein Import von app auf Modulebene (zirkulärer Import). Dieses Modul braucht keinen einzigen
  Helfer aus app.py, deshalb gibt es hier gar keinen app-Import.
- Nachbarschaft ist reine Mathematik (Haversine über City.lat/lon), kein Netzzugriff. Nur
  /api/planer/geocode geht ins Netz und respektiert MEMEOS_OFFLINE.
- scheduled_at ist Berlin-naiv (so schreibt es das Dashboard aus <input type="datetime-local">,
  so liest es scheduler.py). Deshalb rechnet dieses Modul mit Berlin-naiver Zeit, nicht mit UTC.
"""
import os
import math
import logging
from datetime import datetime, timedelta, time as dtime

import requests
from flask import Blueprint, request, jsonify, session, redirect
from functools import wraps

from models import db, City, MemePost, MemeTemplate, AppSettings

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo('Europe/Berlin')
except Exception:                                   # pragma: no cover – Notnagel ohne tzdata
    from datetime import timezone
    BERLIN = timezone(timedelta(hours=1), 'MEZ')

log = logging.getLogger(__name__)
bp = Blueprint('planer', __name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Konstanten
# ═══════════════════════════════════════════════════════════════════════════════

# Einstellungen mit Standardwerten. Bestandsdaten laufen ohne Eintrag in AppSettings weiter.
PLANER_DEFAULTS = {
    'planer_radius_km':           60,   # bis hierhin gilt eine Stadt als Nachbarstadt
    'planer_gap_template_days':   14,   # dasselbe Template in einer Nachbarstadt
    'planer_gap_category_days':    3,   # dieselbe Kategorie in einer Nachbarstadt
    'planer_same_state_gap_days':  2,   # dasselbe Template im selben Bundesland
}

# Sammeltöpfe: auf sie darf die Kategorie-Regel nicht anspringen (Wunsch des Nutzers),
# sonst kollidiert praktisch jeder Post mit jedem.
NEUTRAL_CATEGORIES = ('allgemein', 'sonstige', '')

# Posts, die für die Planung zählen. Entwürfe und Archiviertes blockieren keinen Termin.
PLANNED_STATUSES = ('bereit', 'geplant', 'veroeffentlicht')

MAX_HORIZON_DAYS = 60           # so weit sucht next_free_slot/die Kampagne höchstens nach vorn
MAX_GEOCODE_PER_CALL = 20       # Städte je Aufruf von /api/planer/geocode
CALENDAR_MAX_DAYS = 180         # Obergrenze für calendar-conflicts?days=

EARTH_RADIUS_KM = 6371.0088     # mittlerer Erdradius (IUGG)
UNKNOWN_FAR_KM = 20000.0        # "Abstand unbekannt und kein gemeinsames Bundesland" beim Sortieren
GEOCODE_URL = 'https://geocoding-api.open-meteo.com/v1/search'

SEVERITY_ORDER = {'niedrig': 1, 'mittel': 2, 'hoch': 3}

# City.lat/lon nachziehen. app.py und scheduler.py führen dieselbe Migration aus; sie ist
# idempotent, und so bleibt der Planer auch ohne die anderen beiden Module lauffähig.
CITY_GEO_MIGRATIONS = (
    'ALTER TABLE city ADD COLUMN lat FLOAT',
    'ALTER TABLE city ADD COLUMN lon FLOAT',
)


# ═══════════════════════════════════════════════════════════════════════════════
# Login
# ═══════════════════════════════════════════════════════════════════════════════

def _login_required(f):
    @wraps(f)
    def inner(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Nicht angemeldet'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return inner


# ═══════════════════════════════════════════════════════════════════════════════
# Zeit und Einstellungen
# ═══════════════════════════════════════════════════════════════════════════════

def _now():
    """Berlin-naive Jetzt-Zeit – dieselbe Zeitrechnung, in der scheduled_at gespeichert ist."""
    try:
        return datetime.now(BERLIN).replace(tzinfo=None)
    except Exception:                               # pragma: no cover – defensiv
        return datetime.now()


def _parse_dt(value):
    """ISO-Zeit aus einer Anfrage → naives datetime (oder None)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1]
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except Exception:
                continue
        else:
            return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _parse_time(value, default=dtime(10, 0)):
    """'HH:MM' → time. Leer oder unlesbar → default."""
    if isinstance(value, dtime):
        return value
    text = (str(value or '')).strip()
    if not text:
        return default
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(text, fmt).time()
        except Exception:
            continue
    return default


def _apply_time(dt, time_of_day):
    """Uhrzeit auf den Tag von dt setzen. time_of_day darf None, 'HH:MM' oder time sein."""
    if time_of_day is None:
        return dt
    t = _parse_time(time_of_day, default=None) if not isinstance(time_of_day, dtime) else time_of_day
    if t is None:
        return dt
    return datetime.combine(dt.date(), t)


def _setting_num(key, minimum, maximum):
    """Zahl aus AppSettings mit Standardwert und Grenzen. Unlesbares fällt auf den Standard zurück."""
    default = PLANER_DEFAULTS[key]
    raw = AppSettings.get(key)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        value = float(str(raw).strip().replace(',', '.'))
    except Exception:
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    value = max(minimum, min(maximum, value))
    return round(value, 1) if key == 'planer_radius_km' else int(round(value))


def planer_settings():
    """Alle vier Werte als Zahlen – eine Quelle für Routen, Prüfung und Kampagne."""
    return {
        'planer_radius_km':           _setting_num('planer_radius_km', 1, 2000),
        'planer_gap_template_days':   _setting_num('planer_gap_template_days', 0, 365),
        'planer_gap_category_days':   _setting_num('planer_gap_category_days', 0, 365),
        'planer_same_state_gap_days': _setting_num('planer_same_state_gap_days', 0, 365),
    }


def _max_gap_days(settings):
    return max(settings['planer_gap_template_days'],
               settings['planer_gap_category_days'],
               settings['planer_same_state_gap_days'])


# ═══════════════════════════════════════════════════════════════════════════════
# Geometrie: Entfernung und Nachbarschaft
# ═══════════════════════════════════════════════════════════════════════════════

def haversine_km(lat1, lon1, lat2, lon2):
    """Entfernung zweier Punkte auf der Kugel in km. Fehlt eine Koordinate → None."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    try:
        p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
        d_phi = p2 - p1
        d_lam = math.radians(float(lon2) - float(lon1))
    except (TypeError, ValueError):
        return None
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _norm_state(state):
    return (state or '').strip().casefold()


def _norm_cat(category):
    return (category or '').strip().casefold()


class _CityInfo:
    """Schlanke Kopie einer Stadt – hält keine Session offen und ist schnell zu vergleichen."""
    __slots__ = ('id', 'name', 'state', 'lat', 'lon')

    def __init__(self, city):
        self.id = city.id
        self.name = city.name or ''
        self.state = city.state or ''
        self.lat = getattr(city, 'lat', None)
        self.lon = getattr(city, 'lon', None)

    @property
    def has_coords(self):
        return self.lat is not None and self.lon is not None


def city_distance_km(a, b):
    """Entfernung zweier Städte in km, oder None wenn einer die Koordinaten fehlen."""
    if a is None or b is None:
        return None
    return haversine_km(getattr(a, 'lat', None), getattr(a, 'lon', None),
                        getattr(b, 'lat', None), getattr(b, 'lon', None))


def _pair_info(a, b, radius_km):
    """(Entfernung|None, gleiches Bundesland?, Nachbar?)

    Ohne Koordinaten gibt es keine Entfernung – dann gilt der Rückfall 'gleiches Bundesland'."""
    distance = city_distance_km(a, b)
    state_a = _norm_state(getattr(a, 'state', ''))
    same_state = bool(state_a) and state_a == _norm_state(getattr(b, 'state', ''))
    if distance is None:
        return None, same_state, same_state
    return distance, same_state, distance <= radius_km


def _sort_distance(a, b):
    """Abstand für die Kampagnen-Reihenfolge. Ohne Koordinaten zählt das Bundesland:
    gleiches Bundesland = nah (0), sonst maximal weit."""
    if a is None or b is None or a.id == b.id:
        return 0.0
    distance = city_distance_km(a, b)
    if distance is not None:
        return distance
    state_a = _norm_state(a.state)
    if state_a and state_a == _norm_state(b.state):
        return 0.0
    return UNKNOWN_FAR_KM


def neighbors(city, radius_km=None):
    """Nachbarstädte einer Stadt als City-Objekte.

    Beide Städte mit Koordinaten → Haversine gegen radius_km.
    Fehlen einer der beiden die Koordinaten → Rückfall auf 'gleiches Bundesland'."""
    city = _as_city(city)
    if city is None:
        return []
    radius = float(radius_km) if radius_km is not None else planer_settings()['planer_radius_km']
    me = _CityInfo(city)
    out = []
    for other in City.query.filter(City.id != city.id).all():
        _distance, _same_state, is_neighbor = _pair_info(me, _CityInfo(other), radius)
        if is_neighbor:
            out.append(other)
    return out


def _as_city(city):
    if city is None:
        return None
    if isinstance(city, City):
        return city
    try:
        return City.query.get(int(city))
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Planungsstand: Städte + belegte Termine, einmal geladen
# ═══════════════════════════════════════════════════════════════════════════════

class _Entry:
    """Ein belegter Termin. 'pending' markiert Termine, die erst in dieser Kampagne entstehen."""
    __slots__ = ('post_id', 'city_id', 'template_id', 'category', 'scheduled_at', 'title', 'pending')

    def __init__(self, post_id, city_id, template_id, category, scheduled_at,
                 title='', pending=False):
        self.post_id = post_id
        self.city_id = city_id
        self.template_id = template_id
        self.category = category
        self.scheduled_at = scheduled_at
        self.title = title or ''
        self.pending = pending


class _Plan:
    """Städte und eingeplante Posts eines Zeitfensters – jede Prüfung läuft danach ohne DB."""

    def __init__(self, window=None, settings=None):
        self.settings = settings or planer_settings()
        self.cities = {c.id: _CityInfo(c) for c in City.query.all()}
        self.entries = []
        self._load(window)

    def _load(self, window):
        query = (db.session.query(MemePost.id, MemePost.city_id, MemePost.template_id,
                                  MemePost.scheduled_at, MemePost.title, MemeTemplate.category)
                 .outerjoin(MemeTemplate, MemePost.template_id == MemeTemplate.id)
                 .filter(MemePost.scheduled_at.isnot(None))
                 .filter(MemePost.status.in_(PLANNED_STATUSES)))
        if window:
            start, end = window
            if start is not None:
                query = query.filter(MemePost.scheduled_at >= start)
            if end is not None:
                query = query.filter(MemePost.scheduled_at <= end)
        for post_id, city_id, template_id, scheduled_at, title, category in query.all():
            self.entries.append(_Entry(post_id, city_id, template_id, category,
                                       scheduled_at, title))

    def add(self, entry):
        """Termin merken. Ein vorhandener Eintrag desselben Posts wird ersetzt."""
        self.entries = [e for e in self.entries if e.post_id != entry.post_id]
        self.entries.append(entry)

    def city_name(self, city_id):
        info = self.cities.get(city_id)
        return info.name if info else ''

    def city_busy_on(self, city_id, when, exclude_post_id=None):
        """Hat die Stadt an diesem Kalendertag schon einen Post? (ein Post je Stadt und Tag)"""
        day = when.date()
        for e in self.entries:
            if e.city_id != city_id or e.scheduled_at is None:
                continue
            if exclude_post_id is not None and e.post_id == exclude_post_id:
                continue
            if e.scheduled_at.date() == day:
                return e
        return None

    def conflicts(self, city_id, template_id, category, when, exclude_post_id=None):
        """Alle Konflikte eines geplanten Termins. Je betroffenem Post höchstens ein Eintrag
        (der schwerwiegendste), damit die Warnung im Dashboard lesbar bleibt."""
        me = self.cities.get(city_id)
        if me is None or when is None:
            return []
        s = self.settings
        gap_template = timedelta(days=s['planer_gap_template_days'])
        gap_category = timedelta(days=s['planer_gap_category_days'])
        gap_state = timedelta(days=s['planer_same_state_gap_days'])
        radius = s['planer_radius_km']
        widest = timedelta(days=_max_gap_days(s))
        my_cat = _norm_cat(category)
        cat_counts = my_cat not in NEUTRAL_CATEGORIES

        found = {}
        for e in self.entries:
            if e.city_id == city_id or e.scheduled_at is None:
                continue
            if exclude_post_id is not None and e.post_id == exclude_post_id:
                continue
            other = self.cities.get(e.city_id)
            if other is None:
                continue
            delta = abs(e.scheduled_at - when)
            if delta >= widest:
                continue
            distance, same_state, is_neighbor = _pair_info(me, other, radius)
            same_template = bool(template_id) and e.template_id == template_id

            hits = []
            if same_template and is_neighbor and delta < gap_template:
                hits.append(('template_nachbar', 'hoch',
                             'Gleiches Template in %s %s am %s'
                             % (other.name, _distance_text(distance, same_state),
                                _when_text(e.scheduled_at))))
            if same_template and same_state and delta < gap_state:
                hits.append(('template_bundesland', 'mittel',
                             'Gleiches Template im selben Bundesland (%s) in %s am %s'
                             % (other.state or 'ohne Angabe', other.name,
                                _when_text(e.scheduled_at))))
            if (cat_counts and is_neighbor and delta < gap_category
                    and _norm_cat(e.category) == my_cat):
                hits.append(('kategorie_nachbar', 'niedrig',
                             'Gleiche Kategorie „%s“ in %s %s am %s'
                             % (category, other.name, _distance_text(distance, same_state),
                                _when_text(e.scheduled_at))))
            if not hits:
                continue

            kind, severity, reason = max(hits, key=lambda h: SEVERITY_ORDER[h[1]])
            found[e.post_id] = {
                'city':         other.name,
                'city_id':      other.id,
                'state':        other.state or '',
                'post_id':      e.post_id,
                'title':        e.title,
                'scheduled_at': e.scheduled_at.isoformat(),
                'distance_km':  round(distance, 1) if distance is not None else None,
                'reason':       reason,
                'kind':         kind,
                'severity':     severity,
                'pending':      e.pending,
            }
        return sorted(found.values(),
                      key=lambda c: (-SEVERITY_ORDER[c['severity']], c['scheduled_at'], c['city']))


def _distance_text(distance, same_state):
    if distance is None:
        return '(gleiches Bundesland, keine Koordinaten)' if same_state else '(ohne Koordinaten)'
    return '(%d km entfernt)' % round(distance)


def _when_text(when):
    return when.strftime('%d.%m.%Y %H:%M')


def _window(from_dt, to_dt, settings):
    """Ladefenster für _Plan: Suchzeitraum plus die größte Konfliktspanne an beiden Enden."""
    pad = timedelta(days=_max_gap_days(settings) + 1)
    return (from_dt - pad, to_dt + pad)


def _post_category(post):
    """Kategorie eines Posts – sie hängt am Template, MemePost hat kein eigenes Feld."""
    template = post.template if post.template_id else None
    return (template.category if template else '') or ''


# ═══════════════════════════════════════════════════════════════════════════════
# Öffentliche Prüf- und Vorschlagsfunktionen
# ═══════════════════════════════════════════════════════════════════════════════

def conflicts_for(city_id, template_id, category=None, when=None,
                  exclude_post_id=None, plan=None):
    """Konflikte eines geplanten Termins → [{city, post_id, scheduled_at, reason, severity, …}]

    Regeln (Fenster über die Einstellungen):
      - dasselbe Template in einer Nachbarstadt   → planer_gap_template_days   (hoch)
      - dasselbe Template im selben Bundesland    → planer_same_state_gap_days (mittel)
      - dieselbe Kategorie in einer Nachbarstadt  → planer_gap_category_days   (niedrig)
    'allgemein' und 'sonstige' sind von der Kategorie-Regel ausgenommen."""
    when = when or _now()
    if plan is None:
        settings = planer_settings()
        plan = _Plan(window=_window(when, when, settings), settings=settings)
    return plan.conflicts(city_id, template_id, category, when, exclude_post_id)


def next_free_slot(city_id, template_id, category=None, from_dt=None, time_of_day=None,
                   exclude_post_id=None, plan=None, max_days=MAX_HORIZON_DAYS,
                   respect_city_day=False):
    """Erster konfliktfreier Termin ab from_dt (tageweise vorrücken, höchstens max_days).

    time_of_day ('HH:MM' oder time) setzt die Uhrzeit; der Tag von from_dt ist dann der
    früheste Tag. Ohne freien Termin innerhalb des Zeitraums → None."""
    from_dt = from_dt or _now()
    start = _apply_time(from_dt, time_of_day)
    if plan is None:
        settings = planer_settings()
        plan = _Plan(window=_window(start, start + timedelta(days=max_days), settings),
                     settings=settings)
    for step in range(max_days + 1):
        candidate = start + timedelta(days=step)
        if respect_city_day and plan.city_busy_on(city_id, candidate, exclude_post_id):
            continue
        if not plan.conflicts(city_id, template_id, category, candidate, exclude_post_id):
            return candidate
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Kampagne: mehrere Posts konfliktfrei verteilen
# ═══════════════════════════════════════════════════════════════════════════════

def _greedy_order(posts, plan):
    """Reihenfolge, in der Nachbarn möglichst weit auseinanderliegen.

    Start ist die alphabetisch erste Stadt (damit das Ergebnis reproduzierbar ist), danach
    wird immer der Post gewählt, dessen Stadt am weitesten von der zuletzt geplanten weg ist."""
    remaining = sorted(posts, key=lambda p: (plan.city_name(p.city_id).casefold(), p.id))
    if not remaining:
        return []
    ordered = [remaining.pop(0)]
    while remaining:
        last = plan.cities.get(ordered[-1].city_id)
        best_index = 0
        best_key = None
        for index, post in enumerate(remaining):
            other = plan.cities.get(post.city_id)
            key = (-_sort_distance(last, other), plan.city_name(post.city_id).casefold(), post.id)
            if best_key is None or key < best_key:
                best_key, best_index = key, index
        ordered.append(remaining.pop(best_index))
    return ordered


def plan_campaign(post_ids, start_dt, spread='auto', dry_run=True, time_of_day=None,
                  max_days=MAX_HORIZON_DAYS):
    """Posts auf Termine verteilen, sodass möglichst keine Konflikte entstehen.

    spread 'auto'   – so dicht wie möglich, jeder Post ab start_dt
           'daily'  – ein Post je Tag
           'weekly' – ein Post je Woche
    Immer gilt: ein Post je Stadt und Tag. Findet sich innerhalb von max_days kein freier
    Termin, wird der Post trotzdem eingeplant und in 'conflicts_remaining' gemeldet.
    Ohne dry_run werden scheduled_at und status 'geplant' geschrieben."""
    settings = planer_settings()
    start = _apply_time(start_dt, time_of_day)
    step_days = {'daily': 1, 'weekly': 7}.get(spread, 0)

    posts = MemePost.query.filter(MemePost.id.in_(list(post_ids or []))).all() if post_ids else []
    # Bei 'daily'/'weekly' wandert der Zeiger mit jedem Post weiter – das Ladefenster muss so
    # weit reichen, sonst fehlen dem Planer die belegten Termine am hinteren Ende.
    horizon = start + timedelta(days=max_days + step_days * len(posts))
    plan = _Plan(window=_window(start, horizon, settings), settings=settings)

    usable, skipped = [], []
    for post in posts:
        if post.city_id not in plan.cities:
            skipped.append({'post_id': post.id, 'grund': 'Stadt fehlt oder ist gelöscht'})
            continue
        usable.append(post)

    cursor = start
    planned, remaining = [], []

    for post in _greedy_order(usable, plan):
        category = _post_category(post)
        base = cursor if step_days else start
        chosen, conflicts = None, []
        for step in range(max_days + 1):
            candidate = base + timedelta(days=step)
            if plan.city_busy_on(post.city_id, candidate, exclude_post_id=post.id):
                continue
            if plan.conflicts(post.city_id, post.template_id, category, candidate,
                              exclude_post_id=post.id):
                continue
            chosen = candidate
            break
        if chosen is None:
            # Nichts Freies gefunden: trotzdem einplanen, damit kein Post verlorengeht.
            chosen = base
            conflicts = plan.conflicts(post.city_id, post.template_id, category, chosen,
                                       exclude_post_id=post.id)
            remaining.append({
                'post_id':      post.id,
                'city':         plan.city_name(post.city_id),
                'city_id':      post.city_id,
                'scheduled_at': chosen.isoformat(),
                'conflicts':    conflicts,
            })

        plan.add(_Entry(post.id, post.city_id, post.template_id, category, chosen,
                        title=post.title or '', pending=True))
        planned.append({
            'post_id':      post.id,
            'city':         plan.city_name(post.city_id),
            'city_id':      post.city_id,
            'template_id':  post.template_id,
            'title':        post.title or '',
            'scheduled_at': chosen.isoformat(),
            'conflicts':    len(conflicts),
        })
        if step_days:
            cursor = chosen + timedelta(days=step_days)

    if not dry_run and planned:
        by_id = {p.id: p for p in usable}
        for item in planned:
            post = by_id.get(item['post_id'])
            if post is None:
                continue
            post.scheduled_at = datetime.fromisoformat(item['scheduled_at'])
            post.status = 'geplant'
        db.session.commit()

    return {
        'planned':             planned,
        'conflicts_remaining': remaining,
        'dry_run':             bool(dry_run),
        'spread':              spread,
        'start':               start.isoformat(),
        'skipped':             skipped,
        'settings':            settings,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Routen
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/api/planer/check', methods=['GET'])
@_login_required
def api_planer_check():
    """Prüft einen Wunschtermin und schlägt bei Konflikten den nächsten freien vor."""
    try:
        city_id = int(request.args.get('city_id') or 0)
    except (TypeError, ValueError):
        city_id = 0
    if not city_id:
        return jsonify({'error': 'city_id fehlt'}), 400

    template_id = _int_or_none(request.args.get('template_id'))
    post_id = _int_or_none(request.args.get('post_id'))
    when = _parse_dt(request.args.get('when')) or _now()

    category = (request.args.get('category') or '').strip()
    if not category and template_id:
        template = MemeTemplate.query.get(template_id)
        category = (template.category if template else '') or ''

    settings = planer_settings()
    plan = _Plan(window=_window(when, when + timedelta(days=MAX_HORIZON_DAYS), settings),
                 settings=settings)
    if city_id not in plan.cities:
        return jsonify({'error': 'Stadt nicht gefunden'}), 404

    conflicts = plan.conflicts(city_id, template_id, category, when, exclude_post_id=post_id)
    suggestion = None
    if conflicts:
        slot = next_free_slot(city_id, template_id, category, from_dt=when,
                              exclude_post_id=post_id, plan=plan)
        suggestion = slot.isoformat() if slot else None

    # Kein Konflikt im Sinne der Nachbarschaftsregeln, aber ein nützlicher Hinweis:
    # die Stadt hat an dem Tag schon einen Post.
    busy = plan.city_busy_on(city_id, when, exclude_post_id=post_id)
    return jsonify({
        'conflicts':  conflicts,
        'suggestion': suggestion,
        'ok':         not conflicts,
        'city':       plan.city_name(city_id),
        'city_id':    city_id,
        'category':   category,
        'when':       when.isoformat(),
        'city_day':   ({'post_id': busy.post_id, 'title': busy.title,
                        'scheduled_at': busy.scheduled_at.isoformat()} if busy else None),
        'settings':   settings,
    })


@bp.route('/api/planer/calendar-conflicts', methods=['GET'])
@_login_required
def api_planer_calendar_conflicts():
    """Alle geplanten Posts der nächsten N Tage samt Konflikten, nach Datum gruppiert."""
    try:
        days = int(request.args.get('days') or 14)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(CALENDAR_MAX_DAYS, days))

    start = _parse_dt(request.args.get('from')) or _now()
    start = datetime.combine(start.date(), dtime.min)
    end = start + timedelta(days=days)

    settings = planer_settings()
    plan = _Plan(window=_window(start, end, settings), settings=settings)

    by_date, counts = {}, {'hoch': 0, 'mittel': 0, 'niedrig': 0}
    posts_total = posts_with_conflicts = 0

    for entry in sorted([e for e in plan.entries if start <= e.scheduled_at < end],
                        key=lambda e: (e.scheduled_at, e.post_id)):
        conflicts = plan.conflicts(entry.city_id, entry.template_id, entry.category,
                                   entry.scheduled_at, exclude_post_id=entry.post_id)
        severity = conflicts[0]['severity'] if conflicts else None
        if conflicts:
            posts_with_conflicts += 1
            counts[severity] += 1
        posts_total += 1
        key = entry.scheduled_at.date().isoformat()
        by_date.setdefault(key, []).append({
            'post_id':      entry.post_id,
            'city':         plan.city_name(entry.city_id),
            'city_id':      entry.city_id,
            'title':        entry.title,
            'template_id':  entry.template_id,
            'category':     entry.category or '',
            'scheduled_at': entry.scheduled_at.isoformat(),
            'conflicts':    conflicts,
            'severity':     severity,
        })

    return jsonify({
        'days':      days,
        'from':      start.isoformat(),
        'to':        end.isoformat(),
        'by_date':   by_date,
        'dates_with_conflicts': sorted(d for d, items in by_date.items()
                                       if any(i['conflicts'] for i in items)),
        'summary': {
            'posts':           posts_total,
            'with_conflicts':  posts_with_conflicts,
            'hoch':            counts['hoch'],
            'mittel':          counts['mittel'],
            'niedrig':         counts['niedrig'],
        },
        'settings': settings,
    })


@bp.route('/api/planer/kampagne', methods=['POST'])
@_login_required
def api_planer_kampagne():
    """Verteilt die Posts eines Batches (oder eine Liste) konfliktfrei auf Termine."""
    data = request.get_json(silent=True) or {}

    post_ids = data.get('post_ids') or []
    batch_id = (data.get('batch_id') or '').strip()
    if batch_id and not post_ids:
        post_ids = _post_ids_of_batch(batch_id)
        if post_ids is None:
            return jsonify({'error': 'Render-Queue nicht verfügbar'}), 503
    try:
        post_ids = [int(p) for p in post_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'post_ids muss eine Liste von Zahlen sein'}), 400
    if not post_ids:
        return jsonify({'error': 'Keine Posts angegeben (batch_id oder post_ids)'}), 400

    start = _parse_dt(data.get('start_date')) or _now()
    time_of_day = _parse_time(data.get('time'), default=dtime(10, 0))
    spread = (data.get('spread') or 'auto').strip().lower()
    if spread not in ('auto', 'daily', 'weekly'):
        spread = 'auto'
    dry_run = bool(data.get('dry_run'))

    result = plan_campaign(post_ids, start, spread=spread, dry_run=dry_run,
                           time_of_day=time_of_day)
    if batch_id:
        result['batch_id'] = batch_id
    return jsonify(result)


@bp.route('/api/planer/settings', methods=['GET'])
@_login_required
def api_planer_settings_get():
    settings = planer_settings()
    return jsonify({
        'radius_km':           settings['planer_radius_km'],
        'gap_template_days':   settings['planer_gap_template_days'],
        'gap_category_days':   settings['planer_gap_category_days'],
        'same_state_gap_days': settings['planer_same_state_gap_days'],
        'raw':                 settings,
        'cities_without_coords': _cities_without_coords_count(),
        'neutral_categories':  [c for c in NEUTRAL_CATEGORIES if c],
    })


@bp.route('/api/planer/settings', methods=['POST'])
@_login_required
def api_planer_settings_save():
    data = request.get_json(silent=True) or {}
    limits = {
        'planer_radius_km':           (1, 2000),
        'planer_gap_template_days':   (0, 365),
        'planer_gap_category_days':   (0, 365),
        'planer_same_state_gap_days': (0, 365),
    }
    aliases = {
        'radius_km':           'planer_radius_km',
        'gap_template_days':   'planer_gap_template_days',
        'gap_category_days':   'planer_gap_category_days',
        'same_state_gap_days': 'planer_same_state_gap_days',
    }
    saved = []
    for key, value in data.items():
        setting = aliases.get(key, key)
        if setting not in limits or value is None or str(value).strip() == '':
            continue
        try:
            number = float(str(value).strip().replace(',', '.'))
        except (TypeError, ValueError):
            return jsonify({'error': 'Ungültiger Wert für %s' % key}), 400
        if math.isnan(number) or math.isinf(number):
            return jsonify({'error': 'Ungültiger Wert für %s' % key}), 400
        low, high = limits[setting]
        number = max(low, min(high, number))
        AppSettings.set(setting, round(number, 1) if setting == 'planer_radius_km'
                        else int(round(number)))
        saved.append(setting)
    return jsonify({'ok': True, 'saved': saved, 'settings': planer_settings()})


@bp.route('/api/planer/neighbors', methods=['GET'])
@_login_required
def api_planer_neighbors():
    """Nachbarn einer Stadt – zeigt in den Einstellungen, was der Radius bewirkt."""
    city = _as_city(request.args.get('city_id'))
    if city is None:
        return jsonify({'error': 'Stadt nicht gefunden'}), 404
    try:
        radius = float(request.args.get('radius_km')) if request.args.get('radius_km') else None
    except (TypeError, ValueError):
        radius = None
    radius = radius if radius is not None else planer_settings()['planer_radius_km']

    me = _CityInfo(city)
    out = []
    for other in neighbors(city, radius):
        distance, same_state, _n = _pair_info(me, _CityInfo(other), radius)
        out.append({'city_id': other.id, 'city': other.name, 'state': other.state or '',
                    'distance_km': round(distance, 1) if distance is not None else None,
                    'grund': 'Entfernung' if distance is not None else 'gleiches Bundesland',
                    'same_state': same_state})
    out.sort(key=lambda n: (n['distance_km'] is None, n['distance_km'] or 0, n['city']))
    return jsonify({'city': city.name, 'city_id': city.id, 'radius_km': radius,
                    'has_coords': me.has_coords, 'neighbors': out, 'count': len(out)})


@bp.route('/api/planer/geocode', methods=['POST'])
@_login_required
def api_planer_geocode():
    """Füllt City.lat/lon über Open-Meteo Geocoding – höchstens 20 Städte je Aufruf."""
    if os.getenv('MEMEOS_OFFLINE', '') == '1':
        return jsonify({'error': 'offline (MEMEOS_OFFLINE=1)'}), 503

    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get('limit') or MAX_GEOCODE_PER_CALL)
    except (TypeError, ValueError):
        limit = MAX_GEOCODE_PER_CALL
    limit = max(1, min(MAX_GEOCODE_PER_CALL, limit))

    query = City.query.filter(db.or_(City.lat.is_(None), City.lon.is_(None)))
    city_ids = data.get('city_ids') or []
    if city_ids:
        try:
            query = City.query.filter(City.id.in_([int(c) for c in city_ids]))
        except (TypeError, ValueError):
            return jsonify({'error': 'city_ids muss eine Liste von Zahlen sein'}), 400
    todo = query.order_by(City.name).limit(limit).all()

    updated, failed = [], []
    for city in todo:
        try:
            lat, lon = _geocode(city.name)
        except Exception as ex:
            log.warning('Planer-Geocoding fehlgeschlagen (%s): %s', city.name, ex)
            failed.append({'city_id': city.id, 'city': city.name, 'error': 'Dienst nicht erreichbar'})
            continue
        if lat is None or lon is None:
            failed.append({'city_id': city.id, 'city': city.name, 'error': 'Kein Treffer'})
            continue
        city.lat, city.lon = lat, lon
        updated.append({'city_id': city.id, 'city': city.name, 'lat': lat, 'lon': lon})
    if updated:
        db.session.commit()

    return jsonify({'ok': True, 'updated': updated, 'failed': failed,
                    'remaining': _cities_without_coords_count()})


# ═══════════════════════════════════════════════════════════════════════════════
# Kleinkram
# ═══════════════════════════════════════════════════════════════════════════════

def _int_or_none(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _cities_without_coords_count():
    return City.query.filter(db.or_(City.lat.is_(None), City.lon.is_(None))).count()


def _post_ids_of_batch(batch_id):
    """Post-Ids eines Render-Batches. None, wenn die Render-Queue nicht geladen ist."""
    try:
        from render_queue import RenderTask
    except Exception as ex:                          # pragma: no cover – defensiv
        log.warning('Planer: Render-Queue nicht importierbar: %s', ex)
        return None
    rows = (RenderTask.query
            .filter(RenderTask.batch_id == batch_id, RenderTask.post_id.isnot(None))
            .order_by(RenderTask.id).all())
    seen, out = set(), []
    for task in rows:
        if task.post_id in seen:
            continue
        seen.add(task.post_id)
        out.append(task.post_id)
    return out


def _geocode(name):
    """(lat, lon) über Open-Meteo, auf Deutschland begrenzt. Nutzt scheduler._geocode, wenn da."""
    try:
        import scheduler
        fn = getattr(scheduler, '_geocode', None)
        if fn:
            return fn(name)
    except Exception:
        pass
    response = requests.get(GEOCODE_URL,
                            params={'name': name, 'count': 1, 'language': 'de',
                                    'format': 'json', 'country': 'DE', 'countryCode': 'DE'},
                            timeout=15)
    response.raise_for_status()
    for result in ((response.json() or {}).get('results') or []):
        if (result.get('country_code') or 'DE').upper() != 'DE':
            continue
        lat, lon = result.get('latitude'), result.get('longitude')
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    return None, None


def _migrate_city_geo():
    """City.lat/lon nachziehen (idempotent). app.py und scheduler.py tun dasselbe; so bleibt
    der Planer auch dann lauffähig, wenn eines der beiden Module nicht geladen wurde."""
    for sql in CITY_GEO_MIGRATIONS:
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
        except Exception:
            db.session.rollback()


# ═══════════════════════════════════════════════════════════════════════════════
# Registrierung
# ═══════════════════════════════════════════════════════════════════════════════

def init_app(flask_app):
    if 'planer' in flask_app.blueprints:
        return
    flask_app.register_blueprint(bp)
    with flask_app.app_context():
        _migrate_city_geo()
    log.info('Bundesland-Planer registriert (/api/planer/…)')
