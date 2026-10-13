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
import meme_muster as MU

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

# Farbtoken des Stadt-Stils. Sie sind der Grund, warum dieselbe Vorlage in jeder Stadt
# anders aussieht – wer sie benutzt, baut EINE Vorlage für hundert Handschriften.
STIL_COLOR_VARS = [
    {'key': 'stil:primary',   'label': 'Stil: Hauptfarbe', 'kind': 'stil', 'stil_key': 'primary'},
    {'key': 'stil:secondary', 'label': 'Stil: Zweitfarbe', 'kind': 'stil', 'stil_key': 'secondary'},
    {'key': 'stil:accent',    'label': 'Stil: Akzentfarbe', 'kind': 'stil', 'stil_key': 'accent'},
    {'key': 'stil:dark',      'label': 'Stil: Dunkelton', 'kind': 'stil', 'stil_key': 'dark'},
    {'key': 'stil:light',     'label': 'Stil: Hellton', 'kind': 'stil', 'stil_key': 'light'},
    {'key': 'stil:auto',      'label': 'Stil: automatisch lesbar (Schwarz oder Weiß)',
     'kind': 'stil', 'stil_key': 'auto'},
]

# Schrift-Token des Stadt-Stils (als Wert im Feld „font“ eines Text-Elements)
STIL_FONT_VARS = [
    {'key': 'stil:headline', 'label': 'Stil: Auszeichnungsschrift', 'kind': 'stil',
     'stil_key': 'headline'},
    {'key': 'stil:body',     'label': 'Stil: Grundschrift', 'kind': 'stil',
     'stil_key': 'body'},
]

# Felder des Stadt-Stils, die über /api/stil/stadt/<id> einzeln gesetzt werden können.
# 'farbe' wird über den Renderer geprüft, 'wahl' gegen die erlaubten Werte, 'schrift'
# gegen die Schriftbibliothek.
STIL_FELDER = (
    ('primary',   'farbe',   'Hauptfarbe'),
    ('secondary', 'farbe',   'Zweitfarbe'),
    ('accent',    'farbe',   'Akzentfarbe'),
    ('dark',      'farbe',   'Dunkelton'),
    ('light',     'farbe',   'Hellton'),
    ('font_headline', 'schrift', 'Auszeichnungsschrift'),
    ('font_body',     'schrift', 'Grundschrift'),
    ('schreibweise',  'wahl',  'Schreibweise'),
    ('kasten',        'wahl',  'Textkasten'),
    ('kontur',        'wahl',  'Kontur'),
    ('wasserzeichen_ecke', 'wahl', 'Ecke des Wasserzeichens'),
    ('radius',   'zahl',    'Eckenrundung'),
    ('schatten', 'ja_nein', 'Schatten'),
    ('paket',    'paket',   'Stil-Paket'),
)
_STIL_FELD_MAP = {name: (art, label) for name, art, label in STIL_FELDER}

# Werte, die den optischen Kern eines Stils ausmachen – daran wird verglichen, ob zwei
# Städte gleich aussehen (siehe /api/stil/aehnlich und die Prüfung „alle Städte gleich“).
_STIL_KERN = ('primary', 'secondary', 'accent', 'dark', 'light', 'headline', 'body',
              'schreibweise', 'kasten', 'radius', 'kontur', 'schatten')

# Grenzen der Vorlagen-Prüfung. Die Kontrastwerte sind die der WCAG: unter 4,5:1 ist Text
# schwer zu lesen, unter 3:1 auch in großer Schrift nicht mehr zumutbar.
_KONTRAST_FEHLER, _KONTRAST_HINWEIS = 3.0, 4.5
_PRUEF_MAX_STAEDTE = 200
_PRUEF_MAX_BEFUNDE = 200
_PRUEF_MAX_JE_ELEMENT = 3
_AEHNLICH_MAX_PAARE = 200


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


def _stadtbild_urls():
    """{city_id: {'bild_<key>': '/api/studio/cities/<cid>/assets/<aid>/file'}} für alle Städte.
    Die Bühne des Editors zeigt damit das echte Stadtbild der Vorschau-Stadt statt eines
    Platzhalters; ohne Bild greift sie auf das Ersatzbild (fallback) des Elements zurück."""
    out = {}
    try:
        rows = db.session.query(CityAsset.id, CityAsset.city_id, CityAsset.key).all()
    except Exception as ex:
        log.warning('Studio: Stadtbild-URLs nicht lesbar: %s', ex)
        db.session.rollback()
        return out
    for aid, cid, key in rows:
        if not key:
            continue
        out.setdefault(str(cid), {})[CITY_ASSET_VAR_PREFIX + key] = \
            f'/api/studio/cities/{cid}/assets/{aid}/file'
    return out


def _variables_for(config, stadtbild_keys=None):
    """Variablenliste für den Editor: Stadtname, Stadtwissen, Stadtbilder, Marken-Farben,
    dazu alles, was in der Config sonst noch vorkommt."""
    variables = [{'key': 'city_name',   'label': 'Stadtname', 'kind': 'city'},
                 {'key': 'city_handle', 'label': 'Instagram-Name der Stadt (mit @)',
                  'kind': 'city'},
                 {'key': 'city_state',  'label': 'Bundesland', 'kind': 'city'}]
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


# Klarnamen der Rollen. Die Rollen selbst (mit ihren Vorgaben) stehen im Renderer –
# hier steht nur, wie sie in der Oberfläche heißen.
_ROLLEN_LABELS = {
    'ueberschrift':  'Überschrift',
    'pointe':        'Pointe',
    'memezeile':     'Meme-Zeile (hell mit Kontur)',
    'unterzeile':    'Unterzeile',
    'ort':           'Ort',
    'klein':         'Kleingedrucktes',
    'wasserzeichen': 'Wasserzeichen',
    'frei':          'Frei (keine Vorgaben)',
}


# Die Gestaltungswerte einer Rolle, die der Editor für seine Vorschau braucht. Sie gehen
# mit über die Leitung, damit die Tabelle nur an EINER Stelle steht: in memeos_render.ROLLEN.
# Steht sie doppelt, laufen Vorschau und fertiges Bild irgendwann auseinander.
_ROLLEN_FELDER = ('schrift', 'farbe', 'max', 'min', 'gross', 'kasten', 'kontur',
                  'schatten', 'opacity')


def _rollen_liste():
    """Die Rollentabelle für den Editor: [{'key','label','beschreibung', …Werte}, …].

    Eine Rolle ist EINE Entscheidung statt fünfzehn Einzelwerten – Schrift, Farbe, Größe,
    Kasten und Kontur kommen dann aus dem Stil der Stadt. Jeder am Element gesetzte Wert
    schlägt die Rolle weiterhin."""
    rollen = getattr(R, 'ROLLEN', None)
    if not isinstance(rollen, dict):
        return []
    out = []
    for key, wert in rollen.items():
        wert = wert or {}
        eintrag = {'key': key,
                   'label': _ROLLEN_LABELS.get(key, key),
                   'beschreibung': wert.get('beschreibung', '')}
        for feld in _ROLLEN_FELDER:
            eintrag[feld] = wert.get(feld)
        out.append(eintrag)
    return out


def _stil_token_liste():
    """Die stil:*-Farbtoken mit Klarnamen – dieselbe Form wie BRAND_COLOR_VARS."""
    return [dict(v) for v in STIL_COLOR_VARS]


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


def stadt_variablen(city):
    """{'city_name', 'city_handle', 'city_state'} – die Werte, die direkt aus der Stadt-Zeile
    kommen. Leere Werte fehlen (eine Stadt ohne Instagram-Namen bekommt keinen erfundenen).

    Kommt aus app._stadt_variablen, damit Vorschau und Massenlauf garantiert dasselbe
    einsetzen; ohne app-Modul dieselbe Rechnung noch einmal hier."""
    if city is None:
        return {}
    fn = getattr(_appmod(), '_stadt_variablen', None)
    if callable(fn):
        try:
            werte = fn(city)
            if isinstance(werte, dict):
                return dict(werte)
        except Exception as ex:                      # pragma: no cover – defensiv
            log.warning('Studio: Stadt-Variablen nicht lesbar: %s', ex)
    handle = str(getattr(city, 'instagram_handle', '') or '').strip().rstrip('/')
    if '/' in handle:
        handle = handle.rsplit('/', 1)[-1]
    handle = handle.lstrip('@').strip()
    werte = {'city_name': str(city.name or ''),
             'city_handle': ('@' + handle) if handle else '',
             'city_state': str(getattr(city, 'state', '') or '')}
    return {k: v for k, v in werte.items() if v}


def _values_for_city(city, config, extra=None):
    """Stadt-Variablen (city_name, city_handle, city_state) = Werte der Stadt;
    Wissens-Variablen = bester aktiver Eintrag; Stadtbilder aus CityAsset;
    Rest Platzhalter; extra überschreibt.

    Stadtbild-Variablen bekommen bewusst KEINEN Platzhalter: ein Platzhalter wäre für den
    Renderer ein Dateiname, den es nicht gibt. Ohne Wert lässt er das Bild einfach weg."""
    values = {}
    stadtwerte = stadt_variablen(city)
    values.update(stadtwerte)
    stadtbilder = stadt_bild_werte(city)
    for name in _collect_vars(config, include_hidden=True):
        if name in stadtwerte:
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
        'city_asset_urls': _stadtbild_urls(),
        'city_count': staedte_gesamt,
        'assets': [_studio_asset_dict(a) for a in
                   StudioAsset.query.order_by(StudioAsset.kind, StudioAsset.id.desc()).all()],
        'presets': [p.to_dict() for p in StudioPreset.query.order_by(StudioPreset.name).all()],
        'canvas_presets': CANVAS_PRESETS,
        'asset_kinds': list(STUDIO_ASSET_KINDS),
        'city_asset_kinds': list(CITY_ASSET_KINDS),
        'brand_colors': [dict(v) for v in BRAND_COLOR_VARS],
        # Stadt-Stil: Rollen, der Stil der ersten Vorschau-Stadt und die stil:*-Token.
        # Damit kann der Editor Rollen anbieten und die Farbtoken benennen.
        'rollen': _rollen_liste(),
        'stil': _city_brand(cities[0]) if cities else _city_brand(None),
        'stil_tokens': _stil_token_liste(),
        'stil_fonts': [dict(v) for v in STIL_FONT_VARS],
        'stil_pakete': _stil_kurzliste(),
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
    """Leere Vorlage anlegen – ohne Hintergrundbild, allein von der Leinwandfarbe getragen.

    Mit `muster` wird die Vorlage nicht leer, sondern fertig aufgebaut: Elemente, Rollen und
    Variablen des Musters stehen dann schon drin (siehe meme_muster)."""
    d = request.get_json(silent=True) or {}
    muster = str(d.get('muster') or '').strip().lower()
    if muster and not MU.muster_holen(muster):
        return jsonify({'error': f'Unbekanntes Muster „{muster}“'}), 400

    # Ein Muster bringt sein Format mit; eigene Maße im Rumpf schlagen es trotzdem.
    if muster and d.get('preset') in (None, '') and d.get('width') in (None, '') \
            and d.get('height') in (None, ''):
        masse, fehler = MU.muster_masse(muster), None
    else:
        masse, fehler = _masse_aus_payload(d)
    if fehler:
        return jsonify({'error': fehler}), 400

    if muster:
        config = MU.muster_config(muster, masse[0], masse[1])
        # Eine eigene Hintergrundangabe schlägt die des Musters.
        if 'background' in d:
            hintergrund, fehler = _hintergrund_pruefen(d.get('background'))
            if fehler:
                return jsonify({'error': fehler}), 400
            config['canvas'].pop('background', None)
            if hintergrund is not None:
                config['canvas']['background'] = hintergrund
        vars_ = _required_vars(config)
    else:
        hintergrund, fehler = _hintergrund_pruefen(d.get('background', '#ffffff'))
        if fehler:
            return jsonify({'error': fehler}), 400
        canvas = {'width': masse[0], 'height': masse[1]}
        if hintergrund is not None:
            canvas['background'] = hintergrund
        config = {'canvas': canvas, 'elements': []}
        vars_ = []

    fehler_config = R.validate_config(config)
    if fehler_config:                            # pragma: no cover – nur bei kaputtem Muster
        log.error('Studio: Muster %s ergibt eine ungültige Config: %s', muster, fehler_config)
        return jsonify({'error': 'Muster ergibt keine gültige Vorlage: ' + fehler_config[0]}), 500

    vorgabe_name = (MU.muster_holen(muster) or {}).get('name') if muster else None
    t = MemeTemplate(
        name=(str(d.get('name') or '').strip() or vorgabe_name or 'Neue Vorlage')[:200],
        description=(str(d.get('description') or '').strip() or None),
        category=_kategorie_pruefen(d.get('category')),
        render_type='pil',
        pil_config=json.dumps(config, ensure_ascii=False),
        required_vars=json.dumps(vars_, ensure_ascii=False),
        tags='[]',
        active=True,
    )
    series = str(d.get('series') or '').strip()
    if series:
        t.series = series[:200]
    db.session.add(t)
    db.session.commit()
    log.info('Studio: neue Vorlage %s (%s) angelegt, %dx%d%s', t.id, t.name,
             masse[0], masse[1], f', Muster {muster}' if muster else '')
    return jsonify({'id': t.id, 'studio_url': f'/studio/{t.id}',
                    'template': _template_dict(t), 'config': config,
                    'muster': muster or None, 'variablen': vars_,
                    'canvas': {'width': masse[0], 'height': masse[1]}}), 201


@bp.route('/api/studio/muster', methods=['GET'])
@studio_login_required
def api_studio_muster_liste():
    """Der Musterkatalog: fertige Vorlagen, die nur noch Texte brauchen."""
    return jsonify({'muster': MU.muster_liste()})


@bp.route('/api/studio/<int:template_id>/muster', methods=['POST'])
@studio_login_required
def api_studio_muster_anwenden(template_id):
    """Ein Muster in eine bestehende Vorlage setzen.

    Rumpf:
      muster    Schlüssel aus /api/studio/muster (Pflicht)
      ersetzen  true (Vorgabe) legt die bisherigen Elemente ab, false hängt an
      city_id   Stadt, deren Stil die Wasserzeichen-Ecke bestimmt (optional)
      speichern true schreibt gleich in die Vorlage, sonst kommt nur die Config zurück

    Ohne `speichern` ändert der Aufruf nichts – der Editor legt das Ergebnis dann in
    seinen Rückgängig-Stapel, statt am Server vorbei zu speichern."""
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    key = str(d.get('muster') or '').strip().lower()
    if not MU.muster_holen(key):
        return jsonify({'error': f'Unbekanntes Muster „{key}“'}), 400

    alt = _load_config(t) or {}
    if not isinstance(alt, dict):
        alt = {}
    bg_pfad = _bg_path(t)
    canvas = _canvas_for(alt, _image_size(bg_pfad) if bg_pfad else None)
    breite, hoehe = int(canvas['width']), int(canvas['height'])

    ecke = 'unten_rechts'
    city = _city_from_payload(d)
    if city is not None:
        ecke = str(_city_brand(city).get('wasserzeichen_ecke') or ecke)

    ersetzen = d.get('ersetzen') is not False
    bestand = [] if ersetzen else [el for el in (alt.get('elements') or [])
                                   if isinstance(el, dict)]
    # Ids doppelt zu vergeben wäre der sicherste Weg, Elemente zu verlieren.
    vergeben = {str(el.get('id')) for el in bestand}
    praefix = 'm'
    while any(i.startswith(praefix) for i in vergeben):
        praefix += 'm'
    neue = MU.muster_elemente(key, breite, hoehe, ecke, praefix)

    config = dict(alt)
    config['canvas'] = dict(canvas)
    grund = (MU.muster_holen(key) or {}).get('grund')
    # Die Grundfarbe des Musters nur setzen, wenn kein Foto dahinterliegt – sonst
    # verdeckte sie das Bild, für das die Vorlage gebaut wurde.
    if grund and not bg_pfad and ersetzen:
        config['canvas']['background'] = grund
    config['elements'] = bestand + neue

    fehler = R.validate_config(config)
    if fehler:                                   # pragma: no cover – nur bei kaputtem Muster
        return jsonify({'error': 'Muster passt hier nicht: ' + fehler[0]}), 400

    variablen = MU.muster_variablen(key)
    if d.get('speichern'):
        t.pil_config = json.dumps(config, ensure_ascii=False)
        # Dieselbe Rechnung wie beim Speichern im Editor: was in der Config steht, zählt.
        t.required_vars = json.dumps(_required_vars(config), ensure_ascii=False)
        db.session.commit()
        log.info('Studio: Muster %s in Vorlage %s gesetzt (%s)', key, t.id,
                 'ersetzt' if ersetzen else 'angehängt')
    return jsonify({'muster': key, 'config': config, 'variablen': variablen,
                    'gespeichert': bool(d.get('speichern')),
                    'elemente': len(neue)})


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
# STADT-STIL – die Handschrift einer Stadt verwalten
# ═══════════════════════════════════════════════════════════════════════════════
# Damit hundert Stadtseiten nicht hundertmal gleich aussehen, hat jede Stadt einen Stil:
# Palette, Schriftpaar und Anmutung (Kasten, Kontur, Schatten, Schreibweise). Eine Vorlage,
# die 'stil:*'-Farben und Rollen benutzt, sieht dadurch in jeder Stadt anders aus.
#
# Alles hier ist defensiv gebaut: fehlt models.CityStyle oder eine Renderer-Funktion,
# antworten die Routen mit 501 statt mit einem Absturz.

def _stil_modell():
    """models.CityStyle – oder None, solange es die Klasse nicht gibt."""
    try:
        import models as _m
        return getattr(_m, 'CityStyle', None)
    except Exception as ex:                          # pragma: no cover – defensiv
        log.warning('Studio: Stil-Modell nicht ladbar: %s', ex)
        return None


def _stil_paket_map():
    """{key: Paket-Dict} aus models.STIL_PAKETE. Leer, wenn es die Pakete nicht gibt."""
    try:
        import models as _m
        pakete = getattr(_m, 'STIL_PAKETE', None)
        return dict(pakete) if isinstance(pakete, dict) else {}
    except Exception as ex:                          # pragma: no cover – defensiv
        log.warning('Studio: Stil-Pakete nicht ladbar: %s', ex)
        return {}


def _stil_paket_liste():
    """[{'key','name','beschreibung', …alle Werte…}, …] – für die Oberfläche."""
    try:
        import models as _m
        fn = getattr(_m, 'stil_paket_liste', None)
        if callable(fn):
            return list(fn() or [])
    except Exception as ex:                          # pragma: no cover – defensiv
        log.warning('Studio: Stil-Paketliste nicht lesbar: %s', ex)
    return [dict(p, key=k) for k, p in _stil_paket_map().items()]


def _stil_kurzliste():
    """Nur Schlüssel, Name und Beschreibung – für Listen, in denen die Werte stören."""
    return [{'key': p.get('key'), 'name': p.get('name') or p.get('key'),
             'beschreibung': p.get('beschreibung') or ''} for p in _stil_paket_liste()]


def _stil_wahlwerte(feld):
    """Erlaubte Werte eines Auswahlfelds, direkt aus dem Renderer."""
    quelle = {'schreibweise': 'SCHREIBWEISEN', 'kasten': 'KASTEN_ARTEN',
              'kontur': 'KONTUR_ARTEN', 'wasserzeichen_ecke': 'WASSERZEICHEN_ECKEN'}
    return list(getattr(R, quelle.get(feld, ''), ()) or ())


def _stil_felder_liste():
    """Beschreibung der einzeln setzbaren Felder – die Oberfläche baut daraus ihre Regler."""
    out = []
    for name, art, label in STIL_FELDER:
        eintrag = {'key': name, 'art': art, 'label': label}
        if art == 'wahl':
            eintrag['werte'] = _stil_wahlwerte(name)
        elif art == 'zahl':
            eintrag['min'], eintrag['max'] = 0, 40
        elif art == 'paket':
            eintrag['werte'] = [p['key'] for p in _stil_kurzliste()]
        out.append(eintrag)
    return out


def _stil_zeilen_map():
    """{city_id: CityStyle} aller vorhandenen Stil-Zeilen – EINE Abfrage statt hundert."""
    modell = _stil_modell()
    if modell is None:
        return {}
    try:
        return {z.city_id: z for z in modell.query.all()}
    except Exception as ex:
        log.warning('Studio: Stil-Zeilen nicht lesbar: %s', ex)
        db.session.rollback()
        return {}


def _stil_zeile(city_id):
    """Die Stil-Zeile einer Stadt oder None."""
    modell = _stil_modell()
    if modell is None:
        return None
    try:
        return modell.query.filter_by(city_id=int(city_id)).first()
    except Exception as ex:
        log.warning('Studio: Stil-Zeile von Stadt %s nicht lesbar: %s', city_id, ex)
        db.session.rollback()
        return None


def _stil_kern(stil):
    """Der optische Kern eines Stils als vergleichbares Tupel."""
    return tuple(str(stil.get(k, '')).strip().lower() for k in _STIL_KERN)


def _paket_abweichung(stil):
    """True, wenn der Stil ein Paket nennt, aber nicht mehr genau so aussieht wie das Paket.
    Das ist keine Beanstandung – es sagt der Oberfläche nur, dass von Hand nachgestellt wurde."""
    paket = _stil_paket_map().get(str(stil.get('paket') or '').strip().lower())
    if not paket:
        return False
    try:
        rein = R.stil_dict(paket)
    except Exception:                                # pragma: no cover – defensiv
        return False
    return _stil_kern(rein) != _stil_kern(stil)


def _stadt_stil_dict(city, zeile=None, mit_stil=True):
    """Ein Stadteintrag für die Stil-Oberfläche.

    'abgeleitet' heißt: die Stadt hat keine eigene Zeile in city_style, ihr Stil wird aus
    den alten Marken-Feldern abgeleitet – sie sieht also aus wie jede andere Stadt ohne Stil."""
    stil = _city_brand(city)
    eintrag = {
        'city_id': city.id,
        'name': city.name,
        'state': city.state or '',
        'paket': str(stil.get('paket') or ''),
        'abgeleitet': zeile is None,
        'abweichend': _paket_abweichung(stil),
        # Für die Wasserzeichen-Vorschau der Oberfläche
        'instagram_handle': str(getattr(city, 'instagram_handle', '') or ''),
        'city_handle': stadt_variablen(city).get('city_handle', ''),
    }
    paket = _stil_paket_map().get(eintrag['paket'])
    eintrag['paket_name'] = (paket or {}).get('name', '') if paket else ''
    if mit_stil:
        eintrag['stil'] = stil
    return eintrag


def _stil_antwort(city, zeile=None):
    """Die vollständige Antwort für eine einzelne Stadt (GET und PUT liefern dasselbe)."""
    daten = _stadt_stil_dict(city, zeile)
    daten['felder'] = _stil_felder_liste()
    daten['pakete'] = _stil_kurzliste()
    daten['fonts'] = _public_fonts()
    return daten


# ── Einzelfelder prüfen und setzen ─────────────────────────────────────────────

def _stil_wert_pruefen(feld, wert):
    """(bereinigter Wert, Fehlertext). Genau einer von beiden ist gesetzt."""
    art, label = _STIL_FELD_MAP.get(feld, (None, feld))
    if art is None:
        return None, f'Unbekanntes Feld: {feld}'
    if art == 'farbe':
        s = str(wert or '').strip()
        if not s:
            return None, f'{label}: keine Farbe angegeben'
        try:
            if R.parse_color(s) is None:
                return None, f'{label}: „{s}“ ist keine Farbe'
        except Exception:
            return None, f'{label}: „{s}“ ist keine Farbe'
        return s, None
    if art == 'schrift':
        s = str(wert or '').strip()
        if not s:
            return None, f'{label}: keine Schrift angegeben'
        bekannt = {f.get('key') for f in R.list_fonts()}
        if s not in bekannt:
            return None, f'{label}: Schrift „{s}“ gibt es nicht'
        return s, None
    if art == 'wahl':
        s = str(wert or '').strip().lower()
        erlaubt = _stil_wahlwerte(feld)
        if s not in erlaubt:
            return None, f'{label}: „{wert}“ ist nicht erlaubt (möglich: {", ".join(erlaubt)})'
        return s, None
    if art == 'zahl':
        try:
            z = int(round(float(wert)))
        except (TypeError, ValueError):
            return None, f'{label}: „{wert}“ ist keine Zahl'
        return max(0, min(40, z)), None
    if art == 'ja_nein':
        if isinstance(wert, bool):
            return wert, None
        s = str(wert).strip().lower()
        if s in ('1', 'true', 'ja', 'an', 'on'):
            return True, None
        if s in ('0', 'false', 'nein', 'aus', 'off'):
            return False, None
        return None, f'{label}: „{wert}“ ist kein Ja/Nein'
    if art == 'paket':
        s = str(wert or '').strip().lower()
        if s and s not in _stil_paket_map():
            return None, f'{label}: „{wert}“ gibt es nicht'
        return s, None
    return None, f'Unbekannte Art für {feld}'                # pragma: no cover


def _stil_zeile_sichern(city, zeile):
    """Vorhandene Zeile oder eine neue, mit den abgeleiteten Werten vorbelegt.

    Vorbelegen ist wichtig: wer nur die Akzentfarbe ändert, soll nicht plötzlich die
    Vorgabe-Schrift bekommen, sondern seinen bisherigen (abgeleiteten) Stil behalten."""
    if zeile is not None:
        return zeile
    modell = _stil_modell()
    stil = _city_brand(city)
    zeile = modell(city_id=city.id)
    for feld, ziel in (('primary', 'primary'), ('secondary', 'secondary'),
                       ('accent', 'accent'), ('dark', 'dark'), ('light', 'light'),
                       ('font_headline', 'headline'), ('font_body', 'body'),
                       ('schreibweise', 'schreibweise'), ('kasten', 'kasten'),
                       ('radius', 'radius'), ('kontur', 'kontur'),
                       ('schatten', 'schatten'),
                       ('wasserzeichen_ecke', 'wasserzeichen_ecke')):
        if stil.get(ziel) not in (None, ''):
            setattr(zeile, feld, stil[ziel])
    db.session.add(zeile)
    return zeile


# ── Routen: Stil lesen und setzen ──────────────────────────────────────────────

@bp.route('/api/stil/pakete', methods=['GET'])
@studio_login_required
def api_stil_pakete():
    """Die Stil-Pakete mit Beschreibung – acht Handschriften zur Auswahl."""
    pakete = _stil_paket_liste()
    if not pakete:
        return jsonify({'error': 'Stil-Pakete sind in dieser Version nicht vorhanden'}), 501
    return jsonify({'pakete': pakete,
                    'felder': _stil_felder_liste(),
                    'fonts': _public_fonts(),
                    'rollen': _rollen_liste(),
                    'stil_tokens': _stil_token_liste()})


@bp.route('/api/stil/staedte', methods=['GET'])
@studio_login_required
def api_stil_staedte():
    """Je Stadt der aktuelle Stil plus das Kennzeichen, ob er nur vom Standard abgeleitet ist."""
    zeilen = _stil_zeilen_map()
    staedte = [_stadt_stil_dict(c, zeilen.get(c.id)) for c in _cities()]
    zusammenfassung = {}
    for s in staedte:
        schluessel = s['paket'] or ('ohne_paket' if not s['abgeleitet'] else 'ohne_stil')
        zusammenfassung[schluessel] = zusammenfassung.get(schluessel, 0) + 1
    return jsonify({'staedte': staedte,
                    'pakete': _stil_kurzliste(),
                    'zusammenfassung': zusammenfassung,
                    'ohne_stil': sum(1 for s in staedte if s['abgeleitet'])})


@bp.route('/api/stil/stadt/<int:city_id>', methods=['GET'])
@studio_login_required
def api_stil_stadt_get(city_id):
    city = City.query.get_or_404(city_id)
    return jsonify(_stil_antwort(city, _stil_zeile(city_id)))


@bp.route('/api/stil/stadt/<int:city_id>', methods=['PUT'])
@studio_login_required
def api_stil_stadt_put(city_id):
    """Einzelne Stil-Felder einer Stadt setzen. Alles, was nicht im Rumpf steht, bleibt."""
    city = City.query.get_or_404(city_id)
    if _stil_modell() is None:
        return jsonify({'ok': False, 'errors': ['Stadt-Stile gibt es in dieser Version nicht']}), 501
    d = request.get_json(silent=True) or {}
    if isinstance(d.get('stil'), dict):          # beide Formen zulassen: flach oder unter 'stil'
        d = d['stil']
    gewuenscht = {k: v for k, v in d.items() if k in _STIL_FELD_MAP}
    unbekannt = [k for k in d if k not in _STIL_FELD_MAP]
    if not gewuenscht:
        return jsonify({'ok': False,
                        'errors': ['Kein bekanntes Stil-Feld im Rumpf'
                                   + (f' (unbekannt: {", ".join(sorted(unbekannt))})' if unbekannt else '')],
                        'felder': _stil_felder_liste()}), 400
    werte, fehler = {}, []
    for feld, roh in gewuenscht.items():
        wert, meldung = _stil_wert_pruefen(feld, roh)
        if meldung:
            fehler.append(meldung)
        else:
            werte[feld] = wert
    if fehler:
        return jsonify({'ok': False, 'errors': fehler}), 400

    zeile = _stil_zeile_sichern(city, _stil_zeile(city_id))
    geaendert = []
    for feld, wert in werte.items():
        if getattr(zeile, feld, None) != wert:
            setattr(zeile, feld, wert)
            geaendert.append(feld)
    try:
        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        log.warning('Studio: Stil von Stadt %s nicht speicherbar: %s', city_id, ex)
        return jsonify({'ok': False, 'errors': [f'Nicht gespeichert: {ex}']}), 500
    antwort = _stil_antwort(city, zeile)
    antwort['ok'] = True
    antwort['geaendert'] = sorted(geaendert)
    return jsonify(antwort)


@bp.route('/api/stil/stadt/<int:city_id>/paket', methods=['POST'])
@studio_login_required
def api_stil_stadt_paket(city_id):
    """Ein ganzes Stil-Paket auf eine Stadt anwenden."""
    city = City.query.get_or_404(city_id)
    modell = _stil_modell()
    if modell is None:
        return jsonify({'ok': False, 'error': 'Stadt-Stile gibt es in dieser Version nicht'}), 501
    d = request.get_json(silent=True) or {}
    key = str(d.get('paket') or '').strip().lower()
    if key not in _stil_paket_map():
        return jsonify({'ok': False,
                        'error': f'Unbekanntes Stil-Paket: {d.get("paket")!r}',
                        'pakete': [p['key'] for p in _stil_kurzliste()]}), 400
    zeile = _stil_zeile(city_id) or modell(city_id=city.id)
    if not zeile.paket_setzen(key):                  # pragma: no cover – key ist geprüft
        return jsonify({'ok': False, 'error': 'Paket nicht anwendbar'}), 400
    db.session.add(zeile)
    try:
        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        log.warning('Studio: Paket für Stadt %s nicht speicherbar: %s', city_id, ex)
        return jsonify({'ok': False, 'error': f'Nicht gespeichert: {ex}'}), 500
    antwort = _stil_antwort(city, zeile)
    antwort['ok'] = True
    return jsonify(antwort)


# ── Pakete über die Städte verteilen ───────────────────────────────────────────
# Der Sinn: Nachbarstädte sollen NICHT gleich aussehen. Wer Frankfurt und Offenbach
# nebeneinander im Feed sieht, soll zwei verschiedene Seiten sehen.

def _nachbarn_map(staedte, radius_km=None):
    """{city_id: set(city_id)} – Nachbarschaft laut Planer.

    Ohne planer_bp (oder wenn es dort klemmt) der Rückfall „gleiches Bundesland“:
    lieber eine grobe Nachbarschaft als gar keine."""
    ids = {c.id for c in staedte}
    karte = {c.id: set() for c in staedte}
    fn = None
    try:
        import planer_bp
        fn = getattr(planer_bp, 'neighbors', None)
    except Exception as ex:
        log.warning('Studio: Planer nicht ladbar, Nachbarn nach Bundesland: %s', ex)
    ok = callable(fn)
    if ok:
        for c in staedte:
            try:
                for other in (fn(c, radius_km) or []):
                    if getattr(other, 'id', None) in ids and other.id != c.id:
                        karte[c.id].add(other.id)
            except Exception as ex:
                log.warning('Studio: Nachbarn von %s nicht bestimmbar: %s', c.name, ex)
                ok = False
                break
    if not ok:
        karte = {c.id: set() for c in staedte}
        for a in staedte:
            for b in staedte:
                if a.id != b.id and (a.state or '') and (a.state or '') == (b.state or ''):
                    karte[a.id].add(b.id)
    for a, nachbarn in list(karte.items()):          # Nachbarschaft ist wechselseitig
        for b in nachbarn:
            karte.setdefault(b, set()).add(a)
    return karte


def _verteilen_planen(staedte, paket_keys, karte, paket_von, hat_zeile, nur_ohne_stil):
    """Wer bekommt welches Paket? Reine Rechnung ohne Datenbank – dadurch prüfbar.

    Reihenfolge: Städte mit den meisten Nachbarn zuerst; die sind am schwersten
    unterzubringen. Je Stadt wird ein Paket gewählt, das KEIN Nachbar hat; gibt es keines,
    das insgesamt am seltensten benutzte (und die Stadt landet in 'konflikte').

    Das bisherige Paket einer Stadt bleibt, solange es weiterhin bei keinem Nachbarn
    vorkommt. Ein zweiter Lauf auf einem konfliktfreien Stand ändert deshalb nichts mehr
    und wirft keine Handarbeit weg; geändert wird nur, wo es sich stößt oder noch fehlt."""
    zuteilung = dict(paket_von)
    anzahl = {k: 0 for k in paket_keys}
    for key in paket_von.values():
        if key in anzahl:
            anzahl[key] += 1
    plan, konflikte = [], []
    for c in sorted(staedte, key=lambda s: (-len(karte.get(s.id) or ()), s.id)):
        if nur_ohne_stil and c.id in hat_zeile:
            plan.append({'city_id': c.id, 'name': c.name, 'paket': paket_von.get(c.id, ''),
                         'neu': False, 'uebersprungen': True})
            continue
        belegt = {zuteilung.get(n) for n in karte.get(c.id, ())} - {None, ''}
        frei = [k for k in paket_keys if k not in belegt]
        if not frei:
            frei = list(paket_keys)
            konflikte.append({'city_id': c.id, 'name': c.name,
                              'grund': 'Alle Pakete kommen bei Nachbarn schon vor'})
        vorher = paket_von.get(c.id, '')
        if vorher in anzahl:
            anzahl[vorher] = max(0, anzahl[vorher] - 1)   # diese Stadt wird gleich neu gezählt
        # Bleibt es konfliktfrei, bleibt es: sonst wäre die Verteilung nicht wiederholbar
        # und ein zweiter Lauf würde von Hand gesetzte Stile stillschweigend überschreiben.
        wahl = vorher if vorher in frei else min(frei, key=lambda k: (anzahl.get(k, 0), k))
        anzahl[wahl] = anzahl.get(wahl, 0) + 1
        zuteilung[c.id] = wahl
        plan.append({'city_id': c.id, 'name': c.name, 'paket': wahl,
                     'neu': wahl != vorher, 'uebersprungen': False})
    return plan, konflikte


@bp.route('/api/stil/verteilen', methods=['POST'])
@studio_login_required
def api_stil_verteilen():
    """Stil-Pakete automatisch über die Städte verteilen, so dass Nachbarn verschieden aussehen.

    Rumpf (alles optional):
      paket_keys    Liste der Pakete, die benutzt werden dürfen (Vorgabe: alle)
      nur_ohne_stil true = Städte mit eigener Stil-Zeile bleiben, wie sie sind
      radius_km     Nachbarschafts-Radius (Vorgabe: die Einstellung des Planers)
    """
    modell = _stil_modell()
    if modell is None or not _stil_paket_map():
        return jsonify({'ok': False, 'error': 'Stadt-Stile gibt es in dieser Version nicht'}), 501
    d = request.get_json(silent=True) or {}

    alle = [p['key'] for p in _stil_kurzliste()]
    gewuenscht = d.get('paket_keys')
    if gewuenscht in (None, '', []):
        paket_keys = alle
    else:
        if not isinstance(gewuenscht, (list, tuple)):
            return jsonify({'ok': False, 'error': 'paket_keys muss eine Liste sein'}), 400
        paket_keys = [str(k).strip().lower() for k in gewuenscht]
        unbekannt = [k for k in paket_keys if k not in alle]
        if unbekannt:
            return jsonify({'ok': False,
                            'error': f'Unbekannte Pakete: {", ".join(sorted(set(unbekannt)))}',
                            'pakete': alle}), 400
        paket_keys = sorted(set(paket_keys), key=alle.index)
    if not paket_keys:
        return jsonify({'ok': False, 'error': 'Keine Pakete zur Auswahl'}), 400

    radius = d.get('radius_km')
    try:
        radius = float(radius) if radius not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'radius_km muss eine Zahl sein'}), 400
    nur_ohne_stil = bool(d.get('nur_ohne_stil'))

    staedte = _cities()
    if not staedte:
        return jsonify({'ok': False, 'error': 'Keine Städte vorhanden'}), 400
    zeilen = _stil_zeilen_map()
    paket_von = {c.id: str((zeilen[c.id].paket or '')).strip().lower()
                 for c in staedte if c.id in zeilen}
    hat_zeile = set(zeilen.keys())
    karte = _nachbarn_map(staedte, radius)
    plan, konflikte = _verteilen_planen(staedte, paket_keys, karte, paket_von,
                                        hat_zeile, nur_ohne_stil)

    stadt_von_id = {c.id: c for c in staedte}
    geaendert = 0
    for eintrag in plan:
        if eintrag['uebersprungen'] or not eintrag['neu']:
            continue
        zeile = zeilen.get(eintrag['city_id']) or modell(city_id=eintrag['city_id'])
        if zeile.paket_setzen(eintrag['paket']):
            db.session.add(zeile)
            geaendert += 1
    try:
        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        log.warning('Studio: Verteilung nicht speicherbar: %s', ex)
        return jsonify({'ok': False, 'error': f'Nicht gespeichert: {ex}'}), 500

    paket_namen = {p['key']: p['name'] for p in _stil_kurzliste()}
    zuweisungen = []
    for eintrag in sorted(plan, key=lambda e: stadt_von_id[e['city_id']].name):
        stadt = stadt_von_id[eintrag['city_id']]
        zuweisungen.append({
            'city_id': stadt.id, 'name': stadt.name, 'state': stadt.state or '',
            'paket': eintrag['paket'],
            'paket_name': paket_namen.get(eintrag['paket'], ''),
            'neu': eintrag['neu'], 'uebersprungen': eintrag['uebersprungen'],
            'nachbarn': sorted(stadt_von_id[n].name for n in karte.get(stadt.id, ())
                               if n in stadt_von_id),
        })
    zusammenfassung = {}
    for z in zuweisungen:
        if z['paket']:
            zusammenfassung[z['paket']] = zusammenfassung.get(z['paket'], 0) + 1
    return jsonify({'ok': True,
                    'zuweisungen': zuweisungen,
                    'zusammenfassung': zusammenfassung,
                    'konflikte': konflikte,
                    'geaendert': geaendert,
                    'staedte': len(staedte),
                    'radius_km': radius,
                    'paket_keys': paket_keys})


@bp.route('/api/stil/aehnlich', methods=['GET'])
@studio_login_required
def api_stil_aehnlich():
    """Welche NACHBARstädte sehen gleich aus? Genau da fällt es im Feed auf.

    Verglichen wird zuerst das Paket, sonst der optische Kern des Stils (Palette,
    Schriftpaar, Anmutung). Städte ohne eigene Stil-Zeile sehen alle gleich aus – deshalb
    steht ihre Zahl gesondert in der Antwort."""
    radius = request.args.get('radius_km')
    try:
        radius = float(radius) if radius not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'error': 'radius_km muss eine Zahl sein'}), 400
    staedte = _cities()
    zeilen = _stil_zeilen_map()
    karte = _nachbarn_map(staedte, radius)
    stil_von = {c.id: _city_brand(c) for c in staedte}
    kern_von = {cid: _stil_kern(s) for cid, s in stil_von.items()}
    name_von = {c.id: c.name for c in staedte}

    paare, gesehen = [], set()
    for c in staedte:
        for n in sorted(karte.get(c.id, ())):
            if n not in name_von:
                continue
            schluessel = (min(c.id, n), max(c.id, n))
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            a_paket = str(stil_von[c.id].get('paket') or '')
            b_paket = str(stil_von[n].get('paket') or '')
            if a_paket and a_paket == b_paket:
                grund = 'gleiches Stil-Paket'
            elif kern_von[c.id] == kern_von[n]:
                grund = 'gleiche Farben und Schriften'
            else:
                continue
            paare.append({
                'a': {'city_id': c.id, 'name': c.name},
                'b': {'city_id': n, 'name': name_von[n]},
                'paket': a_paket, 'grund': grund,
                'beide_ohne_stil': c.id not in zeilen and n not in zeilen,
            })
    paare.sort(key=lambda p: (p['a']['name'], p['b']['name']))
    ohne_stil = sum(1 for c in staedte if c.id not in zeilen)
    if not paare:
        text = 'Keine zwei Nachbarstädte sehen gleich aus.'
    else:
        text = (f'{len(paare)} Nachbarpaare sehen gleich aus'
                + (f'; {ohne_stil} Städte haben noch gar keinen eigenen Stil.'
                   if ohne_stil else '.'))
    return jsonify({'paare': paare[:_AEHNLICH_MAX_PAARE],
                    'anzahl': len(paare),
                    'gekuerzt': len(paare) > _AEHNLICH_MAX_PAARE,
                    'staedte_ohne_stil': ohne_stil,
                    'radius_km': radius,
                    'zusammenfassung': text})


# ═══════════════════════════════════════════════════════════════════════════════
# VORLAGEN-PRÜFUNG – taugt diese Vorlage für hundert Städte?
# ═══════════════════════════════════════════════════════════════════════════════
# Sechs Prüfungen gegen die echten Werte echter Städte. Sie ersetzen das, was sonst erst
# im fertigen Massenlauf auffällt: Text, der geschrumpft werden muss, Schrift, die man auf
# dem Foto nicht liest, Elemente, die im Instagram-Raster gar nicht zu sehen sind.

def _pruef_element_id(el, nummer):
    return str(el.get('id') or el.get('var') or f'Element {nummer}')


def _pruef_werte(city, config):
    """Nur die ECHTEN Werte einer Stadt – Platzhalter wie '[park_name]' fallen weg.
    Mit Platzhaltern zu messen hieße, eine erfundene Textlänge zu prüfen."""
    werte = {}
    for name, wert in (_values_for_city(city, config) or {}).items():
        if wert is None:
            continue
        s = str(wert)
        if not s.strip() or s == R.sample_values_placeholder(name):
            continue
        werte[name] = s
    return werte


def _pruef_text(el, werte):
    """(Text, fehlende Variablen) für dieses Element bei diesen Werten.

    Fester Text mit {var} wird eingesetzt; ein reines var-Element ohne Wert liefert None –
    dann steht die Fläche im fertigen Bild leer (das prüft „ohne Rückfallwert“)."""
    fehlend = []
    fest = el.get('text')
    if fest not in (None, ''):
        def ersetzen(treffer):
            wert = werte.get(treffer.group(1))
            if wert in (None, ''):
                fehlend.append(treffer.group(1))
                return ''
            return str(wert)
        return _VAR_RE.sub(ersetzen, str(fest)), fehlend
    var = str(el.get('var') or '').strip()
    if var:
        wert = werte.get(var)
        if wert in (None, ''):
            return None, [var]
        return str(wert), []
    return None, []


def _pruef_flaeche_darunter(el, nummer, elemente, canvas):
    """(Farbe, Deckkraft) der obersten Fläche unter diesem Text – oder (None, 1.0).

    Ein farbiger Balken mit Text darauf ist ein Grundmuster jeder Vorlage. Ohne diesen
    Schritt misst die Prüfung gegen das Hintergrundbild und meldet Fehler, die es nicht
    gibt (oder übersieht welche, die es gibt).

    Bewusst streng: nur unrotierte Rechtecke, die den Text VOLLSTÄNDIG überdecken, und nur
    einfarbig. Bei allem anderen (Ellipse, Verlauf, teilweise Überdeckung) lieber kein
    Urteil als ein falsches. Halbdurchsichtig ist dagegen kein Grund zur Zurückhaltung –
    die Deckkraft kommt mit zurück und wird mit dem Untergrund verrechnet."""
    tx0, ty0, tx1, ty1 = _pruef_zuschnitt_box(el, canvas)
    treffer, deckkraft = None, 1.0
    for i, kandidat in enumerate(elemente, 1):
        if i >= nummer:                              # nur was VOR dem Text gezeichnet wird
            break
        if not isinstance(kandidat, dict) or R._bool(kandidat.get('hidden')):
            continue
        typ = str(kandidat.get('type') or 'text')
        if typ == 'shape':
            if str(kandidat.get('shape') or 'rect') != 'rect':
                continue
        elif typ != 'rect':                          # Altbestand kennt 'rect' als eigenen Typ
            continue
        if R._num(kandidat.get('rotation'), 0):
            continue
        fuellung = kandidat.get('fill')
        if fuellung in (None, '', False) or isinstance(fuellung, dict):
            continue
        roh = kandidat.get('opacity')
        alpha = 1.0 if roh in (None, '') else R._num(roh, 1.0)
        if not (0 < alpha <= 1):
            continue
        kx0, ky0, kx1, ky1 = _pruef_zuschnitt_box(kandidat, canvas)
        if kx0 <= tx0 and ky0 <= ty0 and kx1 >= tx1 and ky1 >= ty1:
            treffer, deckkraft = fuellung, alpha     # später gezeichnet gewinnt
    return treffer, deckkraft


def _pruef_mischen(oben, unten, alpha, stil):
    """Zwei Farbangaben nach Deckkraft mischen -> '#rrggbb', oder None wenn nicht messbar."""
    a, b = _pruef_farbe(oben, stil), _pruef_farbe(unten, stil)
    if a is None or b is None:
        return None
    t = max(0.0, min(1.0, float(alpha)))
    kanaele = [int(round(int(a[i:i + 2], 16) * t + int(b[i:i + 2], 16) * (1 - t)))
               for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(kanaele)


def _pruef_grundfarbe(el, canvas, stil, bg_pfad, config, cache):
    """(Farbe, Beschreibung) aus Hintergrundbild oder Leinwandfarbe – ohne Elemente davor."""
    # Das Hintergrundbild ist für alle Städte dasselbe – gemerkt wird nach Bildausschnitt,
    # damit dieselbe Box nicht für jede Handschrift neu gemessen wird.
    schluessel = tuple(int(round(v)) for v in _pruef_zuschnitt_box(el, canvas))
    if schluessel not in cache:
        farbe = None
        if bg_pfad:
            try:
                farbe = R.hintergrund_farbe_im_bereich(bg_pfad, el, canvas)
            except Exception as ex:                  # pragma: no cover – defensiv
                log.warning('Studio: Hintergrundfarbe nicht messbar: %s', ex)
        if farbe is None:
            # Gemerkt wird die ANGABE, nicht die fertige Farbe: 'brand:bg' löst sich je
            # Stadt anders auf. Ein Verlauf kommt so als Objekt durch und wird nicht
            # beurteilt (lieber kein Befund als ein falscher).
            grund = _hintergrundfarbe(config, stil)
            farbe = grund if grund is not None else '#ffffff'
        cache[schluessel] = farbe
    return cache[schluessel], ('Hintergrundbild' if bg_pfad else 'Hintergrundfarbe')


def _pruef_untergrund(el, canvas, stil, bg_pfad, config, cache, nummer=None, elemente=None):
    """(Farbe hinter dem Text, Beschreibung). Der Reihe nach: Textkasten, eine Fläche unter
    dem Text (bei Halbdurchsicht mit dem Untergrund verrechnet), sonst Bild oder Leinwand."""
    kasten = el.get('box')
    if isinstance(kasten, dict) and kasten.get('fill') not in (None, '', False):
        farbe = kasten['fill']
        if not isinstance(farbe, dict):              # Verlauf im Kasten: nicht messbar
            return farbe, 'Textkasten'

    flaeche, deckkraft = (None, 1.0)
    if nummer is not None and elemente:
        flaeche, deckkraft = _pruef_flaeche_darunter(el, nummer, elemente, canvas)
    if flaeche is not None and deckkraft >= 0.995:
        return flaeche, 'Fläche darunter'

    grund, woher = _pruef_grundfarbe(el, canvas, stil, bg_pfad, config, cache)
    if flaeche is None:
        return grund, woher
    gemischt = _pruef_mischen(flaeche, grund, deckkraft, stil)
    return (gemischt, 'Fläche darunter') if gemischt else (grund, woher)


def _pruef_farbe(wert, stil):
    """Farbtoken -> '#rrggbb'. Verlauf oder unlesbar -> None (dann wird nicht geurteilt).

    parse_color liefert für 'stil:auto' eine Zeichenkette statt eines Tupels – deshalb wird
    ein Zeichenketten-Ergebnis noch einmal durchgereicht statt es blind zu indizieren."""
    if wert in (None, '', False) or isinstance(wert, dict):
        return None
    try:
        rgba = R.parse_color(wert, stil, None)
        if isinstance(rgba, str):
            rgba = R.parse_color(rgba, stil, None)
    except Exception:
        return None
    if not isinstance(rgba, (tuple, list)) or len(rgba) < 3:
        return None
    try:
        return '#%02x%02x%02x' % (int(rgba[0]), int(rgba[1]), int(rgba[2]))
    except (TypeError, ValueError):                  # pragma: no cover – defensiv
        return None


def _pruef_zuschnitt_box(el, canvas):
    """Rechteck eines Elements (x0, y0, x1, y1)."""
    try:
        return tuple(R._el_box(el, canvas))
    except Exception:                                # pragma: no cover – defensiv
        x = float(el.get('x') or 0)
        y = float(el.get('y') or 0)
        w = float(el.get('width') or 0) or (canvas[0] - x)
        h = float(el.get('height') or 0) or canvas[1] * 0.08
        return (x, y, x + w, y + h)


def _pruef_stil_benutzt(config):
    """(benutzt 'stil:'-Token?, benutzt Rollen?) – die Grundlage für „alle Städte gleich“."""
    try:
        text = json.dumps(config, ensure_ascii=False).lower()
    except Exception:                                # pragma: no cover – defensiv
        text = ''
    token = 'stil:' in text
    rollen = False
    for el in (config or {}).get('elements') or []:
        if isinstance(el, dict):
            name = str(el.get('role') or '').strip().lower()
            if name and name != 'frei':
                rollen = True
    return token, rollen


def _budget_fuer(el, canvas, stil, rueckfall=None):
    """Zeichen-Budget dieses Elements für GENAU diese Handschrift.
    Ohne die Renderer-Funktion oder bei einem Fehler: der mitgegebene Rückfallwert."""
    fn = getattr(R, 'zeichen_budget', None)
    if not callable(fn):
        return rueckfall
    try:
        return int(fn(el, canvas, stil))
    except Exception as ex:                          # pragma: no cover – defensiv
        log.warning('Studio: Zeichen-Budget nicht berechenbar: %s', ex)
        return rueckfall


def _pruefe_vorlage(template, config, staedte, budgets=None):
    """Die sechs Prüfungen. Liefert (befunde, Anzahl geprüfter Städte)."""
    befunde = []
    elemente = [el for el in (config or {}).get('elements') or [] if isinstance(el, dict)]
    text_elemente = [(i, el) for i, el in enumerate(elemente, 1)
                     if str(el.get('type') or 'text') == 'text' and not R._bool(el.get('hidden'))]

    bg_pfad = _bg_path(template)
    bild_groesse = _image_size(bg_pfad) if bg_pfad else None
    canvas_dict = _canvas_for(config, bild_groesse)
    canvas = (int(canvas_dict['width']), int(canvas_dict['height']))
    budgets = budgets if budgets is not None else _zeichen_budgets(config, canvas)

    # Werte und Stil je Stadt einmal besorgen
    werte_von, stil_von = {}, {}
    for c in staedte:
        werte_von[c.id] = _pruef_werte(c, config)
        stil_von[c.id] = _city_brand(c)

    # ── 1. Text passt nicht ────────────────────────────────────────────────────
    # Gemessen wird je Schrift-Handschrift (Auszeichnung, Grundschrift, Schreibweise) –
    # nur die entscheidet über die Textbreite. Innerhalb einer Handschrift genügt die
    # längste vorkommende Belegung; passt die, passen die kürzeren erst recht.
    for nummer, el in text_elemente:
        el_id = _pruef_element_id(el, nummer)
        var_name = (str(el.get('var') or '').strip()
                    or ', '.join(_VAR_RE.findall(str(el.get('text') or ''))))
        laengste = {}
        for c in staedte:
            text, _fehlend = _pruef_text(el, werte_von[c.id])
            if not text or not text.strip():
                continue
            stil = stil_von[c.id]
            sig = (str(stil.get('headline')), str(stil.get('body')),
                   str(stil.get('schreibweise')))
            vorher = laengste.get(sig)
            if vorher is None or len(text) > len(vorher[0]):
                laengste[sig] = (text, c, stil)
        eng = []
        for text, stadt, stil in laengste.values():
            try:
                ergebnis = R.passt_text(text, el, canvas, brand=stil)
            except Exception as ex:                  # pragma: no cover – defensiv
                log.warning('Studio: Textprüfung fehlgeschlagen (%s): %s', el_id, ex)
                continue
            if ergebnis.get('passt'):
                continue
            eng.append((float(ergebnis.get('noetige_verkleinerung') or 0), text, stadt, stil))
        # Nur die schlimmsten Fälle je Element: acht Handschriften ergäben sonst achtmal
        # denselben Satz. Wer den schlimmsten behebt, behebt die anderen mit.
        eng.sort(key=lambda e: (-e[0], -len(e[1])))
        weitere = max(0, len(eng) - _PRUEF_MAX_JE_ELEMENT)
        for i, (verkleinerung, text, stadt, stil) in enumerate(eng[:_PRUEF_MAX_JE_ELEMENT]):
            kleiner = int(round(verkleinerung * 100))
            # Die Zahl im Vorschlag muss zu der Stadt passen, an der es scheitert: eine
            # breitere Schrift verträgt weniger Zeichen als die Vorgabe-Handschrift.
            platz = _budget_fuer(el, canvas, stil, budgets.get(el_id))
            vorschlag = (f'Höchstens {platz} Zeichen für dieses Element '
                         f'(dieser Text hat {len(text)}).' if platz else
                         'Text kürzen oder die Box größer machen.')
            meldung = (f'„{text[:60]}“ passt nicht: müsste {kleiner} % kleiner sein als '
                       f'die Mindestgröße.' if kleiner else
                       f'„{text[:60]}“ passt nicht in die Box.')
            if i == 0 and weitere:
                meldung += f' Gilt in {weitere} weiteren Handschriften genauso.'
            befunde.append({
                'art': 'text_passt_nicht', 'schwere': 'fehler', 'element_id': el_id,
                'stadt': stadt.name, 'variable': var_name,
                'text': meldung, 'vorschlag': vorschlag,
            })

    # ── 2. Lesbarkeit ──────────────────────────────────────────────────────────
    # Gerechnet wird je Handschrift, nicht je Stadt: zwei Städte mit demselben Paket
    # ergeben denselben Kontrast. Die Städte hängen als Liste am Befund, damit man sieht,
    # wie viele es betrifft.
    handschriften = {}
    for c in staedte:
        handschriften.setdefault(_stil_kern(stil_von[c.id]),
                                 (stil_von[c.id], []))[1].append(c.name)
    hintergrund_cache = {}
    for nummer, el in text_elemente:
        el_id = _pruef_element_id(el, nummer)
        schlecht = {}
        for stil, stadtnamen in handschriften.values():
            try:
                voll = R.rolle_anwenden(el, stil, canvas[1])
            except Exception:                        # pragma: no cover – defensiv
                voll = el
            untergrund_roh, woher = _pruef_untergrund(voll, canvas, stil, bg_pfad,
                                                      config, hintergrund_cache,
                                                      nummer, elemente)
            untergrund = _pruef_farbe(untergrund_roh, stil)
            if untergrund is None:
                continue
            farbwert = voll.get('color')
            if str(farbwert).strip().lower() == 'stil:auto':
                # Genau das macht der Renderer auch: Schwarz oder Weiß, je nachdem, was auf
                # DIESEM Untergrund besser lesbar ist.
                try:
                    farbwert = R.auto_farbe(untergrund, stil)
                except Exception:                    # pragma: no cover – defensiv
                    pass
            schrift = _pruef_farbe(farbwert, stil)
            if schrift is None:
                continue
            # Eine kräftige Kontur trägt die Lesbarkeit – aber nur, wenn sie sich von der
            # Schrift selbst abhebt. Schwarze Kontur um schwarze Schrift macht den Text nur
            # dicker; dann zählt weiter, was wirklich dahinter liegt. Es gilt also, was von
            # beidem den Text besser trägt.
            kontur = _pruef_farbe(voll.get('stroke'), stil)
            try:
                breite = float(voll.get('stroke_width') or 0)
            except (TypeError, ValueError):
                breite = 0.0
            try:
                verhaeltnis = float(R.kontrast(schrift, untergrund, stil))
                if kontur and breite >= 2:
                    mit_kontur = float(R.kontrast(schrift, kontur, stil))
                    if mit_kontur > verhaeltnis:
                        untergrund, woher, verhaeltnis = kontur, 'Kontur', mit_kontur
            except Exception:                        # pragma: no cover – defensiv
                continue
            if verhaeltnis >= _KONTRAST_HINWEIS:
                continue
            # Dieselbe Farbkombination in mehreren Städten ist EIN Befund, kein Dutzend.
            schluessel = (schrift, untergrund)
            if schluessel in schlecht:
                schlecht[schluessel]['staedte'].extend(stadtnamen)
                continue
            schlecht[schluessel] = {'verhaeltnis': verhaeltnis, 'woher': woher,
                                    'staedte': list(stadtnamen)}
        for (schrift, untergrund), fall in schlecht.items():
            anzahl = len(fall['staedte'])
            befunde.append({
                'art': 'lesbarkeit',
                'schwere': 'fehler' if fall['verhaeltnis'] < _KONTRAST_FEHLER else 'hinweis',
                'element_id': el_id, 'stadt': fall['staedte'][0],
                'staedte': fall['staedte'][:12],
                'text': (f'Kontrast {fall["verhaeltnis"]:.1f}:1 zwischen Schrift {schrift} '
                         f'und {fall["woher"]} {untergrund}'
                         + (f' – betrifft {anzahl} Städte.' if anzahl > 1 else '.')),
                'vorschlag': ('Farbe stil:auto benutzen, einen Textkasten setzen oder eine '
                              'dunkle Kontur geben (mindestens 4,5:1).'),
            })

    # ── 3. Zuschnitt im Instagram-Raster ───────────────────────────────────────
    if canvas[1] > canvas[0]:
        oben = (canvas[1] - canvas[0]) / 2.0
        unten = oben + canvas[0]
        for nummer, el in enumerate(elemente, 1):
            if R._bool(el.get('hidden')):
                continue
            x0, y0, x1, y1 = _pruef_zuschnitt_box(el, canvas)
            if y1 <= oben or y0 >= unten:
                befunde.append({
                    'art': 'zuschnitt', 'schwere': 'hinweis',
                    'element_id': _pruef_element_id(el, nummer), 'stadt': '',
                    'text': ('Liegt ganz außerhalb des quadratischen Ausschnitts, den '
                             f'Instagram im Raster zeigt (y {int(oben)} bis {int(unten)}).'),
                    'vorschlag': f'Element nach y {int(oben)}–{int(unten)} schieben.',
                })

    # ── 4. Ohne Rückfallwert ───────────────────────────────────────────────────
    leer_gewesen = set()
    for c in staedte:
        for _nummer, el in text_elemente:
            _text, fehlend = _pruef_text(el, werte_von[c.id])
            leer_gewesen.update(fehlend)
    for nummer, el in text_elemente:
        var = str(el.get('var') or '').strip()
        namen = [var] if var else _VAR_RE.findall(str(el.get('text') or ''))
        namen = [n for n in namen if n]
        if not namen or el.get('fallback') not in (None, ''):
            continue
        betroffen = [n for n in namen if n in leer_gewesen]
        if betroffen:
            text = (f'{", ".join(betroffen)} ohne Rückfallwert – mindestens eine der '
                    'geprüften Städte hat dafür keinen Wert, dort bleibt die Fläche leer.')
        else:
            text = (f'{", ".join(namen)} ohne Rückfallwert – fehlt der Wert einmal, '
                    'bleibt die Fläche leer.')
        befunde.append({
            'art': 'ohne_rueckfallwert',
            'schwere': 'fehler' if betroffen else 'hinweis',
            'element_id': _pruef_element_id(el, nummer), 'stadt': '',
            'variable': ', '.join(betroffen or namen),
            'text': text,
            'vorschlag': 'Feld „fallback“ am Element füllen.',
        })

    # ── 5. Kein Wasserzeichen ──────────────────────────────────────────────────
    hat_rolle = any(str(el.get('role') or '').strip().lower() == 'wasserzeichen'
                    for _n, el in text_elemente)
    nutzt_handle = any('city_handle' in ([str(el.get('var') or '')]
                                         + _VAR_RE.findall(str(el.get('text') or '')))
                       for _n, el in text_elemente)
    if not hat_rolle and not nutzt_handle:
        befunde.append({
            'art': 'wasserzeichen', 'schwere': 'hinweis', 'element_id': '', 'stadt': '',
            'text': 'Die Vorlage trägt keinen Seitennamen – geklaute Reposts sind nicht zuzuordnen.',
            'vorschlag': ('Ein Text-Element mit Rolle „wasserzeichen“ und Variable '
                          'city_handle in eine Ecke setzen.'),
        })
    elif nutzt_handle:
        ohne_handle = [c.name for c in staedte if not werte_von[c.id].get('city_handle')]
        if ohne_handle:
            befunde.append({
                'art': 'wasserzeichen', 'schwere': 'hinweis', 'element_id': '',
                'stadt': ohne_handle[0],
                'text': (f'{len(ohne_handle)} von {len(staedte)} Städten haben keinen '
                         f'Instagram-Namen hinterlegt – dort bleibt das Wasserzeichen leer.'),
                'vorschlag': 'Instagram-Namen der Städte auf der Städte-Seite eintragen.',
            })

    # ── 6. Alle Städte gleich ──────────────────────────────────────────────────
    token, rollen = _pruef_stil_benutzt(config)
    if not token and not rollen:
        befunde.append({
            'art': 'alle_gleich', 'schwere': 'hinweis', 'element_id': '', 'stadt': '',
            'text': ('Die Vorlage benutzt weder eine Rolle noch einen stil:-Farbwert – '
                     'alle Städte sehen damit gleich aus, nur der Text ist anders.'),
            'vorschlag': ('Farben auf stil:primary / stil:accent / stil:auto umstellen oder '
                          'den Text-Elementen Rollen geben.'),
        })

    rang = {'fehler': 0, 'hinweis': 1}
    befunde.sort(key=lambda b: (rang.get(b['schwere'], 2), b['art'], str(b['element_id'])))
    return befunde, len(staedte)


def _zeichen_budgets(config, canvas):
    """{element_id: Zeichenzahl} – wie viel Platz jedes Text-Element hergibt.
    Ohne die Renderer-Funktion: leeres Dict."""
    fn = getattr(R, 'zeichen_budget', None)
    if not callable(fn):
        return {}
    out = {}
    for nummer, el in enumerate((config or {}).get('elements') or [], 1):
        if not isinstance(el, dict) or str(el.get('type') or 'text') != 'text':
            continue
        try:
            out[_pruef_element_id(el, nummer)] = int(fn(el, canvas))
        except Exception as ex:                      # pragma: no cover – defensiv
            log.warning('Studio: Zeichen-Budget nicht berechenbar: %s', ex)
    return out


@bp.route('/api/studio/<int:template_id>/pruefen', methods=['POST'])
@studio_login_required
def api_studio_pruefen(template_id):
    """Taugt diese Vorlage für hundert Städte?

    Rumpf (beides optional):
      config     die zu prüfende Config (Vorgabe: die gespeicherte)
      city_ids   Städte, gegen die geprüft wird (Vorgabe: alle aktiven)
    """
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    config = _config_from_payload(d, t)
    if config is None:
        return jsonify({'error': 'config muss ein Objekt sein'}), 400

    ids = d.get('city_ids')
    if ids in (None, '', []):
        staedte = _cities()
    else:
        if not isinstance(ids, (list, tuple)):
            return jsonify({'error': 'city_ids muss eine Liste sein'}), 400
        try:
            gewuenscht = [int(i) for i in ids]
        except (TypeError, ValueError):
            return jsonify({'error': 'city_ids muss Zahlen enthalten'}), 400
        gefunden = {c.id: c for c in City.query.filter(City.id.in_(gewuenscht)).all()}
        staedte = [gefunden[i] for i in gewuenscht if i in gefunden]
        if not staedte:
            return jsonify({'error': 'Keine der genannten Städte gefunden'}), 400
    gekuerzt = len(staedte) > _PRUEF_MAX_STAEDTE
    staedte = staedte[:_PRUEF_MAX_STAEDTE]

    try:
        befunde, geprueft = _pruefe_vorlage(t, config, staedte)
    except Exception as ex:
        log.exception('Studio: Vorlagen-Prüfung fehlgeschlagen')
        return jsonify({'error': f'Prüfung fehlgeschlagen: {type(ex).__name__}: {str(ex)[:160]}'}), 500

    fehler = sum(1 for b in befunde if b['schwere'] == 'fehler')
    hinweise = len(befunde) - fehler
    if not befunde:
        text = f'Keine Beanstandungen bei {geprueft} geprüften Städten.'
    else:
        teile = []
        if fehler:
            teile.append(f'{fehler} Fehler')
        if hinweise:
            teile.append(f'{hinweise} Hinweis' + ('e' if hinweise != 1 else ''))
        text = ' und '.join(teile) + f' bei {geprueft} geprüften Städten.'
    if gekuerzt:
        text += f' (Nur die ersten {_PRUEF_MAX_STAEDTE} Städte geprüft.)'
    return jsonify({'befunde': befunde[:_PRUEF_MAX_BEFUNDE],
                    'geprüfte_staedte': geprueft,
                    'fehler': fehler,
                    'hinweise': hinweise,
                    'zusammenfassung': text})


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
