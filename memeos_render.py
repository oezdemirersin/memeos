# -*- coding: utf-8 -*-
"""
memeos_render – lokaler PIL-Renderer für Meme-Templates (ohne Canva, ohne app-Import).

Vertrag (Element-Spezifikation v3):
    list_fonts()                                  -> [{'key','label','path','source'}]
    resolve_font_path(name_or_key, brand=None)    -> Pfad (tolerant, Fallback anton)
    font_key_for(name)                            -> (key, exact: bool)
    validate_config(config)                       -> Liste von Fehlertexten (leer = ok)
    render(bg_path, config, values, brand=None)   -> PNG-Bytes (RGB)
    measure_text_box(text, font_key, size, max_width) -> (Zeilen, Höhe)
    text_metrics(text, el, canvas_groesse, brand=None) -> {'lines','size','width','height', ...}
    element_defaults(typ, shape=None)             -> Standardwerte für ein neues Element
    fill_flaeche(groesse, farbwert, brand=None)   -> RGBA-Bild (Farbe ODER Verlauf)
    sample_values_placeholder(var)                -> '[var]'
    Konstanten: ELEMENT_TYPES, SHAPES, BLEND_MODES, FIT_MODES, IMAGE_FITS, TEXT_FITS,
                MASKS, ALIGNS, VALIGNS

Vertrag Stadt-Stil und Rollen (v3.1, rein additiv):
    stil_dict(quelle)                             -> vollständiges Stil-Dict (alle Schlüssel)
    rolle_anwenden(el, brand, canvas_hoehe)       -> Element mit eingesetzten Rollenvorgaben
    zeichen_budget(el, canvas_groesse, brand=None)-> int (Zeichen bei Mindestgröße)
    passt_text(text, el, canvas_groesse, brand=None)
        -> {'passt', 'groesse', 'zeilen', 'noetige_verkleinerung'}
    kontrast(farbe_a, farbe_b, brand=None)        -> float 1..21
    hintergrund_farbe_im_bereich(bg_bild, el, canvas_groesse) -> '#rrggbb'
    Konstanten: ROLLEN, STIL_STANDARD, STIL_SCHLUESSEL, SCHREIBWEISEN, KASTEN_ARTEN,
                KONTUR_ARTEN, WASSERZEICHEN_ECKEN

Config-Format:
    {"canvas": {"width": W, "height": H, "background": <Farbe|Verlauf|null>},
     "elements": [ {...}, ... ]}                    # Reihenfolge = Zeichenreihenfolge, letztes oben

canvas.background wird VOR dem Hintergrundbild gezeichnet und trägt allein, wenn gar kein
Hintergrundbild gesetzt ist (Bauen von Null). Ohne beides: Weiß + Warnung.

Farbwert (überall, wo unten „Farbe“ steht):
    '#rgb' | '#rrggbb' | '#rrggbbaa' | CSS-Name | 'rgb(...)' | (r,g,b[,a])
    'brand:bg' | 'brand:text' | 'brand:accent'
    'stil:primary' | 'stil:secondary' | 'stil:accent' | 'stil:dark' | 'stil:light'
    'stil:auto'  – Schwarz oder Weiß, je nachdem was auf dem Untergrund besser lesbar ist
                   (Untergrund = gesetzte Kastenfarbe, sonst der Bildmittelwert im Bereich)
    Verlauf: {'type':'linear','angle':0..360,'stops':[{'at':0.0,'color':'#...'}, ...]}
             {'type':'radial','stops':[...]}           (optional 'center':{'x','y'} 0..1)
    Winkel: 0 = von links nach rechts, 90 = von oben nach unten (im Uhrzeigersinn).

Gemeinsame Element-Eigenschaften:
    id, type, name, x, y, width, height, rotation (Grad, positiv = im Uhrzeigersinn,
    Drehpunkt Boxmitte), opacity (0..1), hidden, locked (nur Editor), flip_h, flip_v,
    blend ('normal'|'multiply'|'screen'|'overlay'|'darken'|'lighten'),
    group (Kennung, nur Editor), only_if (Variablenname), skip_if (Variablenname),
    role (Rolle aus ROLLEN – liefert Vorgaben aus dem Stadt-Stil; JEDER am Element
          gesetzte Wert schlägt die Rolle. Ohne role ändert sich nichts.)

type 'text':   var | text ({var} wird ersetzt), fallback, font, max_size, min_size, color,
               stroke, stroke_width, shadow {dx,dy,color,blur}, align, valign, uppercase,
               max_lines, line_height, letter_spacing,
               fit 'shrink'|'wrap'|'clip'|'none', padding (Zahl oder {top,right,bottom,left}),
               box {fill, radius, padding, stroke, stroke_width, per_line}
type 'image':  var | src (Dateiname im Upload-Ordner oder http(s)-URL), fallback,
               fit 'contain'|'cover'|'stretch', radius, mask 'none'|'circle'|'rounded',
               crop {x,y,w,h} (Anteile 0..1 des Quellbilds),
               adjust {brightness, contrast, saturation (0..2), blur (px), grayscale (0..1)}
type 'cover':  fill 'auto'|'inpaint'|Farbe, feather (px)
type 'shape':  shape 'rect'|'ellipse'|'triangle'|'star'|'line'|'arrow'|'bubble',
               fill, radius (rect/bubble), stroke, stroke_width,
               points (star, Standard 5), inner (star, Standard 0.5),
               tail {x,y} (bubble, Leinwand-Pixel)
type 'rect':   Altbestand, verhält sich wie shape mit shape='rect'

Nur Pillow ist Pflicht. numpy beschleunigt Verläufe (Fallback ohne numpy vorhanden),
cv2 + numpy werden für cover:'inpaint' gebraucht (sonst Fallback auf 'auto'),
requests nur für Bild-URLs.
"""
import io
import ipaddress
import json
import logging
import math
import os
import re
import socket
import statistics
import threading
from urllib.parse import urljoin, urlparse

from PIL import (Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont, ImageOps)

log = logging.getLogger('memeos_render')

__all__ = [
    'FONT_BASE_DIR', 'USER_FONT_DIR', 'CANVA_FONT_MAP',
    'list_fonts', 'resolve_font_path', 'font_key_for', 'load_font',
    'validate_config', 'render', 'fit_text', 'measure_text_box', 'text_metrics',
    'sample_values_placeholder', 'parse_color', 'parse_paint', 'fill_flaeche',
    'element_defaults',
    'ELEMENT_TYPES', 'SHAPES', 'BLEND_MODES', 'FIT_MODES', 'IMAGE_FITS', 'TEXT_FITS',
    'MASKS', 'ALIGNS', 'VALIGNS', 'DEFAULT_CANVAS',
    # Stadt-Stil und Rollen (v3.1)
    'stil_dict', 'rolle_anwenden', 'zeichen_budget', 'passt_text', 'kontrast',
    'hintergrund_farbe_im_bereich', 'leuchtdichte', 'auto_farbe',
    'ROLLEN', 'STIL_STANDARD', 'STIL_SCHLUESSEL', 'SCHREIBWEISEN', 'KASTEN_ARTEN',
    'KONTUR_ARTEN', 'WASSERZEICHEN_ECKEN',
]

# ═══════════════════════════════════════════════════════════════════════════════
# Konstanten (Vertrag für Editor, Import und Warteschlange)
# ═══════════════════════════════════════════════════════════════════════════════

ELEMENT_TYPES = ('text', 'image', 'cover', 'shape', 'rect')
SHAPES = ('rect', 'ellipse', 'triangle', 'star', 'line', 'arrow', 'bubble')
BLEND_MODES = ('normal', 'multiply', 'screen', 'overlay', 'darken', 'lighten')
IMAGE_FITS = ('contain', 'cover', 'stretch')
TEXT_FITS = ('shrink', 'wrap', 'clip', 'none')      # 'none' = Altbestand (= shrink ohne Schrumpfen)
FIT_MODES = {'image': IMAGE_FITS, 'text': TEXT_FITS}
MASKS = ('none', 'circle', 'rounded')
ALIGNS = ('left', 'center', 'right')
VALIGNS = ('top', 'middle', 'bottom')
GRADIENT_TYPES = ('linear', 'radial')
DEFAULT_CANVAS = (1080, 1350)

_SUPERSAMPLE = 4                 # Kantenglättung für Formen
_SUPERSAMPLE_MAX_PIXELS = 40_000_000

# ═══════════════════════════════════════════════════════════════════════════════
# Schriften
# ═══════════════════════════════════════════════════════════════════════════════

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_BASE_DIR = os.path.join(_BASE_DIR, 'fonts')                       # mitgeliefert (Repo)
_DATA_ROOT = os.getenv('MEMEOS_DATA_ROOT') or os.path.join(_BASE_DIR, 'instance')
USER_FONT_DIR = os.path.join(_DATA_ROOT, 'fonts')                      # eigene Uploads
_UPLOAD_DIR = os.path.join(_DATA_ROOT, 'uploads')
_RENDER_DIR = os.path.join(_DATA_ROOT, 'renders')

# Bildquellen: nur diese Ordner sind erlaubt, und nur öffentlich erreichbare http(s)-Ziele.
_IMAGE_DIRS = (_UPLOAD_DIR, _RENDER_DIR, USER_FONT_DIR, FONT_BASE_DIR)
_IMAGE_MAX_BYTES = 25 * 1024 * 1024
_IMAGE_TIMEOUT = 10
_IMAGE_MAX_REDIRECTS = 3

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


# ═══════════════════════════════════════════════════════════════════════════════
# Stadt-Stil – die Handschrift einer Stadt
# ═══════════════════════════════════════════════════════════════════════════════
# Das Dict, das render() als `brand` bekommt, kann seit v3.1 mehr tragen als die vier
# alten Felder. stil_dict() macht aus JEDER Teilangabe ein vollständiges Stil-Dict:
# aus dem alten vierfeldrigen brand-Dict genauso wie aus CityStyle.to_dict().
# Die alten Schlüssel bg/text/accent/font bleiben immer erhalten – 'brand:bg' & Co.
# funktionieren unverändert weiter.

SCHREIBWEISEN        = ('gross', 'normal')
KASTEN_ARTEN         = ('keiner', 'voll', 'weich', 'umriss')
KONTUR_ARTEN         = ('keine', 'dunkel', 'palette')
WASSERZEICHEN_ECKEN  = ('unten_links', 'unten_rechts', 'oben_rechts', 'keins')

# Alle Schlüssel, die ein vollständiges Stil-Dict trägt (Vertrag für Editor und Fabrik).
STIL_SCHLUESSEL = (
    'primary', 'secondary', 'accent', 'dark', 'light',      # Palette
    'headline', 'body',                                     # Schriftpaar
    'schreibweise', 'kasten', 'radius', 'kontur', 'schatten',
    'wasserzeichen_ecke', 'paket',
    'bg', 'text', 'font',                                   # Altbestand (brand:*)
)

# Vorgabe-Handschrift für Städte ohne eigenen Stil: läuft auf jedem Foto, weil sie
# nicht auf einen hellen Untergrund baut (keine Kästen, dunkle Kontur, Schatten an).
# ACHTUNG: dieselben Werte stehen als Spalten-Vorgaben in models.CityStyle –
# der Test test_i1_stil.py prüft, dass beide Listen gleich bleiben.
STIL_STANDARD = {
    'primary':   '#3b82f6',
    'secondary': '#1e40af',
    'accent':    '#f59e0b',
    'dark':      '#0f172a',
    'light':     '#ffffff',
    'headline':  'anton',
    'body':      'bold',
    'schreibweise': 'gross',
    'kasten':    'keiner',
    'radius':    16,
    'kontur':    'dunkel',
    'schatten':  True,
    'wasserzeichen_ecke': 'unten_rechts',
    'paket':     '',
}

_STIL_FARBEN = ('primary', 'secondary', 'accent', 'dark', 'light')


def _hex(rgb):
    """(r, g, b[, a]) -> '#rrggbb'."""
    r, g, b = (int(_clamp(_num(v, 0), 0, 255)) for v in tuple(rgb)[:3])
    return '#%02x%02x%02x' % (r, g, b)


def _abdunkeln(farbe, faktor=0.62):
    """Farbe auf faktor ihrer Helligkeit ziehen -> '#rrggbb'. Unbrauchbar -> Vorgabe."""
    rgba = parse_color(farbe, None, None)
    if rgba is None:
        return STIL_STANDARD['secondary']
    f = _clamp(_num(faktor, 0.62), 0.0, 1.0)
    return _hex(tuple(int(round(c * f)) for c in rgba[:3]))


def _mit_alpha(farbe, alpha):
    """Farbe mit Deckkraft 0..1 als '#rrggbbaa'."""
    rgba = parse_color(farbe, None, (0, 0, 0, 255)) or (0, 0, 0, 255)
    a = int(round(_clamp(_num(alpha, 1.0), 0.0, 1.0) * 255))
    return '#%02x%02x%02x%02x' % (rgba[0], rgba[1], rgba[2], a)


def _schriftpaar(quelle):
    """(Auszeichnungsschrift, Grundschrift) aus einer beliebigen Teilangabe.
    Eigene Funktion, weil resolve_font_path() sie bei JEDEM load_font() braucht und dafür
    nicht das ganze Stil-Dict bauen soll."""
    q = quelle if isinstance(quelle, dict) else {}

    def hol(*keys):
        for k in keys:
            v = q.get(k)
            if v not in (None, ''):
                return v
        return None

    roh_headline = hol('headline', 'font_headline', 'font')
    roh_body     = hol('body', 'font_body')
    headline = str(roh_headline or STIL_STANDARD['headline'])
    if roh_body not in (None, ''):
        body = str(roh_body)
    elif roh_headline not in (None, ''):
        body = headline                    # altes brand-Dict: eine Schrift für beides
    else:
        body = STIL_STANDARD['body']       # gar keine Angabe: die Vorgabe-Handschrift
    return headline, body


def stil_dict(quelle=None):
    """Beliebige Teilangabe -> vollständiges Stil-Dict mit allen STIL_SCHLUESSEL.

    quelle darf sein:
      * None                      -> STIL_STANDARD
      * das alte brand-Dict       {'bg','text','accent','font'}
      * CityStyle.to_dict()       {'primary','secondary',…,'font_headline','font_body',…}
      * jede Mischung/Teilmenge davon

    Ableitung aus dem alten brand-Dict (so, wie models.CityStyle.fuer() es auch tut):
      primary = accent, accent = primary, dark = text, light = bg,
      secondary = abgedunkeltes primary, headline = body = font.
    Umgekehrt werden bg/text/font aus light/dark/headline ergänzt, damit ein reines
    Stil-Dict weiterhin als brand für 'brand:*' taugt."""
    q = {}
    if isinstance(quelle, dict):
        q = {str(k): v for k, v in quelle.items() if v not in (None, '')}

    def hol(*keys):
        for k in keys:
            if k in q:
                return q[k]
        return None

    def farbe(wert, vorgabe):
        if wert in (None, ''):
            return vorgabe
        return _hex(parse_color(wert, None, None) or parse_color(vorgabe, None, (0, 0, 0, 255)))

    def brauchbar(wert):
        """True, wenn überhaupt eine lesbare Farbe angegeben wurde."""
        return wert not in (None, '') and parse_color(wert, None, None) is not None

    roh_primary = hol('primary') or hol('accent')
    primary   = farbe(roh_primary, STIL_STANDARD['primary'])
    # Die Ableitungen accent = primary und secondary = abgedunkeltes primary gelten dem
    # alten brand-Dict. Ohne jede Farbangabe (stil_dict(None) oder stil_dict({})) darf
    # nichts abgeleitet werden – sonst käme statt STIL_STANDARD ein anderer Stil heraus.
    ableiten = brauchbar(roh_primary)
    accent    = farbe(hol('accent'), primary if ableiten else STIL_STANDARD['accent'])
    dark      = farbe(hol('dark') or hol('text'), STIL_STANDARD['dark'])
    light     = farbe(hol('light') or hol('bg'), STIL_STANDARD['light'])
    secondary = farbe(hol('secondary'),
                      _abdunkeln(primary) if ableiten else STIL_STANDARD['secondary'])

    headline, body = _schriftpaar(quelle)

    schreibweise = str(hol('schreibweise') or STIL_STANDARD['schreibweise']).strip().lower()
    if schreibweise not in SCHREIBWEISEN:
        schreibweise = STIL_STANDARD['schreibweise']
    kasten = str(hol('kasten') or STIL_STANDARD['kasten']).strip().lower()
    if kasten not in KASTEN_ARTEN:
        kasten = STIL_STANDARD['kasten']
    kontur = str(hol('kontur') or STIL_STANDARD['kontur']).strip().lower()
    if kontur not in KONTUR_ARTEN:
        kontur = STIL_STANDARD['kontur']
    ecke = str(hol('wasserzeichen_ecke') or STIL_STANDARD['wasserzeichen_ecke']).strip().lower()
    if ecke not in WASSERZEICHEN_ECKEN:
        ecke = STIL_STANDARD['wasserzeichen_ecke']

    radius = int(_clamp(_num(hol('radius'), STIL_STANDARD['radius']), 0, 40))
    schatten = _bool(hol('schatten')) if 'schatten' in q else STIL_STANDARD['schatten']

    return {
        'primary': primary, 'secondary': secondary, 'accent': accent,
        'dark': dark, 'light': light,
        'headline': headline, 'body': body,
        'schreibweise': schreibweise, 'kasten': kasten, 'radius': radius,
        'kontur': kontur, 'schatten': bool(schatten), 'wasserzeichen_ecke': ecke,
        'paket': str(hol('paket') or ''),
        # Altbestand: 'brand:bg' | 'brand:text' | 'brand:accent' und font 'brand'
        'bg':   farbe(hol('bg'), light),
        'text': farbe(hol('text'), dark),
        'font': str(hol('font') or headline),
    }


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


_STIL_FONT_TOKEN = {'stil:headline': 'headline', 'stil:body': 'body'}


def font_key_for(name):
    """Liefert (key, exact). key versteht resolve_font_path; exact=False heißt: Ersatzschrift.
    'brand' bleibt 'brand' (wird beim Rendern über brand['font'] aufgelöst),
    'stil:headline'/'stil:body' bleiben ebenso stehen (Auflösung über den Stadt-Stil)."""
    s = str(name or '').strip()
    if s.lower() == 'brand':
        return 'brand', True
    if s.lower() in _STIL_FONT_TOKEN:
        return s.lower(), True
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
    'brand' (-> brand['font']) sowie 'stil:headline'/'stil:body' (-> Schriftpaar des
    Stadt-Stils). Fallback: anton."""
    s = str(name_or_key or '').strip()
    if s.lower() == 'brand':
        s = str((brand or {}).get('font') or _BRAND_DEFAULTS['font'])
    elif s.lower() in _STIL_FONT_TOKEN:
        headline, body = _schriftpaar(brand)
        s = headline if _STIL_FONT_TOKEN[s.lower()] == 'headline' else body
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
# Zahlen / Wahrheitswerte
# ═══════════════════════════════════════════════════════════════════════════════

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


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# ═══════════════════════════════════════════════════════════════════════════════
# Farben und Verläufe
# ═══════════════════════════════════════════════════════════════════════════════

def _is_gradient(spec):
    return isinstance(spec, dict) and str(spec.get('type') or '').strip().lower() in GRADIENT_TYPES


def parse_color(spec, brand=None, default=None):
    """Farbangabe -> RGBA-Tupel. Versteht '#rgb', '#rrggbb', '#rrggbbaa', CSS-Namen, 'rgb(...)',
    Tupel/Listen, 'brand:bg'|'brand:text'|'brand:accent' und 'stil:primary'|'stil:secondary'|
    'stil:accent'|'stil:dark'|'stil:light'|'stil:auto'. Ungültig -> default.

    'stil:auto' wird beim Rendern VORHER durch Schwarz oder Weiß ersetzt (der Renderer kennt
    den Untergrund, siehe _auto_ersetzen). Kommt es hier trotzdem an, entscheidet der Kontrast
    gegen 'light' des Stils – das ist die Annahme „heller Untergrund“.

    Ein Verlauf-Objekt liefert seine mittlere Stopfarbe (Ersatzwert für Stellen, die nur eine
    einzelne Farbe verarbeiten können); die Fläche selbst zeichnet fill_flaeche()."""
    if spec is None or spec == '' or spec is False:
        return default
    if _is_gradient(spec):
        stops = _gradient_stops(spec, brand)
        return stops[len(stops) // 2][1] if stops else default
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
    if isinstance(spec, dict):
        log.warning('Ungültige Farbe %r', spec)
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
    elif s.lower().startswith('stil:'):
        key = s.split(':', 1)[1].strip().lower()
        stil = stil_dict(brand)
        if key == 'auto':
            return auto_farbe(stil['light'])
        if key not in _STIL_FARBEN:
            log.warning('Unbekannte Stil-Farbe %r', spec)
            return default
        s = str(stil[key]).strip()
    try:
        return ImageColor.getcolor(s, 'RGBA')
    except (ValueError, TypeError):
        log.warning('Ungültige Farbe %r', spec)
        return default


def parse_paint(spec, brand=None):
    """Farbwert einordnen: ('color', (r,g,b,a)) | ('gradient', normalisierter Verlauf) | None."""
    if spec is None or spec == '' or spec is False:
        return None
    if _is_gradient(spec):
        stops = _gradient_stops(spec, brand)
        if not stops:
            return None
        return ('gradient', {
            'type': str(spec.get('type')).strip().lower(),
            'angle': _num(spec.get('angle'), 0.0),
            'center': spec.get('center') if isinstance(spec.get('center'), dict) else None,
            'stops': stops,
        })
    rgba = parse_color(spec, brand, None)
    return ('color', rgba) if rgba is not None else None


def _gradient_stops(spec, brand=None):
    """[(pos 0..1, rgba), ...], nach Position sortiert; None/[] wenn unbrauchbar."""
    raw = spec.get('stops') if isinstance(spec, dict) else None
    if not isinstance(raw, (list, tuple)) or not raw:
        return []
    items = []
    for st in raw:
        pos, col = None, None
        if isinstance(st, dict):
            for key in ('at', 'pos', 'position', 'offset'):
                if _is_num(st.get(key)):
                    pos = _clamp(_num(st.get(key)), 0.0, 1.0)
                    break
            col = parse_color(st.get('color'), brand, None)
        else:
            col = parse_color(st, brand, None)
        if col is None:
            continue
        items.append([pos, col])
    if not items:
        return []
    n = len(items)
    for i, it in enumerate(items):
        if it[0] is None:
            it[0] = (i / (n - 1)) if n > 1 else 0.0
    items.sort(key=lambda it: it[0])
    if len(items) == 1:
        items = [[0.0, items[0][1]], [1.0, items[0][1]]]
    return [(float(p), tuple(c)) for p, c in items]


def _lerp_stops(stops, t):
    """Farbe an Position t (0..1) zwischen den Stops."""
    t = _clamp(float(t), 0.0, 1.0)
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            span = (p1 - p0) or 1.0
            f = (t - p0) / span
            return tuple(int(round(c0[k] + (c1[k] - c0[k]) * f)) for k in range(4))
    return stops[-1][1]


def _gradient_numpy(size, gtype, angle, center, stops):
    try:
        import numpy as np
    except ImportError:
        return None
    w, h = size
    xs = np.arange(w, dtype=np.float32)
    ys = np.arange(h, dtype=np.float32)
    if gtype == 'radial':
        cx, cy = 0.5, 0.5
        if isinstance(center, dict):
            if _is_num(center.get('x')):
                cx = _clamp(_num(center.get('x')), 0.0, 1.0)
            if _is_num(center.get('y')):
                cy = _clamp(_num(center.get('y')), 0.0, 1.0)
        px, py = cx * (w - 1 or 1), cy * (h - 1 or 1)
        dx = xs - px
        dy = ys - py
        field = np.sqrt(np.add.outer(dy * dy, dx * dx))
        rmax = max(math.hypot(px, py), math.hypot(w - 1 - px, py),
                   math.hypot(px, h - 1 - py), math.hypot(w - 1 - px, h - 1 - py)) or 1.0
        t = np.clip(field / rmax, 0.0, 1.0)
    else:
        rad = math.radians(_num(angle, 0.0))
        ca, sa = math.cos(rad), math.sin(rad)
        proj = np.add.outer(ys * sa, xs * ca)
        lo, hi = float(proj.min()), float(proj.max())
        t = (proj - lo) / ((hi - lo) or 1.0)
    flat = t.ravel()
    px_pos = np.array([p for p, _ in stops], dtype=np.float32)
    chans = []
    for k in range(4):
        vals = np.array([c[k] for _, c in stops], dtype=np.float32)
        chans.append(np.interp(flat, px_pos, vals))
    arr = np.stack(chans, axis=-1).reshape(h, w, 4)
    return Image.fromarray(np.clip(arr, 0, 255).astype('uint8'), 'RGBA')


def _gradient_pillow(size, gtype, angle, center, stops):
    """Fallback ohne numpy. Gleiche Geometrie wie der numpy-Weg: Der Verlauf spannt genau
    über die Ausdehnung der Fläche in Verlaufsrichtung (linear) bzw. bis zur weitesten Ecke
    (radial). Gezeichnet wird in 256 Stufen."""
    w, h = size
    steps = 256
    lut = [_lerp_stops(stops, i / (steps - 1)) for i in range(steps)]
    if gtype == 'radial':
        cx, cy = 0.5, 0.5
        if isinstance(center, dict):
            if _is_num(center.get('x')):
                cx = _clamp(_num(center.get('x')), 0.0, 1.0)
            if _is_num(center.get('y')):
                cy = _clamp(_num(center.get('y')), 0.0, 1.0)
        px, py = cx * max(1, w - 1), cy * max(1, h - 1)
        rmax = max(math.hypot(px, py), math.hypot(w - 1 - px, py),
                   math.hypot(px, h - 1 - py), math.hypot(w - 1 - px, h - 1 - py)) or 1.0
        rr = int(math.ceil(rmax)) + 1
        base = Image.new('RGBA', (2 * rr, 2 * rr), lut[-1])
        d = ImageDraw.Draw(base)
        for i in range(steps - 1, -1, -1):
            r = rmax * i / (steps - 1)
            d.ellipse((rr - r, rr - r, rr + r, rr + r), fill=lut[i])
        left, top = int(round(rr - px)), int(round(rr - py))
        return base.crop((left, top, left + w, top + h))
    rad = math.radians(_num(angle, 0.0))
    extent = abs((w - 1) * math.cos(rad)) + abs((h - 1) * math.sin(rad)) or 1.0
    m = int(math.ceil(math.hypot(w, h))) + 4
    off = (m - extent) / 2.0
    strip = Image.new('RGBA', (m, 1))
    strip.putdata([_lerp_stops(stops, (i - off) / extent) for i in range(m)])
    big = strip.resize((m, m), Image.NEAREST)
    rot = big.rotate(-_num(angle, 0.0), resample=Image.BILINEAR, expand=False)
    left, top = (m - w) // 2, (m - h) // 2
    return rot.crop((left, top, left + w, top + h))


def fill_flaeche(groesse, farbwert, brand=None):
    """(breite, hoehe) + Farbwert (einfarbig ODER Verlauf) -> RGBA-Bild dieser Größe.

    Gemeinsame Flächenquelle für canvas.background, Formen, Textkästen und cover.
    Liefert None, wenn kein oder ein unbrauchbarer Farbwert übergeben wurde."""
    try:
        w = max(1, int(round(float(groesse[0]))))
        h = max(1, int(round(float(groesse[1]))))
    except (TypeError, ValueError, IndexError):
        return None
    paint = parse_paint(farbwert, brand)
    if paint is None:
        return None
    kind, value = paint
    if kind == 'color':
        return Image.new('RGBA', (w, h), tuple(value))
    stops = value['stops']
    img = _gradient_numpy((w, h), value['type'], value['angle'], value['center'], stops)
    if img is None:
        img = _gradient_pillow((w, h), value['type'], value['angle'], value['center'], stops)
    return img.convert('RGBA')


# ═══════════════════════════════════════════════════════════════════════════════
# Lesbarkeit: Leuchtdichte, Kontrast, 'stil:auto'
# ═══════════════════════════════════════════════════════════════════════════════
# Gerechnet wird nach der üblichen relativen Leuchtdichte: sRGB linearisieren, dann
# 0.2126·R + 0.7152·G + 0.0722·B. Das Kontrastverhältnis ist (L_hell + 0.05)/(L_dunkel + 0.05)
# und liegt damit zwischen 1 (gleiche Farbe) und 21 (Schwarz auf Weiß).

SCHWARZ = '#000000'
WEISS   = '#ffffff'

# Unterhalb dieses Kontrasts zwischen Text und Kontur verschmelzen beide zu einer Fläche –
# die Schrift wird zum Klumpen. Die Kontur weicht dann auf den Gegenpol aus.
KONTUR_MIN_KONTRAST = 2.5


def _linear(kanal):
    c = kanal / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def leuchtdichte(farbe, brand=None):
    """Relative Leuchtdichte 0..1 einer Farbe (Alpha wird ignoriert)."""
    rgba = parse_color(farbe, brand, (0, 0, 0, 255)) or (0, 0, 0, 255)
    return (0.2126 * _linear(rgba[0]) + 0.7152 * _linear(rgba[1]) + 0.0722 * _linear(rgba[2]))


def kontrast(farbe_a, farbe_b, brand=None):
    """Kontrastverhältnis zweier Farben, 1.0 .. 21.0. Schwarz gegen Weiß ergibt 21.0.
    Alpha bleibt außen vor – halbdurchsichtige Farben vorher selbst verrechnen."""
    la = leuchtdichte(farbe_a, brand)
    lb = leuchtdichte(farbe_b, brand)
    hell, dunkel = (la, lb) if la >= lb else (lb, la)
    return (hell + 0.05) / (dunkel + 0.05)


def auto_farbe(untergrund, brand=None):
    """Schwarz oder Weiß – was auf diesem Untergrund besser lesbar ist."""
    return SCHWARZ if kontrast(SCHWARZ, untergrund, brand) >= kontrast(WEISS, untergrund, brand) else WEISS


def _bereich_mittel(img, box, standard=(255, 255, 255)):
    """Mittlere RGB-Farbe eines Bildausschnitts. Leerer/ungültiger Bereich -> standard."""
    if img is None:
        return standard
    try:
        x0, y0, x1, y1 = (int(round(float(v))) for v in tuple(box)[:4])
    except (TypeError, ValueError):
        return standard
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = int(_clamp(x0, 0, img.width))
    x1 = int(_clamp(x1, 0, img.width))
    y0 = int(_clamp(y0, 0, img.height))
    y1 = int(_clamp(y1, 0, img.height))
    if x1 <= x0 or y1 <= y0:
        return standard
    try:
        aus = img.crop((x0, y0, x1, y1)).convert('RGB')
        aus = aus.resize((1, 1), getattr(Image, 'BOX', Image.NEAREST))
        return tuple(aus.getpixel((0, 0)))
    except Exception as ex:                                    # pragma: no cover
        log.warning('Bildmittelwert nicht berechenbar: %s', ex)
        return standard


def _el_box(el, canvas_groesse):
    """Elementrechteck (x0, y0, x1, y1). Ohne height (nur bei Text erlaubt) wird eine
    Ersatzhöhe aus max_size × line_height angenommen, sonst 8 % der Leinwandhöhe."""
    cw, ch = canvas_groesse
    x = _num(el.get('x'), 0.0)
    y = _num(el.get('y'), 0.0)
    w = _num(el.get('width'), 0.0) or max(1.0, cw - x)
    h = el.get('height')
    if h in (None, '') or _num(h, 0) <= 0:
        h = _num(el.get('max_size'), 0) * (_num(el.get('line_height'), 1.15) or 1.15)
        if h <= 0:
            h = ch * 0.08
    else:
        h = _num(h, 0)
    return (x, y, x + w, y + h)


def hintergrund_farbe_im_bereich(bg_bild, el, canvas_groesse):
    """Mittlere Farbe des Hintergrundbilds im Bereich eines Elements als '#rrggbb'.

    bg_bild wird – wie beim Rendern – auf die Leinwandgröße eingepasst (ImageOps.fit),
    damit der Bereich derselbe ist wie im fertigen Bild. Ohne lesbares Bild: '#ffffff'
    (das ist die Grundfläche, auf die render() ohne Hintergrund zeichnet)."""
    try:
        size = (int(canvas_groesse[0]), int(canvas_groesse[1]))
    except (TypeError, ValueError, IndexError):
        size = DEFAULT_CANVAS
    if bg_bild in (None, '', False):
        return WEISS
    try:
        img = _open_background(bg_bild)
    except FileNotFoundError:
        return WEISS
    if img.size != size:
        try:
            img = ImageOps.fit(img, size, method=Image.LANCZOS)
        except Exception as ex:                                # pragma: no cover
            log.warning('Hintergrund nicht einpassbar: %s', ex)
            return WEISS
    return _hex(_bereich_mittel(img, _el_box(el if isinstance(el, dict) else {}, size)))


_AUTO_TOKEN = 'stil:auto'


def _enthaelt_auto(obj):
    """True, wenn irgendwo in der Struktur der Token 'stil:auto' steht."""
    if isinstance(obj, str):
        return obj.strip().lower() == _AUTO_TOKEN
    if isinstance(obj, dict):
        return any(_enthaelt_auto(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_enthaelt_auto(v) for v in obj)
    return False


def _auto_ersetzen(obj, farbe):
    """Kopie der Struktur, in der jedes 'stil:auto' durch `farbe` ersetzt ist.
    Ohne Vorkommen wird dasselbe Objekt zurückgegeben (kein unnötiges Kopieren)."""
    if not _enthaelt_auto(obj):
        return obj
    if isinstance(obj, str):
        return farbe
    if isinstance(obj, dict):
        return {k: _auto_ersetzen(v, farbe) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_auto_ersetzen(v, farbe) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_auto_ersetzen(v, farbe) for v in obj)
    return obj


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


def _padding_box(v, default=0.0):
    """Innenabstand: Zahl oder {top,right,bottom,left} -> (top, right, bottom, left)."""
    if isinstance(v, dict):
        t = _num(v.get('top'), default)
        r = _num(v.get('right'), default)
        b = _num(v.get('bottom'), default)
        l = _num(v.get('left'), default)
        return (max(0.0, t), max(0.0, r), max(0.0, b), max(0.0, l))
    if _is_num(v):
        p = max(0.0, _num(v, default))
        return (p, p, p, p)
    p = max(0.0, float(default))
    return (p, p, p, p)


def _text_layout(text, el, canvas_size, brand=None):
    """Vollständige Textgeometrie eines text-Elements. Gemeinsame Grundlage von
    _render_text() und text_metrics(); der Editor kann damit die Bühne genau nachbauen.

    Liefert dict mit font, size, lines, line_height (px), align, valign,
    lines_geom [(x, y, breite, text)], width/height (Blockmaß), block (x0,y0,x1,y1),
    boxes [(x0,y0,x1,y1), ...] (Textkästen), clip (x0,y0,x1,y1) oder None."""
    cw, ch = canvas_size
    x, y = _num(el.get('x'), 0), _num(el.get('y'), 0)
    width = _num(el.get('width'), 0) or (cw - x)
    if width <= 0:
        width = max(1.0, cw - x)
    height = el.get('height')
    height = _num(height) if height not in (None, '') else None
    if height is not None and height <= 0:
        height = None

    pt, pr, pb, pl = _padding_box(el.get('padding'), 0.0)
    inner_x = x + pl
    inner_y = y + pt
    inner_w = max(1.0, width - pl - pr)
    inner_h = None if height is None else max(1.0, height - pt - pb)

    font_key = el.get('font') or _FALLBACK_KEY
    max_size = _int(el.get('max_size'), 64)
    min_size = _int(el.get('min_size'), 24)
    spacing = _num(el.get('letter_spacing'), 0.0)
    lh_factor = _num(el.get('line_height'), 1.15) or 1.15
    max_lines = _int(el.get('max_lines'), 0) if el.get('max_lines') not in (None, '', 0) else None
    if max_lines is not None and max_lines < 1:
        max_lines = None
    fit = str(el.get('fit') or 'shrink').strip().lower()
    if fit not in TEXT_FITS:
        fit = 'shrink'

    if fit in ('shrink', 'none'):
        low = max_size if fit == 'none' else min_size
        font, lines, lh = fit_text(text, font_key, max_size, low, inner_w, inner_h,
                                   max_lines=max_lines, line_height=lh_factor,
                                   letter_spacing=spacing, brand=brand)
    else:
        font = load_font(font_key, max_size, brand=brand)
        lines, _ = _wrap(text, font, inner_w, spacing, hard_break=True)
        lh = font.size * lh_factor
        if max_lines is not None and len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = _ellipsize(lines[-1], font, inner_w, spacing)
        if fit == 'clip' and inner_h is not None and lh > 0:
            keep = max(1, int(inner_h // lh))
            if len(lines) > keep:
                lines = lines[:keep]

    align = str(el.get('align') or 'left').strip().lower()
    if align not in ALIGNS:
        align = 'left'
    valign = str(el.get('valign') or 'top').strip().lower()
    if valign not in VALIGNS:
        valign = 'top'

    block_h = lh * len(lines)
    cy0 = inner_y
    if inner_h is not None:
        if valign == 'middle':
            cy0 = inner_y + (inner_h - block_h) / 2.0
        elif valign == 'bottom':
            cy0 = inner_y + inner_h - block_h

    geom, cy = [], cy0
    for line in lines:
        lw = _text_width(font, line, spacing)
        if align == 'center':
            lx = inner_x + (inner_w - lw) / 2.0
        elif align == 'right':
            lx = inner_x + inner_w - lw
        else:
            lx = inner_x
        geom.append((lx, cy, lw, line))
        cy += lh

    max_w = max([g[2] for g in geom] or [0.0])
    if geom:
        b_x0 = min(g[0] for g in geom)
        b_x1 = max(g[0] + g[2] for g in geom)
    else:
        b_x0 = b_x1 = inner_x
    block = (b_x0, cy0, b_x1, cy0 + block_h)

    boxes = []
    box_cfg = el.get('box')
    if isinstance(box_cfg, dict) and box_cfg:
        bt, br, bb, bl = _padding_box(box_cfg.get('padding'), round(font.size * 0.18))
        if _bool(box_cfg.get('per_line')):
            # Kastenhöhe = echte Zeilenhöhe der Schrift (Ober- + Unterlänge), NICHT der
            # Zeilenvorschub. Sonst wächst der Kasten mit line_height mit, die Bänder stoßen
            # immer aneinander und ein senkrechter Abstand ist gar nicht einstellbar.
            # Deckel bleibt der Vorschub: bei enger Zeilenschaltung ändert sich nichts
            # gegenüber früher, Unterlängen werden weiter überdeckt.
            try:
                asc, desc = font.getmetrics()
                glyph_h = float(asc) + float(desc)
            except Exception:
                glyph_h = 0.0
            zeilen_h = min(lh, glyph_h) if glyph_h > 0 else lh
            for lx, ly, lw, line in geom:
                if not line.strip():
                    continue
                boxes.append((lx - bl, ly - bt, lx + lw + br, ly + zeilen_h + bb))
        else:
            boxes.append((block[0] - bl, block[1] - bt, block[2] + br, block[3] + bb))

    clip = None
    if fit == 'clip':
        clip = (inner_x, inner_y, inner_x + inner_w,
                inner_y + (inner_h if inner_h is not None else block_h))

    return {
        'font': font, 'size': int(font.size), 'lines': lines, 'line_height': lh,
        'align': align, 'valign': valign, 'letter_spacing': spacing,
        'lines_geom': geom, 'width': max_w, 'height': block_h,
        'block': block, 'boxes': boxes, 'clip': clip, 'fit': fit,
        'inner': (inner_x, inner_y, inner_w, inner_h),
    }


def text_metrics(text, el, canvas_groesse, brand=None):
    """Tatsächliche Textmaße eines text-Elements, ohne zu zeichnen – für die Editor-Bühne.

    text:            fertiger Text (Variablen bereits ersetzt); uppercase wird hier angewandt
    el:              das text-Element (dict) aus der Config; eine 'role' wird zuerst aufgelöst
    canvas_groesse:  (breite, hoehe) der Leinwand
    Liefert {'lines': [...], 'size': int, 'width': int, 'height': int,
             'line_height': float, 'x': int, 'y': int, 'lines_geom': [...],
             'block': (x0,y0,x1,y1), 'boxes': [...], 'align', 'valign'}"""
    el = el if isinstance(el, dict) else {}
    try:
        size = (int(canvas_groesse[0]), int(canvas_groesse[1]))
    except (TypeError, ValueError, IndexError):
        size = DEFAULT_CANVAS
    el = rolle_anwenden(el, brand, size[1])
    s = str(text if text is not None else '')
    if _bool(el.get('uppercase')):
        s = s.upper()
    lay = _text_layout(s, el, size, brand=brand)
    return {
        'lines': list(lay['lines']),
        'size': int(lay['size']),
        'width': int(round(lay['width'])),
        'height': int(round(lay['height'])),
        'line_height': float(lay['line_height']),
        'x': int(round(lay['block'][0])),
        'y': int(round(lay['block'][1])),
        'lines_geom': [(round(g[0], 2), round(g[1], 2), round(g[2], 2), g[3]) for g in lay['lines_geom']],
        'block': tuple(round(v, 2) for v in lay['block']),
        'boxes': [tuple(round(v, 2) for v in b) for b in lay['boxes']],
        'align': lay['align'], 'valign': lay['valign'],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Zeichen-Budget und Passprobe (Grundlage für KI-Prompt und Vorlagen-Prüfung)
# ═══════════════════════════════════════════════════════════════════════════════

# Probetext für die mittlere Zeichenbreite UND für die Passprobe des Budgets: echte
# deutsche Wörter mit Leerzeichen, aber bewusst am breiten Rand des Üblichen (rund 9 %
# breiter als ein Fließsatz). Die genannte Grenze soll auch für breite Wörter halten und
# nicht nur für 'iiiii'. Der Anfang ist ein einzelnes Wort, weil kurze Belegungen (ein
# Stadtname) genau so aussehen – ohne Leerzeichen, an dem sich nichts umbrechen lässt.
_BUDGET_PROBE = 'Potsdam Osnabrueck Wochenmarkt Grossbaustelle Sonderangebot Wohngebiet'
# Ohne height UND ohne max_lines ist die Box nach unten offen. Für das Budget wird dann
# mit so vielen Zeilen gerechnet – mehr Text passt zwar, sieht aber nicht mehr aus wie ein Meme.
_BUDGET_ZEILEN_OHNE_HOEHE = 3
_MIN_PROBE_GROESSE = 4          # Untergrenze der Suche in passt_text()


def _innenmasse(el, canvas_groesse):
    """(inner_w, inner_h) eines Text-Elements nach Abzug des padding. inner_h kann None sein."""
    cw, ch = canvas_groesse
    x = _num(el.get('x'), 0.0)
    width = _num(el.get('width'), 0.0) or (cw - x)
    if width <= 0:
        width = max(1.0, cw - x)
    hoehe = el.get('height')
    hoehe = _num(hoehe) if hoehe not in (None, '') else None
    if hoehe is not None and hoehe <= 0:
        hoehe = None
    pt, pr, pb, pl = _padding_box(el.get('padding'), 0.0)
    return (max(1.0, width - pl - pr),
            None if hoehe is None else max(1.0, hoehe - pt - pb))


def _max_lines_von(el):
    n = _int(el.get('max_lines'), 0) if el.get('max_lines') not in (None, '', 0) else None
    return n if (n is None or n >= 1) else None


def _budget_probe(n):
    """Probetext mit genau n Zeichen im Satzbau von _BUDGET_PROBE."""
    n = max(1, int(n))
    roh = (_BUDGET_PROBE + ' ') * (n // (len(_BUDGET_PROBE) + 1) + 2)
    s = roh[:n]
    return (s[:-1] + 'n') if s.endswith(' ') else s   # kein Text endet auf Leerzeichen


def _budget_zaehlen(probe, font, inner_w, spacing, zeilen):
    """Wie viele Zeichen der Probe in `zeilen` Zeilen der Breite inner_w passen.

    Bricht die Probe genau so um, wie der Renderer es täte, nimmt die erlaubten Zeilen und
    zählt sie zusammen. Weil der Umbruch an Leerzeichen trennt, ist das Ergebnis wieder ein
    Anfangsstück der Probe – die gezählte Länge steht also nachweislich im Bild."""
    lines, _overflow = _wrap(probe, font, inner_w, spacing, hard_break=False)
    genommen = []
    for zeile in lines[:max(1, int(zeilen))]:
        if _text_width(font, zeile, spacing) > inner_w:
            # Ein einzelnes Wort ist breiter als die Box: so weit kürzen, bis es steht.
            while zeile and _text_width(font, zeile, spacing) > inner_w:
                zeile = zeile[:-1]
            genommen.append(zeile)
            break
        genommen.append(zeile)
    return max(1, len(' '.join(genommen)))


def zeichen_budget(el, canvas_groesse, brand=None):
    """Wie viele Zeichen bei MINDESTgröße in die Box dieses Text-Elements passen.

    Gerechnet, nicht geschätzt: der Probetext wird bei min_size genau so umgebrochen, wie
    der Renderer es täte, und die erlaubten Zeilen werden gezählt. Ein Text dieser Länge
    besteht damit auch die Passprobe – die Zahl steht im KI-Prompt und im Prüf-Vorschlag als
    Grenze, die eingehalten werden SOLL, und muss deshalb halten. (Die frühere Rechnung
    „mittlere Zeichenbreite × Zeilenzahl“ ließ dem Wortumbruch keine Reserve und fiel dadurch
    5–10 % zu groß aus; sie sagt jetzt nur noch, wie lang die Probe sein muss.)

    Zeilenzahl: max_lines, sonst height/Zeilenvorschub, sonst _BUDGET_ZEILEN_OHNE_HOEHE (3)."""
    el = el if isinstance(el, dict) else {}
    try:
        size = (int(canvas_groesse[0]), int(canvas_groesse[1]))
    except (TypeError, ValueError, IndexError):
        size = DEFAULT_CANVAS
    el = rolle_anwenden(el, brand, size[1])

    inner_w, inner_h = _innenmasse(el, size)
    min_size = max(1, _int(el.get('min_size'), 24))
    max_size = max(1, _int(el.get('max_size'), 64))
    if min_size > max_size:
        min_size = max_size
    lh_factor = _num(el.get('line_height'), 1.15) or 1.15
    spacing = _num(el.get('letter_spacing'), 0.0)

    font = load_font(el.get('font') or _FALLBACK_KEY, min_size, brand=brand)
    probe = _BUDGET_PROBE.upper() if _bool(el.get('uppercase')) else _BUDGET_PROBE
    breite = _text_width(font, probe, spacing)
    mittel = breite / len(probe) if breite > 0 else max(1.0, font.size * 0.5)

    zeilen = _max_lines_von(el)
    if inner_h is not None:
        nach_hoehe = max(1, int(inner_h // max(1.0, font.size * lh_factor)))
        zeilen = nach_hoehe if zeilen is None else min(zeilen, nach_hoehe)
    if zeilen is None:
        zeilen = _BUDGET_ZEILEN_OHNE_HOEHE
    # Die Schätzung sagt nur noch, wie lang die Probe sein muss, damit sie sicher überläuft.
    laenge = max(1, int(inner_w * zeilen / mittel)) * 2 + 20
    gezaehlt = 1
    for _ in range(3):
        probe = _budget_probe(laenge)
        if _bool(el.get('uppercase')):
            probe = probe.upper()
        gezaehlt = _budget_zaehlen(probe, font, inner_w, spacing, zeilen)
        if gezaehlt < laenge:                # Probe war lang genug: die Box war die Grenze
            return gezaehlt
        laenge *= 2
    return gezaehlt


def _vollstaendig(lay, text, inner_w, inner_h, max_lines):
    """True, wenn der Text im Layout ungekürzt, unüberstehend und in der Box steht."""
    gesetzt = ''.join(''.join(lay['lines']).split())
    gewollt = ''.join(str(text).split())
    if gesetzt != gewollt:
        return False                                   # gekürzt ('…') oder abgeschnitten
    if lay['width'] > inner_w + 0.5:
        return False                                   # ein Wort ist breiter als die Box
    if max_lines is not None and len(lay['lines']) > max_lines:
        return False
    if inner_h is not None and lay['height'] > inner_h + 0.5:
        return False
    return True


def passt_text(text, el, canvas_groesse, brand=None):
    """Passt dieser Text in dieses Element?

    Liefert {'passt': bool,
             'groesse': int,                 Schriftgröße, bei der er vollständig steht
             'zeilen': int,                  Zeilen bei dieser Größe
             'noetige_verkleinerung': float} 0.0 wenn er passt, sonst der Anteil, um den die
                                             Mindestgröße noch unterschritten werden müsste
                                             (0.2 = 20 % kleiner als min_size).
    Für die Vorlagen-Prüfung über viele Städte: die längste vorkommende Belegung hier
    hineingeben, dann steht in 'noetige_verkleinerung', wie schlimm es ist.

    Die Suche unterhalb der Mindestgröße ist eine Halbierungssuche und setzt voraus, dass
    kleinere Schrift eher passt als größere. 'groesse' ist immer eine Größe, bei der der
    Text nachweislich vollständig steht (nachgeprüft), niemals eine geratene."""
    el = el if isinstance(el, dict) else {}
    try:
        size = (int(canvas_groesse[0]), int(canvas_groesse[1]))
    except (TypeError, ValueError, IndexError):
        size = DEFAULT_CANVAS
    el = rolle_anwenden(el, brand, size[1])

    s = str(text if text is not None else '')
    if _bool(el.get('uppercase')):
        s = s.upper()
    inner_w, inner_h = _innenmasse(el, size)
    max_lines = _max_lines_von(el)
    min_size = max(1, _int(el.get('min_size'), 24))
    max_size = max(1, _int(el.get('max_size'), 64))
    if min_size > max_size:
        min_size = max_size

    lay = _text_layout(s, el, size, brand=brand)
    if _vollstaendig(lay, s, inner_w, inner_h, max_lines):
        return {'passt': True, 'groesse': int(lay['size']), 'zeilen': len(lay['lines']),
                'noetige_verkleinerung': 0.0}

    # Passt nicht: größte Schriftgröße UNTER der Mindestgröße suchen, bei der er stünde.
    def bei(groesse):
        probe = dict(el)
        probe['max_size'] = groesse
        probe['min_size'] = groesse
        probe['fit'] = 'shrink'
        return _text_layout(s, probe, size, brand=brand)

    boden = max(1, min(_MIN_PROBE_GROESSE, min_size))
    lo, hi = boden, max(boden, min_size - 1)
    treffer = None
    while lo <= hi:
        mitte = (lo + hi) // 2
        kandidat = bei(mitte)
        if _vollstaendig(kandidat, s, inner_w, inner_h, max_lines):
            treffer = (mitte, kandidat)
            lo = mitte + 1
        else:
            hi = mitte - 1
    if treffer is None:
        treffer = (boden, bei(boden))
    groesse, kandidat = treffer
    return {'passt': False, 'groesse': int(groesse), 'zeilen': len(kandidat['lines']),
            'noetige_verkleinerung': round(max(0.0, 1.0 - groesse / float(min_size)), 3)}


# ═══════════════════════════════════════════════════════════════════════════════
# Standardwerte für neue Elemente (Editor und Renderer sollen sich einig sein)
# ═══════════════════════════════════════════════════════════════════════════════

def element_defaults(typ, shape=None):
    """Sinnvolle Startwerte für ein neues Element. Ohne 'id' – die vergibt der Editor.

    typ:   'text' | 'image' | 'cover' | 'shape' | 'rect'
    shape: bei typ='shape' die Form aus SHAPES (Standard 'rect')"""
    t = str(typ or 'text').strip().lower()
    if t not in ELEMENT_TYPES:
        t = 'text'
    base = {
        'type': t, 'name': '', 'x': 80, 'y': 80, 'width': 400, 'height': 300,
        'rotation': 0, 'opacity': 1, 'hidden': False, 'locked': False,
        'flip_h': False, 'flip_v': False, 'blend': 'normal',
    }
    if t == 'text':
        base.update({
            'name': 'Text', 'x': 60, 'y': 80, 'width': 960, 'height': 300,
            'text': 'Neuer Text', 'font': _FALLBACK_KEY,
            'max_size': 72, 'min_size': 28, 'color': '#ffffff',
            'stroke': '#000000', 'stroke_width': 0,
            'align': 'center', 'valign': 'top', 'uppercase': False,
            'max_lines': 0, 'line_height': 1.15, 'letter_spacing': 0,
            'fit': 'shrink', 'padding': 0,
        })
    elif t == 'image':
        base.update({
            'name': 'Bild', 'x': 140, 'y': 300, 'width': 800, 'height': 800,
            'src': '', 'fit': 'cover', 'radius': 0, 'mask': 'none',
        })
    elif t == 'cover':
        base.update({
            'name': 'Abdeckung', 'x': 100, 'y': 100, 'width': 600, 'height': 200,
            'fill': 'auto', 'feather': 2,
        })
    else:                                       # shape / rect
        s = str(shape or ('rect' if t == 'rect' else 'rect')).strip().lower()
        if s not in SHAPES:
            s = 'rect'
        labels = {'rect': 'Rechteck', 'ellipse': 'Ellipse', 'triangle': 'Dreieck',
                  'star': 'Stern', 'line': 'Linie', 'arrow': 'Pfeil', 'bubble': 'Sprechblase'}
        base.update({
            'name': labels.get(s, 'Form'), 'x': 340, 'y': 500, 'width': 400, 'height': 400,
            'fill': '#ffffff', 'stroke': '', 'stroke_width': 0,
        })
        if t == 'shape':
            base['shape'] = s
        if s == 'rect':
            base['radius'] = 24
        elif s == 'star':
            base.update({'points': 5, 'inner': 0.5})
        elif s == 'line':
            base.update({'x': 240, 'y': 640, 'width': 600, 'height': 8,
                         'fill': '', 'stroke': '#ffffff', 'stroke_width': 8})
        elif s == 'arrow':
            base.update({'width': 480, 'height': 200})
        elif s == 'bubble':
            base.update({'x': 200, 'y': 300, 'width': 680, 'height': 320, 'radius': 48,
                         'fill': '#ffffff',
                         'tail': {'x': 340, 'y': 720}})
    return base


# ═══════════════════════════════════════════════════════════════════════════════
# Rollen – eine Entscheidung statt fünfzehn Einzelwerten
# ═══════════════════════════════════════════════════════════════════════════════
# Ein Text-Element bekommt optional 'role'. Die Rolle liefert Vorgaben, die aus dem
# Stadt-Stil aufgelöst werden – dadurch sieht dieselbe Vorlage in jeder Stadt anders aus.
# JEDER am Element gesetzte Wert schlägt die Rolle. Ohne 'role' ändert sich nichts:
# rolle_anwenden() gibt dann dasselbe Objekt unverändert zurück.
#
# Die Größen sind Anteile der LEINWANDHÖHE, nicht Pixel – dieselbe Rolle sitzt auf
# 1080×1350 und auf 1080×1080 gleich gut.
#
# Bedeutung der Felder einer Rolle:
#   schrift   'headline' | 'body'   -> font 'stil:headline' bzw. 'stil:body'
#   farbe     Farbtoken             -> color
#   max, min  Anteil der Höhe       -> max_size, min_size
#   gross     True | 'stil' | False -> uppercase (True = immer, 'stil' = Schreibweise des Stils)
#   kasten    'stil' | False        -> box (Art, Radius und Farbe kommen aus dem Stil)
#   kontur    'stil' | 'dunkel' | 'gegenpol' | False -> stroke + stroke_width
#   schatten  'stil' | True | False -> shadow
#   opacity   Zahl oder None        -> opacity

ROLLEN = {
    'ueberschrift': {
        'beschreibung': 'Die Zeile, die den Beitrag ansagt – ruhig, lesbar, im Kasten des Stils.',
        'schrift': 'headline', 'farbe': 'stil:auto', 'max': 0.075, 'min': 0.035,
        'gross': 'stil', 'kasten': 'stil', 'kontur': 'stil', 'schatten': False, 'opacity': None,
    },
    'pointe': {
        'beschreibung': 'Der Satz, auf den es ankommt – am größten, in der Akzentfarbe, immer mit dunkler Kontur.',
        'schrift': 'headline', 'farbe': 'stil:accent', 'max': 0.095, 'min': 0.045,
        'gross': False, 'kasten': False, 'kontur': 'dunkel', 'schatten': 'stil', 'opacity': None,
    },
    'memezeile': {
        'beschreibung': 'Die klassische Zeile über oder unter dem Bild – hell mit dunkler Kontur.',
        'schrift': 'headline', 'farbe': 'stil:light', 'max': 0.070, 'min': 0.034,
        # Ohne Kasten, dafür immer mit Kontur: diese Zeile steht per Bauart mitten im Foto.
        'gross': True, 'kasten': False, 'kontur': 'gegenpol', 'schatten': False, 'opacity': None,
    },
    'unterzeile': {
        'beschreibung': 'Erklärt die Überschrift in einem halben Satz – kleiner, ohne Kasten.',
        'schrift': 'body', 'farbe': 'stil:auto', 'max': 0.042, 'min': 0.024,
        'gross': False, 'kasten': False, 'kontur': False, 'schatten': False, 'opacity': None,
    },
    'ort': {
        'beschreibung': 'Der Stadt- oder Ortsname – in der Hauptfarbe und immer in Großbuchstaben.',
        'schrift': 'headline', 'farbe': 'stil:primary', 'max': 0.085, 'min': 0.040,
        # Immer mit Kontur: die Hauptfarbe einer Stadt ist eine kräftige Markenfarbe und steht
        # hier direkt auf dem Foto. Ohne Kontur fiel die Zeile in der Messung bei allen
        # geprüften Stilen durch die Lesbarkeitsprüfung (Kontrast 1.1 bis 1.8 zu 1).
        'gross': True, 'kasten': False, 'kontur': 'gegenpol', 'schatten': False, 'opacity': None,
    },
    'klein': {
        'beschreibung': 'Beiwerk wie Quelle, Datum oder Hinweis – klein und zurückhaltend.',
        'schrift': 'body', 'farbe': 'stil:dark', 'max': 0.026, 'min': 0.018,
        'gross': False, 'kasten': False, 'kontur': False, 'schatten': False, 'opacity': 0.85,
    },
    'wasserzeichen': {
        'beschreibung': 'Der eigene Seitenname in der Ecke – da, aber nie im Weg.',
        # 'stil:auto' statt fest hell: ein weißer Seitenname verschwindet auf einem hellen
        # Foto vollständig. Auto wählt je Untergrund Schwarz oder Weiß, die Deckkraft von
        # 0.7 hält ihn trotzdem zurückhaltend.
        'schrift': 'body', 'farbe': 'stil:auto', 'max': 0.024, 'min': 0.016,
        'gross': False, 'kasten': False, 'kontur': False, 'schatten': False, 'opacity': 0.7,
    },
    'frei': {
        'beschreibung': 'Keine Vorgaben – jeder Wert wird am Element selbst gesetzt.',
        'schrift': None, 'farbe': None, 'max': None, 'min': None,
        'gross': False, 'kasten': False, 'kontur': False, 'schatten': False, 'opacity': None,
    },
}

ROLLEN_NAMEN = tuple(ROLLEN.keys())


def _kasten_vorgabe(stil):
    """Textkasten aus der Kastenart des Stils. None = kein Kasten.
    padding bleibt bewusst offen: _text_layout nimmt dann 18 % der Schriftgröße, der Kasten
    wächst also mit der Schrift mit."""
    art = stil['kasten']
    if art == 'voll':
        return {'fill': stil['light'], 'radius': stil['radius'], 'per_line': True}
    if art == 'weich':
        return {'fill': _mit_alpha(stil['light'], 0.78),
                'radius': max(int(stil['radius']), 18), 'per_line': True}
    if art == 'umriss':
        return {'stroke': stil['primary'], 'stroke_width': 3,
                'radius': stil['radius'], 'per_line': False}
    return None                                       # 'keiner'


def _rollen_vorgaben(name, stil, canvas_hoehe):
    """Die Vorgaben einer Rolle als Element-Felder (nur gesetzte Schlüssel)."""
    r = ROLLEN.get(name) or {}
    h = max(1.0, _num(canvas_hoehe, DEFAULT_CANVAS[1]))
    out = {}
    if r.get('schrift'):
        out['font'] = 'stil:' + r['schrift']
    if r.get('farbe'):
        out['color'] = r['farbe']
    if r.get('max'):
        out['max_size'] = max(1, int(round(r['max'] * h)))
    if r.get('min'):
        out['min_size'] = max(1, int(round(r['min'] * h)))

    gross = r.get('gross')
    if gross is True:
        out['uppercase'] = True
    elif gross == 'stil':
        out['uppercase'] = (stil['schreibweise'] == 'gross')

    if r.get('kasten') == 'stil':
        kasten = _kasten_vorgabe(stil)
        if kasten is not None:
            out['box'] = kasten

    kontur = r.get('kontur')
    strichstaerke = max(2, int(round(0.005 * h)))
    if kontur == 'dunkel':
        out['stroke'] = stil['dark']
        out['stroke_width'] = strichstaerke
    elif kontur == 'gegenpol':
        # Die Schriftfarbe dieser Rolle ist eine kräftige Markenfarbe und steht direkt auf dem
        # Foto. Eine fest dunkle Kontur verschwindet hinter dunklen Markenfarben. Darum die
        # Kontur auf den Gegenpol legen: die von 'dark' und 'light', die weiter weg liegt.
        farbe = r.get('farbe') or stil['dark']
        out['stroke'] = (stil['dark'] if kontrast(farbe, stil['dark'], stil)
                         >= kontrast(farbe, stil['light'], stil) else stil['light'])
        out['stroke_width'] = strichstaerke
    elif kontur == 'stil':
        if stil['kontur'] == 'dunkel':
            out['stroke'] = stil['dark']
            out['stroke_width'] = strichstaerke
        elif stil['kontur'] == 'palette':
            out['stroke'] = stil['secondary']
            out['stroke_width'] = strichstaerke

    schatten = r.get('schatten')
    if schatten is True or (schatten == 'stil' and stil['schatten']):
        versatz = max(1, int(round(0.004 * h)))
        out['shadow'] = {'dx': versatz, 'dy': versatz,
                         'blur': max(1, int(round(0.006 * h))),
                         'color': _mit_alpha(stil['dark'], 0.55)}

    if r.get('opacity') is not None:
        out['opacity'] = r['opacity']
    return out


def rolle_anwenden(el, brand=None, canvas_hoehe=None):
    """Element-Dict mit eingesetzten Rollenvorgaben.

    el:           ein Element aus der Config (dict)
    brand:        Stil- oder brand-Dict (wird durch stil_dict() vervollständigt)
    canvas_hoehe: Höhe der Leinwand in Pixeln (Grundlage aller Rollen-Größen)

    Ein Wert der Rolle wird NUR eingesetzt, wenn das Element den Schlüssel gar nicht hat
    oder er None bzw. '' ist. Wer 'box': false setzt, bekommt also keinen Kasten, auch wenn
    der Stil einen vorsieht. Ohne (oder mit unbekannter) 'role' wird el unverändert und als
    dasselbe Objekt zurückgegeben – alte Vorlagen laufen dadurch bitgleich weiter."""
    if not isinstance(el, dict):
        return el
    name = str(el.get('role') or '').strip().lower()
    if not name or name not in ROLLEN or name == 'frei':
        return el
    stil = stil_dict(brand)
    hoehe = _num(canvas_hoehe, DEFAULT_CANVAS[1]) or DEFAULT_CANVAS[1]
    neu = dict(el)
    for k, v in _rollen_vorgaben(name, stil, hoehe).items():
        if k not in neu or neu.get(k) in (None, ''):
            neu[k] = v
    return neu


# ═══════════════════════════════════════════════════════════════════════════════
# Validierung
# ═══════════════════════════════════════════════════════════════════════════════

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

    def check_paint(value, label, feld):
        """Farbe oder Verlauf prüfen."""
        if value in (None, '', False):
            return
        if _is_gradient(value):
            stops = value.get('stops')
            if not isinstance(stops, (list, tuple)) or not stops:
                errors.append(f'{label}: Verlauf in {feld} hat keine stops')
                return
            if 'angle' in value and value.get('angle') not in (None, '') and not _is_num(value.get('angle')):
                errors.append(f'{label}: {feld}.angle muss eine Zahl sein')
            elif _is_num(value.get('angle')) and not (-360 <= _num(value.get('angle')) <= 360):
                errors.append(f'{label}: {feld}.angle muss zwischen -360 und 360 liegen')
            ok = 0
            for j, st in enumerate(stops):
                col = st.get('color') if isinstance(st, dict) else st
                if parse_color(col) is None:
                    errors.append(f"{label}: ungültige Farbe in {feld}.stops[{j}] ('{col}')")
                else:
                    ok += 1
                if isinstance(st, dict) and st.get('at') not in (None, '') and (
                        not _is_num(st.get('at')) or not (0 <= _num(st.get('at')) <= 1)):
                    errors.append(f'{label}: {feld}.stops[{j}].at muss zwischen 0 und 1 liegen')
            if not ok:
                errors.append(f'{label}: Verlauf in {feld} hat keine gültige Farbe')
            return
        if parse_color(value) is None:
            errors.append(f"{label}: ungültige Farbe in {feld} ('{value}')")

    canvas = config.get('canvas')
    if canvas is not None:
        if not isinstance(canvas, dict):
            errors.append('canvas muss ein Objekt mit width/height sein')
        else:
            for k in ('width', 'height'):
                if canvas.get(k) in (None, ''):
                    continue                    # canvas darf auch nur background enthalten
                if not _is_num(canvas.get(k)) or _num(canvas.get(k)) <= 0:
                    errors.append(f'canvas.{k} muss eine positive Zahl sein')
            if ('width' in canvas) != ('height' in canvas):
                errors.append('canvas braucht width UND height (oder keins von beidem)')
            check_paint(canvas.get('background'), 'canvas', 'background')

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
        if t not in ELEMENT_TYPES:
            errors.append(f"{label}: unbekannter type '{t}' (erlaubt: {', '.join(ELEMENT_TYPES)})")
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
        if el.get('rotation') not in (None, ''):
            if not _is_num(el.get('rotation')) or not (-360 <= _num(el.get('rotation')) <= 360):
                errors.append(f'{label}: rotation muss eine Zahl zwischen -360 und 360 sein')
        if el.get('blend') not in (None, '') and str(el.get('blend')).lower() not in BLEND_MODES:
            errors.append(f"{label}: blend muss {', '.join(BLEND_MODES)} sein")
        for k in ('only_if', 'skip_if'):
            v = el.get(k)
            if v not in (None, '') and not isinstance(v, str):
                errors.append(f'{label}: {k} muss ein Variablenname (Text) sein')
        if el.get('group') not in (None, '') and not isinstance(el.get('group'), (str, int)):
            errors.append(f'{label}: group muss ein Text oder eine Zahl sein')
        rolle = el.get('role')
        if rolle not in (None, ''):
            if not isinstance(rolle, str):
                errors.append(f'{label}: role muss ein Text sein')
            elif rolle.strip().lower() not in ROLLEN:
                errors.append(f"{label}: unbekannte Rolle '{rolle}' "
                              f"(erlaubt: {', '.join(ROLLEN_NAMEN)})")
            elif t != 'text':
                errors.append(f'{label}: role wirkt nur auf text-Elemente')
        if el.get('name') not in (None, '') and not isinstance(el.get('name'), str):
            errors.append(f'{label}: name muss ein Text sein')

        def check_color(key, container=el, lbl=label):
            check_paint(container.get(key), lbl, key)

        if t == 'text':
            if not el.get('var') and not el.get('text') and el.get('fallback') in (None, ''):
                errors.append(f'{label}: text-Element braucht var oder text')
            for k in ('max_size', 'min_size'):
                if k in el and (not _is_num(el.get(k)) or _num(el.get(k)) <= 0):
                    errors.append(f'{label}: {k} muss eine positive Zahl sein')
            if _is_num(el.get('max_size')) and _is_num(el.get('min_size')) and _num(el['min_size']) > _num(el['max_size']):
                errors.append(f'{label}: min_size ist größer als max_size')
            if el.get('align') not in (None, '') and el.get('align') not in ALIGNS:
                errors.append(f"{label}: align muss {', '.join(ALIGNS)} sein")
            if el.get('valign') not in (None, '') and el.get('valign') not in VALIGNS:
                errors.append(f"{label}: valign muss {', '.join(VALIGNS)} sein")
            if el.get('fit') not in (None, '') and el.get('fit') not in TEXT_FITS:
                errors.append(f"{label}: fit muss {', '.join(TEXT_FITS)} sein")
            if el.get('max_lines') not in (None, '', 0) and (not _is_num(el.get('max_lines')) or _num(el.get('max_lines')) < 1):
                errors.append(f'{label}: max_lines muss eine ganze Zahl ≥ 1 sein')
            if el.get('line_height') not in (None, '') and (not _is_num(el.get('line_height')) or _num(el.get('line_height')) <= 0):
                errors.append(f'{label}: line_height muss eine positive Zahl sein')
            if el.get('letter_spacing') not in (None, '') and not _is_num(el.get('letter_spacing')):
                errors.append(f'{label}: letter_spacing muss eine Zahl sein')
            if el.get('stroke_width') not in (None, '') and (not _is_num(el.get('stroke_width')) or _num(el.get('stroke_width')) < 0):
                errors.append(f'{label}: stroke_width muss eine Zahl ≥ 0 sein')
            pad = el.get('padding')
            if pad not in (None, ''):
                if isinstance(pad, dict):
                    for k in ('top', 'right', 'bottom', 'left'):
                        if pad.get(k) not in (None, '') and (not _is_num(pad.get(k)) or _num(pad.get(k)) < 0):
                            errors.append(f'{label}: padding.{k} muss eine Zahl ≥ 0 sein')
                elif not _is_num(pad) or _num(pad) < 0:
                    errors.append(f'{label}: padding muss eine Zahl ≥ 0 oder ein Objekt sein')
            check_color('color')
            check_color('stroke')
            box = el.get('box')
            if box not in (None, '', False):
                if not isinstance(box, dict):
                    errors.append(f'{label}: box muss ein Objekt (fill, radius, padding, stroke, stroke_width, per_line) sein')
                else:
                    if box.get('fill') in (None, '') and box.get('stroke') in (None, ''):
                        errors.append(f'{label}: box braucht fill oder stroke')
                    check_paint(box.get('fill'), label + ' box', 'fill')
                    check_paint(box.get('stroke'), label + ' box', 'stroke')
                    for k in ('radius', 'stroke_width'):
                        if box.get(k) not in (None, '') and (not _is_num(box.get(k)) or _num(box.get(k)) < 0):
                            errors.append(f'{label}: box.{k} muss eine Zahl ≥ 0 sein')
                    bpad = box.get('padding')
                    if bpad not in (None, ''):
                        if isinstance(bpad, dict):
                            for k in ('top', 'right', 'bottom', 'left'):
                                if bpad.get(k) not in (None, '') and (not _is_num(bpad.get(k)) or _num(bpad.get(k)) < 0):
                                    errors.append(f'{label}: box.padding.{k} muss eine Zahl ≥ 0 sein')
                        elif not _is_num(bpad) or _num(bpad) < 0:
                            errors.append(f'{label}: box.padding muss eine Zahl ≥ 0 oder ein Objekt sein')
            sh = el.get('shadow')
            if sh not in (None, '', False):
                if not isinstance(sh, dict):
                    errors.append(f'{label}: shadow muss ein Objekt (dx, dy, color, blur) sein')
                else:
                    for k in ('dx', 'dy', 'blur'):
                        if k in sh and not _is_num(sh.get(k)):
                            errors.append(f'{label}: shadow.{k} muss eine Zahl sein')
                    check_paint(sh.get('color'), label + ' shadow', 'color')

        elif t == 'image':
            if not el.get('var') and not el.get('src'):
                errors.append(f'{label}: image-Element braucht var oder src')
            if el.get('fit') not in (None, '') and el.get('fit') not in IMAGE_FITS:
                errors.append(f"{label}: fit muss {', '.join(IMAGE_FITS)} sein")
            if el.get('radius') not in (None, '') and (not _is_num(el.get('radius')) or _num(el.get('radius')) < 0):
                errors.append(f'{label}: radius muss eine Zahl ≥ 0 sein')
            if el.get('mask') not in (None, '') and str(el.get('mask')).lower() not in MASKS:
                errors.append(f"{label}: mask muss {', '.join(MASKS)} sein")
            crop = el.get('crop')
            if crop not in (None, '', False):
                if not isinstance(crop, dict):
                    errors.append(f'{label}: crop muss ein Objekt (x, y, w, h als Anteile 0..1) sein')
                else:
                    for k in ('x', 'y', 'w', 'h'):
                        if crop.get(k) in (None, ''):
                            continue
                        if not _is_num(crop.get(k)) or not (0 <= _num(crop.get(k)) <= 1):
                            errors.append(f'{label}: crop.{k} muss zwischen 0 und 1 liegen')
                    for k in ('w', 'h'):
                        if _is_num(crop.get(k)) and _num(crop.get(k)) <= 0:
                            errors.append(f'{label}: crop.{k} muss größer als 0 sein')
            adj = el.get('adjust')
            if adj not in (None, '', False):
                if not isinstance(adj, dict):
                    errors.append(f'{label}: adjust muss ein Objekt (brightness, contrast, saturation, blur, grayscale) sein')
                else:
                    for k in ('brightness', 'contrast', 'saturation'):
                        if adj.get(k) in (None, ''):
                            continue
                        if not _is_num(adj.get(k)) or not (0 <= _num(adj.get(k)) <= 2):
                            errors.append(f'{label}: adjust.{k} muss zwischen 0 und 2 liegen')
                    if adj.get('blur') not in (None, '') and (not _is_num(adj.get('blur')) or _num(adj.get('blur')) < 0):
                        errors.append(f'{label}: adjust.blur muss eine Zahl ≥ 0 sein')
                    if adj.get('grayscale') not in (None, '') and (
                            not _is_num(adj.get('grayscale')) or not (0 <= _num(adj.get('grayscale')) <= 1)):
                        errors.append(f'{label}: adjust.grayscale muss zwischen 0 und 1 liegen')

        elif t == 'cover':
            fill = el.get('fill', 'auto')
            if fill not in (None, '', 'auto', 'inpaint'):
                check_paint(fill, label, 'fill')
            if el.get('feather') not in (None, '') and (not _is_num(el.get('feather')) or _num(el.get('feather')) < 0):
                errors.append(f'{label}: feather muss eine Zahl ≥ 0 sein')

        else:                                   # shape / rect
            shape = str(el.get('shape') or 'rect').strip().lower()
            if t == 'shape' and shape not in SHAPES:
                errors.append(f"{label}: unbekannte shape '{el.get('shape')}' (erlaubt: {', '.join(SHAPES)})")
                shape = 'rect'
            if el.get('fill') in (None, '') and el.get('stroke') in (None, ''):
                errors.append(f'{label}: {t}-Element braucht fill oder stroke')
            check_color('fill')
            check_color('stroke')
            for k in ('radius', 'stroke_width'):
                if el.get(k) not in (None, '') and (not _is_num(el.get(k)) or _num(el.get(k)) < 0):
                    errors.append(f'{label}: {k} muss eine Zahl ≥ 0 sein')
            if shape == 'star':
                if el.get('points') not in (None, '') and (not _is_num(el.get('points')) or _num(el.get('points')) < 3):
                    errors.append(f'{label}: points muss eine ganze Zahl ≥ 3 sein')
                if el.get('inner') not in (None, '') and (not _is_num(el.get('inner')) or not (0 < _num(el.get('inner')) <= 1)):
                    errors.append(f'{label}: inner muss größer als 0 und höchstens 1 sein')
            if shape == 'bubble':
                tail = el.get('tail')
                if tail not in (None, '', False):        # ohne tail = Standardposition, erlaubt
                    if not isinstance(tail, dict):
                        errors.append(f'{label}: tail muss ein Objekt mit x und y sein')
                    else:
                        for k in ('x', 'y'):
                            if not _is_num(tail.get(k)):
                                errors.append(f'{label}: tail.{k} muss eine Zahl sein')

    return errors


# ═══════════════════════════════════════════════════════════════════════════════
# Rendern – Grundlagen
# ═══════════════════════════════════════════════════════════════════════════════

_VAR_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')


def _open_background(bg_path):
    """bg_path: Dateipfad, Bytes, Dateiobjekt oder PIL-Image. Fehlt die Datei oder ist sie
    nicht lesbar (leer, abgeschnitten, kein Bildformat) -> FileNotFoundError. Das ist der
    einzige Fehler, der nach außen geht; die Aufrufer fangen genau diesen ab."""
    if isinstance(bg_path, Image.Image):
        return bg_path.convert('RGBA')
    if isinstance(bg_path, (bytes, bytearray)):
        source, label = io.BytesIO(bg_path), f'{len(bg_path)} Bytes'
    elif hasattr(bg_path, 'read'):
        source, label = bg_path, getattr(bg_path, 'name', 'Dateiobjekt')
    else:
        if not bg_path or not os.path.isfile(str(bg_path)):
            raise FileNotFoundError(f'Hintergrundbild fehlt: {bg_path!r}')
        source = label = str(bg_path)
    try:
        return Image.open(source).convert('RGBA')
    except FileNotFoundError:
        raise
    except Exception as ex:
        raise FileNotFoundError(f'Hintergrundbild nicht lesbar: {label} ({type(ex).__name__}: {ex})')


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


def _has_value(values, name):
    """True, wenn die Variable vorhanden und nicht leer ist (Grundlage für only_if/skip_if)."""
    if not name:
        return False
    v = values.get(str(name))
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


def _apply_opacity(layer, opacity):
    if opacity >= 1:
        return layer
    alpha = layer.getchannel('A').point(lambda a: int(a * opacity))
    layer.putalpha(alpha)
    return layer


def _mask_layer(layer, mask):
    """Alpha der Ebene mit einer L-Maske multiplizieren (in-place)."""
    layer.putalpha(ImageChops.multiply(layer.getchannel('A'), mask))
    return layer


def _blit(layer, patch, ox, oy):
    """RGBA-Ausschnitt an (ox, oy) auf die Ebene legen. Anders als Image.alpha_composite()
    verträgt das negative Koordinaten und Überstand – beides kommt bei Konturen,
    Sprechblasen-Spitzen und Textkästen am Rand ständig vor."""
    ox, oy = int(round(ox)), int(round(oy))
    px0, py0 = max(0, -ox), max(0, -oy)
    px1 = min(patch.width, layer.width - ox)
    py1 = min(patch.height, layer.height - oy)
    if px1 <= px0 or py1 <= py0:
        return layer
    if (px0, py0, px1, py1) != (0, 0, patch.width, patch.height):
        patch = patch.crop((px0, py0, px1, py1))
    layer.alpha_composite(patch, (ox + px0, oy + py0))
    return layer


def _transform_layer(layer, cx, cy, rotation, flip_h, flip_v):
    """Ebene um die Boxmitte (cx, cy) spiegeln und drehen. Positive Winkel = im Uhrzeigersinn.
    Der Mittelpunkt bleibt dabei an derselben Stelle."""
    rot = _num(rotation, 0.0) % 360.0
    if rot > 180:
        rot -= 360
    if abs(rot) < 0.01 and not flip_h and not flip_v:
        return layer
    bbox = layer.getbbox()
    if bbox is None:
        return layer
    cxi, cyi = int(round(cx)), int(round(cy))
    rx = int(math.ceil(max(abs(bbox[0] - cxi), abs(bbox[2] - cxi)))) + 2
    ry = int(math.ceil(max(abs(bbox[1] - cyi), abs(bbox[3] - cyi)))) + 2
    rx, ry = max(1, rx), max(1, ry)
    patch = layer.crop((cxi - rx, cyi - ry, cxi + rx, cyi + ry))
    if flip_h:
        patch = ImageOps.mirror(patch)
    if flip_v:
        patch = ImageOps.flip(patch)
    if abs(rot) >= 0.01:
        patch = patch.rotate(-rot, resample=Image.BICUBIC, expand=True)
    out = Image.new('RGBA', layer.size, (0, 0, 0, 0))
    out.paste(patch, (cxi - patch.width // 2, cyi - patch.height // 2))
    return out


_CHOPS = {
    'multiply': ImageChops.multiply,
    'screen': ImageChops.screen,
    'overlay': ImageChops.overlay,
    'darken': ImageChops.darker,
    'lighten': ImageChops.lighter,
}


def _composite(base, layer, blend='normal'):
    """Ebene auf die (deckende) Leinwand bringen. 'normal' = alpha_composite,
    sonst der jeweilige Mischmodus über ImageChops, maskiert mit dem Alpha der Ebene."""
    mode = str(blend or 'normal').strip().lower()
    if mode not in BLEND_MODES:
        if mode:
            log.warning('Unbekannter blend-Modus %r, nutze normal', blend)
        mode = 'normal'
    if mode == 'normal':
        return Image.alpha_composite(base, layer)
    op = _CHOPS[mode]
    mixed = op(base.convert('RGB'), layer.convert('RGB')).convert('RGBA')
    mixed.putalpha(layer.getchannel('A'))
    return Image.alpha_composite(base, mixed)


# ═══════════════════════════════════════════════════════════════════════════════
# Rendern – Text
# ═══════════════════════════════════════════════════════════════════════════════

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


def _rounded_mask(size, radius, box=None):
    """L-Maske mit abgerundetem Rechteck, kantengeglättet über Supersampling."""
    w, h = size
    scale = _SUPERSAMPLE if w * h * _SUPERSAMPLE ** 2 <= _SUPERSAMPLE_MAX_PIXELS else 1
    m = Image.new('L', (max(1, w * scale), max(1, h * scale)), 0)
    x0, y0, x1, y1 = box if box else (0, 0, w, h)
    ImageDraw.Draw(m).rounded_rectangle(
        (x0 * scale, y0 * scale, x1 * scale - 1, y1 * scale - 1),
        radius=max(0, radius) * scale, fill=255)
    if scale > 1:
        m = m.resize((w, h), Image.LANCZOS)
    return m


def _text_untergrund(img, el, lay, brand):
    """Was liegt hinter diesem Text? Eine gesetzte Kastenfarbe gewinnt (halbdurchsichtige
    wird über den Bildmittelwert gerechnet), sonst der Mittelwert des bisher gezeichneten
    Bildes im Textblock. Grundlage für 'stil:auto'."""
    box_cfg = el.get('box')
    if isinstance(box_cfg, dict):
        fuellung = box_cfg.get('fill')
        if fuellung not in (None, '', False) and not _enthaelt_auto(fuellung):
            c = parse_color(fuellung, brand, None)
            if c is not None:
                if c[3] >= 250:
                    return tuple(c[:3])
                hinter = _bereich_mittel(img, lay['block'])
                a = c[3] / 255.0
                return tuple(int(round(c[i] * a + hinter[i] * (1 - a))) for i in range(3))
    return _bereich_mittel(img, lay['block'])


def _render_text(img, el, values, brand):
    text = _value_for(el, values)
    if text is None:
        # Ohne Wert UND ohne fallback fällt die Zeile ersatzlos weg. Das war bisher völlig
        # still: die Vorlage rendert scheinbar sauber, das Bild ist aber nur der Hintergrund.
        log.warning('Text-Element %s ohne Wert (var=%r, kein fallback) – nicht gezeichnet',
                    el.get('id') or el.get('name') or el.get('var') or '?', el.get('var'))
        return None
    if _bool(el.get('uppercase')):
        text = text.upper()

    lay = _text_layout(text, el, img.size, brand=brand)
    # 'stil:auto' braucht den Untergrund und den kennt erst dieser Moment: das Bild ist bis
    # hierher gezeichnet, die Textgeometrie steht. Danach ist el frei von 'stil:auto'.
    if _enthaelt_auto(el):
        el = _auto_ersetzen(el, auto_farbe(_text_untergrund(img, el, lay, brand)))
    font, lines, lh = lay['font'], lay['lines'], lay['line_height']
    spacing = lay['letter_spacing']
    color = parse_color(el.get('color'), brand, (255, 255, 255, 255))
    stroke = parse_color(el.get('stroke'), brand, None)
    stroke_width = max(0, _int(el.get('stroke_width'), 0))
    if stroke is None:
        stroke_width = 0
    else:
        # Eine Kontur soll den Text vom Untergrund abheben. Fällt sie mit der Textfarbe
        # zusammen, wird die Schrift zu einem Klumpen – genau das passiert, wenn ein Stil
        # dunkle Kontur vorgibt und 'stil:auto' auf hellem Grund ebenfalls Dunkel wählt.
        # Dann auf den Gegenpol drehen, statt den Text unlesbar zu machen.
        if kontrast(color, stroke, brand) < KONTUR_MIN_KONTRAST:
            stroke = parse_color(SCHWARZ if leuchtdichte(color, brand) > 0.5 else WEISS,
                                 brand, (0, 0, 0, 255))

    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))

    # 1) Kästen hinter dem Text (Blockkasten oder je Zeile)
    box_cfg = el.get('box')
    if lay['boxes'] and isinstance(box_cfg, dict):
        b_radius = max(0, _int(box_cfg.get('radius'), 0))
        b_stroke = parse_paint(box_cfg.get('stroke'), brand)
        b_stroke_w = max(0, _int(box_cfg.get('stroke_width'), 0)) if b_stroke else 0
        for bx0, by0, bx1, by1 in lay['boxes']:
            bw = max(1, int(math.ceil(bx1 - bx0)))
            bh = max(1, int(math.ceil(by1 - by0)))
            ox, oy = int(math.floor(bx0)), int(math.floor(by0))
            fill_img = fill_flaeche((bw, bh), box_cfg.get('fill'), brand)
            if fill_img is not None:
                _mask_layer(fill_img, _rounded_mask((bw, bh), b_radius))
                _blit(layer, fill_img, ox, oy)
            if b_stroke_w:
                outer = _rounded_mask((bw, bh), b_radius)
                inner = _rounded_mask((bw, bh), max(0, b_radius - b_stroke_w),
                                      box=(b_stroke_w, b_stroke_w, bw - b_stroke_w, bh - b_stroke_w))
                stroke_img = fill_flaeche((bw, bh), box_cfg.get('stroke'), brand)
                if stroke_img is not None:
                    _mask_layer(stroke_img, ImageChops.subtract(outer, inner))
                    _blit(layer, stroke_img, ox, oy)

    # 2) Schatten
    shadow = el.get('shadow')
    if isinstance(shadow, dict) and shadow:
        s_color = parse_color(shadow.get('color'), brand, (0, 0, 0, 160))
        dx, dy = _num(shadow.get('dx'), 2), _num(shadow.get('dy'), 2)
        blur = max(0.0, _num(shadow.get('blur'), 0))
        s_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(s_layer)
        for lx, ly, _lw, line in lay['lines_geom']:
            _draw_line(sd, (lx + dx, ly + dy), line, font, s_color, stroke_width, s_color, spacing)
        if blur > 0:
            s_layer = s_layer.filter(ImageFilter.GaussianBlur(blur))
        layer = Image.alpha_composite(layer, s_layer)

    # 3) Text
    draw = ImageDraw.Draw(layer)
    for lx, ly, _lw, line in lay['lines_geom']:
        _draw_line(draw, (lx, ly), line, font, color, stroke_width, stroke, spacing)

    # 4) fit='clip': alles außerhalb der (um padding verkleinerten) Elementbox abschneiden
    if lay['clip']:
        cx0, cy0, cx1, cy1 = lay['clip']
        m = Image.new('L', img.size, 0)
        ImageDraw.Draw(m).rectangle((int(round(cx0)), int(round(cy0)),
                                     int(round(cx1)) - 1, int(round(cy1)) - 1), fill=255)
        _mask_layer(layer, m)

    # Drehpunkt: ohne height gibt es keine Elementbox – dann die tatsächliche Textmitte
    center = None
    if el.get('height') in (None, ''):
        ix, _iy, iw, _ih = lay['inner']
        center = (ix + iw / 2.0, (lay['block'][1] + lay['block'][3]) / 2.0)
    return layer, center


# ═══════════════════════════════════════════════════════════════════════════════
# Rendern – Bild
# ═══════════════════════════════════════════════════════════════════════════════

def _is_safe_public_url(url):
    """True, wenn die URL http(s) ist und JEDE aufgelöste Adresse öffentlich ist.

    Gleiche Prüfung wie app._safe_fetch_url, hier lokal umgesetzt: memeos_render importiert
    bewusst kein app.py. Verhindert, dass eine im Studio eingetippte oder in einer
    Template-Config gespeicherte URL den Server auf localhost, ins interne Netz oder auf
    den Metadatendienst der Cloud (169.254.169.254) schicken kann."""
    try:
        parts = urlparse(str(url or ''))
    except ValueError:
        return False
    if parts.scheme not in ('http', 'https'):
        return False
    try:
        host, port = parts.hostname, parts.port
    except ValueError:          # unbrauchbare Portangabe
        return False
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == 'https' else 80),
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _fetch_image_url(url):
    """Bild von einer öffentlichen http(s)-URL holen. Weiterleitungen werden einzeln geprüft
    (sonst ließe sich die Zielprüfung per Redirect umgehen), die Größe ist begrenzt."""
    import requests  # nur bei URLs nötig
    current = str(url).strip()
    for _ in range(_IMAGE_MAX_REDIRECTS + 1):
        if not _is_safe_public_url(current):
            raise ValueError(f'Bild-URL nicht erlaubt (kein öffentliches http/https-Ziel): {current}')
        resp = requests.get(current, timeout=_IMAGE_TIMEOUT, allow_redirects=False, stream=True)
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get('Location')
                if not location:
                    raise ValueError('Weiterleitung ohne Ziel')
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            data = b''
            for chunk in resp.iter_content(1 << 16):
                data += chunk
                if len(data) > _IMAGE_MAX_BYTES:
                    raise ValueError(f'Bild größer als {_IMAGE_MAX_BYTES // (1024 * 1024)} MB')
        finally:
            resp.close()
        return data
    raise ValueError('Zu viele Weiterleitungen')


def _local_image_path(src):
    """Dateiname → Pfad in einem der erlaubten Ordner. Absolute Pfade, Ordnerangaben und
    Symlinks aus den Ordnern heraus werden abgelehnt – sonst ließe sich über eine
    Template-Config jede Datei des Servers in ein Bild hineinrendern."""
    s = str(src).strip().replace('\\', '/')
    if os.path.isabs(s) or s.startswith('~') or (len(s) > 1 and s[1] == ':'):
        raise ValueError(f'Absolute Bildpfade sind nicht erlaubt: {src}')
    name = os.path.basename(s)
    if not name or name in ('.', '..'):
        raise ValueError(f'Bildquelle ohne Dateinamen: {src}')
    for folder in _IMAGE_DIRS:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        real, root = os.path.realpath(path), os.path.realpath(folder)
        if real == root or real.startswith(root + os.sep):
            return real
        log.warning('Bildquelle %s zeigt aus %s heraus, übersprungen', name, folder)
    raise FileNotFoundError(f'Bildquelle nicht gefunden: {name}')


def _load_image_source(src):
    s = str(src).strip()
    if s.lower().startswith(('http://', 'https://')):
        return Image.open(io.BytesIO(_fetch_image_url(s))).convert('RGBA')
    return Image.open(_local_image_path(s)).convert('RGBA')


def _apply_crop(im, crop):
    """crop {x, y, w, h} als Anteile 0..1 des Quellbilds."""
    if not isinstance(crop, dict) or not crop:
        return im
    cx = _clamp(_num(crop.get('x'), 0.0), 0.0, 1.0)
    cy = _clamp(_num(crop.get('y'), 0.0), 0.0, 1.0)
    cw = _clamp(_num(crop.get('w'), 1.0 - cx), 0.0, 1.0)
    chh = _clamp(_num(crop.get('h'), 1.0 - cy), 0.0, 1.0)
    x0 = int(round(cx * im.width))
    y0 = int(round(cy * im.height))
    x1 = min(im.width, x0 + max(1, int(round(cw * im.width))))
    y1 = min(im.height, y0 + max(1, int(round(chh * im.height))))
    if x1 <= x0 or y1 <= y0:
        return im
    return im.crop((x0, y0, x1, y1))


def _apply_adjust(im, adjust):
    """brightness/contrast/saturation (0..2, 1 = unverändert), blur (px), grayscale (0..1)."""
    if not isinstance(adjust, dict) or not adjust:
        return im
    alpha = im.getchannel('A')
    rgb = im.convert('RGB')
    for key, cls in (('brightness', ImageEnhance.Brightness),
                     ('contrast', ImageEnhance.Contrast),
                     ('saturation', ImageEnhance.Color)):
        if adjust.get(key) in (None, ''):
            continue
        f = _clamp(_num(adjust.get(key), 1.0), 0.0, 4.0)
        if abs(f - 1.0) > 1e-3:
            rgb = cls(rgb).enhance(f)
    gray = _clamp(_num(adjust.get('grayscale'), 0.0), 0.0, 1.0)
    if gray > 0:
        rgb = Image.blend(rgb, ImageOps.grayscale(rgb).convert('RGB'), gray)
    blur = max(0.0, _num(adjust.get('blur'), 0.0))
    if blur > 0:
        rgb = rgb.filter(ImageFilter.GaussianBlur(blur))
    out = rgb.convert('RGBA')
    out.putalpha(alpha)
    return out


def _render_image(img, el, values, brand):
    var = el.get('var')
    src = values.get(var) if var else el.get('src')
    if src is None or (isinstance(src, str) and not src.strip()):
        src = el.get('fallback')
    if src is None or (isinstance(src, str) and not src.strip()):
        return None
    x, y = _int(el.get('x'), 0), _int(el.get('y'), 0)
    w, h = max(1, _int(el.get('width'), 100)), max(1, _int(el.get('height'), 100))
    fit = str(el.get('fit') or 'contain').strip().lower()
    if fit not in IMAGE_FITS:
        fit = 'contain'
    radius = max(0, _int(el.get('radius'), 0))
    mask_kind = str(el.get('mask') or 'none').strip().lower()
    if mask_kind not in MASKS:
        mask_kind = 'none'

    overlay = _load_image_source(src)
    overlay = _apply_crop(overlay, el.get('crop'))
    overlay = _apply_adjust(overlay, el.get('adjust'))

    if fit == 'cover':
        overlay = ImageOps.fit(overlay, (w, h), method=Image.LANCZOS)
        ox, oy = x, y
    elif fit == 'stretch':
        overlay = overlay.resize((w, h), Image.LANCZOS)
        ox, oy = x, y
    else:
        overlay = ImageOps.contain(overlay, (w, h), method=Image.LANCZOS)
        ox = x + (w - overlay.width) // 2
        oy = y + (h - overlay.height) // 2

    ow, oh = overlay.size
    if mask_kind == 'circle':
        scale = _SUPERSAMPLE if ow * oh * _SUPERSAMPLE ** 2 <= _SUPERSAMPLE_MAX_PIXELS else 1
        m = Image.new('L', (ow * scale, oh * scale), 0)
        ImageDraw.Draw(m).ellipse((0, 0, ow * scale - 1, oh * scale - 1), fill=255)
        if scale > 1:
            m = m.resize((ow, oh), Image.LANCZOS)
        _mask_layer(overlay, m)
    elif mask_kind == 'rounded':
        r = radius if radius > 0 else int(round(min(ow, oh) * 0.12))
        _mask_layer(overlay, _rounded_mask((ow, oh), r))
    elif radius > 0:
        _mask_layer(overlay, _rounded_mask((ow, oh), radius))

    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    layer.paste(overlay, (ox, oy), overlay)
    return layer


# ═══════════════════════════════════════════════════════════════════════════════
# Rendern – cover (Schrift auf fertigen Memes abdecken)
# ═══════════════════════════════════════════════════════════════════════════════

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
        return None
    box = (x0, y0, x1, y1)
    feather = max(0.0, _num(el.get('feather'), 0))
    fill = el.get('fill', 'auto')
    fill = 'auto' if fill in (None, '') else fill

    mask = Image.new('L', img.size, 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(feather))

    source = None
    if isinstance(fill, str) and fill.strip().lower() == 'inpaint':
        source = _inpaint(img, box)
        if source is None:
            log.info('cover inpaint: cv2/numpy nicht verfügbar, nutze fill=auto')
            fill = 'auto'
    if source is None:
        if isinstance(fill, str) and fill.strip().lower() == 'auto':
            source = Image.new('RGBA', img.size, _median_edge_color(img, box))
        else:
            source = fill_flaeche(img.size, fill, brand)
            if source is None:
                source = Image.new('RGBA', img.size, _median_edge_color(img, box))
    source = source.convert('RGBA')
    _mask_layer(source, mask)
    return source


# ═══════════════════════════════════════════════════════════════════════════════
# Rendern – Formen
# ═══════════════════════════════════════════════════════════════════════════════

def _star_points(cx, cy, rx, ry, points, inner):
    pts = []
    n = max(3, int(points))
    for i in range(2 * n):
        r = 1.0 if i % 2 == 0 else max(0.01, min(1.0, inner))
        ang = -math.pi / 2 + i * math.pi / n
        pts.append((cx + rx * r * math.cos(ang), cy + ry * r * math.sin(ang)))
    return pts


def _round_rect_path(x0, y0, x1, y1, r, steps=10):
    """Abgerundetes Rechteck als Punktliste (im Uhrzeigersinn) plus Einfügestellen je Kante.
    Liefert (punkte, slots) – slots = {'top': idx, 'right': idx, 'bottom': idx, 'left': idx};
    an idx können zusätzliche Punkte in Laufrichtung eingefügt werden."""
    r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    pts, slots = [], {}

    def arc(cx, cy, a0, a1):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    if r > 0:
        arc(x0 + r, y0 + r, 180, 270)            # oben links
    else:
        pts.append((x0, y0))
    slots['top'] = len(pts)
    if r > 0:
        arc(x1 - r, y0 + r, 270, 360)            # oben rechts
    else:
        pts.append((x1, y0))
    slots['right'] = len(pts)
    if r > 0:
        arc(x1 - r, y1 - r, 0, 90)               # unten rechts
    else:
        pts.append((x1, y1))
    slots['bottom'] = len(pts)
    if r > 0:
        arc(x0 + r, y1 - r, 90, 180)             # unten links
    else:
        pts.append((x0, y1))
    slots['left'] = len(pts)
    return pts, slots


def _bubble_path(w, h, r, tail):
    """Sprechblase als EIN zusammenhängender Polygonzug: abgerundetes Rechteck + Spitze."""
    tx, ty = tail
    pts, slots = _round_rect_path(0.0, 0.0, float(w), float(h), r)
    # nächste Kante bestimmen
    over = {'top': -ty, 'bottom': ty - h, 'left': -tx, 'right': tx - w}
    side = max(over, key=lambda k: over[k])
    if over[side] <= 0:                                  # Spitze liegt im Körper -> unten
        side = 'bottom'
    bw = max(6.0, min(w, h) * 0.12)
    if side in ('top', 'bottom'):
        edge_y = 0.0 if side == 'top' else float(h)
        cxp = _clamp(tx, r + bw + 2, w - r - bw - 2) if w > 2 * (r + bw + 2) else w / 2.0
        b1 = (cxp - bw, edge_y)
        b2 = (cxp + bw, edge_y)
        ins = [b1, (tx, ty), b2] if side == 'top' else [b2, (tx, ty), b1]
    else:
        edge_x = 0.0 if side == 'left' else float(w)
        cyp = _clamp(ty, r + bw + 2, h - r - bw - 2) if h > 2 * (r + bw + 2) else h / 2.0
        b1 = (edge_x, cyp - bw)
        b2 = (edge_x, cyp + bw)
        ins = [b1, (tx, ty), b2] if side == 'right' else [b2, (tx, ty), b1]
    idx = slots[side]
    return pts[:idx] + ins + pts[idx:]


def _render_shape(img, el, values, brand):
    """Formen mit Kantenglättung: auf vierfach vergrößerter Maske zeichnen, dann LANCZOS."""
    el_type = str(el.get('type') or 'shape').strip().lower()
    shape = str(el.get('shape') or 'rect').strip().lower()
    if el_type == 'rect' and not el.get('shape'):
        shape = 'rect'
    if shape not in SHAPES:
        log.warning('Unbekannte Form %r, nutze rect', el.get('shape'))
        shape = 'rect'

    x, y = _num(el.get('x'), 0), _num(el.get('y'), 0)
    w = max(1, _int(el.get('width'), 1))
    h = max(1, _int(el.get('height'), 1))
    fill_spec = el.get('fill')
    stroke_spec = el.get('stroke')
    stroke_w = max(0.0, _num(el.get('stroke_width'), 0))
    if parse_paint(stroke_spec, brand) is None:
        stroke_w = 0.0
    if shape == 'line':
        if parse_paint(stroke_spec, brand) is None:
            stroke_spec, stroke_w = fill_spec, (stroke_w or max(2.0, _num(el.get('stroke_width'), 4)))
        if stroke_w <= 0:
            stroke_w = 4.0
        fill_spec = None
    if parse_paint(fill_spec, brand) is None and stroke_w <= 0:
        return None

    # lokaler Rahmen: Elementbox plus Rand für die Kontur, bei bubble auch die Spitze
    margin = stroke_w / 2.0 + 2.0
    fx0, fy0, fx1, fy1 = -margin, -margin, w + margin, h + margin
    tail = None
    if shape == 'bubble':
        t = el.get('tail')
        if isinstance(t, dict) and _is_num(t.get('x')) and _is_num(t.get('y')):
            tail = (_num(t['x']) - x, _num(t['y']) - y)     # tail ist in Leinwand-Pixeln
        else:
            tail = (w * 0.25, h * 1.25)
        fx0, fy0 = min(fx0, tail[0] - margin), min(fy0, tail[1] - margin)
        fx1, fy1 = max(fx1, tail[0] + margin), max(fy1, tail[1] + margin)
    fw = max(1, int(math.ceil(fx1 - fx0)))
    fh = max(1, int(math.ceil(fy1 - fy0)))

    scale = _SUPERSAMPLE if fw * fh * _SUPERSAMPLE ** 2 <= _SUPERSAMPLE_MAX_PIXELS else 1
    SW, SH = fw * scale, fh * scale

    def P(p):
        return ((p[0] - fx0) * scale, (p[1] - fy0) * scale)

    fill_mask = Image.new('L', (SW, SH), 0)
    stroke_mask = Image.new('L', (SW, SH), 0)
    fd = ImageDraw.Draw(fill_mask)
    sd = ImageDraw.Draw(stroke_mask)
    sw_s = max(1, int(round(stroke_w * scale)))

    if shape == 'rect':
        radius = max(0.0, _num(el.get('radius'), 0)) * scale
        p0, p1 = P((0, 0)), P((w, h))
        rect = (p0[0], p0[1], p1[0] - 1, p1[1] - 1)
        fd.rounded_rectangle(rect, radius=radius, fill=255)
        if stroke_w > 0:
            sd.rounded_rectangle(rect, radius=radius, outline=255, width=sw_s)
    elif shape == 'ellipse':
        p0, p1 = P((0, 0)), P((w, h))
        box = (p0[0], p0[1], p1[0] - 1, p1[1] - 1)
        fd.ellipse(box, fill=255)
        if stroke_w > 0:
            sd.ellipse(box, outline=255, width=sw_s)
    elif shape == 'line':
        a, b = P((0, 0)), P((w, h))
        sd.line([a, b], fill=255, width=sw_s)
        rr = sw_s / 2.0
        for p in (a, b):
            sd.ellipse((p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr), fill=255)
    else:
        if shape == 'triangle':
            pts = [(w / 2.0, 0.0), (float(w), float(h)), (0.0, float(h))]
        elif shape == 'star':
            pts = _star_points(w / 2.0, h / 2.0, w / 2.0, h / 2.0,
                               _int(el.get('points'), 5) or 5,
                               _num(el.get('inner'), 0.5) or 0.5)
        elif shape == 'arrow':
            head = min(w * 0.45, h * 0.9)
            thick = stroke_w if (stroke_w and not el.get('fill')) else h * 0.4
            thick = _clamp(thick, 2.0, h)
            ty0, ty1 = (h - thick) / 2.0, (h + thick) / 2.0
            sx = max(0.0, w - head)
            pts = [(0.0, ty0), (sx, ty0), (sx, 0.0), (float(w), h / 2.0),
                   (sx, float(h)), (sx, ty1), (0.0, ty1)]
        else:                                    # bubble
            radius = _num(el.get('radius'), min(w, h) * 0.18)
            pts = _bubble_path(w, h, radius, tail)
        spts = [P(p) for p in pts]
        fd.polygon(spts, fill=255)
        if stroke_w > 0:
            sd.line(spts + [spts[0]], fill=255, width=sw_s, joint='curve')

    if scale > 1:
        fill_mask = fill_mask.resize((fw, fh), Image.LANCZOS)
        stroke_mask = stroke_mask.resize((fw, fh), Image.LANCZOS)

    patch = Image.new('RGBA', (fw, fh), (0, 0, 0, 0))
    fill_img = fill_flaeche((fw, fh), fill_spec, brand)
    if fill_img is not None:
        _mask_layer(fill_img, fill_mask)
        patch = Image.alpha_composite(patch, fill_img)
    if stroke_w > 0:
        stroke_img = fill_flaeche((fw, fh), stroke_spec, brand)
        if stroke_img is not None:
            _mask_layer(stroke_img, stroke_mask)
            patch = Image.alpha_composite(patch, stroke_img)

    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    return _blit(layer, patch, x + fx0, y + fy0)


_HANDLERS = {
    'text': _render_text,
    'image': _render_image,
    'cover': _render_cover,
    'shape': _render_shape,
    'rect': _render_shape,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Rendern – Hauptlauf
# ═══════════════════════════════════════════════════════════════════════════════

def _canvas_config(config):
    c = config.get('canvas')
    return c if isinstance(c, dict) else {}


def _canvas_size(canvas, bg_img):
    if _is_num(canvas.get('width')) and _is_num(canvas.get('height')):
        cw, ch = _int(canvas['width']), _int(canvas['height'])
        if cw > 0 and ch > 0:
            return (cw, ch)
    if bg_img is not None:
        return bg_img.size
    return DEFAULT_CANVAS


def render(bg_path, config, values, brand=None):
    """Rendert Grundfläche + Hintergrundbild + Elemente und liefert PNG-Bytes (RGB).

    bg_path: Pfad (oder Bytes/Dateiobjekt/PIL-Image) des Hintergrunds. None/'' ist erlaubt –
             dann trägt canvas.background allein (Bauen von Null). Ist ein Hintergrund
             angegeben, aber nicht lesbar, und es gibt KEIN canvas.background, wird wie
             bisher FileNotFoundError geworfen (der einzige Fehler, der nach außen geht).
    config:  dict (oder JSON-Text) mit 'elements' und optional 'canvas'.
    values:  {var: Wert}; Nicht-Strings werden mit str() gewandelt.
    brand:   das alte {'bg','text','accent','font'} ODER ein vollständiges Stil-Dict
             (CityStyle.to_dict() bzw. stil_dict()). Beides wird verstanden: 'brand:*' und
             font 'brand' laufen unverändert, zusätzlich gibt es 'stil:*' und die Rollen.
    Fehler einzelner Elemente werden geloggt und übersprungen."""
    try:
        config = _coerce_config(config) or {}
    except Exception as ex:
        log.warning('Config nicht lesbar (%s), rendere nur den Hintergrund', ex)
        config = {}
    if not isinstance(config, dict):
        config = {}
    values = {str(k): v for k, v in (values or {}).items()}
    brand = dict(brand) if brand else None
    canvas = _canvas_config(config)
    bg_paint = canvas.get('background')
    has_bg_paint = parse_paint(bg_paint, brand) is not None

    bg_img = None
    if bg_path not in (None, '', False):
        try:
            bg_img = _open_background(bg_path)
        except FileNotFoundError:
            if not has_bg_paint:
                raise
            log.warning('Hintergrundbild fehlt, zeichne auf canvas.background: %r', bg_path)

    size = _canvas_size(canvas, bg_img)
    base = Image.new('RGBA', size, (255, 255, 255, 255))
    if has_bg_paint:
        fill_img = fill_flaeche(size, bg_paint, brand)
        if fill_img is not None:
            base = Image.alpha_composite(base, fill_img)
    elif bg_img is None:
        log.warning('Weder Hintergrundbild noch canvas.background – nutze Weiß (%dx%d)', *size)
    if bg_paint not in (None, '', False) and not has_bg_paint:
        log.warning('canvas.background %r unbrauchbar, nutze Weiß', bg_paint)

    if bg_img is not None:
        if bg_img.size != size:
            bg_img = ImageOps.fit(bg_img, size, method=Image.LANCZOS)
        base = Image.alpha_composite(base, bg_img)
    img = base

    elements = config.get('elements') or []
    if not isinstance(elements, list):
        elements = []
    text_versucht = text_gezeichnet = 0     # nur für den Sammelhinweis am Ende
    for i, el in enumerate(elements):
        if not isinstance(el, dict):
            log.warning('Element %d ist kein Objekt, übersprungen', i + 1)
            continue
        # Rolle zuerst: sie kann Schrift, Größen, Farbe, Kasten, Kontur, Schatten und
        # opacity setzen. Ohne 'role' kommt dasselbe Objekt zurück (bitgleiche Altlast).
        el = rolle_anwenden(el, brand, size[1])
        if _bool(el.get('hidden')):
            continue
        opacity = _clamp(_num(el.get('opacity'), 1.0), 0.0, 1.0)
        if opacity <= 0:
            continue
        only_if, skip_if = el.get('only_if'), el.get('skip_if')
        if only_if and not _has_value(values, only_if):
            continue
        if skip_if and _has_value(values, skip_if):
            continue
        el_type = str(el.get('type') or 'text').lower()
        handler = _HANDLERS.get(el_type)
        el_id = el.get('id') or el.get('var') or f'el{i + 1}'
        if handler is None:
            log.warning("Element %s: unbekannter type '%s', übersprungen", el_id, el_type)
            continue
        if el_type == 'text':
            text_versucht += 1
        elif _enthaelt_auto(el):
            # Bei Text entscheidet _render_text selbst (es kennt den echten Textblock);
            # alle anderen Typen bekommen den Mittelwert ihres Elementrechtecks.
            el = _auto_ersetzen(el, auto_farbe(_bereich_mittel(img, _el_box(el, size))))
        try:
            result = handler(img, el, values, brand)
            if isinstance(result, tuple):
                layer, center = result
            else:
                layer, center = result, None
            if layer is None:
                continue
            if el_type == 'text':
                text_gezeichnet += 1
            if opacity < 1:
                layer = _apply_opacity(layer, opacity)
            rot = _num(el.get('rotation'), 0.0)
            flip_h, flip_v = _bool(el.get('flip_h')), _bool(el.get('flip_v'))
            if rot or flip_h or flip_v:
                if center is None:
                    center = (_num(el.get('x'), 0) + _num(el.get('width'), 0) / 2.0,
                              _num(el.get('y'), 0) + _num(el.get('height'), 0) / 2.0)
                layer = _transform_layer(layer, center[0], center[1], rot, flip_h, flip_v)
            img = _composite(img, layer, el.get('blend'))
        except Exception as ex:
            log.warning('Element %s (%s) fehlgeschlagen: %s', el_id, el_type, ex)

    if text_versucht and not text_gezeichnet:
        # Genau der Fall „Bild sieht aus wie der reine Hintergrund“: jede Textzeile ist an
        # einer fehlenden Variablen gescheitert. Ausgabe bleibt wie bisher – aber sichtbar.
        log.warning('Rendern ohne jeden Text: alle %d Text-Elemente ohne Wert '
                    '(vorhandene Variablen: %s)', text_versucht,
                    ', '.join(sorted(k for k, v in values.items()
                                     if v is not None and str(v).strip())) or 'keine')

    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    return buf.getvalue()
