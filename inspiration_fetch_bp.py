"""
inspiration_fetch_bp.py – Inspiration-Abholung von Instagram über RapidAPI (Phase B7).

Blueprint 'inspiration_fetch'. Holt die Beiträge einer beobachteten Instagram-Seite
(MemoInspirationSource) über einen der abonnierten Instagram-Scraper auf RapidAPI und legt
sie als MemoInspirationPost an. Vorbild ist die Funktion inspiration_fetch aus ContentOS:
Kandidatenliste (instagram-scraper21 zuerst, danach Fallbacks) und die tolerante
Normalisierung der sehr unterschiedlichen Antwortformate.

Kein app-Import auf Modulebene (app.py registriert dieses Modul in register_extensions).

Routen (alle mit Login):
  POST /api/inspiration/sources/<id>/fetch   {limit, max_pages}
  POST /api/inspiration/fetch-all            {limit, max_pages}
  GET  /api/settings/integrations
  POST /api/settings/integrations            {rapidapi_key, inspo_fetch_limit, rapidapi_key_clear}

Einstellungen (AppSettings):
  rapidapi_key             RapidAPI-Schlüssel (Fallback: Env RAPIDAPI_KEY)
  inspo_fetch_limit        Beiträge je Abruf (Standard 50)
  rapidapi_calls_<YYYY-MM> Zähler der HTTP-Aufrufe an RapidAPI im Monat
"""
import os
import json
import time
import logging
import threading
from datetime import datetime
from functools import wraps

import requests
from flask import Blueprint, request, jsonify, session, redirect

from models import db, MemoInspirationSource, MemoInspirationPost, AppSettings

log = logging.getLogger(__name__)

bp = Blueprint('inspiration_fetch', __name__)

# ── Konstanten ─────────────────────────────────────────────────────────────────
HTTP_TIMEOUT        = 20        # Sekunden je HTTP-Aufruf
DEFAULT_FETCH_LIMIT = 50        # Beiträge je Abruf, wenn nichts eingestellt ist
TEST_LIMIT          = 3         # limit='test'
DEFAULT_MAX_PAGES   = 2         # Seiten je Abruf (jede Seite = ein RapidAPI-Aufruf)
MAX_LIMIT           = 500
MAX_PAGES_CAP       = 20
FETCH_ALL_PAUSE     = 1.0       # Sekunden Pause zwischen zwei Quellen bei "Alle abholen"

SETTING_KEY         = 'rapidapi_key'
SETTING_LIMIT       = 'inspo_fetch_limit'
SETTING_CALLS_PREFIX = 'rapidapi_calls_'

HINT_NO_KEY = 'Kein RapidAPI-Key. Einstellungen → Integrationen'

# HTTP-Status, bei denen die nächste Kandidaten-API probiert wird (nicht abonniert, nicht
# vorhanden, Kontingent erschöpft, Serverfehler). 401 = Key ungültig, ebenfalls weiter, aber
# gesondert gemerkt, damit der Hinweis am Ende stimmt.
_SKIP_STATUSES = {401, 403, 404, 429}


def _p(**fixed):
    """Baut den Parameter-Erzeuger (username, cursor) → dict für eine Kandidaten-API.
    cursor_param=None → API kennt keine Blätterfunktion."""
    user_param   = fixed.pop('user_param')
    cursor_param = fixed.pop('cursor_param', None)

    def mk(username, cursor):
        params = {user_param: username}
        params.update(fixed)
        if cursor and cursor_param:
            params[cursor_param] = cursor
        return params
    return mk


# Reihenfolge = Probierreihenfolge. Der RapidAPI-Key gilt für alle abonnierten APIs; die erste,
# die mit 200 und Beiträgen antwortet, wird für die weiteren Seiten benutzt.
# (name, host, url, params(username, cursor))
CANDIDATE_APIS = [
    # ── Abonnierte API (instagram-scraper21) — zuerst ─────────────────────────
    ('instagram-scraper21',
     'instagram-scraper21.p.rapidapi.com',
     'https://instagram-scraper21.p.rapidapi.com/api/v1/posts',
     _p(user_param='username', cursor_param='cursor', limit='100', include_captions='true')),
    # ── Fallbacks ─────────────────────────────────────────────────────────────
    ('instagram-scraper-api2',
     'instagram-scraper-api2.p.rapidapi.com',
     'https://instagram-scraper-api2.p.rapidapi.com/v1/posts',
     _p(user_param='username_or_id_or_url', cursor_param='pagination_token')),
    ('instagram-scraper-api2-v1.2',
     'instagram-scraper-api2.p.rapidapi.com',
     'https://instagram-scraper-api2.p.rapidapi.com/v1.2/posts',
     _p(user_param='username_or_id_or_url', cursor_param='pagination_token')),
    ('instagram-looter2',
     'instagram-looter2.p.rapidapi.com',
     'https://instagram-looter2.p.rapidapi.com/feed-by-username',
     _p(user_param='username', count='50')),
    ('instagram47',
     'instagram47.p.rapidapi.com',
     'https://instagram47.p.rapidapi.com/getMediaByUsername',
     _p(user_param='username')),
    ('instagram-data1',
     'instagram-data1.p.rapidapi.com',
     'https://instagram-data1.p.rapidapi.com/user/posts',
     _p(user_param='username', cursor_param='cursor')),
    ('instagram130',
     'instagram130.p.rapidapi.com',
     'https://instagram130.p.rapidapi.com/v1/posts',
     _p(user_param='username_or_id_or_url', cursor_param='cursor')),
    ('rocketapi-for-instagram',
     'rocketapi-for-instagram.p.rapidapi.com',
     'https://rocketapi-for-instagram.p.rapidapi.com/instagram/user/get_media',
     _p(user_param='username', cursor_param='cursor')),
    ('instagram-scraper3',
     'instagram-scraper3.p.rapidapi.com',
     'https://instagram-scraper3.p.rapidapi.com/user/posts',
     _p(user_param='username')),
    ('instagram-api-2022',
     'instagram-api-2022.p.rapidapi.com',
     'https://instagram-api-2022.p.rapidapi.com/api/user/posts',
     _p(user_param='username')),
]


# ── Login ──────────────────────────────────────────────────────────────────────
def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Nicht angemeldet'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ── Einstellungen ──────────────────────────────────────────────────────────────
def get_rapidapi_key():
    """Key aus AppSettings, sonst aus der Umgebung. Leer, wenn nichts gesetzt ist."""
    key = (AppSettings.get(SETTING_KEY) or '').strip()
    if key:
        return key, 'settings'
    key = (os.getenv('RAPIDAPI_KEY') or '').strip()
    if key:
        return key, 'env'
    return '', ''


def get_fetch_limit():
    return _to_int(AppSettings.get(SETTING_LIMIT), DEFAULT_FETCH_LIMIT, 1, MAX_LIMIT)


def _month_key(now=None):
    now = now or datetime.utcnow()
    return f'{SETTING_CALLS_PREFIX}{now:%Y-%m}'


def get_calls_this_month():
    return _to_int(AppSettings.get(_month_key()), 0, 0, None)


def _count_calls(n):
    """Zählt n HTTP-Aufrufe an RapidAPI im Monatszähler (AppSettings)."""
    if n <= 0:
        return
    key = _month_key()
    try:
        AppSettings.set(key, _to_int(AppSettings.get(key), 0, 0, None) + n)
    except Exception as ex:      # Zähler darf einen Abruf nie scheitern lassen
        log.warning(f'RapidAPI-Zähler {key} nicht gespeichert: {ex}')
        db.session.rollback()


def _to_int(value, default, lo=None, hi=None):
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _parse_limit(value):
    """limit aus dem Request: 'test' → 3, Zahl → 1..MAX_LIMIT, sonst Einstellung."""
    if isinstance(value, str) and value.strip().lower() == 'test':
        return TEST_LIMIT
    if value is None or value == '':
        return get_fetch_limit()
    return _to_int(value, get_fetch_limit(), 1, MAX_LIMIT)


def _parse_max_pages(value):
    if value is None or value == '':
        return DEFAULT_MAX_PAGES
    return _to_int(value, DEFAULT_MAX_PAGES, 1, MAX_PAGES_CAP)


# ── Normalisierung (reine Funktionen, ohne DB) ─────────────────────────────────
def _is_url(v):
    return isinstance(v, str) and v.startswith('http')


def _first_url(seq):
    """Erste URL aus einer Liste von Strings oder Dicts mit 'url'."""
    if not isinstance(seq, list) or not seq:
        return None
    first = seq[0]
    if isinstance(first, dict):
        u = first.get('url') or first.get('src') or first.get('videoUrl')
        return u if _is_url(u) else None
    return first if _is_url(first) else None


def _last_url(seq, fallback):
    if not isinstance(seq, list) or not seq:
        return fallback
    last = seq[-1]
    u = last.get('url') if isinstance(last, dict) else last
    return u if _is_url(u) else fallback


def extract_items(raw):
    """Zerlegt eine Rohantwort in (items, cursor, has_next). Toleriert die bekannten Formen:
    {'data': {'items': [...], 'pagination_token': ...}}, {'data': [...]}, {'items': [...]},
    {'edges': [{'node': {...}}]}, {'data': {'user': {'edge_owner_to_timeline_media': {...}}}}
    oder eine nackte Liste."""
    if isinstance(raw, list):
        return raw, None, False
    if not isinstance(raw, dict):
        return [], None, False

    data_block = raw.get('data')
    if isinstance(data_block, list):
        return data_block, None, False
    if not isinstance(data_block, dict):
        data_block = {}

    # GraphQL-Form (looter2 u. a.): data.user.edge_owner_to_timeline_media.{edges, page_info}
    timeline = ((data_block.get('user') or {}).get('edge_owner_to_timeline_media')
                if isinstance(data_block.get('user'), dict) else None)
    if isinstance(timeline, dict):
        edges = timeline.get('edges') or []
        page_info = timeline.get('page_info') or {}
        items = [e.get('node', e) for e in edges if isinstance(e, dict)]
        return items, page_info.get('end_cursor'), bool(page_info.get('has_next_page'))

    items = (data_block.get('items') or data_block.get('posts') or data_block.get('edges')
             or data_block.get('medias') or data_block.get('media')
             or raw.get('items') or raw.get('posts') or raw.get('edges')
             or raw.get('medias') or raw.get('result') or [])
    if isinstance(items, dict):        # {'result': {'edges': [...]}} u. ä.
        items = items.get('items') or items.get('edges') or items.get('posts') or []
    if not isinstance(items, list):
        items = []
    items = [it.get('node', it) if isinstance(it, dict) and 'node' in it else it for it in items]
    items = [it for it in items if isinstance(it, dict)]

    page_info = (data_block.get('page_info') or raw.get('page_info') or {})
    cursor = (data_block.get('end_cursor') or data_block.get('next_cursor')
              or data_block.get('pagination_token') or data_block.get('next_max_id')
              or raw.get('end_cursor') or raw.get('next_cursor') or raw.get('pagination_token')
              or raw.get('next_max_id') or page_info.get('end_cursor'))
    has_next = bool(data_block.get('has_next_page') or data_block.get('more_available')
                    or raw.get('has_next_page') or raw.get('more_available')
                    or page_info.get('has_next_page') or cursor)
    return items, (str(cursor) if cursor else None), has_next


def _extract_image(item):
    """→ (image_url, thumbnail_url) oder (None, None)."""
    if not isinstance(item, dict):
        return None, None
    # instagram-scraper21: image = [{url, width, height}, ...] (größtes zuerst)
    img_list = item.get('image')
    if isinstance(img_list, list) and img_list:
        best = _first_url(img_list)
        if best:
            return best, _last_url(img_list, best)
    # instagram-scraper-api2 u. a.: image_versions(2).items / candidates
    for key in ('image_versions2', 'image_versions'):
        block = item.get(key) or {}
        cand = block.get('items') or block.get('candidates') if isinstance(block, dict) else block
        if isinstance(cand, list) and cand:
            best = _first_url(cand)
            if best:
                return best, _last_url(cand, best)
    # Direkte URL-Felder
    for key in ('displayUrl', 'display_url', 'thumbnail_url', 'thumbnail_src', 'image_url',
                'imageUrl', 'thumbnail', 'url'):
        v = item.get(key)
        if _is_url(v):
            return v, v
        if isinstance(v, list):
            u = _first_url(v)
            if u:
                return u, u
    # display_resources: [{src, config_width}]
    dr = item.get('display_resources')
    if isinstance(dr, list) and dr:
        srcs = [d.get('src') for d in dr if isinstance(d, dict) and _is_url(d.get('src'))]
        if srcs:
            return srcs[-1], srcs[0]
    # Karussell: erstes Kind
    for child in _carousel_children(item)[:1]:
        if isinstance(child, dict):
            return _extract_image(child)
        if _is_url(child):
            return child, child
    return None, None


def _carousel_children(item):
    for key in ('carousel_media', 'images', 'sidecar', 'children', 'resources'):
        cm = item.get(key)
        if isinstance(cm, list) and cm:
            return cm
    sidecar = item.get('edge_sidecar_to_children')
    if isinstance(sidecar, dict):
        return [e.get('node', e) for e in (sidecar.get('edges') or []) if isinstance(e, dict)]
    return []


def _extract_video_url(item):
    v = item.get('video')
    if isinstance(v, list) and v:
        u = _first_url(v)
        if u:
            return u
    if _is_url(v):
        return v
    for key in ('video_url', 'videoUrl', 'video_versions', 'video_resources'):
        vv = item.get(key)
        if _is_url(vv):
            return vv
        if isinstance(vv, list):
            u = _first_url(vv)
            if u:
                return u
    return None


def _extract_caption(item):
    cap = item.get('caption')
    if isinstance(cap, str):
        return cap.strip()
    if isinstance(cap, dict):
        t = cap.get('text')
        if isinstance(t, str):
            return t.strip()
    edges = (item.get('edge_media_to_caption') or {}).get('edges') if isinstance(
        item.get('edge_media_to_caption'), dict) else None
    if isinstance(edges, list) and edges:
        node = edges[0].get('node') if isinstance(edges[0], dict) else None
        if isinstance(node, dict) and isinstance(node.get('text'), str):
            return node['text'].strip()
    for key in ('caption_text', 'text', 'description', 'title'):
        t = item.get(key)
        if isinstance(t, str) and t.strip():
            return t.strip()
    return ''


def _int_or_none(val):
    if isinstance(val, bool):
        return None
    if isinstance(val, dict):
        val = val.get('count')
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _extract_likes(item):
    for key in ('likeCount', 'like_count', 'likes', 'likes_count',
                'edge_media_to_like', 'edge_liked_by', 'edge_media_preview_like'):
        v = _int_or_none(item.get(key))
        if v is not None:
            return v
    return None


def _extract_comments(item):
    for key in ('commentsCount', 'comment_count', 'comments', 'comments_count',
                'edge_media_to_comment', 'edge_media_to_parent_comment'):
        v = _int_or_none(item.get(key))
        if v is not None:
            return v
    return None


def _extract_date(item):
    ts = (item.get('timestamp') or item.get('taken_at') or item.get('taken_at_timestamp')
          or item.get('created_time') or item.get('created_at') or item.get('date'))
    if ts in (None, ''):
        return None
    try:
        if isinstance(ts, str) and not ts.strip().lstrip('-').isdigit():
            s = ts.strip().replace('Z', '+00:00')
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        n = int(float(ts))
        if n > 10 ** 12:            # Millisekunden
            n //= 1000
        return datetime.utcfromtimestamp(n)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _extract_media_type(item):
    """'video' | 'carousel' | 'image' — scraper21: product_type ('clips'/'feed') + video=[…];
    api2: media_type 1/2/8; GraphQL: __typename GraphVideo/GraphSidecar; sonst Kinderzahl."""
    type_str = str(item.get('type') or item.get('product_type') or item.get('__typename')
                   or item.get('media_name') or '').lower()
    mt = item.get('media_type')
    children = _carousel_children(item)
    has_video = bool(item.get('video')) or bool(item.get('is_video')) or _is_url(item.get('video_url'))
    if (type_str in ('video', 'clips', 'reel', 'reels', 'graphvideo', 'igtv')
            or has_video or mt == 2 or str(mt).lower() == 'video'):
        return 'video'
    if (type_str in ('sidecar', 'carousel', 'graphsidecar', 'carousel_container', 'album')
            or mt == 8 or str(mt).lower() in ('carousel', 'sidecar') or len(children) > 1):
        return 'carousel'
    return 'image'


def normalize_item(item):
    """Ein Roh-Beitrag → einheitliches Dict oder None (ohne Kennung nicht verwertbar).
    Felder: code, image_url, thumbnail_url, caption, like_count, comment_count,
    post_date (datetime|None), media_type, carousel_urls (Liste), video_url."""
    if not isinstance(item, dict):
        return None
    code = str(item.get('shortCode') or item.get('code') or item.get('shortcode')
               or item.get('id') or item.get('pk') or '').strip()
    if not code:
        return None

    media_type = _extract_media_type(item)
    image_url, thumb_url = _extract_image(item)

    carousel_urls = []
    if media_type == 'carousel':
        for slide in _carousel_children(item):
            if _is_url(slide):
                carousel_urls.append(slide)
            elif isinstance(slide, dict):
                u, _ = _extract_image(slide)
                if u:
                    carousel_urls.append(u)
        if not image_url and carousel_urls:
            image_url = thumb_url = carousel_urls[0]

    return {
        'code':          code[:50],
        'image_url':     image_url,
        'thumbnail_url': thumb_url or image_url,
        'caption':       _extract_caption(item),
        'like_count':    _extract_likes(item),
        'comment_count': _extract_comments(item),
        'post_date':     _extract_date(item),
        'media_type':    media_type,
        'carousel_urls': carousel_urls,
        'video_url':     _extract_video_url(item) if media_type == 'video' else None,
    }


def normalize_posts(raw_json, api_name=''):
    """Rohantwort einer Kandidaten-API → Liste einheitlicher Beitrags-Dicts (siehe normalize_item).
    Reine Funktion ohne DB. api_name dient nur dem Logging; die Feld-Erkennung ist formattolerant
    und deckt instagram-scraper21, instagram-scraper-api2 und die GraphQL-Formen ab."""
    items, _cursor, _has_next = extract_items(raw_json)
    out = []
    dropped = 0
    for it in items:
        n = normalize_item(it)
        if n:
            out.append(n)
        else:
            dropped += 1
    if dropped:
        log.info(f'RapidAPI {api_name or "?"}: {dropped} Beitrag/Beiträge ohne Kennung übersprungen')
    return out


# ── Abruf ──────────────────────────────────────────────────────────────────────
class FetchError(Exception):
    """Fachlicher Fehler beim Abruf (wird als 400 mit Hinweis ausgeliefert)."""


def _http_get(url, headers, params):
    """Ein HTTP-Aufruf; ausgelagert, damit Tests requests.get ersetzen können."""
    return requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)


def fetch_raw_posts(username, rapidapi_key, limit, max_pages):
    """Probiert die Kandidaten-APIs nacheinander und blättert mit der ersten, die antwortet.
    → dict(items=[roh], api_name, pages, calls, errors=[...])  oder FetchError."""
    username = (username or '').strip().lstrip('@')
    if not username:
        raise FetchError('Quelle hat keinen Benutzernamen')

    items, errors, calls = [], [], 0
    working = None
    saw_empty_200 = False
    all_401 = True

    # Schritt 1: Welche API antwortet? (erste Seite)
    for name, host, url, mk_params in CANDIDATE_APIS:
        hdrs = {'x-rapidapi-key': rapidapi_key, 'x-rapidapi-host': host}
        try:
            resp = _http_get(url, hdrs, mk_params(username, None))
        except requests.RequestException as ex:
            errors.append(f'{name}: {type(ex).__name__}')
            log.warning(f'RapidAPI {name}: Verbindungsfehler {ex}')
            all_401 = False
            continue
        calls += 1
        status = getattr(resp, 'status_code', 0)
        if status != 200:
            body = ''
            try:
                body = (resp.text or '')[:300].replace('\n', ' ')
            except Exception:
                pass
            errors.append(f'{name}: HTTP {status}')
            log.warning(f'RapidAPI {name}: HTTP {status} – {body}')
            if status != 401:
                all_401 = False
            # 401/403/404/429/5xx (_SKIP_STATUSES) und auch 400/422: nächste API probieren
            continue
        all_401 = False
        try:
            raw = resp.json()
        except ValueError as ex:
            errors.append(f'{name}: keine gültige JSON-Antwort')
            log.warning(f'RapidAPI {name}: JSON-Fehler {ex} – {(getattr(resp, "text", "") or "")[:300]}')
            continue
        if isinstance(raw, dict) and raw.get('error') and not raw.get('data') and not raw.get('items'):
            errors.append(f'{name}: {str(raw.get("error"))[:120]}')
            log.warning(f'RapidAPI {name}: Fehlerantwort {str(raw)[:300]}')
            continue
        page_items, cursor, has_next = extract_items(raw)
        if not page_items:
            saw_empty_200 = True
            errors.append(f'{name}: 200 ohne Beiträge')
            log.info(f'RapidAPI {name}: 200, aber keine Beiträge – {str(raw)[:300]}')
            continue
        items.extend(page_items)
        working = (name, host, url, mk_params, cursor, has_next)
        break

    if not working:
        if all_401 and calls:
            raise FetchError('RapidAPI-Key ungültig (HTTP 401). Einstellungen → Integrationen prüfen. '
                             + '; '.join(errors[:3]))
        if saw_empty_200:
            raise FetchError(f'Keine Beiträge gefunden für @{username} (API antwortete ohne Beiträge). '
                             + '; '.join(errors[:3]))
        raise FetchError('Keine Antwort von RapidAPI. Ist ein Instagram-Scraper abonniert? '
                         + '; '.join(errors[:3]))

    # Schritt 2: weitere Seiten mit derselben API
    name, host, url, mk_params, cursor, has_next = working
    hdrs = {'x-rapidapi-key': rapidapi_key, 'x-rapidapi-host': host}
    pages = 1
    while pages < max_pages and has_next and cursor and len(items) < limit:
        try:
            resp = _http_get(url, hdrs, mk_params(username, cursor))
        except requests.RequestException as ex:
            log.warning(f'RapidAPI {name}: Seite {pages + 1} Verbindungsfehler {ex}')
            break
        calls += 1
        if getattr(resp, 'status_code', 0) != 200:
            log.warning(f'RapidAPI {name}: Seite {pages + 1} HTTP {resp.status_code}')
            break
        try:
            raw = resp.json()
        except ValueError:
            break
        page_items, cursor, has_next = extract_items(raw)
        pages += 1
        if not page_items:
            break
        items.extend(page_items)

    return {'items': items, 'api_name': name, 'pages': pages, 'calls': calls, 'errors': errors}


def fetch_source(src, limit=None, max_pages=None, rapidapi_key=None):
    """Holt Beiträge einer Quelle, legt neue MemoInspirationPost an (Dedup über instagram_code),
    setzt src.last_fetch und zählt die RapidAPI-Aufrufe. → Ergebnis-Dict oder FetchError."""
    limit     = limit or get_fetch_limit()
    max_pages = max_pages or DEFAULT_MAX_PAGES
    if not rapidapi_key:
        rapidapi_key, _ = get_rapidapi_key()
    if not rapidapi_key:
        raise FetchError(HINT_NO_KEY)
    if (src.platform or 'instagram') != 'instagram':
        raise FetchError(f'@{src.username}: Abholung gibt es nur für Instagram-Quellen')

    raw = fetch_raw_posts(src.username, rapidapi_key, limit, max_pages)
    _count_calls(raw['calls'])

    posts = normalize_posts({'items': raw['items']}, raw['api_name'])[:limit]

    new_count = skipped = no_image = 0
    seen = set()
    for n in posts:
        code = n['code']
        if code in seen:
            skipped += 1
            continue
        seen.add(code)
        existing = MemoInspirationPost.query.filter_by(instagram_code=code).first()
        if existing:
            if n['like_count'] is not None:
                existing.like_count = n['like_count']
            skipped += 1
            continue
        if not n['image_url']:
            no_image += 1
            skipped += 1
            continue
        db.session.add(MemoInspirationPost(
            source_id=src.id,
            instagram_code=code,
            image_url=n['image_url'][:1000],
            caption=n['caption'] or '',
            like_count=n['like_count'],
            post_date=n['post_date'],
            media_type=n['media_type'],
            carousel_urls=json.dumps(n['carousel_urls']) if n['carousel_urls'] else None,
            status='new',
        ))
        new_count += 1

    src.last_fetch = datetime.utcnow()
    db.session.commit()

    result = {
        'ok': True,
        'source_id': src.id,
        'username': src.username,
        'fetched': len(posts),
        'new': new_count,
        'skipped': skipped,
        'without_image': no_image,
        'api_used': raw['api_name'],
        'pages': raw['pages'],
        'calls': raw['calls'],
        'message': f'{new_count} neue Beiträge von @{src.username} ({raw["api_name"]}, '
                   f'{raw["pages"]} Seite(n))',
    }
    if raw['errors']:
        result['api_errors'] = raw['errors']
    log.info(f'Inspiration-Abruf @{src.username}: {result["message"]}, {skipped} übersprungen')
    return result


# ── Routen ─────────────────────────────────────────────────────────────────────
_fetch_lock = threading.Lock()


@bp.route('/api/inspiration/sources/<int:src_id>/fetch', methods=['POST'])
@_login_required
def api_source_fetch(src_id):
    src = MemoInspirationSource.query.get_or_404(src_id)
    d = request.get_json(silent=True) or {}
    key, _ = get_rapidapi_key()
    if not key:
        return jsonify({'ok': False, 'error': HINT_NO_KEY}), 400
    if not _fetch_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Ein Abruf läuft bereits. Bitte kurz warten.'}), 409
    try:
        result = fetch_source(src, _parse_limit(d.get('limit')), _parse_max_pages(d.get('max_pages')), key)
        return jsonify(result)
    except FetchError as ex:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(ex)}), 400
    except Exception as ex:
        db.session.rollback()
        log.exception(f'Inspiration-Abruf @{src.username} fehlgeschlagen')
        return jsonify({'ok': False, 'error': f'Abruf fehlgeschlagen: {ex}'}), 500
    finally:
        _fetch_lock.release()


@bp.route('/api/inspiration/fetch-all', methods=['POST'])
@_login_required
def api_fetch_all():
    d = request.get_json(silent=True) or {}
    key, _ = get_rapidapi_key()
    if not key:
        return jsonify({'ok': False, 'error': HINT_NO_KEY}), 400
    limit     = _parse_limit(d.get('limit'))
    max_pages = _parse_max_pages(d.get('max_pages'))
    sources = MemoInspirationSource.query.order_by(MemoInspirationSource.username).all()
    if not sources:
        return jsonify({'ok': True, 'results': [], 'sources': 0, 'new_total': 0, 'fetched_total': 0,
                        'message': 'Keine Quellen angelegt'})
    if not _fetch_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Ein Abruf läuft bereits. Bitte kurz warten.'}), 409
    results, new_total, fetched_total, failed = [], 0, 0, 0
    try:
        for i, src in enumerate(sources):
            if i and FETCH_ALL_PAUSE:
                time.sleep(FETCH_ALL_PAUSE)
            try:
                r = fetch_source(src, limit, max_pages, key)
                new_total += r['new']
                fetched_total += r['fetched']
            except FetchError as ex:
                db.session.rollback()
                failed += 1
                r = {'ok': False, 'source_id': src.id, 'username': src.username, 'error': str(ex)}
            except Exception as ex:
                db.session.rollback()
                failed += 1
                log.exception(f'Inspiration-Abruf @{src.username} fehlgeschlagen')
                r = {'ok': False, 'source_id': src.id, 'username': src.username,
                     'error': f'Abruf fehlgeschlagen: {ex}'}
            results.append(r)
    finally:
        _fetch_lock.release()
    return jsonify({
        'ok': True, 'results': results, 'sources': len(sources), 'failed': failed,
        'new_total': new_total, 'fetched_total': fetched_total,
        'message': f'{new_total} neue Beiträge aus {len(sources) - failed} von {len(sources)} Quellen',
    })


def _integrations_payload():
    key, source = get_rapidapi_key()
    return {
        'rapidapi_key_set':     bool(key),
        'rapidapi_hint':        key[-4:] if key else '',
        'rapidapi_key_source':  source,                     # 'settings' | 'env' | ''
        'inspo_fetch_limit':    get_fetch_limit(),
        'rapidapi_calls_month': get_calls_this_month(),
        'month':                datetime.utcnow().strftime('%Y-%m'),
    }


@bp.route('/api/settings/integrations', methods=['GET'])
@_login_required
def api_integrations_get():
    return jsonify(_integrations_payload())


@bp.route('/api/settings/integrations', methods=['POST'])
@_login_required
def api_integrations_post():
    d = request.get_json(silent=True) or {}
    key = d.get('rapidapi_key')
    if isinstance(key, str) and key.strip():
        AppSettings.set(SETTING_KEY, key.strip())
    elif d.get('rapidapi_key_clear'):
        AppSettings.set(SETTING_KEY, '')
    if 'inspo_fetch_limit' in d and d.get('inspo_fetch_limit') not in (None, ''):
        lim = _to_int(d.get('inspo_fetch_limit'), None, 1, MAX_LIMIT)
        if lim is None:
            return jsonify({'ok': False, 'error': 'Limit muss eine Zahl zwischen 1 und 500 sein'}), 400
        AppSettings.set(SETTING_LIMIT, lim)
    payload = _integrations_payload()
    payload['ok'] = True
    return jsonify(payload)


# ── Registrierung ──────────────────────────────────────────────────────────────
def init_app(flask_app):
    """Blueprint registrieren. Keine Threads, keine Worker – Abrufe laufen nur von Hand."""
    if 'inspiration_fetch' in flask_app.blueprints:
        return
    flask_app.register_blueprint(bp)
    log.info('inspiration_fetch registriert (RapidAPI-Abholung)')
