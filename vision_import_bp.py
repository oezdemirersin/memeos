"""Bild-Import mit KI-Erkennung (Phase B6).

Ein fertiges Meme (PNG/JPG/WebP, z. B. schon mit "Darmstadt" im Text) wird hochgeladen und
als PIL-Template angelegt. Optional erkennt Claude die Textzeilen im Bild; jede stadtspezifische
Zeile wird zu einem cover-Element (deckt den alten Text ab) plus einem text-Element mit
Variable. Die Feinjustierung passiert im Studio (/studio/<id>).

Routen (Login):
  POST /api/templates/import/image        multipart: file, name, category, analyze, city_hint
  POST /api/templates/<id>/analyze        JSON: city_hint, force
  GET  /api/templates/import/image/help   Anleitungstext

Regeln:
  - app.py wird NIE auf Modulebene importiert (zirkulärer Import); Zugriff nur über _appmod().
  - Ohne ANTHROPIC_API_KEY oder bei KI-Fehler: Template trotzdem anlegen, analysis null, hint setzen.
  - Dateien landen unter <DATA_ROOT>/uploads/, nie unter static/.
"""
import os
import io
import re
import sys
import json
import time
import base64
import logging
import importlib
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect
from PIL import Image

from models import db, MemeTemplate, KNOWLEDGE_CATEGORIES

log = logging.getLogger(__name__)
bp = Blueprint('vision_import', __name__)

# Gleiches Modell wie die übrigen Claude-Aufrufe in app.py; per Env übersteuerbar.
VISION_MODEL = os.getenv('MEMEOS_VISION_MODEL') or 'claude-haiku-4-5-20251001'
MAX_TOKENS = 1500
MAX_EDGE = 1568            # Anthropic-Empfehlung: längste Kante, spart Tokens
COVER_GROW = 0.04          # Abdeckung um 4 % größer als die erkannte Textbox ...
COVER_FEATHER = 6
COVER_PAD_PX = 2 * COVER_FEATHER + 2   # ... plus fester Rand je Seite, damit die weiche Kante
                                       # (Gauß-Blur) nicht in den alten Text hineinreicht
JPEG_QUALITY = 85
ALLOWED_EXT = ('png', 'jpg', 'jpeg', 'webp')
_MEDIA_TYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}

HINT_NO_KEY = 'Ohne Anthropic-Key: Boxen im Studio von Hand setzen'
HINT_NO_ANALYSIS = 'Ohne KI-Analyse importiert: Boxen im Studio von Hand setzen'

_HEX_RE = re.compile(r'^#?([0-9a-fA-F]{6})$')
_VAR_CLEAN_RE = re.compile(r'[^a-z0-9_]+')
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════════════════════════════
# Infrastruktur
# ═══════════════════════════════════════════════════════════════════════════════

def _appmod():
    """app.py nur zur Laufzeit laden (kein zirkulärer Import auf Modulebene).
    Läuft app.py als __main__ (python3 app.py), liegt es unter sys.modules['__main__'];
    ein 'import app' würde die Datei ein zweites Mal ausführen (zweite Flask-App, Seeds,
    Migrationen). Darum zuerst __main__ und sys.modules prüfen."""
    main = sys.modules.get('__main__')
    if main is not None and hasattr(main, '_DATA_ROOT'):
        return main
    mod = sys.modules.get('app')
    if mod is not None:
        return mod
    try:
        return importlib.import_module('app')
    except Exception as ex:  # pragma: no cover
        log.warning(f'vision_import: app-Modul nicht ladbar: {ex}')
        return None


def _data_root():
    appmod = _appmod()
    root = getattr(appmod, '_DATA_ROOT', None) if appmod else None
    return root or os.getenv('MEMEOS_DATA_ROOT') or os.path.join(_BASE_DIR, 'instance')


def _upload_dir():
    d = os.path.join(_data_root(), 'uploads')
    os.makedirs(d, exist_ok=True)
    return d


def _api_key():
    return (os.getenv('ANTHROPIC_API_KEY') or '').strip()


def login_required(f):
    """Wie login_required in app.py; für /api/-Pfade 401 JSON statt Redirect."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Nicht angemeldet'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def _truthy(value, default=True):
    if value is None or value == '':
        return default
    return str(value).strip().lower() not in ('0', 'false', 'off', 'nein', 'no')


def _tmpl_dict(t):
    appmod = _appmod()
    fn = getattr(appmod, '_tmpl_dict', None) if appmod else None
    if fn:
        try:
            return fn(t)
        except Exception as ex:
            log.warning(f'vision_import: _tmpl_dict fehlgeschlagen: {ex}')
    return {'id': t.id, 'name': t.name, 'category': t.category,
            'preview_image': t.preview_image, 'preview_url': t.preview_url or ''}


def _load_config(t):
    try:
        cfg = json.loads(t.pil_config or '{}')
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _config_is_empty(cfg):
    return not (isinstance(cfg, dict) and isinstance(cfg.get('elements'), list) and cfg['elements'])


# ═══════════════════════════════════════════════════════════════════════════════
# Variablennamen / Kategorien
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_label(label):
    """'🚨 Problemort / Kriminalität' → 'Problemort / Kriminalität'."""
    return re.sub(r'^[^A-Za-zÄÖÜäöüß0-9]+', '', label or '').strip()


def known_variable_keys():
    return ['city_name'] + [k for k, _l, _c in KNOWLEDGE_CATEGORIES]


def _sanitize_var(name, fallback):
    s = _VAR_CLEAN_RE.sub('_', str(name or '').strip().lower()).strip('_')
    if not s or s[0].isdigit():
        return fallback
    return s[:40]


def _hex_color(value, default):
    m = _HEX_RE.match(str(value or '').strip())
    return f'#{m.group(1).upper()}' if m else default


# ═══════════════════════════════════════════════════════════════════════════════
# Bildvorbereitung + Prompt
# ═══════════════════════════════════════════════════════════════════════════════

def _image_size(path):
    with Image.open(path) as img:
        return img.size


def _prepare_image_for_ai(path):
    """Bild auf max. MAX_EDGE Kante skalieren und als JPEG q85 base64-kodieren.
    Rückgabe (b64, media_type, (orig_w, orig_h))."""
    with Image.open(path) as img:
        orig_w, orig_h = img.size
        img = img.convert('RGBA')
        scale = min(1.0, MAX_EDGE / float(max(orig_w, orig_h)))
        if scale < 1.0:
            new_size = (max(1, round(orig_w * scale)), max(1, round(orig_h * scale)))
            img = img.resize(new_size, Image.LANCZOS)
        # Transparenz auf Weiß legen (JPEG kennt kein Alpha)
        flat = Image.new('RGB', img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[3])
        buf = io.BytesIO()
        flat.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode('ascii'), 'image/jpeg', (orig_w, orig_h)


def build_prompt(city_hint=None):
    cats = '\n'.join(f'  - {k}: {_clean_label(label)}' for k, label, _c in KNOWLEDGE_CATEGORIES)
    hint_block = ''
    if city_hint:
        hint_block = (
            f'\nHinweis: Im Bild steht der Stadtname "{city_hint}". Jede Zeile, die diesen Namen '
            f'enthält (auch als Wortteil oder gebeugt, z. B. "{city_hint}er"), ist sicher '
            f'city_specific mit suggested_var "city_name".\n'
        )
    return f"""Du analysierst ein fertiges Meme-Bild einer deutschen Stadtseite auf Instagram.
Ziel: Das Bild soll als Vorlage für andere Städte dienen. Dafür brauche ich alle Textzeilen im Bild
mit ihrer Position und der Einschätzung, ob die Zeile stadtspezifisch ist.

Finde jede Textzeile (ein zusammenhängender Textblock in einer Zeile oder ein enger Absatz) und liefere:
- "text": der exakte Text der Zeile
- "box": Position als Promille (0-1000) der Bildbreite bzw. -höhe: "x", "y" (linke obere Ecke), "w", "h".
  Die Box umschließt den Text eng.
- "city_specific": true, wenn die Zeile einen Ortsnamen, Stadtnamen, Stadtteil, Verein, ein Lokal,
  eine Straße, Schule oder einen anderen ortsbezogenen Eigennamen nennt. Reiner Meme-Format-Text
  ("POV:", "Wenn du ...", "Niemand:", "Ich:", "Als ...") ist NICHT city_specific.
- "suggested_var": nur für city_specific-Zeilen. Einer dieser Schlüssel oder ein eigener snake_case-Name:
  - city_name: der Stadtname selbst
{cats}
  Für nicht stadtspezifische Zeilen null.
- "style": {{"color": "#rrggbb" (geschätzte Textfarbe), "uppercase": true|false,
  "align": "left"|"center"|"right", "weight": "bold"|"regular"}}
- "reason": kurze Begründung, höchstens 60 Zeichen

Zusätzlich "background_kind": "flat" (einfarbige Fläche hinter dem Text), "photo" (Foto oder Bild
direkt hinter dem Text) oder "mixed" (teils Fläche, teils Foto).
{hint_block}
Antworte NUR mit JSON, ohne Erklärung davor oder danach:
{{"text_blocks": [{{"text": "...", "box": {{"x": 0, "y": 0, "w": 0, "h": 0}}, "city_specific": false,
"suggested_var": null, "style": {{"color": "#FFFFFF", "uppercase": true, "align": "center", "weight": "bold"}},
"reason": "..."}}], "background_kind": "flat"}}"""


def _call_claude_vision(b64, media_type, prompt):
    """Ein Aufruf; Rückgabe (roher Antworttext, input_tokens, output_tokens, stop_reason).
    Getrennt gehalten, damit Tests den Aufruf ersetzen können."""
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key(), timeout=90.0, max_retries=1)
    msg = client.messages.create(
        model=VISION_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}},
                {'type': 'text', 'text': prompt},
            ],
        }],
    )
    raw = ''.join(getattr(b, 'text', '') for b in msg.content if getattr(b, 'type', '') == 'text')
    usage = getattr(msg, 'usage', None)
    in_tok = int(getattr(usage, 'input_tokens', 0) or 0)
    out_tok = int(getattr(usage, 'output_tokens', 0) or 0)
    return raw, in_tok, out_tok, getattr(msg, 'stop_reason', None)


def _log_usage(in_tok, out_tok):
    appmod = _appmod()
    fn = getattr(appmod, '_log_ai_usage', None) if appmod else None
    if not fn:
        return
    try:
        fn('vision_import', VISION_MODEL, in_tok, out_tok)
    except Exception as ex:
        log.warning(f'vision_import: _log_ai_usage fehlgeschlagen: {ex}')
        try:
            db.session.rollback()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Analyse-Antwort normalisieren
# ═══════════════════════════════════════════════════════════════════════════════

def _promille(v):
    try:
        return max(0, min(1000, int(round(float(v)))))
    except Exception:
        return None


def normalize_analysis(data, city_hint=None):
    """Rohes KI-JSON → verlässliche Struktur. Wirft ValueError, wenn nichts Brauchbares drin ist."""
    if not isinstance(data, dict):
        raise ValueError('Antwort ist kein JSON-Objekt')
    raw_blocks = data.get('text_blocks')
    if not isinstance(raw_blocks, list):
        raise ValueError('text_blocks fehlt')
    hint = (city_hint or '').strip().lower()
    blocks = []
    for i, rb in enumerate(raw_blocks):
        if not isinstance(rb, dict):
            continue
        text = str(rb.get('text') or '').strip()
        box = rb.get('box') if isinstance(rb.get('box'), dict) else {}
        x, y, w, h = (_promille(box.get('x')), _promille(box.get('y')),
                      _promille(box.get('w')), _promille(box.get('h')))
        if not text or None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        w = min(w, 1000 - x) or 1
        h = min(h, 1000 - y) or 1
        city_specific = bool(rb.get('city_specific'))
        var = rb.get('suggested_var')
        if hint and hint in text.lower():
            city_specific = True
            if not var or str(var).strip().lower() in ('', 'null', 'none'):
                var = 'city_name'
        style = rb.get('style') if isinstance(rb.get('style'), dict) else {}
        align = str(style.get('align') or 'center').lower()
        weight = str(style.get('weight') or 'bold').lower()
        blocks.append({
            'index': len(blocks),
            'text': text,
            'box': {'x': x, 'y': y, 'w': w, 'h': h},
            'city_specific': city_specific,
            'suggested_var': _sanitize_var(var, f'text_{len(blocks)}') if city_specific else None,
            'style': {
                'color': _hex_color(style.get('color'), '#FFFFFF'),
                'uppercase': bool(style.get('uppercase', text.isupper())),
                'align': align if align in ('left', 'center', 'right') else 'center',
                'weight': 'bold' if weight != 'regular' else 'regular',
            },
            'reason': str(rb.get('reason') or '')[:120],
        })
    bg = str(data.get('background_kind') or 'flat').lower()
    return {
        'text_blocks': blocks,
        'background_kind': bg if bg in ('flat', 'photo', 'mixed') else 'flat',
    }


def analyze_image(path, city_hint=None):
    """Bild an Claude schicken. Rückgabe normalisierte Analyse oder {'error': ...}. Wirft nie."""
    if not _api_key():
        return {'error': HINT_NO_KEY}
    try:
        b64, media_type, _size = _prepare_image_for_ai(path)
    except Exception as ex:
        return {'error': f'Bild nicht lesbar: {type(ex).__name__}: {str(ex)[:120]}'}
    try:
        raw, in_tok, out_tok, stop_reason = _call_claude_vision(b64, media_type, build_prompt(city_hint))
    except Exception as ex:
        log.error(f'vision_import: Claude-Aufruf fehlgeschlagen: {ex}')
        return {'error': f'KI nicht erreichbar: {type(ex).__name__}: {str(ex)[:120]}'}
    _log_usage(in_tok, out_tok)
    if stop_reason == 'max_tokens':
        return {'error': 'KI-Antwort abgeschnitten (zu viele Textzeilen?)'}
    match = re.search(r'\{.*\}', raw or '', re.DOTALL)
    if not match:
        return {'error': 'KI-Antwort ohne JSON'}
    try:
        data = json.loads(match.group(0))
    except Exception:
        return {'error': 'KI-Antwort kein gültiges JSON'}
    try:
        analysis = normalize_analysis(data, city_hint)
    except ValueError as ex:
        return {'error': f'KI-Antwort unbrauchbar: {ex}'}
    analysis['model'] = VISION_MODEL
    analysis['usage'] = {'input_tokens': in_tok, 'output_tokens': out_tok}
    return analysis


# ═══════════════════════════════════════════════════════════════════════════════
# Config-Erzeugung (reine Funktion, unit-testbar)
# ═══════════════════════════════════════════════════════════════════════════════

def _px_box(box, w, h, grow=0.0, pad_px=0):
    """Promille-Box → Pixel-Box (x, y, width, height), optional um grow (Anteil) und pad_px
    (Pixel je Seite) vergrößert und auf das Bild begrenzt."""
    x = box['x'] / 1000.0 * w
    y = box['y'] / 1000.0 * h
    bw = box['w'] / 1000.0 * w
    bh = box['h'] / 1000.0 * h
    if grow or pad_px:
        dx = bw * grow / 2.0 + pad_px
        dy = bh * grow / 2.0 + pad_px
        x -= dx
        y -= dy
        bw += 2 * dx
        bh += 2 * dy
    x0 = max(0, int(round(x)))
    y0 = max(0, int(round(y)))
    x1 = min(w, int(round(x + bw)))
    y1 = min(h, int(round(y + bh)))
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def build_config_from_analysis(analysis, w, h):
    """Analyse → pil_config gemäß Renderer-Vertrag (memeos_render).

    Pro city_specific-Block: cover_<i> (Box +4 % und +COVER_PAD_PX je Seite, fill inpaint bei
    photo/mixed sonst auto, feather 6)
    und danach text_<i> (var = suggested_var, gleiche Box, Schrift/Farbe/Ausrichtung aus style,
    fallback = Originaltext). Nicht-stadtspezifische Blöcke bleiben im Bild (kein Element).
    """
    w, h = int(w), int(h)
    blocks = (analysis or {}).get('text_blocks') or []
    bg = (analysis or {}).get('background_kind') or 'flat'
    photo = bg in ('photo', 'mixed')
    elements = []
    for i, blk in enumerate(blocks):
        if not blk.get('city_specific'):
            continue
        idx = blk.get('index', i)
        x, y, bw, bh = _px_box(blk['box'], w, h, grow=COVER_GROW, pad_px=COVER_PAD_PX)
        style = blk.get('style') or {}
        max_size = max(14, int(round(bh * 0.8)))
        min_size = max(14, int(round(max_size * 0.4)))
        min_size = min(min_size, max_size)
        elements.append({
            'id': f'cover_{idx}', 'type': 'cover',
            'x': x, 'y': y, 'width': bw, 'height': bh,
            'fill': 'inpaint' if photo else 'auto', 'feather': COVER_FEATHER,
        })
        text_el = {
            'id': f'text_{idx}', 'type': 'text',
            'var': blk.get('suggested_var') or f'text_{idx}',
            'x': x, 'y': y, 'width': bw, 'height': bh,
            'font': 'anton' if style.get('weight', 'bold') != 'regular' else 'bold',
            'max_size': max_size, 'min_size': min_size,
            'color': _hex_color(style.get('color'), '#FFFFFF'),
            'align': style.get('align') if style.get('align') in ('left', 'center', 'right') else 'center',
            'valign': 'middle',
            'uppercase': bool(style.get('uppercase', False)),
            'fit': 'shrink',
            # fallback = erkannte Originalzeile. Liefert die KI für die Variable nichts, steht sie
            # beim Rendern wieder da statt einer leeren Fläche – im Vorrat sofort erkennbar.
            'fallback': str(blk.get('text') or '').strip(),
        }
        if photo:
            text_el['stroke'] = '#000000'
            text_el['stroke_width'] = 3
        elements.append(text_el)
    return {'canvas': {'width': w, 'height': h}, 'elements': elements}


def summarize_analysis(analysis, w=None, h=None):
    """Antwort-Payload für Dashboard/Studio: Blöcke mit Pixelboxen, feste Texte, Variablen, Zählung."""
    blocks = (analysis or {}).get('text_blocks') or []
    out_blocks = []
    variables = []
    for blk in blocks:
        b = dict(blk)
        if w and h:
            x, y, bw, bh = _px_box(blk['box'], w, h)
            b['box_px'] = {'x': x, 'y': y, 'w': bw, 'h': bh}
        out_blocks.append(b)
        if blk.get('city_specific') and blk.get('suggested_var') and blk['suggested_var'] not in variables:
            variables.append(blk['suggested_var'])
    city_count = sum(1 for b in blocks if b.get('city_specific'))
    return {
        'text_blocks': out_blocks,
        'background_kind': (analysis or {}).get('background_kind') or 'flat',
        'fixed_texts': [b['text'] for b in blocks if not b.get('city_specific')],
        'variables': variables,
        'counts': {'total': len(blocks), 'city_specific': city_count},
        'model': (analysis or {}).get('model'),
        'usage': (analysis or {}).get('usage'),
    }


def _hint_for(summary):
    n = summary['counts']['total']
    m = summary['counts']['city_specific']
    return f'{n} Textzeilen erkannt, {m} als stadtspezifisch markiert'


def _notes_for(summary, city_hint=None):
    stamp = datetime.now().strftime('%d.%m.%Y %H:%M')
    lines = [f'KI-Analyse vom {stamp} ({summary.get("model") or VISION_MODEL})'
             + (f', Stadt im Bild: {city_hint}' if city_hint else '') + ':']
    for b in summary['text_blocks']:
        mark = f'→ {b["suggested_var"]}' if b.get('city_specific') else 'fest im Bild'
        lines.append(f'- "{b["text"]}" [{mark}]')
    lines.append(f'Hintergrund: {summary.get("background_kind")}')
    return '\n'.join(lines)


def _apply_analysis(t, analysis, w, h, city_hint=None):
    """Config, required_vars und notes aus der Analyse ins Template schreiben (ohne commit)."""
    config = build_config_from_analysis(analysis, w, h)
    summary = summarize_analysis(analysis, w, h)
    t.pil_config = json.dumps(config, ensure_ascii=False)
    t.required_vars = json.dumps(summary['variables'])
    note = _notes_for(summary, city_hint)
    t.notes = (note + ('\n\n' + t.notes if (t.notes or '').strip() else ''))[:4000]
    return config, summary


# ═══════════════════════════════════════════════════════════════════════════════
# Routen
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_upload(file_storage):
    """Rückgabe (ext, (w, h)) oder wirft ValueError mit deutschem Text."""
    fname = file_storage.filename or ''
    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if ext not in ALLOWED_EXT:
        raise ValueError('Nur PNG, JPG oder WebP erlaubt')
    try:
        stream = file_storage.stream
        stream.seek(0)
        with Image.open(stream) as img:
            img.verify()
        stream.seek(0)
        with Image.open(stream) as img:
            size = img.size
            fmt = (img.format or '').lower()
        stream.seek(0)
    except Exception:
        raise ValueError('Datei ist kein lesbares Bild')
    if size[0] < 100 or size[1] < 100:
        raise ValueError('Bild zu klein (mindestens 100 × 100 Pixel)')
    ext = {'jpeg': 'jpg', 'png': 'png', 'webp': 'webp'}.get(fmt, 'jpg' if ext == 'jpeg' else ext)
    return ext, size


def _save_upload(file_storage, ext):
    updir = _upload_dir()
    ts = int(time.time())
    filename = f'import_img_{ts}.{ext}'
    n = 1
    while os.path.exists(os.path.join(updir, filename)):
        n += 1
        filename = f'import_img_{ts}_{n}.{ext}'
    path = os.path.join(updir, filename)
    file_storage.save(path)
    return filename, path


@bp.route('/api/templates/import/image', methods=['POST'])
@login_required
def api_import_image():
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'error': 'Keine Datei (Feld "file" fehlt)'}), 400
    f = request.files['file']
    try:
        ext, (w, h) = _validate_upload(f)
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400

    name = (request.form.get('name') or '').strip()
    if not name:
        stem = os.path.splitext(os.path.basename(f.filename or ''))[0]
        name = re.sub(r'[_\-]+', ' ', stem).strip() or 'Importiertes Bild'
    name = name[:200]
    category = (request.form.get('category') or 'allgemein').strip() or 'allgemein'
    analyze = _truthy(request.form.get('analyze'), default=True)
    city_hint = (request.form.get('city_hint') or '').strip() or None

    filename, path = _save_upload(f, ext)

    t = MemeTemplate(
        name=name,
        description=f'Importiert aus Bild am {datetime.now().strftime("%d.%m.%Y")}',
        category=category,
        render_type='pil',
        pil_config=json.dumps({'canvas': {'width': w, 'height': h}, 'elements': []}),
        required_vars='[]',
        tags='[]',
        preview_image=filename,
        notes=(f'Stadt im Bild: {city_hint}' if city_hint else ''),
    )
    appmod = _appmod()
    upload_fn = getattr(appmod, '_upload_cloudinary', None) if appmod else None
    if upload_fn:
        try:
            cloud_url = upload_fn(path, folder='memeos/templates', resource_type='image')
            if cloud_url:
                t.preview_url = cloud_url
        except Exception as ex:
            log.warning(f'vision_import: Cloudinary-Upload fehlgeschlagen: {ex}')
    db.session.add(t)
    db.session.commit()

    analysis_payload = None
    if not analyze:
        hint = HINT_NO_ANALYSIS
    elif not _api_key():
        hint = HINT_NO_KEY
    else:
        analysis = analyze_image(path, city_hint)
        if analysis.get('error'):
            hint = f'KI-Analyse fehlgeschlagen ({analysis["error"]}). Boxen im Studio von Hand setzen'
        else:
            _config, summary = _apply_analysis(t, analysis, w, h, city_hint)
            db.session.commit()
            analysis_payload = summary
            hint = _hint_for(summary)

    return jsonify({
        'id': t.id,
        'studio_url': f'/studio/{t.id}',
        'analysis': analysis_payload,
        'hint': hint,
        'width': w, 'height': h,
        'preview_url': t.preview_url or f'/uploads/{filename}',
        'template': _tmpl_dict(t),
    }), 201


@bp.route('/api/templates/<int:tmpl_id>/analyze', methods=['POST'])
@login_required
def api_template_analyze(tmpl_id):
    t = MemeTemplate.query.get_or_404(tmpl_id)
    d = request.get_json(silent=True) or {}
    city_hint = (d.get('city_hint') or request.form.get('city_hint') or '').strip() or None
    force = _truthy(d.get('force', request.form.get('force')), default=False)

    if not _api_key():
        return jsonify({'error': 'Ohne Anthropic-Key keine Analyse möglich', 'hint': HINT_NO_KEY}), 400

    appmod = _appmod()
    bg_fn = getattr(appmod, '_template_bg_path', None) if appmod else None
    path = None
    if bg_fn:
        try:
            path = bg_fn(t)
        except Exception as ex:
            log.warning(f'vision_import: _template_bg_path fehlgeschlagen: {ex}')
    if not path and t.preview_image:
        candidate = os.path.join(_upload_dir(), os.path.basename(t.preview_image))
        path = candidate if os.path.exists(candidate) else None
    if not path:
        return jsonify({'error': 'Kein Hintergrundbild beim Template hinterlegt'}), 400
    try:
        w, h = _image_size(path)
    except Exception:
        return jsonify({'error': 'Hintergrundbild nicht lesbar'}), 400

    analysis = analyze_image(path, city_hint)
    if analysis.get('error'):
        return jsonify({'error': analysis['error'],
                        'hint': 'KI-Analyse fehlgeschlagen. Boxen im Studio von Hand setzen'}), 502

    existing = _load_config(t)
    applied = False
    if _config_is_empty(existing) or force:
        _config, summary = _apply_analysis(t, analysis, w, h, city_hint)
        db.session.commit()
        applied = True
        hint = _hint_for(summary)
    else:
        summary = summarize_analysis(analysis, w, h)
        hint = (_hint_for(summary)
                + f'; bestehende Config mit {len(existing["elements"])} Elementen NICHT überschrieben '
                  '(force=true zum Überschreiben)')

    return jsonify({
        'id': t.id,
        'studio_url': f'/studio/{t.id}',
        'analysis': summary,
        'hint': hint,
        'applied': applied,
        'width': w, 'height': h,
        'template': _tmpl_dict(t),
    })


HELP_TEXT = """Bild-Import: Ein fertiges Meme als Vorlage für andere Städte nutzen.

1. Bild wählen (PNG, JPG oder WebP), am besten das Original in voller Größe.
2. Name und Kategorie vergeben. Bei "Stadt im Bild" die Stadt eintragen, deren Name im Meme steht.
   Dann werden alle Zeilen mit diesem Namen sicher als stadtspezifisch erkannt.
3. "Textzeilen mit KI erkennen" angehakt lassen: Claude findet die Textzeilen, ihre Position und
   markiert stadtspezifische Zeilen (Ortsnamen, Stadtteile, Vereine, Lokale, Straßen, Schulen).
   Meme-Format-Text wie "POV:" bleibt fest im Bild.
4. Ergebnis: Für jede stadtspezifische Zeile entsteht eine Abdeckung über dem alten Text plus eine
   Textbox mit Variable (z. B. city_name). Feste Zeilen bleiben unverändert im Bild.
5. Mit "Im Studio öffnen" Boxen verschieben, Schrift, Farbe und Variablen anpassen.

Ohne Anthropic-Key wird das Bild trotzdem als Template angelegt; die Boxen setzt du dann im Studio
von Hand. Die Analyse lässt sich später jederzeit über "Erneut analysieren" nachholen."""


@bp.route('/api/templates/import/image/help', methods=['GET'])
@login_required
def api_import_image_help():
    return jsonify({
        'help': HELP_TEXT,
        'ai_available': bool(_api_key()),
        'model': VISION_MODEL,
        'allowed_extensions': list(ALLOWED_EXT),
        'variables': known_variable_keys(),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Registrierung
# ═══════════════════════════════════════════════════════════════════════════════

def init_app(flask_app):
    if 'vision_import' in flask_app.blueprints:
        return
    flask_app.register_blueprint(bp)
    log.info('vision_import: Blueprint registriert (Modell %s)', VISION_MODEL)
