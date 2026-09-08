"""Bild-Import mit KI-Erkennung (Phase B6).

Ein fertiges Meme (PNG/JPG/WebP, z. B. schon mit "Darmstadt" im Text) wird hochgeladen und
als PIL-Template angelegt. Optional erkennt Claude die Textzeilen im Bild; jede stadtspezifische
Zeile wird zu einem cover-Element (deckt den alten Text ab) plus einem text-Element mit
Variable. Die Feinjustierung passiert im Studio (/studio/<id>).

Nachbau (H2): Statt die fremde Schrift nur zu überdecken, wird der Hintergrund BESORGT
(Katalog → eigene Bibliothek → aus dem Bild freigeräumt) und das Meme darauf neu aufgebaut.
Das Ergebnis geht immer als Gegenüberstellung an den Nutzer; nichts wird still übernommen.

Ehrliche Grenzen (gelten überall, auch in der Oberfläche so benannt):
  - Es gibt hier kein Bildmodell, das fehlende Bildteile neu malt. Ein sauberer Hintergrund
    kommt aus dem Katalog, aus der eigenen Bibliothek oder wird freigeräumt — sonst gar nicht.
  - Schriften werden auf die vorhandene Bibliothek abgebildet, nicht nachgebaut.
  - Positionen, Farben und Größen sind Schätzungen der KI. Der Nachbau ist ein Vorschlag.

Routen (Login):
  POST /api/templates/import/image           multipart: file, name, category, analyze, city_hint
  POST /api/templates/<id>/analyze           JSON: city_hint, force
  GET  /api/templates/import/image/help      Anleitungstext
  POST /api/templates/nachbau                multipart: file, name, category, city_hint, dry_run
  POST /api/templates/nachbau/<id>/uebernehmen   JSON: config
  GET  /api/templates/nachbau/katalog        Katalogstand
  POST /api/templates/nachbau/katalog/aktualisieren

Regeln:
  - app.py wird NIE auf Modulebene importiert (zirkulärer Import); Zugriff nur über _appmod().
  - meme_katalog (Bereich H1) wird defensiv geladen: fehlt es, entfällt nur der Katalogweg.
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
import shutil
import logging
import tempfile
import importlib
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from models import db, MemeTemplate, KNOWLEDGE_CATEGORIES

log = logging.getLogger(__name__)
bp = Blueprint('vision_import', __name__)

# Gleiches Modell wie die übrigen Claude-Aufrufe in app.py; per Env übersteuerbar.
VISION_MODEL = os.getenv('MEMEOS_VISION_MODEL') or 'claude-haiku-4-5-20251001'
MAX_TOKENS = 2500          # tiefere Analyse (Format, Aufbau, Regionen, Palette) braucht Platz;
                           # stop_reason 'max_tokens' wird geprüft und als Fehler gemeldet
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

# ── Nachbau (H2) ──────────────────────────────────────────────────────────────
KATALOG_GUETE_MIN = 0.55       # ab hier gilt ein Katalogtreffer als brauchbar (= meme_katalog.GUETE_SICHER)
FREI_FEATHER = 3               # weiche Kante beim Freiräumen; der Kern bleibt exakt in der Füllfarbe
FREI_GROW = COVER_GROW
FREI_PAD_PX = COVER_PAD_PX
FOTO_REGION_MIN = 0.15         # Fotoregionen ab 15 % der Fläche werden als Bildplatz vorgeschlagen
VORSCHAU_MAX_EDGE = 760        # Vergleichsbilder klein halten (JSON-Antwort)
VORSCHAU_QUALITY = 82
GROESSE_REL_MIN = 0.01
GROESSE_REL_MAX = 0.40
MIN_SIZE_ANTEIL = 0.45         # min_size = 45 % von max_size

REGION_ARTEN = ('foto', 'flaeche', 'comic', 'screenshot')
SCHRIFT_ARTEN = ('impact', 'sans', 'serif', 'handschrift', 'unbekannt')
# Abbildung auf die vorhandene Bibliothek (memeos_render._BUILTIN_FONTS). Mehr gibt es nicht:
# eine fremde Schrift wird NICHT nachgebaut, sondern ersetzt — bei serif/handschrift mit Warnung.
SCHRIFT_MAP = {'impact': 'anton', 'unbekannt': 'anton', 'sans': 'bold',
               'serif': 'bold', 'handschrift': 'bold'}
SCHRIFT_ERSATZ = {
    'serif': 'Serifenschrift durch Oswald Bold ersetzt (keine Serifenschrift in der Bibliothek)',
    'handschrift': 'Handschrift durch Oswald Bold ersetzt (keine Handschrift in der Bibliothek)',
}
HINT_NACHBAU_KEY = ('Ohne gültigen Anthropic-Key ist der Nachbau nicht möglich: '
                    'die Analyse des fremden Memes läuft über die KI')

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


def _katalog():
    """meme_katalog (Bereich H1) laden, wenn es da ist. Fehlt es, entfällt NUR der Katalogweg;
    Analyse, Freiräumen und Nachbau laufen weiter. Deshalb try/except statt Modulimport."""
    try:
        import meme_katalog
        return meme_katalog
    except ImportError as ex:
        log.info(f'vision_import: meme_katalog nicht vorhanden ({ex}) – Katalogweg entfällt')
        return None
    except Exception as ex:      # pragma: no cover – Modul da, aber kaputt
        log.warning(f'vision_import: meme_katalog nicht ladbar: {ex}')
        return None


def _render_mod():
    """memeos_render laden (Vorschau, validate_config). None nur, wenn das Modul fehlt."""
    try:
        import memeos_render
        return memeos_render
    except Exception as ex:      # pragma: no cover
        log.warning(f'vision_import: memeos_render nicht ladbar: {ex}')
        return None


def _key_problem():
    """None, wenn ein KI-Aufruf erlaubt und aussichtsreich ist; sonst der Text fürs Dashboard.

    Prüft die FORM des Schlüssels über app._ai_key_plausibel (ein Platzhalter aus .env.example
    bestand die alte Prüfung bool(key) und lief in jedem Auftrag in ein 401) und danach die
    Budgetbremse. Beides ohne Netzaufruf."""
    key = _api_key()
    appmod = _appmod()
    pruef = getattr(appmod, '_ai_key_plausibel', None) if appmod else None
    if pruef is not None:
        try:
            if not pruef(key):
                return ('Kein gültiger Anthropic-Key hinterlegt '
                        '(erwartet wird ein Schlüssel, der mit sk-ant- beginnt)')
        except Exception as ex:  # pragma: no cover
            log.warning(f'vision_import: _ai_key_plausibel fehlgeschlagen: {ex}')
            if not key:
                return 'Kein Anthropic-Key hinterlegt'
    elif not key:
        return 'Kein Anthropic-Key hinterlegt'
    bremse = getattr(appmod, '_ai_budget_gesperrt', None) if appmod else None
    if bremse is not None:
        try:
            gesperrt = bremse()
            if gesperrt:
                return str(gesperrt)
        except Exception as ex:  # pragma: no cover
            log.warning(f'vision_import: _ai_budget_gesperrt fehlgeschlagen: {ex}')
    return None


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
- "kontur": true, wenn der Text eine Umrandung in einer anderen Farbe hat (typisch weiße Schrift
  mit schwarzem Rand), sonst false
- "schatten": true, wenn der Text einen Schlagschatten wirft, sonst false
- "kasten": {{"vorhanden": true|false, "farbe": "#rrggbb"}} — liegt direkt hinter dem Text eine
  gefüllte Fläche (Balken, Sprechblasenfüllung, weißer Streifen)? Dann die Farbe dieser Fläche.
  Ohne Fläche: {{"vorhanden": false, "farbe": null}}
- "schriftart": "impact" (fette Meme-Versalien), "sans" (normale serifenlose Schrift),
  "serif", "handschrift" oder "unbekannt"
- "groesse_rel": Höhe der Großbuchstaben als Anteil der BILDHÖHE, Zahl zwischen 0 und 1
  (Beispiel: Schrift ist ein Zwanzigstel des Bildes hoch → 0.05)
- "reason": kurze Begründung, höchstens 60 Zeichen

Zusätzlich auf oberster Ebene:
- "background_kind": "flat" (einfarbige Fläche hinter dem Text), "photo" (Foto oder Bild
  direkt hinter dem Text) oder "mixed" (teils Fläche, teils Foto)
- "format": {{"name": "...", "sicherheit": 0.0-1.0}} — ist das ein bekanntes Meme-Format?
  Dann den geläufigen Namen nennen (zum Beispiel "Drake Hotline Bling", "Two Buttons",
  "Distracted Boyfriend", "Bernie Sanders", "UNO Draw 25 Cards", "Left Exit 12 Off Ramp",
  "Expanding Brain", "Change My Mind", "Woman Yelling at a Cat"). Erkennst du kein bekanntes
  Format: {{"name": null, "sicherheit": 0}}
- "aufbau": {{"beschreibung": "ein Satz, wie das Bild aufgebaut ist",
  "regionen": [{{"art": "foto"|"flaeche"|"comic"|"screenshot",
  "box": {{"x": 0, "y": 0, "w": 0, "h": 0}} (Promille wie oben), "beschreibung": "kurz"}}]}}
  — die sichtbaren Bereiche des Bildes, also wo ein Foto steht, wo eine einfarbige Fläche,
  wo eine Zeichnung, wo ein Bildschirmfoto. Höchstens sechs Regionen.
- "palette": bis zu fünf dominante Farben des Bildes als "#rrggbb"
{hint_block}
Antworte NUR mit JSON, ohne Erklärung davor oder danach:
{{"text_blocks": [{{"text": "...", "box": {{"x": 0, "y": 0, "w": 0, "h": 0}}, "city_specific": false,
"suggested_var": null, "style": {{"color": "#FFFFFF", "uppercase": true, "align": "center", "weight": "bold"}},
"kontur": true, "schatten": false, "kasten": {{"vorhanden": false, "farbe": null}},
"schriftart": "impact", "groesse_rel": 0.06, "reason": "..."}}],
"background_kind": "flat", "format": {{"name": null, "sicherheit": 0}},
"aufbau": {{"beschreibung": "...", "regionen": [{{"art": "foto", "box": {{"x": 0, "y": 0, "w": 1000, "h": 500}},
"beschreibung": "..."}}]}}, "palette": ["#000000"]}}"""


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


def _log_usage(in_tok, out_tok, feature='vision_import'):
    appmod = _appmod()
    fn = getattr(appmod, '_log_ai_usage', None) if appmod else None
    if not fn:
        return
    try:
        fn(feature, VISION_MODEL, in_tok, out_tok)
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


def _bool_feld(v, default=False):
    """Wahrheitswert aus der KI-Antwort. Auch "ja"/"true"/1 zählen; Unsinn ergibt default."""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ('1', 'true', 'ja', 'yes', 'on'):
        return True
    if s in ('0', 'false', 'nein', 'no', 'off', 'null', 'none', ''):
        return False
    return default


def _anteil(v, lo=0.0, hi=1.0):
    """Zahl zwischen lo und hi oder None. Prozentangaben (5 statt 0.05) werden umgerechnet:
    die KI liefert groesse_rel gelegentlich als Prozent, obwohl 0..1 verlangt ist."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float('inf'), float('-inf')):
        return None
    if f > 1.0 and f <= 100.0:
        f = f / 100.0
    return max(lo, min(hi, f))


def _promille_box(raw):
    """Rohe Box → {'x','y','w','h'} in Promille oder None."""
    if not isinstance(raw, dict):
        return None
    x, y = _promille(raw.get('x')), _promille(raw.get('y'))
    w, h = _promille(raw.get('w')), _promille(raw.get('h'))
    if None in (x, y, w, h) or w <= 0 or h <= 0:
        return None
    return {'x': x, 'y': y, 'w': max(1, min(w, 1000 - x)), 'h': max(1, min(h, 1000 - y))}


def _normalize_format(raw):
    """{'name': str|None, 'sicherheit': 0..1}. Ein Name ohne Bild-Beleg ist nur ein Hinweis;
    ob es wirklich dieses Format ist, entscheidet der Bildvergleich im Katalog."""
    if not isinstance(raw, dict):
        raw = {}
    name = str(raw.get('name') or '').strip()
    if name.lower() in ('', 'null', 'none', 'unbekannt', 'unknown', 'kein', 'keins'):
        name = None
    sicher = _anteil(raw.get('sicherheit'), 0.0, 1.0)
    return {'name': name[:120] if name else None,
            'sicherheit': round(sicher, 3) if sicher is not None else 0.0}


def _normalize_aufbau(raw):
    """{'beschreibung': str, 'regionen': [{'art','box','beschreibung'}]} — höchstens 8 Regionen."""
    if not isinstance(raw, dict):
        raw = {}
    regionen = []
    for r in (raw.get('regionen') if isinstance(raw.get('regionen'), list) else [])[:8]:
        if not isinstance(r, dict):
            continue
        box = _promille_box(r.get('box'))
        if box is None:
            continue
        art = str(r.get('art') or '').strip().lower()
        regionen.append({
            'art': art if art in REGION_ARTEN else 'flaeche',
            'box': box,
            'beschreibung': str(r.get('beschreibung') or '')[:160],
            'anteil': round(box['w'] * box['h'] / 1_000_000.0, 4),
        })
    return {'beschreibung': str(raw.get('beschreibung') or '')[:400], 'regionen': regionen}


def _normalize_palette(raw):
    farben = []
    if isinstance(raw, list):
        for c in raw[:12]:
            hexwert = _hex_color(c, None)
            if hexwert and hexwert not in farben:
                farben.append(hexwert)
    return farben[:5]


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
        kasten_raw = rb.get('kasten') if isinstance(rb.get('kasten'), dict) else {}
        kasten_farbe = _hex_color(kasten_raw.get('farbe'), None)
        # Ein Kasten ohne erkennbare Farbe nützt nichts: dann lieber kein Kasten als ein
        # geratener Balken über dem Bild.
        kasten_da = _bool_feld(kasten_raw.get('vorhanden')) and bool(kasten_farbe)
        art = str(rb.get('schriftart') or '').strip().lower()
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
            'kontur': _bool_feld(rb.get('kontur')),
            'schatten': _bool_feld(rb.get('schatten')),
            'kasten': {'vorhanden': kasten_da, 'farbe': kasten_farbe if kasten_da else None},
            'schriftart': art if art in SCHRIFT_ARTEN else 'unbekannt',
            # None heißt: nicht geliefert. Der Nachbau nimmt dann die Boxhöhe als Maß.
            'groesse_rel': _anteil(rb.get('groesse_rel'), GROESSE_REL_MIN, GROESSE_REL_MAX),
            'reason': str(rb.get('reason') or '')[:120],
        })
    bg = str(data.get('background_kind') or 'flat').lower()
    return {
        'text_blocks': blocks,
        'background_kind': bg if bg in ('flat', 'photo', 'mixed') else 'flat',
        'format': _normalize_format(data.get('format')),
        'aufbau': _normalize_aufbau(data.get('aufbau')),
        'palette': _normalize_palette(data.get('palette')),
    }


def analyze_image(path, city_hint=None, feature='vision_import'):
    """Bild an Claude schicken. Rückgabe normalisierte Analyse oder {'error': ...}. Wirft nie.

    feature landet in der Kostenerfassung (_log_ai_usage), damit der Nachbau in der
    KI-Kosten-Seite getrennt von der einfachen Bild-Analyse sichtbar ist."""
    if not _api_key():
        return {'error': HINT_NO_KEY}
    problem = _key_problem()
    if problem:
        # Kein Netzaufruf: unplausibler Schlüssel läuft in ein 401, erreichtes Budget kostet Geld.
        return {'error': problem}
    try:
        b64, media_type, _size = _prepare_image_for_ai(path)
    except Exception as ex:
        return {'error': f'Bild nicht lesbar: {type(ex).__name__}: {str(ex)[:120]}'}
    try:
        raw, in_tok, out_tok, stop_reason = _call_claude_vision(b64, media_type, build_prompt(city_hint))
    except Exception as ex:
        log.error(f'vision_import: Claude-Aufruf fehlgeschlagen: {ex}')
        return {'error': f'KI nicht erreichbar: {type(ex).__name__}: {str(ex)[:120]}'}
    _log_usage(in_tok, out_tok, feature)
    if stop_reason == 'max_tokens':
        return {'error': f'KI-Antwort abgeschnitten (max_tokens {MAX_TOKENS} erreicht, '
                         f'zu viele Textzeilen?)'}
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
        'format': (analysis or {}).get('format') or {'name': None, 'sicherheit': 0.0},
        'aufbau': (analysis or {}).get('aufbau') or {'beschreibung': '', 'regionen': []},
        'palette': (analysis or {}).get('palette') or [],
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
# Nachbau (H2) – Hintergrund besorgen
#
# Drei Wege, in dieser Reihenfolge:
#   a) Katalog      – bekanntes Meme-Format, Originalvorlage OHNE Schrift
#   b) Bibliothek   – dasselbe, aber aus dem eigenen Bestand
#   c) Freigeräumt  – die Schrift wird aus dem hochgeladenen Bild entfernt
# Bei a) und b) ist der Hintergrund schriftfrei, deshalb braucht der Nachbau dort KEINE
# cover-Elemente. Bei c) bleiben Reste möglich – dafür liegen die cover-Elemente darüber.
# ═══════════════════════════════════════════════════════════════════════════════

def _als_pil(quelle):
    """PIL-Bild, Pfad, Bytes oder Datei-Objekt → geladenes RGB-Bild (eigene Kopie)."""
    if isinstance(quelle, Image.Image):
        return quelle.convert('RGB')
    if isinstance(quelle, (bytes, bytearray)):
        with Image.open(io.BytesIO(bytes(quelle))) as im:
            return im.convert('RGB')
    with Image.open(quelle) as im:
        return im.convert('RGB')


def _hex_zu_rgb(hexwert, default=(0, 0, 0)):
    m = _HEX_RE.match(str(hexwert or '').strip())
    if not m:
        return default
    s = m.group(1)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _kontrastfarbe(hexwert):
    """Schwarz zu heller Schrift, Weiß zu dunkler – für Kontur und Schatten."""
    r, g, b = _hex_zu_rgb(hexwert, (255, 255, 255))
    return '#000000' if (0.299 * r + 0.587 * g + 0.114 * b) >= 128 else '#FFFFFF'


def _rect(box_px):
    """(x, y, w, h) → (x0, y0, x1, y1)."""
    x, y, w, h = box_px
    return (x, y, x + w, y + h)


def _randfarbe_median(img, rect, ring=6):
    """Medianfarbe eines Rings um die Box. Median statt Mittelwert, damit ein einzelner
    Ausreißer (Kante, Rest der Schrift) die Farbe nicht verzieht."""
    import statistics
    x0, y0, x1, y1 = rect
    breite, hoehe = img.size
    rgb = img.convert('RGB')
    streifen = [
        (x0, max(0, y0 - ring), x1, y0),
        (x0, y1, x1, min(hoehe, y1 + ring)),
        (max(0, x0 - ring), y0, x0, y1),
        (x1, y0, min(breite, x1 + ring), y1),
    ]
    punkte = []
    for sx0, sy0, sx1, sy1 in streifen:
        sx0, sy0 = max(0, int(sx0)), max(0, int(sy0))
        sx1, sy1 = min(breite, int(sx1)), min(hoehe, int(sy1))
        if sx1 <= sx0 or sy1 <= sy0:
            continue
        teil = rgb.crop((sx0, sy0, sx1, sy1))
        if teil.width * teil.height > 4096:      # nur eine Stichprobe, das reicht für den Median
            teil = teil.resize((max(1, min(teil.width, 64)), max(1, min(teil.height, 64))),
                               Image.NEAREST)
        punkte.extend(teil.getdata())
    if not punkte:
        return (0, 0, 0)
    return tuple(int(statistics.median(p[i] for p in punkte)) for i in range(3))


def _inpaint_boxen(img, rects):
    """cv2-Inpainting (TELEA) über ALLEN Boxen in einem Durchgang.
    None, wenn cv2/numpy fehlen – der Aufrufer füllt dann mit der Randfarbe."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    try:
        arr = np.array(img.convert('RGB'))[:, :, ::-1].copy()        # RGB → BGR
        maske = np.zeros(arr.shape[:2], dtype=np.uint8)
        for x0, y0, x1, y1 in rects:
            if x1 > x0 and y1 > y0:
                maske[y0:y1, x0:x1] = 255
        out = cv2.inpaint(arr, maske, 5, cv2.INPAINT_TELEA)
        return Image.fromarray(out[:, :, ::-1].copy())
    except Exception as ex:      # pragma: no cover – cv2 da, aber unglücklich
        log.warning(f'vision_import: Inpainting fehlgeschlagen: {ex}')
        return None


def _fuellen(img, rect, farbe, feather=FREI_FEATHER):
    """Box mit einer Farbe füllen: der Kern bleibt EXAKT in dieser Farbe, nur die Kante
    wird weich, damit kein harter Rechteckrand stehen bleibt."""
    from PIL import ImageChops
    x0, y0, x1, y1 = (int(v) for v in rect)
    if x1 <= x0 or y1 <= y0:
        return img
    schicht = Image.new('RGB', img.size, tuple(farbe))
    maske = Image.new('L', img.size, 0)
    ImageDraw.Draw(maske).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    if feather > 0:
        maske = maske.filter(ImageFilter.GaussianBlur(feather))
        if x1 - 1 - feather > x0 + feather and y1 - 1 - feather > y0 + feather:
            kern = Image.new('L', img.size, 0)
            ImageDraw.Draw(kern).rectangle(
                (x0 + feather, y0 + feather, x1 - 1 - feather, y1 - 1 - feather), fill=255)
            maske = ImageChops.lighter(maske, kern)
    return Image.composite(schicht, img.convert('RGB'), maske)


def freiraeumen(bild, analyse):
    """Alle erkannten Textzeilen aus dem Bild entfernen (nicht nur die stadtspezifischen –
    der Nachbau setzt auch den festen Text selbst wieder darüber).

    Drei Fälle je Zeile, in dieser Güte-Reihenfolge:
      1. Kasten hinter dem Text erkannt → Fläche mit GENAU dieser Farbe füllen (sauberste Lösung)
      2. Hintergrund 'flat'             → Median der Randfarbe rund um die Box
      3. 'photo'/'mixed'                → Inpainting über cv2 (ohne cv2: Randfarbe + Warnung)

    → (PIL-Bild, [warnungen], {'kasten': n, 'flaeche': n, 'inpaint': n})"""
    img = _als_pil(bild)
    w, h = img.size
    blocks = (analyse or {}).get('text_blocks') or []
    bg_art = (analyse or {}).get('background_kind') or 'flat'
    warnungen = []
    zaehler = {'kasten': 0, 'flaeche': 0, 'inpaint': 0}

    kasten, flaeche, inpaint = [], [], []
    for blk in blocks:
        box = blk.get('box') if isinstance(blk.get('box'), dict) else None
        if not box or not all(k in box for k in ('x', 'y', 'w', 'h')):
            continue
        rect = _rect(_px_box(box, w, h, grow=FREI_GROW, pad_px=FREI_PAD_PX))
        k = blk.get('kasten') if isinstance(blk.get('kasten'), dict) else {}
        farbe = _hex_color(k.get('farbe'), None) if k.get('vorhanden') else None
        if farbe:
            kasten.append((rect, farbe))
        elif bg_art == 'flat':
            flaeche.append(rect)
        else:
            inpaint.append(rect)

    if inpaint:
        neu = _inpaint_boxen(img, inpaint)
        if neu is not None:
            img = neu
            zaehler['inpaint'] = len(inpaint)
        else:
            warnungen.append('Ohne cv2/numpy kein Inpainting: die Textstellen wurden mit der '
                             'Randfarbe gefüllt – auf einem Foto sieht man das')
            flaeche.extend(inpaint)
    for rect in flaeche:
        # geschätzte Farbe: weiche Kante, damit kein Rechteck stehen bleibt
        img = _fuellen(img, rect, _randfarbe_median(img, rect))
        zaehler['flaeche'] += 1
    for rect, farbe in kasten:
        # bekannte Farbe: harte Kante. Ein Balken hat im Meme eine Kante – eine weiche
        # Kante sähe aus wie ein Leuchten um die Fläche.
        img = _fuellen(img, rect, _hex_zu_rgb(farbe), feather=0)
        zaehler['kasten'] += 1
    return img, warnungen, zaehler


def _bg_ablegen(img, ordner, vorsilbe='nachbau_bg'):
    """Hintergrund als PNG ablegen → (dateiname, pfad)."""
    os.makedirs(ordner, exist_ok=True)
    ts = int(time.time())
    name = f'{vorsilbe}_{ts}.png'
    n = 1
    while os.path.exists(os.path.join(ordner, name)):
        n += 1
        name = f'{vorsilbe}_{ts}_{n}.png'
    pfad = os.path.join(ordner, name)
    img.convert('RGB').save(pfad, format='PNG', optimize=True)
    return name, pfad


def _katalog_bilddatei(kat, fmt, datenpfad=None):
    """Lokalen Pfad zum Vorlagenbild eines Treffers holen.

    finde_format() liefert die Zeile als dict (to_dict), format_bilddatei() erwartet laut
    Vertrag die Zeile. Deshalb wird erst die DB-Zeile nachgeschlagen und beides versucht;
    zuletzt wird die Datei direkt im formate-Ordner gesucht. Wirft nie."""
    if not isinstance(fmt, dict):
        fmt = {}
    zeile = None
    try:
        from models import MemeFormat
        if fmt.get('id'):
            zeile = db.session.get(MemeFormat, int(fmt['id']))
    except Exception as ex:
        log.info(f'vision_import: Katalogzeile {fmt.get("id")} nicht ladbar ({ex})')
    for kandidat in (zeile, fmt):
        if kandidat is None:
            continue
        try:
            pfad = kat.format_bilddatei(kandidat, datenpfad)
        except Exception as ex:
            log.info(f'vision_import: format_bilddatei({type(kandidat).__name__}) '
                     f'fehlgeschlagen ({type(ex).__name__}: {ex})')
            continue
        if pfad and os.path.exists(pfad):
            return pfad
    name = os.path.basename((fmt.get('filename') or '').strip())
    if name:
        ordner = []
        try:
            ordner.append(kat.formate_ordner(datenpfad))
        except Exception:
            pass
        ordner.append(_upload_dir())
        for d in ordner:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None


def hintergrund_besorgen(bild, analyse, datenpfad=None, ziel_ordner=None):
    """Sauberen Hintergrund für den Nachbau besorgen.

    → {'weg': 'katalog'|'bibliothek'|'freigeraeumt', 'pfad': str, 'treffer': dict|None,
       'hinweis': str, 'dateiname': str, 'warnungen': [str], 'zaehler': {...}}

    ziel_ordner: wohin der erzeugte Hintergrund geschrieben wird (Standard: uploads/).
    Der Katalog wird immer unter datenpfad gesucht – ein Probelauf (dry_run) schreibt in
    einen Wegwerf-Ordner, soll aber trotzdem im echten Katalog nachschlagen dürfen."""
    img = _als_pil(bild)
    w, h = img.size
    ziel = ziel_ordner or _upload_dir()
    warnungen = []
    formatname = ((analyse or {}).get('format') or {}).get('name')

    kat = _katalog()
    bester = None
    if kat is None:
        warnungen.append('Vorlagen-Katalog nicht verfügbar – der Hintergrund wird aus dem '
                         'hochgeladenen Bild freigeräumt')
    else:
        liste = []
        try:
            liste = kat.finde_format(img, name_hinweis=formatname, datenpfad=datenpfad) or []
        except Exception as ex:
            warnungen.append(f'Katalogsuche fehlgeschlagen ({type(ex).__name__}) – '
                             f'der Hintergrund wird freigeräumt')
            log.warning(f'vision_import: finde_format fehlgeschlagen: {ex}')

        def _guete(t):
            try:
                return float((t or {}).get('guete') or 0)
            except (TypeError, ValueError):
                return 0.0

        liste = [t for t in liste if isinstance(t, dict)]
        gut = [t for t in liste if _guete(t) >= KATALOG_GUETE_MIN]
        if gut:
            bester = gut[0]
        elif liste:
            spitze = liste[0]
            warnungen.append(
                f'Bester Katalogtreffer "{(spitze.get("format") or {}).get("name", "?")}" nur '
                f'Güte {spitze.get("guete")} (nötig {KATALOG_GUETE_MIN}) – zu unsicher, '
                f'der Hintergrund wird freigeräumt')

    if bester:
        fmt = bester.get('format') or {}
        pfad_quelle = _katalog_bilddatei(kat, fmt, datenpfad)
        if pfad_quelle:
            try:
                with Image.open(pfad_quelle) as roh:
                    passend = ImageOps.fit(roh.convert('RGB'), (w, h),
                                           method=Image.LANCZOS, centering=(0.5, 0.5))
                dateiname, pfad = _bg_ablegen(passend, ziel)
                weg = 'bibliothek' if (fmt.get('quelle') or '') == 'eigen' else 'katalog'
                quelle_text = ('der eigenen Bibliothek' if weg == 'bibliothek'
                               else 'dem Vorlagen-Katalog')
                return {
                    'weg': weg,
                    'pfad': pfad,
                    'dateiname': dateiname,
                    'treffer': bester,
                    'warnungen': warnungen,
                    'zaehler': {'kasten': 0, 'flaeche': 0, 'inpaint': 0},
                    'hinweis': (
                        f'Originalvorlage aus {quelle_text}: "{fmt.get("name") or "ohne Namen"}" '
                        f'(Güte {bester.get("guete")}, erkannt über {bester.get("grund")}). '
                        f'Dieser Hintergrund ist ohne jede Schrift. Bitte im Vergleich prüfen, '
                        f'ob es wirklich dieselbe Vorlage ist – übernommen wird nichts von allein.'),
                }
            except Exception as ex:
                warnungen.append(f'Katalogbild nicht verwendbar ({type(ex).__name__}) – '
                                 f'der Hintergrund wird freigeräumt')
                log.warning(f'vision_import: Katalogbild {pfad_quelle} unbrauchbar: {ex}')
        else:
            warnungen.append('Passendes Format gefunden, aber das Vorlagenbild ließ sich nicht '
                             'laden (kein Netz?) – der Hintergrund wird freigeräumt')

    frei, frei_warnungen, zaehler = freiraeumen(img, analyse)
    warnungen.extend(frei_warnungen)
    dateiname, pfad = _bg_ablegen(frei, ziel)
    stellen = zaehler['kasten'] + zaehler['flaeche'] + zaehler['inpaint']
    teile = []
    if zaehler['kasten']:
        teile.append(f'{zaehler["kasten"]}× mit der Kastenfarbe gefüllt')
    if zaehler['flaeche']:
        teile.append(f'{zaehler["flaeche"]}× mit der Randfarbe gefüllt')
    if zaehler['inpaint']:
        teile.append(f'{zaehler["inpaint"]}× retuschiert (Inpainting)')
    return {
        'weg': 'freigeraeumt',
        'pfad': pfad,
        'dateiname': dateiname,
        'treffer': None,
        'warnungen': warnungen,
        'zaehler': zaehler,
        'hinweis': (
            f'Kein sicherer Treffer im Katalog. Die Schrift wurde aus dem hochgeladenen Bild '
            f'entfernt ({stellen} Stellen' + (f': {", ".join(teile)}' if teile else '') + '). '
            f'Es wird nichts neu gemalt – Reste der alten Schrift sind möglich, bitte den '
            f'Vergleich prüfen.'),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Nachbau (H2) – Konfiguration bauen
# ═══════════════════════════════════════════════════════════════════════════════

def _schrift_fuer(blk, warnungen, nummer):
    """Erkannte Schriftart auf die Bibliothek abbilden. Nachbilden kann MemeOS eine fremde
    Schrift nicht – serif und handschrift werden ersetzt und das steht in den Warnungen."""
    art = str(blk.get('schriftart') or 'unbekannt').lower()
    if art not in SCHRIFT_ARTEN:
        art = 'unbekannt'
    if art in SCHRIFT_ERSATZ:
        warnungen.append(f'Zeile {nummer}: {SCHRIFT_ERSATZ[art]}')
    if art == 'unbekannt' and (blk.get('style') or {}).get('weight') == 'regular':
        return 'bold'
    return SCHRIFT_MAP.get(art, 'anton')


def nachbau_config(analyse, hintergrund, w, h, warnungen=None):
    """Analyse + besorgter Hintergrund → Template-Konfiguration nach Renderer v3.

    Anders als build_config_from_analysis (die nur die stadtspezifischen Zeilen abdeckt und
    den Rest im Bild stehen lässt) baut diese Funktion das Meme NEU auf: der Hintergrund ist
    schriftfrei, JEDE erkannte Zeile wird als text-Element gesetzt – feste Zeilen mit 'text',
    stadtspezifische mit 'var' und 'fallback'.

    cover-Elemente entstehen NUR beim Weg 'freigeraeumt' (dort kann ein Rest der alten Schrift
    im Bild stehen); beim Katalog- und Bibliotheksweg ist der Hintergrund garantiert leer.

    warnungen: optionale Liste, in die Hinweise angehängt werden (Schriftersatz und so weiter).
    """
    w, h = int(w), int(h)
    warn = warnungen if isinstance(warnungen, list) else []
    analyse = analyse or {}
    weg = (hintergrund or {}).get('weg') or 'freigeraeumt'
    freigeraeumt = (weg == 'freigeraeumt')
    blocks = analyse.get('text_blocks') or []
    bg_art = analyse.get('background_kind') or 'flat'
    foto = bg_art in ('photo', 'mixed')

    elements = []
    for i, blk in enumerate(blocks):
        box = blk.get('box') if isinstance(blk.get('box'), dict) else None
        if not box or not all(k in box for k in ('x', 'y', 'w', 'h')):
            continue
        idx = blk.get('index', i)
        nummer = i + 1
        style = blk.get('style') or {}
        kasten = blk.get('kasten') if isinstance(blk.get('kasten'), dict) else {}
        kasten_farbe = _hex_color(kasten.get('farbe'), None) if kasten.get('vorhanden') else None

        tx, ty, tw, th = _px_box(box, w, h, grow=0.06)
        cx, cy, cw, ch = _px_box(box, w, h, grow=FREI_GROW, pad_px=FREI_PAD_PX)

        rel = blk.get('groesse_rel')
        max_size = max(12, int(round(rel * h))) if rel else max(12, int(round(th * 0.8)))
        min_size = max(10, int(round(max_size * MIN_SIZE_ANTEIL)))
        min_size = min(min_size, max_size)
        farbe = _hex_color(style.get('color'), '#FFFFFF')
        kasten_pad = max(4, int(round(max_size * 0.20))) if kasten_farbe else 0

        if freigeraeumt and kasten_farbe:
            # Kastenfarbe bekannt: cover-Streifen und Textkasten wären zwei gleichfarbige
            # Rechtecke mit verschiedener Geometrie – die sichtbare Fläche ist deren
            # Vereinigung und bekommt an allen vier Ecken Stufen. Deshalb übernimmt der
            # cover-Streifen die ganze Fläche (Textrechteck samt Innenabstand) und das
            # text-Element bekommt weiter unten keinen eigenen Kasten: eine gerade Kante.
            rand_x1 = max(cx + cw, tx + tw + kasten_pad)
            rand_y1 = max(cy + ch, ty + th + kasten_pad)
            cx = max(0, min(cx, tx - kasten_pad))
            cy = max(0, min(cy, ty - kasten_pad))
            cw = max(1, min(w, rand_x1) - cx)
            ch = max(1, min(h, rand_y1) - cy)

        if freigeraeumt:
            # Sicherheitsnetz über dem freigeräumten Bild: wo die Retusche unsauber war,
            # deckt der cover-Streifen den Rest ab, bevor die neue Zeile darüber kommt.
            elements.append({
                'id': f'cover_{idx}', 'type': 'cover',
                'x': cx, 'y': cy, 'width': cw, 'height': ch,
                'fill': kasten_farbe or ('inpaint' if foto else 'auto'),
                # bekannte Kastenfarbe: harte Kante (sonst leuchtet die Fläche);
                # geschätzte oder retuschierte Füllung: weiche Kante
                'feather': 0 if kasten_farbe else COVER_FEATHER,
                'name': f'Abdeckung Zeile {nummer}',
            })

        text_el = {
            'id': f'text_{idx}', 'type': 'text',
            'x': tx, 'y': ty, 'width': tw, 'height': th,
            'font': _schrift_fuer(blk, warn, nummer),
            'max_size': max_size, 'min_size': min_size,
            'color': farbe,
            'align': style.get('align') if style.get('align') in ('left', 'center', 'right') else 'center',
            'valign': 'middle',
            'uppercase': bool(style.get('uppercase', False)),
            'fit': 'shrink',
            'name': f'Zeile {nummer}',
        }
        if blk.get('city_specific'):
            text_el['var'] = blk.get('suggested_var') or f'text_{idx}'
            # fallback = die Originalzeile: liefert die Variable nichts, steht wieder etwas da
            # statt einer leeren Fläche – im Vorrat sofort erkennbar.
            text_el['fallback'] = str(blk.get('text') or '').strip()
        else:
            text_el['text'] = str(blk.get('text') or '').strip()
        if blk.get('kontur'):
            text_el['stroke'] = _kontrastfarbe(farbe)
            text_el['stroke_width'] = max(2, int(round(max_size * 0.06)))
        if blk.get('schatten'):
            versatz = max(1, int(round(max_size * 0.05)))
            text_el['shadow'] = {'dx': versatz, 'dy': versatz,
                                 'blur': max(2, int(round(max_size * 0.06))),
                                 'color': '#000000B3'}
        if kasten_farbe and not freigeraeumt:
            # Beim Weg 'freigeraeumt' zeichnet bereits der cover-Streifen oben die Fläche;
            # ein zweiter Kasten in derselben Farbe gäbe nur eine treppige Kante.
            text_el['box'] = {'fill': kasten_farbe,
                              'padding': kasten_pad,
                              'radius': 6}
        elements.append(text_el)

    config = {'canvas': {'width': w, 'height': h}, 'elements': elements}

    # Fotoregionen als VORSCHLAG mitgeben, nicht automatisch einbauen: ob dort ein Stadtbild
    # hingehört, entscheidet der Nutzer in der Oberfläche.
    vorschlaege = []
    for region in ((analyse.get('aufbau') or {}).get('regionen') or []):
        if region.get('art') != 'foto':
            continue
        box = region.get('box') or {}
        anteil = region.get('anteil')
        if anteil is None:
            anteil = (box.get('w', 0) * box.get('h', 0)) / 1_000_000.0
        if anteil < FOTO_REGION_MIN:
            continue
        x, y, bw, bh = _px_box(box, w, h)
        vorschlaege.append({
            'box': dict(box),
            'box_px': {'x': x, 'y': y, 'w': bw, 'h': bh},
            'anteil': round(anteil, 4),
            'beschreibung': region.get('beschreibung') or '',
        })
    if vorschlaege:
        config['vorschlag_bild'] = vorschlaege
        warn.append(f'{len(vorschlaege)} Fotofläche(n) erkannt – daraus lässt sich im Studio ein '
                    f'Stadtbild-Element machen (Variable bild_<schlüssel>); '
                    f'eingebaut wurde davon nichts.')
    return config


def nachbau_variablen(config):
    """Alle Variablennamen einer Config in Reihenfolge (für required_vars)."""
    namen = []
    for el in (config or {}).get('elements') or []:
        if not isinstance(el, dict):
            continue
        var = str(el.get('var') or '').strip()
        if var and var not in namen:
            namen.append(var)
    return namen


def _klein_b64(bild, max_edge=VORSCHAU_MAX_EDGE, quality=VORSCHAU_QUALITY):
    """Bild verkleinern und als base64-JPEG liefern (klein genug für eine JSON-Antwort)."""
    img = _als_pil(bild)
    skala = min(1.0, float(max_edge) / float(max(img.size)))
    if skala < 1.0:
        img = img.resize((max(1, round(img.width * skala)), max(1, round(img.height * skala))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode('ascii')


def _nachbau_vorschau(bg_pfad, config, blocks):
    """Nachbau mit Platzhalterwerten rendern → (base64-JPEG, fehlertext|None).

    Platzhalter statt echter Städtenamen: der Vergleich soll zeigen, WELCHE Stellen später
    ausgetauscht werden. Feste Zeilen stehen mit ihrem Originaltext da."""
    rend = _render_mod()
    if rend is None:
        return None, 'Renderer nicht verfügbar'
    werte = {}
    for blk in blocks or []:
        var = blk.get('suggested_var')
        if blk.get('city_specific') and var:
            try:
                werte[var] = rend.sample_values_placeholder(var)
            except Exception:
                werte[var] = f'[{var}]'
    try:
        png = rend.render(bg_pfad, config, werte)
    except Exception as ex:
        log.warning(f'vision_import: Vorschau fehlgeschlagen: {ex}')
        return None, f'Vorschau nicht gerendert ({type(ex).__name__}: {str(ex)[:120]})'
    try:
        return _klein_b64(png), None
    except Exception as ex:      # pragma: no cover
        return None, f'Vorschau nicht lesbar ({type(ex).__name__})'


def _config_fehler(config):
    """validate_config, aber ohne harte Abhängigkeit vom Renderer."""
    rend = _render_mod()
    if rend is None:
        return []
    try:
        return list(rend.validate_config(config) or [])
    except Exception as ex:      # pragma: no cover
        log.warning(f'vision_import: validate_config fehlgeschlagen: {ex}')
        return []


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


def _save_upload(file_storage, ext, ordner=None, vorsilbe='import_img'):
    updir = ordner or _upload_dir()
    os.makedirs(updir, exist_ok=True)
    ts = int(time.time())
    filename = f'{vorsilbe}_{ts}.{ext}'
    n = 1
    while os.path.exists(os.path.join(updir, filename)):
        n += 1
        filename = f'{vorsilbe}_{ts}_{n}.{ext}'
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


# ═══════════════════════════════════════════════════════════════════════════════
# Routen – Nachbau (H2)
# ═══════════════════════════════════════════════════════════════════════════════

GRENZEN = [
    'Es wird nichts neu gemalt: ein sauberer Hintergrund kommt aus dem Vorlagen-Katalog, aus '
    'der eigenen Bibliothek oder wird aus dem hochgeladenen Bild freigeräumt – sonst gar nicht.',
    'Schriften werden auf die vorhandene Bibliothek abgebildet, nicht nachgebaut.',
    'Positionen, Farben und Größen sind Schätzungen der KI. Der Nachbau ist ein Vorschlag, '
    'den du im Vergleich abnimmst oder im Studio nachbesserst.',
    'Fremde Bilder gehören ihren Urhebern. Ein Nachbau macht daraus keine eigenen Bildrechte.',
]


def _nachbau_notiz(summary, hintergrund, city_hint=None, quelle_datei=None):
    zeilen = [_notes_for(summary, city_hint)]
    fmt = (summary.get('format') or {})
    if fmt.get('name'):
        zeilen.append(f'Erkanntes Format: {fmt["name"]} (Sicherheit {fmt.get("sicherheit")})')
    zeilen.append(f'Hintergrund-Weg: {hintergrund.get("weg")} – {hintergrund.get("hinweis")}')
    if quelle_datei:
        zeilen.append(f'Hochgeladenes Original: {quelle_datei}')
    return '\n'.join(zeilen)


@bp.route('/api/templates/nachbau', methods=['POST'])
@login_required
def api_template_nachbau():
    """Fremdes Meme hochladen → Analyse, Hintergrund besorgen, Nachbau, Gegenüberstellung.

    multipart: file, name, category, city_hint, dry_run
    dry_run=1 rechnet alles durch und legt NICHTS an (kein Template, keine Datei im
    Upload-Ordner) – nur die Analyse kostet KI-Geld."""
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'error': 'Keine Datei (Feld "file" fehlt)'}), 400
    f = request.files['file']
    try:
        ext, (w, h) = _validate_upload(f)
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400

    problem = _key_problem()
    if problem:
        # Kein Netzaufruf, kein Template: ohne KI gibt es keine Analyse und damit keinen Nachbau.
        return jsonify({'error': problem, 'hint': HINT_NACHBAU_KEY, 'grenzen': GRENZEN}), 400

    name = (request.form.get('name') or '').strip()
    if not name:
        stem = os.path.splitext(os.path.basename(f.filename or ''))[0]
        name = re.sub(r'[_\-]+', ' ', stem).strip() or 'Nachbau'
    name = name[:200]
    category = (request.form.get('category') or 'allgemein').strip() or 'allgemein'
    city_hint = (request.form.get('city_hint') or '').strip() or None
    dry_run = _truthy(request.form.get('dry_run'), default=False)

    wegwerf = tempfile.mkdtemp(prefix='memeos_nachbau_') if dry_run else None
    ziel = wegwerf or _upload_dir()
    try:
        quelle_datei, quelle_pfad = _save_upload(f, ext, ordner=ziel, vorsilbe='nachbau_quelle')

        analyse = analyze_image(quelle_pfad, city_hint, feature='meme_nachbau')
        if analyse.get('error'):
            return jsonify({'error': analyse['error'],
                            'hint': 'Ohne Analyse kein Nachbau. Das Bild lässt sich weiterhin '
                                    'über den einfachen Bild-Import anlegen und im Studio bauen.',
                            'grenzen': GRENZEN}), 502

        try:
            hintergrund = hintergrund_besorgen(quelle_pfad, analyse, ziel_ordner=ziel)
        except Exception as ex:
            log.error(f'vision_import: Hintergrund nicht besorgt: {ex}', exc_info=True)
            return jsonify({'error': f'Hintergrund nicht besorgt ({type(ex).__name__}: '
                                     f'{str(ex)[:160]})',
                            'hint': 'Die Analyse hat geklappt, das Freiräumen nicht. Das Bild '
                                    'lässt sich über den einfachen Bild-Import anlegen.',
                            'analyse': summarize_analysis(analyse, w, h)}), 500
        warnungen = list(hintergrund.get('warnungen') or [])
        config = nachbau_config(analyse, hintergrund, w, h, warnungen)
        warnungen.extend(f'Config: {t}' for t in _config_fehler(config))

        summary = summarize_analysis(analyse, w, h)
        if not summary['counts']['total']:
            warnungen.append('Keine Textzeile erkannt – der Nachbau ist leer, hier hilft nur das Studio.')
        elif not summary['counts']['city_specific']:
            warnungen.append('Keine stadtspezifische Zeile erkannt – die Vorlage ist für alle '
                             'Städte gleich. Im Studio eine Zeile zur Variable machen.')

        vorschau, vorschau_fehler = _nachbau_vorschau(hintergrund['pfad'], config,
                                                      analyse.get('text_blocks'))
        if vorschau_fehler:
            warnungen.append(vorschau_fehler)
        try:
            original = _klein_b64(quelle_pfad)
        except Exception as ex:      # pragma: no cover
            original = None
            warnungen.append(f'Original nicht verkleinerbar ({type(ex).__name__})')

        template_id = None
        studio_url = None
        if not dry_run:
            t = MemeTemplate(
                name=name,
                description=f'Nachbau aus einem hochgeladenen Meme am '
                            f'{datetime.now().strftime("%d.%m.%Y")}',
                category=category,
                render_type='pil',
                pil_config=json.dumps(config, ensure_ascii=False),
                required_vars=json.dumps(nachbau_variablen(config)),
                tags='[]',
                preview_image=hintergrund['dateiname'],
                notes=_nachbau_notiz(summary, hintergrund, city_hint, quelle_datei)[:4000],
            )
            appmod = _appmod()
            upload_fn = getattr(appmod, '_upload_cloudinary', None) if appmod else None
            if upload_fn:
                try:
                    cloud_url = upload_fn(hintergrund['pfad'], folder='memeos/templates',
                                          resource_type='image')
                    if cloud_url:
                        t.preview_url = cloud_url
                except Exception as ex:
                    log.warning(f'vision_import: Cloudinary-Upload fehlgeschlagen: {ex}')
            db.session.add(t)
            db.session.commit()
            template_id = t.id
            studio_url = f'/studio/{t.id}'

        return jsonify({
            'analyse': summary,
            'hintergrund': {
                'weg': hintergrund['weg'],
                'hinweis': hintergrund['hinweis'],
                'treffer': hintergrund.get('treffer'),
                'datei': None if dry_run else hintergrund['dateiname'],
                'zaehler': hintergrund.get('zaehler'),
            },
            'config': config,
            'warnungen': warnungen,
            'grenzen': GRENZEN,
            'vorschau_b64': vorschau,
            'original_b64': original,
            'bild_mime': 'image/jpeg',
            'template_id': template_id,
            'studio_url': studio_url,
            'variables': nachbau_variablen(config),
            'vorschlag_bild': config.get('vorschlag_bild') or [],
            'width': w, 'height': h,
            'dry_run': dry_run,
            'hint': ('Probelauf: nichts angelegt. Passt der Nachbau, dasselbe Bild ohne dry_run '
                     'schicken.' if dry_run else
                     'Nachbau angelegt. Im Studio prüfen und nachbessern – der Vergleich oben '
                     'zeigt Original und Nachbau nebeneinander.'),
        }), (200 if dry_run else 201)
    finally:
        if wegwerf:
            shutil.rmtree(wegwerf, ignore_errors=True)


@bp.route('/api/templates/nachbau/<int:tmpl_id>/uebernehmen', methods=['POST'])
@login_required
def api_template_nachbau_uebernehmen(tmpl_id):
    """Die im Dialog angepasste Konfiguration speichern. JSON: {'config': {...}}"""
    t = MemeTemplate.query.get_or_404(tmpl_id)
    d = request.get_json(silent=True) or {}
    config = d.get('config')
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            return jsonify({'error': 'config ist kein gültiges JSON'}), 400
    if not isinstance(config, dict):
        return jsonify({'error': 'config muss ein Objekt sein'}), 400
    if not isinstance(config.get('elements'), list):
        return jsonify({'error': 'config braucht eine Liste "elements"'}), 400

    fehler = _config_fehler(config)
    if fehler:
        return jsonify({'error': 'Konfiguration nicht verwendbar', 'fehler': fehler}), 400

    variablen = nachbau_variablen(config)
    t.pil_config = json.dumps(config, ensure_ascii=False)
    t.required_vars = json.dumps(variablen)
    if t.render_type != 'pil':
        t.render_type = 'pil'
    db.session.commit()
    return jsonify({
        'id': t.id,
        'studio_url': f'/studio/{t.id}',
        'variables': variablen,
        'elemente': len(config['elements']),
        'hint': f'{len(config["elements"])} Elemente gespeichert, '
                f'{len(variablen)} Variablen: {", ".join(variablen) or "keine"}',
        'template': _tmpl_dict(t),
    })


def _katalog_stand():
    """Wie viele Vorlagen kennt der Katalog, woher, und wie alt ist der Stand?"""
    kat = _katalog()
    stand = {'anzahl': 0, 'zuletzt': None, 'quellen': {}, 'mit_bild': 0,
             'verfuegbar': kat is not None, 'guete_min': KATALOG_GUETE_MIN, 'hinweis': ''}
    if kat is None:
        stand['hinweis'] = ('Vorlagen-Katalog nicht vorhanden – der Nachbau räumt den '
                            'Hintergrund immer aus dem hochgeladenen Bild frei')
        return stand
    try:
        stand['offline'] = bool(kat.offline())
    except Exception:
        stand['offline'] = False
    try:
        from models import MemeFormat
        zeilen = db.session.query(MemeFormat.quelle, MemeFormat.filename,
                                  MemeFormat.created_at).all()
    except Exception as ex:
        db.session.rollback()
        stand['hinweis'] = f'Katalog nicht lesbar ({type(ex).__name__}: {str(ex)[:120]})'
        return stand
    zuletzt = None
    for quelle, dateiname, angelegt in zeilen:
        q = (quelle or 'unbekannt')
        eintrag = stand['quellen'].setdefault(q, {'anzahl': 0, 'mit_bild': 0})
        eintrag['anzahl'] += 1
        if dateiname:
            eintrag['mit_bild'] += 1
            stand['mit_bild'] += 1
        if angelegt and (zuletzt is None or angelegt > zuletzt):
            zuletzt = angelegt
    stand['anzahl'] = len(zeilen)
    stand['zuletzt'] = zuletzt.isoformat() if zuletzt else None
    if not stand['anzahl']:
        stand['hinweis'] = ('Katalog leer – erst aktualisieren, sonst wird der Hintergrund '
                            'immer freigeräumt')
    elif not stand['mit_bild']:
        stand['hinweis'] = ('Katalog kennt nur Namen, keine Bilder – ohne Bild gibt es keinen '
                            'Bildvergleich. Aktualisieren mit "laden"')
    return stand


@bp.route('/api/templates/nachbau/katalog', methods=['GET'])
@login_required
def api_nachbau_katalog():
    return jsonify(_katalog_stand())


@bp.route('/api/templates/nachbau/katalog/aktualisieren', methods=['POST'])
@login_required
def api_nachbau_katalog_aktualisieren():
    """Öffentlichen Katalog holen und die eigene Bibliothek aufnehmen. Der Netzaufruf steckt
    in meme_katalog und unterbleibt bei MEMEOS_OFFLINE=1 (dann kommt nur eine Meldung)."""
    kat = _katalog()
    if kat is None:
        return jsonify({'error': 'Vorlagen-Katalog nicht vorhanden (meme_katalog fehlt)'}), 400
    d = request.get_json(silent=True) or {}
    laden = _truthy(d.get('laden', request.form.get('laden')), default=True)
    try:
        katalog = kat.katalog_aktualisieren(None, laden=laden)
    except Exception as ex:
        log.error(f'vision_import: katalog_aktualisieren fehlgeschlagen: {ex}')
        return jsonify({'error': f'Katalog nicht aktualisiert ({type(ex).__name__}: '
                                 f'{str(ex)[:160]})'}), 502
    try:
        eigene = kat.eigene_aufnehmen(None)
    except Exception as ex:
        log.error(f'vision_import: eigene_aufnehmen fehlgeschlagen: {ex}')
        eigene = {'meldung': f'Eigene Bibliothek nicht aufgenommen ({type(ex).__name__})',
                  'fehler': [str(ex)[:160]]}
    return jsonify({
        'katalog': katalog,
        'eigene': eigene,
        'stand': _katalog_stand(),
        'hint': f'{katalog.get("meldung", "")} | {eigene.get("meldung", "")}'.strip(' |'),
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
von Hand. Die Analyse lässt sich später jederzeit über "Erneut analysieren" nachholen.

Nachbau (der größere Weg): Beim Import wird die fremde Schrift nur überdeckt – das Bild bleibt
das fremde Bild. Der Nachbau geht weiter: er besorgt einen sauberen Hintergrund OHNE Schrift und
baut das Meme darauf neu auf.

1. Hintergrund besorgen, in dieser Reihenfolge:
   a) Vorlagen-Katalog: bekannte Meme-Formate (Drake, Two Buttons und so weiter) als Original.
   b) Eigene Bibliothek: deine schon vorhandenen Hintergründe und Aufkleber.
   c) Freiräumen: die Schrift wird aus dem hochgeladenen Bild entfernt – mit der Kastenfarbe,
      mit der Randfarbe oder per Retusche.
2. Jede erkannte Zeile wird neu gesetzt: feste Zeilen mit ihrem Text, stadtspezifische als
   Variable mit dem Originaltext als Rückfall.
3. Du bekommst Original und Nachbau nebeneinander und nimmst ab oder besserst im Studio nach.

Ehrliche Grenzen: Es wird nichts neu gemalt. Ist das Format unbekannt und die Retusche unsauber,
sieht man das – dann ist ein eigener Hintergrund die bessere Wahl. Schriften werden auf die
vorhandene Bibliothek abgebildet, nicht nachgebaut. Und: ein Nachbau macht aus einem fremden Bild
keine eigenen Bildrechte."""


@bp.route('/api/templates/import/image/help', methods=['GET'])
@login_required
def api_import_image_help():
    return jsonify({
        'help': HELP_TEXT,
        'ai_available': bool(_api_key()),
        'ai_problem': _key_problem() or '',
        'model': VISION_MODEL,
        'allowed_extensions': list(ALLOWED_EXT),
        'variables': known_variable_keys(),
        'nachbau': {
            'route': '/api/templates/nachbau',
            'felder': ['file', 'name', 'category', 'city_hint', 'dry_run'],
            'wege': ['katalog', 'bibliothek', 'freigeraeumt'],
            'grenzen': GRENZEN,
            'katalog': _katalog_stand(),
        },
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Registrierung
# ═══════════════════════════════════════════════════════════════════════════════

def init_app(flask_app):
    if 'vision_import' in flask_app.blueprints:
        return
    flask_app.register_blueprint(bp)
    log.info('vision_import: Blueprint registriert (Modell %s)', VISION_MODEL)
