# -*- coding: utf-8 -*-
"""
studio_bp – Template-Studio (Phase B2): visueller Editor für pil_config eines MemeTemplate.

Blueprint 'studio'. Registrierung in app.py (register_extensions):
    import studio_bp
    studio_bp.init_app(flask_app)

Routen (alle login-geschützt, /api/-Routen liefern 401 JSON statt Redirect):
    GET  /studio/<int:template_id>              Editor-Seite (templates/studio.html)
    GET  /api/studio/<int:id>                   Template, Config, Hintergrund, Canvas, Schriften, Variablen, Städte
    PUT  /api/studio/<int:id>/config            {config, name?, series?, series_position?} -> ok/required_vars/errors
    POST /api/studio/<int:id>/validate          {config} -> {errors}
    POST /api/studio/<int:id>/background        multipart 'file' (png/jpg/webp) -> background_url, canvas
    POST /api/studio/<int:id>/preview           {config, city_id, values?} -> PNG
    POST /api/studio/<int:id>/preview-ai        {city_id, config?} -> {values, fit_score, reasoning, brief, image_b64}
    POST /api/studio/<int:id>/duplicate         -> {id}
    GET  /api/studio/fonts                      Schriftenliste
    POST /api/studio/fonts                      multipart 'file' (.ttf/.otf, max 5 MB) -> Schriftenliste
    GET  /api/studio/fonts/<key>/file           Schriftdatei (für @font-face in der Bühne)

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

from models import db, City, CityKnowledge, MemeTemplate, KNOWLEDGE_CATEGORIES
import memeos_render as R

log = logging.getLogger(__name__)
bp = Blueprint('studio', __name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_IMAGE_EXTS = {'png': 'png', 'jpg': 'jpg', 'jpeg': 'jpg', 'webp': 'webp'}
_FONT_EXTS = ('.ttf', '.otf')
_FONT_MAX_BYTES = 5 * 1024 * 1024
_BG_MAX_BYTES = 25 * 1024 * 1024
_VAR_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
_KNOWLEDGE_KEYS = [k for k, _, _ in KNOWLEDGE_CATEGORIES]
_DEFAULT_CANVAS = {'width': 1080, 'height': 1350}


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
    if not (isinstance(canvas, dict) and R._is_num(canvas.get('width')) and R._is_num(canvas.get('height'))
            and float(canvas['width']) > 0 and float(canvas['height']) > 0):
        cfg.pop('canvas', None)
    return cfg


def _collect_vars(config, include_hidden=False):
    """Alle Variablennamen aus text-/image-Elementen (var + {var} in festen Texten)."""
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
    return sorted(names)


def _clean_label(label):
    """Emoji-Präfix der Kategorie-Labels entfernen (Studio-UI ist emojifrei)."""
    s = re.sub(r'^[^\w(]+', '', str(label or ''), flags=re.UNICODE).strip()
    return s or str(label or '')


def _variables_for(config):
    variables = [{'key': 'city_name', 'label': 'Stadtname', 'kind': 'city'}]
    variables += [{'key': k, 'label': _clean_label(lbl), 'kind': 'knowledge'} for k, lbl, _ in KNOWLEDGE_CATEGORIES]
    known = {v['key'] for v in variables}
    for name in _collect_vars(config, include_hidden=True):
        if name not in known:
            variables.append({'key': name, 'label': name, 'kind': 'free'})
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
    """Canvas, wie der Renderer sie sieht: config.canvas gewinnt, sonst Bildgröße."""
    c = (config or {}).get('canvas')
    if isinstance(c, dict) and R._is_num(c.get('width')) and R._is_num(c.get('height')):
        return {'width': int(float(c['width'])), 'height': int(float(c['height']))}
    return image_size


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


def _values_for_city(city, config, extra=None):
    """city_name = Stadt; Wissens-Variablen = bester aktiver Eintrag; Rest Platzhalter; extra überschreibt."""
    values = {}
    if city:
        values['city_name'] = city.name
    for name in _collect_vars(config, include_hidden=True):
        if name == 'city_name':
            continue
        val = None
        if city and name in _KNOWLEDGE_KEYS:
            val = _knowledge_value(city, name)
        values[name] = val if val else R.sample_values_placeholder(name)
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            s = str(v)
            if s.strip():
                values[str(k)] = s
    return values


def _render_png(template, config, values, city):
    bg = _bg_path(template)
    if not bg:
        return None, ('Kein Hintergrundbild. Bitte zuerst über „Hintergrund ersetzen“ ein Bild hochladen.', 400)
    try:
        png = R.render(bg, config, values, brand=_city_brand(city) if city else None)
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
    return jsonify({
        'template': _template_dict(t),
        'config': config,
        'background_url': background_url,
        'canvas': _canvas_for(config, image_size),
        'image_size': image_size,
        'fonts': _public_fonts(),
        'variables': _variables_for(config),
        'cities': [{'id': c.id, 'name': c.name, 'state': c.state or ''} for c in cities],
        'brand_example': _city_brand(cities[0]) if cities else _city_brand(None),
        'ai_available': bool(_anthropic_key()),
    })


@bp.route('/api/studio/<int:template_id>/validate', methods=['POST'])
@studio_login_required
def api_studio_validate(template_id):
    t = MemeTemplate.query.get_or_404(template_id)
    d = request.get_json(silent=True) or {}
    config = _config_from_payload(d, t)
    if config is None:
        return jsonify({'errors': ['config muss ein Objekt sein']}), 400
    return jsonify({'errors': R.validate_config(config), 'required_vars': _collect_vars(config)})


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
    required = _collect_vars(config)
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
    if not _bg_path(t):
        return jsonify({'error': 'Kein Hintergrundbild. Bitte zuerst ein Bild hochladen.'}), 400

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
    log.info('Template-Studio registriert (/studio/<id>)')
