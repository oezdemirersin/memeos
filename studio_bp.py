# -*- coding: utf-8 -*-
"""
studio_bp – Template-Studio (Phase B2): visueller Editor für pil_config eines MemeTemplate.

Blueprint 'studio'. Registrierung in app.py (register_extensions):
    import studio_bp
    studio_bp.init_app(flask_app)

Routen (alle login-geschützt, /api/-Routen liefern 401 JSON statt Redirect):
    GET  /studio/<int:template_id>              Editor-Seite (templates/studio.html)
    GET  /api/studio/<int:id>                   Template, Config, Hintergrund, Canvas, Schriften, Variablen,
                                                Städte, Stadtbild-Schlüssel, Aufkleber, Textstile, Größen
    PUT  /api/studio/<int:id>/config            {config, name?, series?, series_position?} -> ok/required_vars/errors
    POST /api/studio/<int:id>/validate          {config} -> {errors}
    POST /api/studio/<int:id>/background        multipart 'file' (png/jpg/webp) -> background_url, canvas
    POST /api/studio/<int:id>/hintergrund-farbe {background, bild_entfernen?} -> canvas ohne Bild
    POST /api/studio/<int:id>/preview           {config, city_id, values?} -> PNG
    POST /api/studio/<int:id>/preview-ai        {city_id, config?} -> {values, fit_score, reasoning, brief, image_b64}
    POST /api/studio/<int:id>/preview-grid      {config?, city_ids} -> {images:[{city_id, city, image_b64, …}]}
    POST /api/studio/<int:id>/duplicate         -> {id}
    POST /api/studio/neu                        {name, category, width, height, background, preset?} -> {id, studio_url}
    GET  /api/studio/fonts                      Schriftenliste
    POST /api/studio/fonts                      multipart 'file' (.ttf/.otf, max 5 MB) -> Schriftenliste
    GET  /api/studio/fonts/<key>/file           Schriftdatei (für @font-face in der Bühne)
    GET/POST /api/studio/assets                 Aufkleber-Bibliothek (stadtunabhängig)
    GET  /api/studio/assets/<id>/file           Aufkleber-Datei
    DELETE   /api/studio/assets/<id>[?force=1]  Aufkleber löschen (409, solange eine Vorlage ihn nutzt)
    GET/POST /api/studio/presets                Textstile
    DELETE   /api/studio/presets/<id>           Textstil löschen
    GET/POST /api/studio/cities/<city_id>/assets            Stadtbilder einer Stadt
    GET  /api/studio/cities/<city_id>/assets/<id>/file      Stadtbild-Datei
    DELETE   /api/studio/cities/<city_id>/assets/<id>       Stadtbild löschen
    GET  /api/studio/city-asset-keys            alle Stadtbild-Schlüssel + wie viele Städte sie haben

Stadtbilder: ein image-Element mit var='bild_<schlüssel>' zieht seinen Wert aus dem CityAsset
mit diesem Schlüssel. Der Renderer weiß davon nichts – er bekommt fertige values. Das Auflösen
macht stadt_bild_werte(city); render_queue muss dieselbe Funktion aufrufen (siehe deren Doc).

Kein app-Import auf Modulebene (zirkulärer Import); Helfer aus app.py werden zur Laufzeit über
_appmod() geholt. Der Renderer (memeos_render) hat keinen app-Import und darf direkt importiert werden.
"""
import base64
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   send_file, session)
from PIL import Image, ImageFont

from models import (db, City, CityAsset, CityKnowledge, MemeTemplate, StudioAsset,
                    StudioPreset, TemplateCategory, KNOWLEDGE_CATEGORIES, TEMPLATE_CAT_MAP,
                    CITY_ASSET_KINDS, STUDIO_ASSET_KINDS, CITY_ASSET_VAR_PREFIX)
import memeos_render as R

log = logging.getLogger(__name__)
bp = Blueprint('studio', __name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_IMAGE_EXTS = {'png': 'png', 'jpg': 'jpg', 'jpeg': 'jpg', 'webp': 'webp'}
_FONT_EXTS = ('.ttf', '.otf')
_FONT_MAX_BYTES = 5 * 1024 * 1024
_BG_MAX_BYTES = 25 * 1024 * 1024
_ASSET_MAX_BYTES = 10 * 1024 * 1024
_PRESET_MAX_BYTES = 8 * 1024
_GRID_MAX_CITIES = 12
_VAR_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
_KEY_RE = re.compile(r'^[a-z0-9][a-z0-9_]{0,39}$')
_KNOWLEDGE_KEYS = [k for k, _, _ in KNOWLEDGE_CATEGORIES]
_DEFAULT_CANVAS = {'width': 1080, 'height': 1350}

# Leinwandgrößen zur Auswahl beim Anlegen einer Vorlage (wird in /api/studio/<id> mitgeliefert)
CANVAS_PRESETS = [
    {'key': 'feed',      'label': 'Feed (4:5)',      'width': 1080, 'height': 1350},
    {'key': 'quadrat',   'label': 'Quadrat (1:1)',   'width': 1080, 'height': 1080},
    {'key': 'story',     'label': 'Story (9:16)',    'width': 1080, 'height': 1920},
    {'key': 'karussell', 'label': 'Karussell (1:1)', 'width': 1080, 'height': 1080},
]
_CANVAS_PRESET_MAP = {p['key']: p for p in CANVAS_PRESETS}
_CANVAS_MIN, _CANVAS_MAX = 64, 8000

# Marken-Farben der Stadt, die überall als Farbwert eingesetzt werden können
BRAND_COLOR_VARS = [
    {'key': 'brand:bg',     'label': 'Marke: Hintergrund', 'kind': 'marke', 'brand_key': 'bg'},
    {'key': 'brand:text',   'label': 'Marke: Schrift',     'kind': 'marke', 'brand_key': 'text'},
    {'key': 'brand:accent', 'label': 'Marke: Akzent',      'kind': 'marke', 'brand_key': 'accent'},
]


# ═══════════════════════════════════════════════════════════════════════════════
# Laufzeit-Zugriff auf app.py
# ═══════════════════════════════════════════════════════════════════════════════

def _appmod():
    """app.py als Modul – ohne Import auf Modulebene. Läuft app.py als __main__
    (python app.py), wird dieses Modul genommen, sonst sys.modules['app']."""
    main = sys.modules.get('__main__')
    if main is not None and hasattr(main, '_city_brand') and hasattr(main, '_DATA_ROOT'):
        return main
    if 'app' in sys.modules:
        return sys.modules['app']
    import app as appmod  # noqa
    return appmod


def _data_root():
    root = getattr(_appmod(), '_DATA_ROOT', None)
    return root or os.getenv('MEMEOS_DATA_ROOT') or os.path.join(_BASE_DIR, 'instance')


def _upload_dir():
    d = getattr(_appmod(), '_UPLOAD_DIR', None) or os.path.join(_data_root(), 'uploads')
    os.makedirs(d, exist_ok=True)
    return d


def _font_dir():
    # dieselbe Konstante wie list_fonts(), damit Uploads sofort gelistet werden
    d = getattr(R, 'USER_FONT_DIR', None) or os.path.join(_data_root(), 'fonts')
    os.makedirs(d, exist_ok=True)
    return d


def _renderer_ordner_anmelden(ordner):
    """Der Renderer löst Bild-Dateinamen nur in seinen eigenen Ordnern auf (_IMAGE_DIRS,
    Schutz gegen Pfad-Ausbrüche). Der Aufkleber-/Stadtbild-Ordner gehört dazu – sonst
    findet render() eine hochgeladene Datei nicht. Wird bei jedem Zugriff geprüft, damit
    es auch nach einem Neuladen des Renderer-Moduls stimmt."""
    try:
        dirs = getattr(R, '_IMAGE_DIRS', None)
        if not dirs:
            # Niemals selbst eine Liste anlegen: dann stünde dort nur noch dieser eine Ordner
            # und der Renderer fände seine eigenen Uploads nicht mehr.
            log.warning('Studio: memeos_render._IMAGE_DIRS fehlt – Aufkleber und Stadtbilder '
                        'werden beim Rendern nicht gefunden')
            return
        real = os.path.realpath(ordner)
        if real not in tuple(os.path.realpath(d) for d in dirs):
            R._IMAGE_DIRS = tuple(dirs) + (real,)
    except Exception as ex:                      # pragma: no cover – defensiv
        log.warning('Studio: Bildordner konnte dem Renderer nicht bekannt gemacht werden: %s', ex)


def _assets_dir():
    """<DATA_ROOT>/assets – Aufkleber UND Stadtbilder. Liegt bewusst nicht unter static/
    (wird bei jedem Deploy neu ausgecheckt), siehe project_persistent_media."""
    d = os.path.join(_data_root(), 'assets')
    os.makedirs(d, exist_ok=True)
    _renderer_ordner_anmelden(d)
    return d


def _city_brand(city):
    fn = getattr(_appmod(), '_city_brand', None)
    if fn:
        return fn(city)
    return {
        'bg': (city.brand_bg if city and city.brand_bg else '#ffffff'),
        'text': (city.brand_text_color if city and city.brand_text_color else '#000000'),
        'accent': (city.accent_color if city and city.accent_color else '#3b82f6'),
        'font': (city.brand_font if city and city.brand_font else 'Arial'),
    }


def _bg_path(template):
    """Lokaler Pfad des Hintergrunds (lädt über app._template_bg_path ggf. von preview_url nach)."""
    fn = getattr(_appmod(), '_template_bg_path', None)
    if fn:
        try:
            return fn(template)
        except Exception as ex:  # pragma: no cover – defensiv
            log.warning('Studio: _template_bg_path fehlgeschlagen: %s', ex)
    name = os.path.basename(template.preview_image or '')
    if name:
        local = os.path.join(_upload_dir(), name)
        if os.path.exists(local):
            return local
    return None


def _anthropic_key():
    return getattr(_appmod(), 'ANTHROPIC_API_KEY', '') or os.getenv('ANTHROPIC_API_KEY', '')


# ═══════════════════════════════════════════════════════════════════════════════
# Login
# ═══════════════════════════════════════════════════════════════════════════════

def studio_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Nicht angemeldet'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════════════
# Config-Helfer
# ═══════════════════════════════════════════════════════════════════════════════

def _load_config(template):
    """pil_config als Objekt; immer {'elements': [...]} (ggf. plus 'canvas'). Fehlende ids/types ergänzen."""
    try:
        cfg = json.loads(template.pil_config or '{}')
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    elements = cfg.get('elements')
    if not isinstance(elements, list):
        elements = []
    seen = set()
    out = []
    for i, el in enumerate(elements):
        if not isinstance(el, dict):
            continue
        el = dict(el)
        el.setdefault('type', 'text')
        eid = str(el.get('id') or '') or f'el{i + 1}'
        while eid in seen:
            eid = f'{eid}_{i + 1}'
        el['id'] = eid
        seen.add(eid)
        out.append(el)
    cfg['elements'] = out
    canvas = cfg.get('canvas')
    if not isinstance(canvas, dict):
        cfg.pop('canvas', None)
        return cfg
    canvas = dict(canvas)
    masse_ok = (R._is_num(canvas.get('width')) and R._is_num(canvas.get('height'))
                and float(canvas['width']) > 0 and float(canvas['height']) > 0)
    if not masse_ok:
        canvas.pop('width', None)
        canvas.pop('height', None)
    # Eine Leinwand nur mit Hintergrundfarbe ist gültig (Vorlage von Null) und darf nicht
    # verloren gehen; ohne Maße UND ohne Farbe bleibt nichts Sinnvolles übrig.
    if masse_ok or canvas.get('background') not in (None, '', False):
        cfg['canvas'] = canvas
    else:
        cfg.pop('canvas', None)
    return cfg


def ist_stadtbild_var(name):
    """True für 'bild_wappen' & Co. – Variablen, die aus den Stadtbildern kommen."""
    s = str(name or '')
    return s.startswith(CITY_ASSET_VAR_PREFIX) and len(s) > len(CITY_ASSET_VAR_PREFIX)


def _collect_vars(config, include_hidden=False, ohne_stadtbilder=False):
    """Alle Variablennamen aus text-/image-Elementen (var + {var} in festen Texten).

    ohne_stadtbilder=True lässt 'bild_*' weg – die kommen aus den Stadtbildern und sind
    optional; sie gehören deshalb nicht in required_vars (sonst meldet die Render-Queue
    für jede Stadt ohne Wappen eine fehlende Variable).
    only_if/skip_if werden bewusst nicht gesammelt: Bedingungen sind ebenfalls optional."""
    names = set()
    for el in (config or {}).get('elements') or []:
        if not isinstance(el, dict):
            continue
        if not include_hidden and R._bool(el.get('hidden')):
            continue
        t = str(el.get('type') or 'text')
        if t not in ('text', 'image'):
            continue
        var = el.get('var')
        if var and str(var).strip():
            names.add(str(var).strip())
        if t == 'text' and el.get('text'):
            names.update(_VAR_RE.findall(str(el['text'])))
    if ohne_stadtbilder:
        names = {n for n in names if not ist_stadtbild_var(n)}
    return sorted(names)


def _required_vars(config):
    """Was beim Speichern als required_vars in der Vorlage landet: var-Felder von text- und
    image-Elementen (plus {var} in festen Texten), OHNE Stadtbilder und ohne Bedingungen."""
    return _collect_vars(config, ohne_stadtbilder=True)


def _clean_label(label):
    """Emoji-Präfix der Kategorie-Labels entfernen (Studio-UI ist emojifrei)."""
    s = re.sub(r'^[^\w(]+', '', str(label or ''), flags=re.UNICODE).strip()
    return s or str(label or '')


def _stadtbild_schluessel():
    """Alle über die Städte vorkommenden Stadtbild-Schlüssel mit Anzahl der Städte.
    Grundlage für die Auswahl im Editor und für die Vollständigkeitsanzeige."""
    try:
        rows = (db.session.query(CityAsset.key, db.func.count(db.distinct(CityAsset.city_id)))
                .group_by(CityAsset.key).order_by(CityAsset.key).all())
    except Exception as ex:
        log.warning('Studio: Stadtbild-Schlüssel nicht lesbar: %s', ex)
        db.session.rollback()
        return [], 0
    try:
        gesamt = City.query.filter_by(active=True).count() or City.query.count()
    except Exception:
        gesamt = 0
    liste = [{'key': k, 'var': CITY_ASSET_VAR_PREFIX + k, 'cities': int(n or 0),
              'total_cities': int(gesamt), 'complete': bool(gesamt and int(n or 0) >= gesamt)}
             for k, n in rows]
    return liste, int(gesamt)


def _variables_for(config, stadtbild_keys=None):
    """Variablenliste für den Editor: Stadtname, Stadtwissen, Stadtbilder, Marken-Farben,
    dazu alles, was in der Config sonst noch vorkommt."""
    variables = [{'key': 'city_name', 'label': 'Stadtname', 'kind': 'city'}]
    variables += [{'key': k, 'label': _clean_label(lbl), 'kind': 'knowledge'} for k, lbl, _ in KNOWLEDGE_CATEGORIES]
    if stadtbild_keys is None:
        stadtbild_keys = _stadtbild_schluessel()[0]
    for eintrag in stadtbild_keys:
        variables.append({'key': eintrag['var'], 'label': 'Stadtbild: ' + eintrag['key'],
                          'kind': 'stadtbild', 'cities': eintrag.get('cities', 0),
                          'total_cities': eintrag.get('total_cities', 0)})
    variables += [dict(v) for v in BRAND_COLOR_VARS]
    known = {v['key'] for v in variables}
    for name in _collect_vars(config, include_hidden=True):
        if name in known:
            continue
        kind = 'stadtbild' if ist_stadtbild_var(name) else 'free'
        label = ('Stadtbild: ' + name[len(CITY_ASSET_VAR_PREFIX):]) if kind == 'stadtbild' else name
        variables.append({'key': name, 'label': label, 'kind': kind})
        known.add(name)
    return variables


def _image_size(path):
    try:
        with Image.open(path) as im:
            w, h = im.size
        return {'width': int(w), 'height': int(h)}
    except Exception as ex:
        log.warning('Studio: Bildgröße nicht lesbar (%s): %s', path, ex)
        return None


def _canvas_for(config, image_size):
    """Canvas, wie der Renderer sie sieht: config.canvas gewinnt, sonst Bildgröße.
    Ohne beides (Vorlage von Null, noch ohne Größe) die Standardgröße des Renderers –
    damit die Bühne im Editor nie ohne Maße dasteht."""
    c = (config or {}).get('canvas')
    if isinstance(c, dict) and R._is_num(c.get('width')) and R._is_num(c.get('height')):
        return {'width': int(float(c['width'])), 'height': int(float(c['height']))}
    if image_size:
        return image_size
    breite, hoehe = getattr(R, 'DEFAULT_CANVAS', (1080, 1350))
    return {'width': int(breite), 'height': int(hoehe)}


def _hintergrundfarbe(config, brand=None):
    """canvas.background, sofern der Renderer daraus eine Fläche machen kann – sonst None."""
    c = (config or {}).get('canvas')
    if not isinstance(c, dict):
        return None
    spec = c.get('background')
    if spec in (None, '', False):
        return None
    try:
        return spec if R.parse_paint(spec, brand) is not None else None
    except Exception:
        return None


def _public_fonts():
    out = []
    for f in R.list_fonts():
        out.append({'key': f.get('key'), 'label': f.get('label'), 'source': f.get('source')})
    return out


def _cities():
    rows = City.query.filter_by(active=True).order_by(City.name).all()
    if not rows:
        rows = City.query.order_by(City.name).all()
    return rows


def _city_from_payload(d):
    cid = (d or {}).get('city_id')
    city = None
    if cid not in (None, ''):
        try:
            city = City.query.get(int(cid))
        except (TypeError, ValueError):
            city = None
    if city is None:
        rows = _cities()
        city = rows[0] if rows else None
    return city


def _config_from_payload(d, template):
    cfg = (d or {}).get('config')
    if cfg is None:
        return _load_config(template)
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            return None
    if not isinstance(cfg, dict):
        return None
    return cfg


def _knowledge_value(city, category):
    now = datetime.utcnow()
    entry = (CityKnowledge.query
             .filter_by(city_id=city.id, category=category, active=True)
             .filter(db.or_(CityKnowledge.cooldown_until.is_(None), CityKnowledge.cooldown_until < now))
             .order_by(CityKnowledge.confidence.desc(), CityKnowledge.id.asc())
             .first())
    return entry.name if entry else None


def _asset_bildwert(asset):
    """Wert, den der Renderer für ein Bild-Element braucht: der Dateiname (wird in den
    erlaubten Ordnern gesucht) oder – wenn die Datei fehlt – die Cloudinary-URL."""
    name = os.path.basename((asset.filename or '').strip())
    if name:
        for ordner in (_assets_dir(), _upload_dir()):
            if os.path.isfile(os.path.join(ordner, name)):
                return name
    url = (asset.url or '').strip()
    return url or None


def stadt_bild_werte(city):
    """{'bild_<schlüssel>': <Dateiname oder URL>} für alle hinterlegten Bilder einer Stadt.

    Das Ergebnis wird unverändert unter die values gemischt, die an memeos_render.render()
    gehen; ein image-Element mit var='bild_wappen' zieht daraus seinen Wert. Fehlt ein Bild,
    fehlt auch der Eintrag – der Renderer lässt das Element dann weg (und only_if greift).
    render_queue soll genau dasselbe tun (siehe Bericht G3)."""
    if city is None:
        return {}
    try:
        rows = CityAsset.query.filter_by(city_id=city.id).order_by(CityAsset.key).all()
    except Exception as ex:
        log.warning('Studio: Stadtbilder für Stadt %s nicht lesbar: %s', getattr(city, 'id', '?'), ex)
        db.session.rollback()
        return {}
    werte = {}
    for asset in rows:
        wert = _asset_bildwert(asset)
        if wert:
            werte[CITY_ASSET_VAR_PREFIX + (asset.key or '')] = wert
    return werte


def _values_for_city(city, config, extra=None):
    """city_name = Stadt; Wissens-Variablen = bester aktiver Eintrag; Stadtbilder aus CityAsset;
    Rest Platzhalter; extra überschreibt.

    Stadtbild-Variablen bekommen bewusst KEINEN Platzhalter: ein Platzhalter wäre für den
    Renderer ein Dateiname, den es nicht gibt. Ohne Wert lässt er das Bild einfach weg."""
    values = {}
    if city:
        values['city_name'] = city.name
    stadtbilder = stadt_bild_werte(city)
    for name in _collect_vars(config, include_hidden=True):
        if name == 'city_name':
            continue
        if ist_stadtbild_var(name):
            wert = stadtbilder.get(name)
            if wert:
                values[name] = wert
            continue
        val = None
        if city and name in _KNOWLEDGE_KEYS:
            val = _knowledge_value(city, name)
        values[name] = val if val else R.sample_values_placeholder(name)
    for name, wert in stadtbilder.items():
        values.setdefault(name, wert)
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            s = str(v)
            if s.strip():
                values[str(k)] = s
    return values


def _fehlende_variablen(config, values):
    """Variablen der Vorlage, für die es für diese Stadt keinen echten Wert gibt –
    also Platzhalter statt Stadtwissen bzw. ein fehlendes Stadtbild."""
    fehlen = []
    for name in _collect_vars(config):
        if ist_stadtbild_var(name):
            if not (values.get(name) or '').strip():
                fehlen.append(name)
            continue
        wert = values.get(name)
        if wert is None or not str(wert).strip() or str(wert) == R.sample_values_placeholder(name):
            fehlen.append(name)
    return fehlen


def _render_png(template, config, values, city):
    """PNG-Bytes oder (None, (Fehlertext, Status)). Ohne Hintergrundbild wird auf
    canvas.background gerendert – eine Vorlage von Null hat kein Bild und ist trotzdem gültig."""
    brand = _city_brand(city) if city else None
    bg = _bg_path(template)
    if not bg and _hintergrundfarbe(config, brand) is None:
        return None, ('Kein Hintergrund: weder ein Bild noch eine Hintergrundfarbe. '
                      'Bitte ein Bild hochladen oder unter „Hintergrund“ eine Farbe setzen.', 400)
    try:
        png = R.render(bg, config, values, brand=brand)
    except FileNotFoundError as ex:
        return None, (f'Hintergrundbild fehlt: {ex}', 400)
    except Exception as ex:
        log.exception('Studio: Rendern fehlgeschlagen')
        return None, (f'Rendern fehlgeschlagen: {type(ex).__name__}: {str(ex)[:160]}', 500)
    return png, None


def _template_dict(t):
    return {
        'id': t.id,
        'name': t.name,
        'description': t.description or '',
        'category': t.category or 'allgemein',
        'series': t.series or '',
        'series_position': t.series_position,
        'render_type': t.render_type or 'pil',
        'required_vars': t.get_required_vars(),
        'example_text': t.example_text or '',
        'rating': t.rating or 0,
        'active': bool(t.active),
    }


def _safe_stem(name):
    stem = os.path.splitext(os.path.basename(name or ''))[0]
    stem = re.sub(r'[^A-Za-z0-9_\-]+', '_', stem).strip('_')
    return stem[:60] or 'schrift'


# ═══════════════════════════════════════════════════════════════════════════════
# Bilddateien der Bibliotheken
# ═══════════════════════════════════════════════════════════════════════════════

_UMLAUTE = {'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'ß': 'ss', 'Ä': 'ae', 'Ö': 'oe', 'Ü': 'ue'}


def _schluessel_saeubern(wert):
    """Freitext → Variablenschlüssel ('Wappen (neu)' → 'wappen_neu'). '' wenn nichts übrig bleibt."""
    s = str(wert or '').strip().lower()
    for zeichen, ersatz in _UMLAUTE.items():
        s = s.replace(zeichen, ersatz)
    s = re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    s = re.sub(r'_+', '_', s)[:40].strip('_')
    return s if _KEY_RE.match(s or '') else ''


def _bilddatei_lesen(feld='file', max_bytes=_ASSET_MAX_BYTES):
    """Hochgeladenes Bild prüfen. -> (daten, endung, größe) oder (None, Fehlertext, None)."""
    f = request.files.get(feld)
    if not f or not f.filename:
        return None, f'Keine Datei übermittelt (Feld „{feld}“)', None
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in _IMAGE_EXTS:
        return None, 'Nur PNG, JPG oder WEBP erlaubt', None
    data = f.read()
    if not data:
        return None, 'Datei ist leer', None
    if len(data) > max_bytes:
        return None, f'Datei zu groß (max. {max_bytes // (1024 * 1024)} MB)', None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            groesse = im.size
    except Exception:
        return None, 'Datei ist kein lesbares Bild', None
    return data, _IMAGE_EXTS[ext], {'width': int(groesse[0]), 'height': int(groesse[1])}


def _asset_schreiben(daten, ext, praefix):
    """Datei unter <DATA_ROOT>/assets ablegen; Rückgabe (dateiname, pfad)."""
    ordner = _assets_dir()
    stamm = _safe_stem(praefix) or 'bild'
    name = f'{stamm}_{int(time.time() * 1000)}.{ext}'
    pfad = os.path.join(ordner, name)
    with open(pfad, 'wb') as fh:
        fh.write(daten)
    return name, pfad


def _cloudinary_hochladen(pfad, ordner='memeos/studio-assets'):
    uploader = getattr(_appmod(), '_upload_cloudinary', None)
    if not uploader:
        return None
    try:
        return uploader(pfad, folder=ordner, resource_type='image')
    except Exception as ex:                      # pragma: no cover – Netz
        log.warning('Studio: Cloudinary-Upload fehlgeschlagen: %s', ex)
        return None


def _datei_aufraeumen(dateiname):
    """Datei im Asset-Ordner löschen – aber nur, wenn kein anderer Eintrag sie noch nutzt."""
    name = os.path.basename((dateiname or '').strip())
    if not name:
        return False
    try:
        if (CityAsset.query.filter_by(filename=name).count()
                or StudioAsset.query.filter_by(filename=name).count()):
            return False
    except Exception as ex:
        log.warning('Studio: Prüfung auf Mehrfachnutzung fehlgeschlagen: %s', ex)
        return False
    pfad = os.path.join(_assets_dir(), name)
    try:
        if os.path.isfile(pfad):
            os.remove(pfad)
            return True
    except OSError as ex:
        log.warning('Studio: Datei %s nicht gelöscht: %s', name, ex)
    return False


def _vorlagen_mit_datei(dateiname):
    """Vorlagen, die diese Datei fest als src eingebaut haben. Ein Aufkleber, den noch eine
    Vorlage benutzt, soll nicht versehentlich verschwinden – erst nachfragen, dann force=1."""
    name = os.path.basename((dateiname or '').strip())
    if not name:
        return []
    try:
        rows = (MemeTemplate.query
                .filter(MemeTemplate.pil_config.like('%' + name + '%'))
                .order_by(MemeTemplate.id).limit(20).all())
    except Exception as ex:
        log.warning('Studio: Verwendungssuche fehlgeschlagen: %s', ex)
        db.session.rollback()
        return []
    return [{'id': t.id, 'name': t.name or ''} for t in rows]


def _force_gesetzt():
    return str(request.args.get('force') or '').strip().lower() in ('1', 'true', 'ja')


def _asset_datei_ausliefern(asset):
    name = os.path.basename((asset.filename or '').strip())
    if name:
        pfad = os.path.join(_assets_dir(), name)
        if os.path.isfile(pfad):
            resp = send_file(pfad, conditional=True)
            resp.headers['Cache-Control'] = 'private, max-age=3600'
            return resp
    if (asset.url or '').strip():
        return redirect(asset.url.strip())
    return jsonify({'error': 'Datei nicht gefunden'}), 404


def _asset_dict(asset, datei_url):
    d = asset.to_dict()
    d['vorschau_url'] = (asset.url or '').strip() or datei_url
    d['datei_url'] = datei_url
    return d


def _studio_asset_dict(asset):
    return _asset_dict(asset, f'/api/studio/assets/{asset.id}/file')


def _city_asset_dict(asset):
    return _asset_dict(asset, f'/api/studio/cities/{asset.city_id}/assets/{asset.id}/file')


# ═══════════════════════════════════════════════════════════════════════════════
# Seite
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/studio/<int:template_id>')
@studio_login_required
def studio_page(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    return render_template('studio.html', template_id=t.id, template_name=t.name or '')


# ═══════════════════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/api/studio/<int:template_id>', methods=['GET'])
@studio_login_required
def api_studio_get(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    config = _load_config(t)
    bg = _bg_path(t)
    image_size = _image_size(bg) if bg else None
    background_url = f'/uploads/{os.path.basename(bg)}' if bg else None
    cities = _cities()
    stadtbild_keys, staedte_gesamt = _stadtbild_schluessel()
    return jsonify({
        'template': _template_dict(t),
        'config': config,
        'background_url': background_url,
        'canvas': _canvas_for(config, image_size),
        'image_size': image_size,
        'fonts': _public_fonts(),
        'variables': _variables_for(config, stadtbild_keys),
        'cities': [{'id': c.id, 'name': c.name, 'state': c.state or ''} for c in cities],
        'brand_example': _city_brand(cities[0]) if cities else _city_brand(None),
        'ai_available': bool(_anthropic_key()),
        'city_assets': stadtbild_keys,
        'city_count': staedte_gesamt,
        'assets': [_studio_asset_dict(a) for a in
                   StudioAsset.query.order_by(StudioAsset.kind, StudioAsset.id.desc()).all()],
        'presets': [p.to_dict() for p in StudioPreset.query.order_by(StudioPreset.name).all()],
        'canvas_presets': CANVAS_PRESETS,
        'asset_kinds': list(STUDIO_ASSET_KINDS),
        'city_asset_kinds': list(CITY_ASSET_KINDS),
        'brand_colors': [dict(v) for v in BRAND_COLOR_VARS],
    })


@bp.route('/api/studio/<int:template_id>/validate', methods=['POST'])
@studio_login_required
def api_studio_validate(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    config = _config_from_payload(d, t)
    if config is None:
        return jsonify({'errors': ['config muss ein Objekt sein']}), 400
    return jsonify({'errors': R.validate_config(config), 'required_vars': _required_vars(config)})


@bp.route('/api/studio/<int:template_id>/config', methods=['PUT'])
@studio_login_required
def api_studio_save(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    config = d.get('config')
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = None
    if not isinstance(config, dict):
        return jsonify({'ok': False, 'errors': ['config fehlt oder ist kein Objekt'], 'required_vars': []}), 400

    errors = R.validate_config(config)
    required = _required_vars(config)
    if errors:
        return jsonify({'ok': False, 'errors': errors, 'required_vars': required}), 400

    t.pil_config = json.dumps(config, ensure_ascii=False)
    t.render_type = 'pil'
    t.required_vars = json.dumps(required, ensure_ascii=False)

    # Kopfzeilen-Felder (optional)
    if 'name' in d and str(d.get('name') or '').strip():
        t.name = str(d['name']).strip()[:200]
    if 'series' in d:
        s = str(d.get('series') or '').strip()
        t.series = s[:200] or None
    if 'series_position' in d:
        sp = d.get('series_position')
        try:
            t.series_position = int(sp) if sp not in (None, '') else None
        except (TypeError, ValueError):
            pass
    db.session.commit()
    return jsonify({'ok': True, 'required_vars': required, 'errors': [], 'template': _template_dict(t)})


@bp.route('/api/studio/<int:template_id>/background', methods=['POST'])
@studio_login_required
def api_studio_background(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'Keine Datei übermittelt (Feld „file“)'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in _IMAGE_EXTS:
        return jsonify({'error': 'Nur PNG, JPG oder WEBP erlaubt'}), 400
    data = f.read()
    if not data:
        return jsonify({'error': 'Datei ist leer'}), 400
    if len(data) > _BG_MAX_BYTES:
        return jsonify({'error': 'Datei zu groß (max. 25 MB)'}), 400
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        return jsonify({'error': 'Datei ist kein lesbares Bild'}), 400

    name = f'template_{t.id}_bg_{int(time.time())}.{_IMAGE_EXTS[ext]}'
    path = os.path.join(_upload_dir(), name)
    with open(path, 'wb') as fh:
        fh.write(data)
    t.preview_image = name

    uploader = getattr(_appmod(), '_upload_cloudinary', None)
    cloud_url = None
    if uploader:
        try:
            cloud_url = uploader(path, folder='memeos/templates', resource_type='image')
        except Exception as ex:  # pragma: no cover
            log.warning('Studio: Cloudinary-Upload fehlgeschlagen: %s', ex)
    if cloud_url:
        t.preview_url = cloud_url
    db.session.commit()

    image_size = {'width': int(w), 'height': int(h)}
    config = _load_config(t)
    return jsonify({
        'background_url': f'/uploads/{name}',
        'canvas': _canvas_for(config, image_size),
        'image_size': image_size,
        'preview_image': name,
        'preview_url': t.preview_url or None,
        'cloudinary': bool(cloud_url),
    })


@bp.route('/api/studio/<int:template_id>/preview', methods=['POST'])
@studio_login_required
def api_studio_preview(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    config = _config_from_payload(d, t)
    if config is None:
        return jsonify({'error': 'config muss ein Objekt sein'}), 400
    city = _city_from_payload(d)
    values = _values_for_city(city, config, d.get('values'))
    png, err = _render_png(t, config, values, city)
    if err:
        return jsonify({'error': err[0]}), err[1]
    resp = send_file(io.BytesIO(png), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/api/studio/<int:template_id>/preview-ai', methods=['POST'])
@studio_login_required
def api_studio_preview_ai(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    if not _anthropic_key():
        return jsonify({'error': 'Kein Anthropic-Key hinterlegt – KI-Werte nicht verfügbar'}), 400
    d = request.get_json(silent=True) or {}
    city = _city_from_payload(d)
    if city is None:
        return jsonify({'error': 'Keine Stadt gefunden'}), 400
    config = _config_from_payload(d, t)
    if config is None:
        return jsonify({'error': 'config muss ein Objekt sein'}), 400
    if not _bg_path(t) and _hintergrundfarbe(config, _city_brand(city)) is None:
        return jsonify({'error': 'Kein Hintergrund: weder ein Bild noch eine Hintergrundfarbe.'}), 400

    fit_fn = getattr(_appmod(), '_claude_fit_and_vars', None)
    if not fit_fn:
        return jsonify({'error': 'KI-Funktion in app.py nicht gefunden'}), 500
    # Hinweis: nutzt template.required_vars aus der DB – die Seite speichert vor dem KI-Aufruf.
    res = fit_fn(city, t) or {}
    if res.get('error'):
        return jsonify({'error': res['error']}), 400

    ai_vars = res.get('vars') or {}
    values = _values_for_city(city, config)
    for k, v in ai_vars.items():
        if v not in (None, '') and str(v).strip():
            values[str(k)] = str(v)
    png, err = _render_png(t, config, values, city)
    if err:
        return jsonify({'error': err[0]}), err[1]
    return jsonify({
        'values': values,
        'fit_score': res.get('fit_score'),
        'reasoning': res.get('reasoning') or '',
        'brief': res.get('brief') or '',
        'warning': res.get('warning') or '',
        'city': {'id': city.id, 'name': city.name},
        'image_b64': base64.b64encode(png).decode('ascii'),
    })


@bp.route('/api/studio/<int:template_id>/duplicate', methods=['POST'])
@studio_login_required
def api_studio_duplicate(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    copy = MemeTemplate(
        name=(str(d.get('name') or '').strip() or f'{t.name} Kopie')[:200],
        description=t.description,
        canva_url=t.canva_url,
        render_type=t.render_type or 'pil',
        pil_config=t.pil_config or '{}',
        required_vars=t.required_vars or '[]',
        tags=t.tags or '[]',
        category=t.category or 'allgemein',
        rating=t.rating or 0,
        preview_image=t.preview_image,     # dieselbe Datei – ein Hintergrund-Upload im Duplikat legt eine neue an
        preview_url=t.preview_url,
        example_text=t.example_text,
        notes=t.notes,
        seasonal_from=t.seasonal_from,
        seasonal_to=t.seasonal_to,
        min_population=t.min_population or 0,
        active=True,
        series=t.series,
        series_position=t.series_position,
    )
    db.session.add(copy)
    db.session.commit()
    return jsonify({'id': copy.id, 'name': copy.name})


# ── Neue Vorlage von Null ───────────────────────────────────────────────────────

def _kategorie_pruefen(wert):
    """Bekannten Kategorie-Schlüssel zurückgeben, sonst 'allgemein'."""
    key = re.sub(r'[^a-z0-9_]+', '', str(wert or '').strip().lower())[:50]
    if not key:
        return 'allgemein'
    if key in TEMPLATE_CAT_MAP:
        return key
    try:
        if TemplateCategory.query.filter_by(key=key).first():
            return key
    except Exception:
        db.session.rollback()
    return 'allgemein'


def _masse_aus_payload(d):
    """(breite, höhe) oder (None, Fehlertext). preset gewinnt, sonst width/height, sonst Feed."""
    preset = _CANVAS_PRESET_MAP.get(str((d or {}).get('preset') or '').strip().lower())
    if preset:
        return (preset['width'], preset['height']), None
    breite, hoehe = (d or {}).get('width'), (d or {}).get('height')
    if breite in (None, '') and hoehe in (None, ''):
        p = _CANVAS_PRESET_MAP['feed']
        return (p['width'], p['height']), None
    try:
        breite, hoehe = int(float(breite)), int(float(hoehe))
    except (TypeError, ValueError):
        return None, 'width und height müssen Zahlen sein'
    if not (_CANVAS_MIN <= breite <= _CANVAS_MAX and _CANVAS_MIN <= hoehe <= _CANVAS_MAX):
        return None, f'width und height müssen zwischen {_CANVAS_MIN} und {_CANVAS_MAX} liegen'
    return (breite, hoehe), None


def _hintergrund_pruefen(spec):
    """Farbe/Verlauf für canvas.background prüfen. -> (wert, None) oder (None, Fehlertext).
    Leer/None ist erlaubt und bedeutet: keine Grundfarbe."""
    if spec in (None, '', False):
        return None, None
    fehler = R.validate_config({'canvas': {'background': spec}, 'elements': []})
    if fehler:
        return None, fehler[0]
    return spec, None


@bp.route('/api/studio/neu', methods=['POST'])
@studio_login_required
def api_studio_neu():
    """Leere Vorlage anlegen – ohne Hintergrundbild, allein von der Leinwandfarbe getragen."""
    d = request.get_json(silent=True) or {}
    masse, fehler = _masse_aus_payload(d)
    if fehler:
        return jsonify({'error': fehler}), 400
    hintergrund, fehler = _hintergrund_pruefen(d.get('background', '#ffffff'))
    if fehler:
        return jsonify({'error': fehler}), 400

    canvas = {'width': masse[0], 'height': masse[1]}
    if hintergrund is not None:
        canvas['background'] = hintergrund
    config = {'canvas': canvas, 'elements': []}

    t = MemeTemplate(
        name=(str(d.get('name') or '').strip() or 'Neue Vorlage')[:200],
        description=(str(d.get('description') or '').strip() or None),
        category=_kategorie_pruefen(d.get('category')),
        render_type='pil',
        pil_config=json.dumps(config, ensure_ascii=False),
        required_vars='[]',
        tags='[]',
        active=True,
    )
    series = str(d.get('series') or '').strip()
    if series:
        t.series = series[:200]
    db.session.add(t)
    db.session.commit()
    log.info('Studio: neue Vorlage %s (%s) angelegt, %dx%d', t.id, t.name, masse[0], masse[1])
    return jsonify({'id': t.id, 'studio_url': f'/studio/{t.id}',
                    'template': _template_dict(t), 'config': config,
                    'canvas': {'width': masse[0], 'height': masse[1]}}), 201


@bp.route('/api/studio/<int:template_id>/hintergrund-farbe', methods=['POST'])
@studio_login_required
def api_studio_hintergrund_farbe(template_id):
    """canvas.background setzen oder entfernen – die Vorlage kommt ohne Bild aus.
    Mit bild_entfernen=true wird zusätzlich das Hintergrundbild abgehängt (die Datei
    bleibt liegen, Duplikate der Vorlage können sie noch nutzen)."""
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    if 'background' not in d:
        return jsonify({'error': 'background fehlt (Farbe, Verlauf oder null zum Entfernen)'}), 400
    hintergrund, fehler = _hintergrund_pruefen(d.get('background'))
    if fehler:
        return jsonify({'error': fehler}), 400

    config = _load_config(t)
    canvas = config.get('canvas')
    if not isinstance(canvas, dict):
        canvas = {}
    if hintergrund is None:
        canvas.pop('background', None)
    else:
        canvas['background'] = hintergrund
    if not (R._is_num(canvas.get('width')) and R._is_num(canvas.get('height'))):
        bg = _bg_path(t)
        groesse = _image_size(bg) if bg else None
        groesse = groesse or dict(_DEFAULT_CANVAS)
        canvas['width'], canvas['height'] = groesse['width'], groesse['height']
    config['canvas'] = canvas

    if R._bool(d.get('bild_entfernen')):
        t.preview_image = None
        t.preview_url = None

    t.pil_config = json.dumps(config, ensure_ascii=False)
    t.render_type = 'pil'
    db.session.commit()
    bg = _bg_path(t)
    return jsonify({
        'ok': True,
        'config': config,
        'canvas': _canvas_for(config, _image_size(bg) if bg else None),
        'background': canvas.get('background'),
        'background_url': f'/uploads/{os.path.basename(bg)}' if bg else None,
    })


@bp.route('/api/studio/<int:template_id>/preview-grid', methods=['POST'])
@studio_login_required
def api_studio_preview_grid(template_id):
    """Dieselbe Vorlage für mehrere Städte rendern – nacheinander, nicht parallel
    (Pillow-Renderings sind speicherhungrig, siehe project_memory_oom_2026-08-11)."""
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    config = _config_from_payload(d, t)
    if config is None:
        return jsonify({'error': 'config muss ein Objekt sein'}), 400

    ids = d.get('city_ids')
    staedte = []
    if isinstance(ids, list) and ids:
        gesehen = set()
        for roh in ids[:_GRID_MAX_CITIES]:
            try:
                cid = int(roh)
            except (TypeError, ValueError):
                continue
            if cid in gesehen:
                continue
            gesehen.add(cid)
            city = City.query.get(cid)
            if city:
                staedte.append(city)
    else:
        staedte = _cities()[:_GRID_MAX_CITIES]
    if not staedte:
        return jsonify({'error': 'Keine Stadt gefunden'}), 400

    bilder, fehler_liste = [], []
    for city in staedte:
        values = _values_for_city(city, config)
        png, err = _render_png(t, config, values, city)
        if err:
            fehler_liste.append({'city_id': city.id, 'city': city.name, 'error': err[0]})
            continue
        bilder.append({
            'city_id': city.id,
            'city': city.name,
            'city_color': city.accent_color or '#3b82f6',
            'image_b64': base64.b64encode(png).decode('ascii'),
            'fehlende_variablen': _fehlende_variablen(config, values),
        })
    if not bilder and fehler_liste:
        return jsonify({'images': [], 'fehler': fehler_liste,
                        'error': fehler_liste[0]['error']}), 400
    return jsonify({'images': bilder, 'fehler': fehler_liste,
                    'max_cities': _GRID_MAX_CITIES})


# ── Aufkleber-Bibliothek (stadtunabhängig) ──────────────────────────────────────

@bp.route('/api/studio/assets', methods=['GET'])
@studio_login_required
def api_studio_assets_list():
    art = str(request.args.get('kind') or '').strip().lower()
    q = StudioAsset.query
    if art in STUDIO_ASSET_KINDS:
        q = q.filter_by(kind=art)
    rows = q.order_by(StudioAsset.kind, StudioAsset.id.desc()).all()
    return jsonify({'assets': [_studio_asset_dict(a) for a in rows],
                    'kinds': list(STUDIO_ASSET_KINDS)})


@bp.route('/api/studio/assets', methods=['POST'])
@studio_login_required
def api_studio_assets_upload():
    daten, ext, groesse = _bilddatei_lesen()
    if daten is None:
        return jsonify({'error': ext}), 400
    art = str(request.form.get('kind') or 'sticker').strip().lower()
    if art not in STUDIO_ASSET_KINDS:
        art = 'sticker'
    name = str(request.form.get('name') or '').strip()[:200]
    dateiname, pfad = _asset_schreiben(daten, ext, 'aufkleber')
    asset = StudioAsset(name=name or os.path.splitext(dateiname)[0],
                        filename=dateiname, kind=art,
                        url=_cloudinary_hochladen(pfad))
    db.session.add(asset)
    db.session.commit()
    return jsonify({'asset': _studio_asset_dict(asset), 'image_size': groesse}), 201


@bp.route('/api/studio/assets/<int:asset_id>/file', methods=['GET'])
@studio_login_required
def api_studio_asset_file(asset_id):
    return _asset_datei_ausliefern(StudioAsset.query.get_or_404(asset_id))


@bp.route('/api/studio/assets/<int:asset_id>', methods=['DELETE'])
@studio_login_required
def api_studio_assets_delete(asset_id):
    asset = StudioAsset.query.get_or_404(asset_id)
    dateiname = asset.filename
    benutzt = _vorlagen_mit_datei(dateiname)
    if benutzt and not _force_gesetzt():
        return jsonify({'error': 'Der Aufkleber steckt noch in %d Vorlage(n). '
                                 'Zum Löschen ?force=1 anhängen.' % len(benutzt),
                        'verwendet_in': benutzt}), 409
    db.session.delete(asset)
    db.session.commit()
    return jsonify({'ok': True, 'datei_geloescht': _datei_aufraeumen(dateiname),
                    'verwendet_in': benutzt})


# ── Textstile ──────────────────────────────────────────────────────────────────

def _preset_props_pruefen(props):
    """Textstil-Eigenschaften prüfen: ein Objekt, ohne Position/Typ, und als text-Element gültig."""
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except Exception:
            return None, 'props ist kein gültiges JSON'
    if not isinstance(props, dict):
        return None, 'props muss ein Objekt sein'
    verboten = ('id', 'type', 'x', 'y', 'width', 'height', 'var', 'text', 'hidden', 'locked')
    sauber = {k: v for k, v in props.items() if k not in verboten}
    if not sauber:
        return None, 'props ist leer (Position und Text gehören nicht in einen Stil)'
    if len(json.dumps(sauber, ensure_ascii=False)) > _PRESET_MAX_BYTES:
        return None, 'props ist zu groß'
    probe = dict(sauber)
    probe.update({'id': 'probe', 'type': 'text', 'text': 'Probe',
                  'x': 0, 'y': 0, 'width': 100, 'height': 100})
    fehler = R.validate_config({'elements': [probe]})
    if fehler:
        return None, fehler[0]
    return sauber, None


@bp.route('/api/studio/presets', methods=['GET'])
@studio_login_required
def api_studio_presets_list():
    return jsonify({'presets': [p.to_dict() for p in
                                StudioPreset.query.order_by(StudioPreset.name).all()]})


@bp.route('/api/studio/presets', methods=['POST'])
@studio_login_required
def api_studio_presets_create():
    d = request.get_json(silent=True) or {}
    name = str(d.get('name') or '').strip()[:200]
    if not name:
        return jsonify({'error': 'name fehlt'}), 400
    props, fehler = _preset_props_pruefen(d.get('props'))
    if fehler:
        return jsonify({'error': fehler}), 400
    preset = StudioPreset.query.filter_by(name=name).first()
    if preset:
        preset.props = json.dumps(props, ensure_ascii=False)
    else:
        preset = StudioPreset(name=name, props=json.dumps(props, ensure_ascii=False))
        db.session.add(preset)
    db.session.commit()
    return jsonify({'preset': preset.to_dict()}), 201


@bp.route('/api/studio/presets/<int:preset_id>', methods=['DELETE'])
@studio_login_required
def api_studio_presets_delete(preset_id):
    preset = StudioPreset.query.get_or_404(preset_id)
    db.session.delete(preset)
    db.session.commit()
    return jsonify({'ok': True})


# ── Stadtbilder ────────────────────────────────────────────────────────────────

@bp.route('/api/studio/cities/<int:city_id>/assets', methods=['GET'])
@studio_login_required
def api_studio_city_assets_list(city_id):
    city = City.query.get_or_404(city_id)
    rows = CityAsset.query.filter_by(city_id=city.id).order_by(CityAsset.key).all()
    return jsonify({
        'city': {'id': city.id, 'name': city.name},
        'assets': [_city_asset_dict(a) for a in rows],
        'kinds': list(CITY_ASSET_KINDS),
        'known_keys': _stadtbild_schluessel()[0],
    })


@bp.route('/api/studio/cities/<int:city_id>/assets', methods=['POST'])
@studio_login_required
def api_studio_city_assets_upload(city_id):
    city = City.query.get_or_404(city_id)
    key = _schluessel_saeubern(request.form.get('key') or request.form.get('name'))
    if not key:
        return jsonify({'error': 'key fehlt oder ergibt keinen Schlüssel '
                                 '(erlaubt sind Buchstaben, Ziffern und _)'}), 400
    daten, ext, groesse = _bilddatei_lesen()
    if daten is None:
        return jsonify({'error': ext}), 400
    art = str(request.form.get('kind') or '').strip().lower()
    if art not in CITY_ASSET_KINDS:
        art = key if key in CITY_ASSET_KINDS else 'sonstige'
    name = str(request.form.get('name') or '').strip()[:200] or key

    dateiname, pfad = _asset_schreiben(daten, ext, f'stadt{city.id}_{key}')
    url = _cloudinary_hochladen(pfad, ordner='memeos/stadtbilder')
    asset = CityAsset.query.filter_by(city_id=city.id, key=key).first()
    alt_datei = asset.filename if asset else None
    if asset:
        asset.name, asset.filename, asset.url, asset.kind = name, dateiname, url, art
    else:
        asset = CityAsset(city_id=city.id, key=key, name=name,
                          filename=dateiname, url=url, kind=art)
        db.session.add(asset)
    db.session.commit()
    if alt_datei and alt_datei != dateiname:
        _datei_aufraeumen(alt_datei)
    return jsonify({'asset': _city_asset_dict(asset), 'image_size': groesse,
                    'var': asset.var_name}), 201


@bp.route('/api/studio/cities/<int:city_id>/assets/<int:asset_id>/file', methods=['GET'])
@studio_login_required
def api_studio_city_asset_file(city_id, asset_id):
    asset = CityAsset.query.filter_by(id=asset_id, city_id=city_id).first_or_404()
    return _asset_datei_ausliefern(asset)


@bp.route('/api/studio/cities/<int:city_id>/assets/<int:asset_id>', methods=['DELETE'])
@studio_login_required
def api_studio_city_assets_delete(city_id, asset_id):
    asset = CityAsset.query.filter_by(id=asset_id, city_id=city_id).first_or_404()
    dateiname, key = asset.filename, asset.key
    db.session.delete(asset)
    db.session.commit()
    # Vorlagen mit var='bild_<key>' bleiben gültig – dort fehlt danach nur dieses eine
    # Stadtbild, und der Renderer lässt das Element für diese Stadt weg.
    return jsonify({'ok': True, 'datei_geloescht': _datei_aufraeumen(dateiname),
                    'var': CITY_ASSET_VAR_PREFIX + (key or '')})


@bp.route('/api/studio/city-asset-keys', methods=['GET'])
@studio_login_required
def api_studio_city_asset_keys():
    """Alle vorkommenden Stadtbild-Schlüssel mit Angabe, wie viele Städte sie haben –
    Auswahl im Editor und Vollständigkeitsanzeige ('Wappen: 7 von 10 Städten')."""
    liste, gesamt = _stadtbild_schluessel()
    fehlend = {}
    if liste:
        vorhanden = {}
        for key, cid in db.session.query(CityAsset.key, CityAsset.city_id).all():
            vorhanden.setdefault(key, set()).add(cid)
        staedte = _cities()
        for eintrag in liste:
            da = vorhanden.get(eintrag['key'], set())
            fehlend[eintrag['key']] = [{'id': c.id, 'name': c.name}
                                       for c in staedte if c.id not in da]
    return jsonify({'keys': liste, 'total_cities': gesamt,
                    'var_prefix': CITY_ASSET_VAR_PREFIX, 'missing': fehlend})


# ── Schriften ───────────────────────────────────────────────────────────────────

@bp.route('/api/studio/fonts', methods=['GET'])
@studio_login_required
def api_studio_fonts():
    return jsonify(_public_fonts())


@bp.route('/api/studio/fonts', methods=['POST'])
@studio_login_required
def api_studio_fonts_upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'Keine Datei übermittelt (Feld „file“)'}), 400
    lower = f.filename.lower()
    if not lower.endswith(_FONT_EXTS):
        return jsonify({'error': 'Nur .ttf oder .otf erlaubt'}), 400
    data = f.read()
    if not data:
        return jsonify({'error': 'Datei ist leer'}), 400
    if len(data) > _FONT_MAX_BYTES:
        return jsonify({'error': 'Schrift zu groß (max. 5 MB)'}), 400
    try:
        ImageFont.truetype(io.BytesIO(data), 24)
    except Exception:
        return jsonify({'error': 'Datei ist keine lesbare Schrift'}), 400
    ext = '.otf' if lower.endswith('.otf') else '.ttf'
    name = _safe_stem(f.filename) + ext
    path = os.path.join(_font_dir(), name)
    with open(path, 'wb') as fh:
        fh.write(data)
    # Cache des Renderers leeren, damit eine ersetzte Datei sofort wirkt
    try:
        with R._font_lock:
            R._font_cache.clear()
    except Exception:
        pass
    return jsonify(_public_fonts())


@bp.route('/api/studio/fonts/<key>/file', methods=['GET'])
@studio_login_required
def api_studio_font_file(key):
    for f in R.list_fonts():
        if f.get('key') == key and f.get('path') and os.path.isfile(f['path']):
            mime = 'font/otf' if f['path'].lower().endswith('.otf') else 'font/ttf'
            resp = send_file(f['path'], mimetype=mime, conditional=True)
            resp.headers['Cache-Control'] = 'private, max-age=3600'
            return resp
    return jsonify({'error': 'Schrift nicht gefunden'}), 404


# ═══════════════════════════════════════════════════════════════════════════════
# Registrierung
# ═══════════════════════════════════════════════════════════════════════════════

def init_app(flask_app):
    if 'studio' in flask_app.blueprints:
        return
    flask_app.register_blueprint(bp)
    with flask_app.app_context():
        # Neue Tabellen (city_asset, studio_asset, studio_preset). db.create_all legt nur
        # fehlende Tabellen an und rührt vorhandene nicht an; app.py hat seinen create_all
        # bereits vor register_extensions gelaufen.
        try:
            db.create_all()
        except Exception as ex:
            log.warning('Studio: Tabellen konnten nicht angelegt werden: %s', ex)
            db.session.rollback()
        try:
            _assets_dir()           # legt <DATA_ROOT>/assets an und meldet ihn dem Renderer
        except Exception as ex:
            log.warning('Studio: Asset-Ordner nicht bereit: %s', ex)
    log.info('Template-Studio registriert (/studio/<id>, Stadtbilder, Aufkleber, Textstile)')
