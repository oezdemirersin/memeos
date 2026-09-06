# -*- coding: utf-8 -*-
"""
memeos_render – lokaler PIL-Renderer für Meme-Templates (ohne Canva, ohne app-Import).

Vertrag (siehe Integrationsnotiz scratchpad/integration/renderer.md):
    list_fonts()                                  -> [{'key','label','path','source'}]
    resolve_font_path(name_or_key, brand=None)    -> Pfad (tolerant, Fallback anton)
    font_key_for(name)                            -> (key, exact: bool)
    validate_config(config)                       -> Liste von Fehlertexten (leer = ok)
    render(bg_path, config, values, brand=None)   -> PNG-Bytes (RGB)
    measure_text_box(text, font_key, size, max_width) -> (Zeilen, Höhe)
    sample_values_placeholder(var)                -> '[var]'

Config-Format:
    {"canvas": {"width": W, "height": H},          # optional, sonst Größe des Hintergrunds
     "elements": [ {...}, ... ]}                    # Reihenfolge = Zeichenreihenfolge

Elemente (Koordinaten in Canvas-Pixeln):
    gemeinsam: id, type ('text'|'image'|'cover'|'rect'), x, y, width, height,
               opacity (0-1, default 1), hidden (bool)
    text:  var ODER text (fester Text, {var} wird ersetzt), font (Key/Name/'brand'),
           max_size, min_size, color, stroke, stroke_width,
           shadow {'dx','dy','color','blur'}, align left|center|right,
           valign top|middle|bottom, uppercase, max_lines, line_height (1.15),
           letter_spacing (px), fallback, fit 'shrink' (Standard) | 'none'
    image: var (Pfad/URL) ODER src, fit 'contain'|'cover', radius
    cover: fill 'auto' | '#hex' | 'inpaint' | 'brand:x', feather (px)
    rect:  fill, radius, stroke, stroke_width
    Farben: '#rgb', '#rrggbb', '#rrggbbaa', CSS-Name oder 'brand:bg'|'brand:text'|'brand:accent'.

Nur Pillow ist Pflicht. cv2 + numpy werden für cover:'inpaint' genutzt, wenn vorhanden
(sonst Fallback auf 'auto'). requests wird nur für Bild-URLs gebraucht.
"""
import io
import json
import logging
import os
import re
import statistics
import threading

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

log = logging.getLogger('memeos_render')

__all__ = [
    'FONT_BASE_DIR', 'USER_FONT_DIR', 'CANVA_FONT_MAP',
    'list_fonts', 'resolve_font_path', 'font_key_for', 'load_font',
    'validate_config', 'render', 'fit_text', 'measure_text_box',
    'sample_values_placeholder', 'parse_color',
]

# ═══════════════════════════════════════════════════════════════════════════════
# Schriften
# ═══════════════════════════════════════════════════════════════════════════════

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_BASE_DIR = os.path.join(_BASE_DIR, 'fonts')                       # mitgeliefert (Repo)
_DATA_ROOT = os.getenv('MEMEOS_DATA_ROOT') or os.path.join(_BASE_DIR, 'instance')
USER_FONT_DIR = os.path.join(_DATA_ROOT, 'fonts')                      # eigene Uploads
_UPLOAD_DIR = os.path.join(_DATA_ROOT, 'uploads')

_FONT_EXTS = ('.ttf', '.otf', '.ttc')

# key -> (Label, Dateiname)
_BUILTIN_FONTS = {
    'anton':              ('Anton', 'anton.ttf'),
    'bold':               ('Oswald Bold', 'bold.ttf'),
    'arial_rounded_bold': ('Arial Rounded Bold', 'arial_rounded_bold.ttf'),
}
_FALLBACK_KEY = 'anton'

# Canva-/Systemschriftnamen -> eingebauter Key
CANVA_FONT_MAP = {
    'Bebas Neue':            'anton',
    'Bebas Neue Bold':       'anton',
    'Bebas':                 'anton',
    'Anton':                 'anton',
    'Impact':                'anton',
    'League Gothic':         'anton',
    'Oswald':                'bold',
    'Oswald Bold':           'bold',
    'Arial Black':           'bold',
    'Montserrat':            'bold',
    'Montserrat Bold':       'bold',
    'Arial':                 'bold',
    'Helvetica':             'bold',
    'Roboto':                'bold',
    'Open Sans':             'bold',
    'DM Sans':               'bold',
    'Arial Rounded MT Bold': 'arial_rounded_bold',
    'Arial Rounded':         'arial_rounded_bold',
    'VAG Rounded':           'arial_rounded_bold',
}
# Aliase, die dieselbe Schriftfamilie meinen (font_key_for -> exact=True)
_EXACT_ALIASES = {'anton', 'oswald', 'oswaldbold', 'arialroundedmtbold', 'arialroundedbold'}

_BRAND_DEFAULTS = {'bg': '#ffffff', 'text': '#000000', 'accent': '#3b82f6', 'font': 'Arial'}

_font_cache = {}
_font_lock = threading.Lock()


def _norm(name):
    """Schriftname normalisieren: Kleinschreibung, ohne Leerzeichen/Bindestriche/Unterstriche, ohne Endung."""
    s = str(name or '').strip().lower()
    s = re.sub(r'\.(ttf|otf|ttc)$', '', s)
    return re.sub(r'[\s\-_]+', '', s)


def _user_font_files():
    out = []
    try:
        if os.path.isdir(USER_FONT_DIR):
            for fn in sorted(os.listdir(USER_FONT_DIR)):
                if fn.lower().endswith(_FONT_EXTS) and os.path.isfile(os.path.join(USER_FONT_DIR, fn)):
                    out.append(fn)
    except OSError as ex:
        log.warning('Schriftordner %s nicht lesbar: %s', USER_FONT_DIR, ex)
    return out


def list_fonts():
    """Alle verfügbaren Schriften: eingebaute zuerst, dann eigene Uploads aus USER_FONT_DIR."""
    fonts = []
    for key, (label, fn) in _BUILTIN_FONTS.items():
        path = os.path.join(FONT_BASE_DIR, fn)
        if os.path.isfile(path):
            fonts.append({'key': key, 'label': label, 'path': path, 'source': 'builtin'})
    for fn in _user_font_files():
        stem = os.path.splitext(fn)[0]
        key = re.sub(r'[\s\-]+', '_', stem.strip().lower())
        label = re.sub(r'[_\-]+', ' ', stem).strip() or stem
        fonts.append({'key': key, 'label': label, 'path': os.path.join(USER_FONT_DIR, fn), 'source': 'user'})
    return fonts


def _font_index():
    """normalisierter Name -> (key, path). Eigene Uploads gewinnen vor eingebauten Schriften."""
    idx = {}
    for f in list_fonts():                      # builtin zuerst eintragen …
        if f['source'] == 'builtin':
            idx[_norm(f['key'])] = (f['key'], f['path'])
            idx[_norm(f['label'])] = (f['key'], f['path'])
            idx[_norm(os.path.basename(f['path']))] = (f['key'], f['path'])
    for f in list_fonts():                      # … dann von user überschreiben lassen
        if f['source'] == 'user':
            idx[_norm(f['key'])] = (f['key'], f['path'])
            idx[_norm(f['label'])] = (f['key'], f['path'])
            idx[_norm(os.path.basename(f['path']))] = (f['key'], f['path'])
    return idx


_CANVA_NORM = {_norm(k): v for k, v in CANVA_FONT_MAP.items()}


def _fallback_path():
    return os.path.join(FONT_BASE_DIR, _BUILTIN_FONTS[_FALLBACK_KEY][1])


def font_key_for(name):
    """Liefert (key, exact). key versteht resolve_font_path; exact=False heißt: Ersatzschrift.
    'brand' bleibt 'brand' (wird beim Rendern über brand['font'] aufgelöst)."""
    s = str(name or '').strip()
    if s.lower() == 'brand':
        return 'brand', True
    n = _norm(s)
    if not n:
        return _FALLBACK_KEY, False
    idx = _font_index()
    if n in idx:
        return idx[n][0], True
    if n in _CANVA_NORM:
        return _CANVA_NORM[n], n in _EXACT_ALIASES
    # Dateiname in einem der Ordner?
    safe = os.path.basename(s)
    for folder in (USER_FONT_DIR, FONT_BASE_DIR):
        for fn in (safe, safe + '.ttf', safe + '.otf'):
            p = os.path.join(folder, fn)
            if fn and os.path.isfile(p):
                return re.sub(r'[\s\-]+', '_', os.path.splitext(fn)[0].lower()), True
    return _FALLBACK_KEY, False


def resolve_font_path(name_or_key, brand=None):
    """Pfad zur Schriftdatei. Tolerant gegen Groß-/Kleinschreibung, Leerzeichen, Bindestriche,
    '.ttf'. Kennt Canva-/Systemnamen (CANVA_FONT_MAP), eigene Uploads, absolute Pfade und
    'brand' (-> brand['font']). Fallback: anton."""
    s = str(name_or_key or '').strip()
    if s.lower() == 'brand':
        s = str((brand or {}).get('font') or _BRAND_DEFAULTS['font'])
    if not s:
        return _fallback_path()
    if os.path.isabs(s) and os.path.isfile(s) and s.lower().endswith(_FONT_EXTS):
        return s
    n = _norm(s)
    idx = _font_index()
    if n in idx:
        return idx[n][1]
    if n in _CANVA_NORM:
        key = _CANVA_NORM[n]
        if key in idx:
            return idx[key][1]
        return os.path.join(FONT_BASE_DIR, _BUILTIN_FONTS[key][1])
    safe = os.path.basename(s)
    for folder in (USER_FONT_DIR, FONT_BASE_DIR):
        for fn in (safe, safe + '.ttf', safe + '.otf'):
            p = os.path.join(folder, fn)
            if fn and os.path.isfile(p):
                return p
    return _fallback_path()


def load_font(name_or_key, size, brand=None):
    """ImageFont mit Cache. Bei kaputter/fehlender Datei Pillow-Standardschrift."""
    size = max(1, int(round(float(size or 1))))
    path = resolve_font_path(name_or_key, brand=brand)
    key = (path, size)
    with _font_lock:
        font = _font_cache.get(key)
    if font is not None:
        return font
    try:
        font = ImageFont.truetype(path, size)
    except Exception as ex:
        log.warning('Schrift %s nicht ladbar (%s), nutze Standardschrift', path, ex)
        try:
            font = ImageFont.load_default(size)
        except TypeError:
            font = ImageFont.load_default()
    with _font_lock:
        _font_cache[key] = font
    return font


# ═══════════════════════════════════════════════════════════════════════════════
# Farben / Zahlen
# ═══════════════════════════════════════════════════════════════════════════════

def parse_color(spec, brand=None, default=None):
    """Farbangabe -> RGBA-Tupel. Versteht '#rgb', '#rrggbb', '#rrggbbaa', CSS-Namen, 'rgb(...)',
    Tupel/Listen und 'brand:bg'|'brand:text'|'brand:accent'. Ungültig -> default."""
    if spec is None or spec == '' or spec is False:
        return default
    if isinstance(spec, (tuple, list)):
        try:
            vals = [max(0, min(255, int(v))) for v in spec]
            if len(vals) == 3:
                vals.append(255)
            if len(vals) == 4:
                return tuple(vals)
        except (TypeError, ValueError):
            pass
        return default
    s = str(spec).strip()
    if s.lower().startswith('brand:'):
        key = s.split(':', 1)[1].strip().lower()
        merged = dict(_BRAND_DEFAULTS)
        merged.update({k: v for k, v in (brand or {}).items() if v})
        s = merged.get(key)
        if s is None:
            log.warning('Unbekannte Brand-Farbe %r', spec)
            return default
        s = str(s).strip()
    try:
        return ImageColor.getcolor(s, 'RGBA')
    except (ValueError, TypeError):
        log.warning('Ungültige Farbe %r', spec)
        return default


def _is_num(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v.strip())
            return True
        except ValueError:
            return False
    return False


def _num(v, default=0.0):
    try:
        if isinstance(v, bool):
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _int(v, default=0):
    return int(round(_num(v, default)))


def _bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'ja', 'yes', 'on')
    return bool(v)


# ═══════════════════════════════════════════════════════════════════════════════
# Text: Umbruch, Schrumpfen, Kürzen
# ═══════════════════════════════════════════════════════════════════════════════

def _text_width(font, s, spacing=0.0):
    if not s:
        return 0.0
    if spacing:
        return sum(font.getlength(ch) for ch in s) + spacing * (len(s) - 1)
    return font.getlength(s)


def _break_word(word, font, max_width, spacing):
    """Zu langes Wort zeichenweise auf mehrere Zeilen verteilen."""
    pieces, cur = [], ''
    for ch in word:
        trial = cur + ch
        if _text_width(font, trial, spacing) <= max_width or not cur:
            cur = trial
        else:
            pieces.append(cur)
            cur = ch
    if cur:
        pieces.append(cur)
    return pieces or ['']


def _wrap(text, font, max_width, spacing=0.0, hard_break=False):
    """Zeilenumbruch an Wortgrenzen; '\\n' im Text erzwingt eine neue Zeile.
    Liefert (Zeilen, overflow). overflow=True, wenn ein einzelnes Wort breiter als max_width ist
    und nicht hart getrennt wurde."""
    lines, overflow = [], False
    for para in str(text).split('\n'):
        words = para.split()
        if not words:
            lines.append('')
            continue
        cur = ''
        for w in words:
            trial = f'{cur} {w}'.strip()
            if _text_width(font, trial, spacing) <= max_width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            if _text_width(font, w, spacing) > max_width:
                if hard_break:
                    pieces = _break_word(w, font, max_width, spacing)
                    lines.extend(pieces[:-1])
                    cur = pieces[-1]
                else:
                    overflow = True
                    cur = w
            else:
                cur = w
        if cur:
            lines.append(cur)
    # führende/abschließende Leerzeilen entfernen, innere behalten
    while lines and lines[0] == '':
        lines.pop(0)
    while lines and lines[-1] == '':
        lines.pop()
    return (lines or ['']), overflow


def _ellipsize(line, font, max_width, spacing=0.0):
    ell = '…'
    if _text_width(font, line + ell, spacing) <= max_width:
        return line + ell
    words = line.split(' ')
    while len(words) > 1:
        words.pop()
        cand = ' '.join(words) + ell
        if _text_width(font, cand, spacing) <= max_width:
            return cand
    s = words[0] if words else line
    while len(s) > 1:
        s = s[:-1]
        if _text_width(font, s + ell, spacing) <= max_width:
            return s + ell
    return ell


def fit_text(text, font_key, max_size, min_size, max_width, max_height=None,
             max_lines=None, line_height=1.15, letter_spacing=0.0, brand=None):
    """Größtmögliche Fontgröße, bei der `text` umgebrochen in max_width (und optional
    max_height / max_lines) passt. Passt er auch bei min_size nicht, wird er auf die
    erlaubte Zeilenzahl gekürzt und die letzte Zeile mit '…' beendet.
    Liefert (font, lines, line_height_px)."""
    max_size = max(1, _int(max_size, 64))
    min_size = max(1, _int(min_size, 24))
    if min_size > max_size:
        min_size = max_size
    max_width = max(1.0, _num(max_width, 1))
    lh_factor = _num(line_height, 1.15) or 1.15
    spacing = _num(letter_spacing, 0.0)
    max_lines = _int(max_lines, 0) if max_lines not in (None, '', 0) else None
    if max_lines is not None and max_lines < 1:
        max_lines = None

    sizes = list(range(max_size, min_size - 1, -2))
    if not sizes or sizes[-1] != min_size:
        sizes.append(min_size)

    font = lines = lh = None
    for size in sizes:
        font = load_font(font_key, size, brand=brand)
        lines, overflow = _wrap(text, font, max_width, spacing, hard_break=(size == min_size))
        lh = font.size * lh_factor
        fits = (not overflow
                and (max_lines is None or len(lines) <= max_lines)
                and (max_height is None or lh * len(lines) <= max_height))
        if fits:
            return font, lines, lh

    # passt auch bei min_size nicht -> kürzen
    limit = max_lines
    if max_height is not None and lh:
        by_height = max(1, int(max_height // lh))
        limit = by_height if limit is None else min(limit, by_height)
    if limit is not None and len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = _ellipsize(lines[-1], font, max_width, spacing)
    return font, lines, lh


def measure_text_box(text, font_key, size, max_width, letter_spacing=0.0, line_height=1.15,
                     uppercase=False, brand=None):
    """Für Vorschauen: (Zeilen, Höhe in px) bei fester Schriftgröße."""
    s = str(text if text is not None else '')
    if uppercase:
        s = s.upper()
    font = load_font(font_key, size, brand=brand)
    lines, _ = _wrap(s, font, max(1.0, _num(max_width, 1)), _num(letter_spacing, 0.0), hard_break=True)
    height = font.size * (_num(line_height, 1.15) or 1.15) * len(lines)
    return lines, height


def sample_values_placeholder(var):
    return f'[{var}]'


# ═══════════════════════════════════════════════════════════════════════════════
# Validierung
# ═══════════════════════════════════════════════════════════════════════════════

_TYPES = ('text', 'image', 'cover', 'rect')
_ALIGNS = ('left', 'center', 'right')
_VALIGNS = ('top', 'middle', 'bottom')
_IMAGE_FITS = ('contain', 'cover')
_TEXT_FITS = ('shrink', 'none')


def _coerce_config(config):
    if config is None:
        return {}
    if isinstance(config, (bytes, str)):
        s = config.decode('utf-8') if isinstance(config, bytes) else config
        if not s.strip():
            return {}
        return json.loads(s)
    return config


def validate_config(config):
    """Prüft eine Config; liefert eine Liste deutscher Fehlertexte (leer = ok)."""
    errors = []
    try:
        config = _coerce_config(config)
    except Exception as ex:
        return [f'pil_config ist kein gültiges JSON: {ex}']
    if not isinstance(config, dict):
        return ['Config muss ein Objekt ({...}) sein']

    canvas = config.get('canvas')
    if canvas is not None:
        if not isinstance(canvas, dict):
            errors.append('canvas muss ein Objekt mit width/height sein')
        else:
            for k in ('width', 'height'):
                if not _is_num(canvas.get(k)) or _num(canvas.get(k)) <= 0:
                    errors.append(f'canvas.{k} muss eine positive Zahl sein')

    elements = config.get('elements')
    if elements is None:
        return errors + ['elements fehlt']
    if not isinstance(elements, list):
        return errors + ['elements muss eine Liste sein']

    seen_ids = set()
    for i, el in enumerate(elements):
        label = f'Element {i + 1}'
        if not isinstance(el, dict):
            errors.append(f'{label}: muss ein Objekt sein')
            continue
        el_id = el.get('id')
        if el_id is not None:
            label += f" ('{el_id}')"
            if str(el_id) in seen_ids:
                errors.append(f'{label}: id doppelt vergeben')
            seen_ids.add(str(el_id))

        t = el.get('type', 'text')
        if t not in _TYPES:
            errors.append(f"{label}: unbekannter type '{t}' (erlaubt: {', '.join(_TYPES)})")
            continue

        for k in ('x', 'y'):
            if k in el and not _is_num(el.get(k)):
                errors.append(f'{label}: {k} muss eine Zahl sein')
        need_wh = ('width', 'height') if t != 'text' else ('width',)
        for k in need_wh:
            if el.get(k) in (None, ''):
                errors.append(f'{label}: {k} fehlt')
            elif not _is_num(el.get(k)) or _num(el.get(k)) <= 0:
                errors.append(f'{label}: {k} muss eine positive Zahl sein')
        if t == 'text' and el.get('height') not in (None, '') and (not _is_num(el.get('height')) or _num(el.get('height')) <= 0):
            errors.append(f'{label}: height muss eine positive Zahl sein')

        if 'opacity' in el and el.get('opacity') is not None:
            if not _is_num(el.get('opacity')) or not (0 <= _num(el.get('opacity')) <= 1):
                errors.append(f'{label}: opacity muss zwischen 0 und 1 liegen')

        def check_color(key, container=el, lbl=label):
            v = container.get(key)
            if v in (None, ''):
                return
            if parse_color(v) is None:
                errors.append(f"{lbl}: ungültige Farbe in {key} ('{v}')")

        if t == 'text':
            if not el.get('var') and not el.get('text') and el.get('fallback') in (None, ''):
                errors.append(f'{label}: text-Element braucht var oder text')
            for k in ('max_size', 'min_size'):
                if k in el and (not _is_num(el.get(k)) or _num(el.get(k)) <= 0):
                    errors.append(f'{label}: {k} muss eine positive Zahl sein')
            if _is_num(el.get('max_size')) and _is_num(el.get('min_size')) and _num(el['min_size']) > _num(el['max_size']):
                errors.append(f'{label}: min_size ist größer als max_size')
            if el.get('align') not in (None, '') and el.get('align') not in _ALIGNS:
                errors.append(f"{label}: align muss {', '.join(_ALIGNS)} sein")
            if el.get('valign') not in (None, '') and el.get('valign') not in _VALIGNS:
                errors.append(f"{label}: valign muss {', '.join(_VALIGNS)} sein")
            if el.get('fit') not in (None, '') and el.get('fit') not in _TEXT_FITS:
                errors.append(f"{label}: fit muss {', '.join(_TEXT_FITS)} sein")
            if el.get('max_lines') not in (None, '', 0) and (not _is_num(el.get('max_lines')) or _num(el.get('max_lines')) < 1):
                errors.append(f'{label}: max_lines muss eine ganze Zahl ≥ 1 sein')
            if el.get('line_height') not in (None, '') and (not _is_num(el.get('line_height')) or _num(el.get('line_height')) <= 0):
                errors.append(f'{label}: line_height muss eine positive Zahl sein')
            if el.get('letter_spacing') not in (None, '') and not _is_num(el.get('letter_spacing')):
                errors.append(f'{label}: letter_spacing muss eine Zahl sein')
            if el.get('stroke_width') not in (None, '') and (not _is_num(el.get('stroke_width')) or _num(el.get('stroke_width')) < 0):
                errors.append(f'{label}: stroke_width muss eine Zahl ≥ 0 sein')
            check_color('color')
            check_color('stroke')
            sh = el.get('shadow')
            if sh not in (None, '', False):
                if not isinstance(sh, dict):
                    errors.append(f'{label}: shadow muss ein Objekt (dx, dy, color, blur) sein')
                else:
                    for k in ('dx', 'dy', 'blur'):
                        if k in sh and not _is_num(sh.get(k)):
                            errors.append(f'{label}: shadow.{k} muss eine Zahl sein')
                    check_color('color', sh, label + ' shadow')

        elif t == 'image':
            if not el.get('var') and not el.get('src'):
                errors.append(f'{label}: image-Element braucht var oder src')
            if el.get('fit') not in (None, '') and el.get('fit') not in _IMAGE_FITS:
                errors.append(f"{label}: fit muss {', '.join(_IMAGE_FITS)} sein")
            if el.get('radius') not in (None, '') and (not _is_num(el.get('radius')) or _num(el.get('radius')) < 0):
                errors.append(f'{label}: radius muss eine Zahl ≥ 0 sein')

        elif t == 'cover':
            fill = el.get('fill', 'auto')
            if fill not in (None, '', 'auto', 'inpaint') and parse_color(fill) is None:
                errors.append(f"{label}: fill muss 'auto', 'inpaint' oder eine Farbe sein ('{fill}')")
            if el.get('feather') not in (None, '') and (not _is_num(el.get('feather')) or _num(el.get('feather')) < 0):
                errors.append(f'{label}: feather muss eine Zahl ≥ 0 sein')

        elif t == 'rect':
            if el.get('fill') in (None, '') and el.get('stroke') in (None, ''):
                errors.append(f'{label}: rect-Element braucht fill oder stroke')
            check_color('fill')
            check_color('stroke')
            for k in ('radius', 'stroke_width'):
                if el.get(k) not in (None, '') and (not _is_num(el.get(k)) or _num(el.get(k)) < 0):
                    errors.append(f'{label}: {k} muss eine Zahl ≥ 0 sein')

    return errors


# ═══════════════════════════════════════════════════════════════════════════════
# Rendern
# ═══════════════════════════════════════════════════════════════════════════════

_VAR_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')


def _open_background(bg_path):
    """bg_path: Dateipfad, Bytes, Dateiobjekt oder PIL-Image. Fehlt die Datei -> FileNotFoundError."""
    if isinstance(bg_path, Image.Image):
        return bg_path.convert('RGBA')
    if isinstance(bg_path, (bytes, bytearray)):
        return Image.open(io.BytesIO(bg_path)).convert('RGBA')
    if hasattr(bg_path, 'read'):
        return Image.open(bg_path).convert('RGBA')
    if not bg_path or not os.path.isfile(str(bg_path)):
        raise FileNotFoundError(f'Hintergrundbild fehlt: {bg_path!r}')
    return Image.open(str(bg_path)).convert('RGBA')


def _value_for(el, values):
    """Textwert eines Elements ermitteln. None = Element überspringen."""
    var = el.get('var')
    if var:
        v = values.get(var)
        if v is None or (isinstance(v, str) and not v.strip()):
            fb = el.get('fallback')
            return str(fb) if fb not in (None, '') else None
        return str(v)
    tmpl = el.get('text')
    if tmpl not in (None, ''):
        tmpl = str(tmpl)
        missing = []

        def sub(m):
            v = values.get(m.group(1))
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(m.group(1))
                return ''
            return str(v)

        out = _VAR_RE.sub(sub, tmpl)
        if not out.strip():
            fb = el.get('fallback')
            return str(fb) if fb not in (None, '') else None
        return out
    fb = el.get('fallback')
    return str(fb) if fb not in (None, '') else None


def _apply_opacity(layer, opacity):
    if opacity >= 1:
        return layer
    alpha = layer.getchannel('A').point(lambda a: int(a * opacity))
    layer.putalpha(alpha)
    return layer


def _draw_line(draw, xy, line, font, fill, stroke_width, stroke_fill, spacing):
    x, y = xy
    if spacing:
        cx = x
        for ch in line:
            draw.text((cx, y), ch, font=font, fill=fill,
                      stroke_width=stroke_width, stroke_fill=stroke_fill)
            cx += font.getlength(ch) + spacing
    else:
        draw.text((x, y), line, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)


def _render_text(img, el, values, brand):
    text = _value_for(el, values)
    if text is None:
        return img
    if _bool(el.get('uppercase')):
        text = text.upper()

    x, y = _int(el.get('x'), 0), _int(el.get('y'), 0)
    width = _int(el.get('width'), img.width - x)
    if width <= 0:
        width = max(1, img.width - x)
    height = el.get('height')
    height = _int(height) if height not in (None, '') else None
    if height is not None and height <= 0:
        height = None

    font_key = el.get('font') or _FALLBACK_KEY
    max_size = _int(el.get('max_size'), 64)
    min_size = _int(el.get('min_size'), 24)
    if str(el.get('fit') or 'shrink').lower() == 'none':
        min_size = max_size
    spacing = _num(el.get('letter_spacing'), 0.0)
    stroke_width = max(0, _int(el.get('stroke_width'), 0))

    font, lines, lh = fit_text(
        text, font_key, max_size, min_size, width, height,
        max_lines=el.get('max_lines'), line_height=el.get('line_height') or 1.15,
        letter_spacing=spacing, brand=brand,
    )

    color = parse_color(el.get('color'), brand, (255, 255, 255, 255))
    stroke = parse_color(el.get('stroke'), brand, None)
    if stroke is None:
        stroke_width = 0
    align = str(el.get('align') or 'left').lower()
    valign = str(el.get('valign') or 'top').lower()
    opacity = max(0.0, min(1.0, _num(el.get('opacity'), 1.0)))

    block_h = lh * len(lines)
    cy0 = y
    if height is not None:
        if valign == 'middle':
            cy0 = y + (height - block_h) / 2
        elif valign == 'bottom':
            cy0 = y + height - block_h

    def positions():
        cy = cy0
        for line in lines:
            lw = _text_width(font, line, spacing)
            if align == 'center':
                lx = x + (width - lw) / 2
            elif align == 'right':
                lx = x + width - lw
            else:
                lx = x
            yield lx, cy, line
            cy += lh

    # Schatten
    shadow = el.get('shadow')
    if isinstance(shadow, dict) and shadow:
        s_color = parse_color(shadow.get('color'), brand, (0, 0, 0, 160))
        dx, dy = _num(shadow.get('dx'), 2), _num(shadow.get('dy'), 2)
        blur = max(0.0, _num(shadow.get('blur'), 0))
        layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for lx, cy, line in positions():
            _draw_line(ld, (lx + dx, cy + dy), line, font, s_color, stroke_width, s_color, spacing)
        if blur > 0:
            layer = layer.filter(ImageFilter.GaussianBlur(blur))
        img = Image.alpha_composite(img, _apply_opacity(layer, opacity))

    # Haupttext
    if opacity >= 1:
        draw = ImageDraw.Draw(img)
        for lx, cy, line in positions():
            _draw_line(draw, (lx, cy), line, font, color, stroke_width, stroke, spacing)
    else:
        layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for lx, cy, line in positions():
            _draw_line(ld, (lx, cy), line, font, color, stroke_width, stroke, spacing)
        img = Image.alpha_composite(img, _apply_opacity(layer, opacity))
    return img


def _load_image_source(src):
    s = str(src).strip()
    if s.lower().startswith(('http://', 'https://')):
        import requests  # nur bei URLs nötig
        resp = requests.get(s, timeout=10)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert('RGBA')
    candidates = [s]
    if not os.path.isabs(s):
        candidates.append(os.path.join(_UPLOAD_DIR, os.path.basename(s)))
    for p in candidates:
        if os.path.isfile(p):
            return Image.open(p).convert('RGBA')
    raise FileNotFoundError(f'Bildquelle nicht gefunden: {s}')


def _render_image(img, el, values, brand):
    var = el.get('var')
    src = values.get(var) if var else el.get('src')
    if src is None or (isinstance(src, str) and not src.strip()):
        src = el.get('fallback')
    if src is None or (isinstance(src, str) and not src.strip()):
        return img
    x, y = _int(el.get('x'), 0), _int(el.get('y'), 0)
    w, h = max(1, _int(el.get('width'), 100)), max(1, _int(el.get('height'), 100))
    fit = str(el.get('fit') or 'contain').lower()
    radius = max(0, _int(el.get('radius'), 0))
    opacity = max(0.0, min(1.0, _num(el.get('opacity'), 1.0)))

    overlay = _load_image_source(src)
    if fit == 'cover':
        overlay = ImageOps.fit(overlay, (w, h), method=Image.LANCZOS)
        ox, oy = x, y
    else:
        overlay = ImageOps.contain(overlay, (w, h), method=Image.LANCZOS)
        ox = x + (w - overlay.width) // 2
        oy = y + (h - overlay.height) // 2
    if radius > 0:
        mask = Image.new('L', overlay.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, overlay.width - 1, overlay.height - 1),
                                               radius=radius, fill=255)
        overlay.putalpha(ImageChops.multiply(overlay.getchannel('A'), mask))
    overlay = _apply_opacity(overlay, opacity)
    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    layer.paste(overlay, (ox, oy), overlay)
    return Image.alpha_composite(img, layer)


def _median_edge_color(img, box):
    """Median-Farbe eines 2 px breiten Rings um die Box (außen; am Bildrand innen)."""
    x0, y0, x1, y1 = box
    W, H = img.size
    rgb = img.convert('RGB')
    strips = [
        (x0 - 2, y0 - 2, x1 + 2, y0),      # oben
        (x0 - 2, y1, x1 + 2, y1 + 2),      # unten
        (x0 - 2, y0, x0, y1),              # links
        (x1, y0, x1 + 2, y1),              # rechts
    ]
    pixels = []
    for sx0, sy0, sx1, sy1 in strips:
        sx0, sy0 = max(0, sx0), max(0, sy0)
        sx1, sy1 = min(W, sx1), min(H, sy1)
        if sx1 > sx0 and sy1 > sy0:
            pixels.extend(rgb.crop((sx0, sy0, sx1, sy1)).getdata())
    if not pixels:  # Box deckt das ganze Bild -> innerer Rand
        inner = [(x0, y0, x1, min(y1, y0 + 2)), (x0, max(y0, y1 - 2), x1, y1),
                 (x0, y0, min(x1, x0 + 2), y1), (max(x0, x1 - 2), y0, x1, y1)]
        for sx0, sy0, sx1, sy1 in inner:
            if sx1 > sx0 and sy1 > sy0:
                pixels.extend(rgb.crop((sx0, sy0, sx1, sy1)).getdata())
    if not pixels:
        return (0, 0, 0, 255)
    r = int(statistics.median(p[0] for p in pixels))
    g = int(statistics.median(p[1] for p in pixels))
    b = int(statistics.median(p[2] for p in pixels))
    return (r, g, b, 255)


def _inpaint(img, box):
    """cv2.inpaint (TELEA, Radius 5) über der Box. ImportError -> None (Aufrufer fällt auf 'auto' zurück)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    x0, y0, x1, y1 = box
    arr = np.array(img.convert('RGB'))[:, :, ::-1].copy()       # RGB -> BGR
    mask = np.zeros(arr.shape[:2], dtype=np.uint8)
    mask[y0:y1, x0:x1] = 255
    out = cv2.inpaint(arr, mask, 5, cv2.INPAINT_TELEA)
    return Image.fromarray(out[:, :, ::-1].copy()).convert('RGBA')


def _render_cover(img, el, values, brand):
    x, y = _int(el.get('x'), 0), _int(el.get('y'), 0)
    w, h = max(1, _int(el.get('width'), 1)), max(1, _int(el.get('height'), 1))
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(img.width, x + w), min(img.height, y + h)
    if x1 <= x0 or y1 <= y0:
        return img
    box = (x0, y0, x1, y1)
    feather = max(0.0, _num(el.get('feather'), 0))
    opacity = max(0.0, min(1.0, _num(el.get('opacity'), 1.0)))
    fill = el.get('fill', 'auto')
    fill = 'auto' if fill in (None, '') else fill

    mask = Image.new('L', img.size, 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(feather))
    if opacity < 1:
        mask = mask.point(lambda a: int(a * opacity))

    source = None
    if str(fill).lower() == 'inpaint':
        source = _inpaint(img, box)
        if source is None:
            log.info('cover inpaint: cv2/numpy nicht verfügbar, nutze fill=auto')
            fill = 'auto'
    if source is None:
        if str(fill).lower() == 'auto':
            color = _median_edge_color(img, box)
        else:
            color = parse_color(fill, brand, None)
            if color is None:
                color = _median_edge_color(img, box)
        source = Image.new('RGBA', img.size, color)
    return Image.composite(source, img, mask)


def _render_rect(img, el, values, brand):
    x, y = _int(el.get('x'), 0), _int(el.get('y'), 0)
    w, h = max(1, _int(el.get('width'), 1)), max(1, _int(el.get('height'), 1))
    radius = max(0, _int(el.get('radius'), 0))
    fill = parse_color(el.get('fill'), brand, None)
    stroke = parse_color(el.get('stroke'), brand, None)
    stroke_width = max(0, _int(el.get('stroke_width'), 0)) if stroke is not None else 0
    opacity = max(0.0, min(1.0, _num(el.get('opacity'), 1.0)))
    if fill is None and stroke_width == 0:
        return img
    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (x, y, x + w - 1, y + h - 1), radius=radius, fill=fill,
        outline=stroke if stroke_width else None, width=stroke_width,
    )
    return Image.alpha_composite(img, _apply_opacity(layer, opacity))


_HANDLERS = {
    'text': _render_text,
    'image': _render_image,
    'cover': _render_cover,
    'rect': _render_rect,
}


def render(bg_path, config, values, brand=None):
    """Rendert Hintergrund + Elemente und liefert PNG-Bytes (RGB).

    bg_path: Pfad (oder Bytes/Dateiobjekt/PIL-Image) des Hintergrunds – fehlt die Datei,
             wird FileNotFoundError geworfen (der einzige Fehler, der nach außen geht).
    config:  dict (oder JSON-Text) mit 'elements' und optional 'canvas'.
    values:  {var: Wert}; Nicht-Strings werden mit str() gewandelt.
    brand:   {'bg','text','accent','font'} für 'brand:x'-Farben und font 'brand'.
    Fehler einzelner Elemente werden geloggt und übersprungen."""
    img = _open_background(bg_path)
    try:
        config = _coerce_config(config) or {}
    except Exception as ex:
        log.warning('Config nicht lesbar (%s), rendere nur den Hintergrund', ex)
        config = {}
    if not isinstance(config, dict):
        config = {}
    values = {str(k): v for k, v in (values or {}).items()}
    brand = dict(brand) if brand else None

    canvas = config.get('canvas')
    if isinstance(canvas, dict) and _is_num(canvas.get('width')) and _is_num(canvas.get('height')):
        cw, ch = _int(canvas['width']), _int(canvas['height'])
        if cw > 0 and ch > 0 and (cw, ch) != img.size:
            img = ImageOps.fit(img, (cw, ch), method=Image.LANCZOS)

    elements = config.get('elements') or []
    if not isinstance(elements, list):
        elements = []
    for i, el in enumerate(elements):
        if not isinstance(el, dict):
            log.warning('Element %d ist kein Objekt, übersprungen', i + 1)
            continue
        if _bool(el.get('hidden')):
            continue
        if _num(el.get('opacity'), 1.0) <= 0:
            continue
        el_type = str(el.get('type') or 'text').lower()
        handler = _HANDLERS.get(el_type)
        el_id = el.get('id') or el.get('var') or f'el{i + 1}'
        if handler is None:
            log.warning("Element %s: unbekannter type '%s', übersprungen", el_id, el_type)
            continue
        try:
            img = handler(img, el, values, brand)
        except Exception as ex:
            log.warning('Element %s (%s) fehlgeschlagen: %s', el_id, el_type, ex)

    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    return buf.getvalue()
