"""Vorlagen-Katalog und Bildvergleich (H1).

Wenn jemand ein fremdes Meme hochlädt, soll MemeOS den *sauberen* Original-Hintergrund
besorgen statt die Schrift nur wegzuretuschieren. Dieses Modul ist der Nachschlagedienst
dafür: es hält einen Katalog bekannter Meme-Vorlagen (öffentlicher Imgflip-Katalog plus
die eigene Bibliothek) und findet zu einem hochgeladenen Bild die passende Zeile.

Ehrliche Grenze, die hier gilt: es wird nichts neu gemalt. Gefunden werden kann nur, was
im Katalog steht. Findet dieses Modul nichts, muss der Aufrufer den Hintergrund aus dem
hochgeladenen Bild freiräumen — und der Nutzer nimmt das Ergebnis im Vergleich ab.

Kernstück ist ein Differenzhash (dhash) ohne neue Abhängigkeit, nur mit Pillow:
das Bild wird graustufig auf (groesse+1) x groesse verkleinert, dann wird jeder Bildpunkt
mit seinem rechten Nachbarn verglichen — 64 Vergleiche, 64 Bit.

Das hochgeladene Meme trägt Text, die Katalogvorlage nicht. Der Hash über das *ganze*
Bild weicht deshalb ab. Darum wird zusätzlich über Teilausschnitte verglichen (obere
Hälfte, untere Hälfte, mittige 60 % der Fläche und ein engerer Kern mit 25 % der Fläche)
und der beste Teiltreffer gewertet: Meme-Text sitzt fast immer oben und unten, die Mitte
bleibt meist unberührt.

Netz: NUR katalog_aktualisieren() und format_bilddatei() gehen ins Netz, und beide nur,
wenn MEMEOS_OFFLINE nicht gesetzt ist. Alles andere arbeitet rein lokal.

Öffentliche Schnittstelle (Vertrag für Bereich 2):
    dhash(bild, groesse=8)                                  -> int (64 Bit)
    hamming(a, b)                                           -> int
    phash_hex(bild)                                         -> str (16 Hex-Zeichen)
    hex_zu_int(text)                                        -> int | None
    katalog_aktualisieren(datenpfad, laden=True)            -> dict
    eigene_aufnehmen(datenpfad)                             -> dict
    hintergrund_unsauber(template)                          -> bool
    finde_format(bild, name_hinweis=None, max_abstand=14,
                 datenpfad=None, grenze=25)                 -> list[dict]
    format_bilddatei(format_zeile, datenpfad)               -> str | None
    formate_ordner(datenpfad)                               -> str
    guete_label(guete)                                      -> 'sicher'|'unsicher'|'schwach'
    hash_satz(bild)                                         -> dict[str, int]
    name_aehnlichkeit(a, b)                                 -> float
    offline()                                               -> bool
    cache_leeren()                                          -> None
"""
import io
import json
import os
import re
import sys
import difflib
import logging
import importlib
import unicodedata

import requests
from PIL import Image

from models import db, MemeFormat, MemeTemplate, StudioAsset

log = logging.getLogger(__name__)

IMGFLIP_URL = 'https://api.imgflip.com/get_memes'
IMGFLIP_TIMEOUT = 20

MAX_BILD_BYTES = 8 * 1024 * 1024      # 8 MB je Katalogbild
HASH_GROESSE = 8                       # 8x8 Vergleiche = 64 Bit
MITTE_FLAECHE = 0.60                   # mittiger Ausschnitt: 60 % der Fläche
KERN_FLAECHE = 0.25                    # engerer Kern: 25 % der Fläche (50 % je Kante)
KONTRAST_MIN = 12                      # Graustufen-Spanne, unter der ein Ausschnitt nichts sagt
NAME_SCHWELLE = 0.60                   # ab hier gilt ein Namensvergleich als Treffer

# Schwellen für die Beschriftung in der Oberfläche. Gemessen (siehe Bericht H1):
# echte Paare (dasselbe Bild, einmal mit Meme-Text) kamen auf Güte 0,60 bis 0,85;
# der schlimmste gebaute Fehltreffer (zwei fremde Bilder mit großer weißer Mitte)
# auf 0,38. Dazwischen liegt die Grenze.
GUETE_SICHER = 0.55                    # ab hier: "gefunden"
GUETE_UNSICHER = 0.30                  # darunter: nur ein schwacher Hinweis
TEILVERGLEICH_MAX = 400                # so viele Katalogbilder liest eine Suche höchstens von
                                       # Platte. Gemessen: 200 Bilder kalt 0,47 s, warm 0,003 s
                                       # (Zwischenspeicher). Darüber greift die Vorsortierung nach
                                       # dem gespeicherten Hash — eine Heuristik: liegt die echte
                                       # Vorlage dort weiter hinten als TEILVERGLEICH_MAX, wird sie
                                       # nur noch über den vollen Hash und den Namen gefunden.
CACHE_MAX = 2000                       # Obergrenze für den Hash-Zwischenspeicher

SEGMENTE = ('voll', 'oben', 'unten', 'mitte', 'kern')
BILD_ENDUNGEN = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp', 'GIF': '.gif'}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_UNSAUBER_RE = re.compile(r'[^a-z0-9]+')
_DATEINAME_RE = re.compile(r'[^A-Za-z0-9_.-]+')

# Zwischenspeicher für die Teil-Hashes der Katalogbilder: Pfad -> (mtime, groesse, hashes)
_TEIL_CACHE = {}


class KatalogFehler(Exception):
    """Etwas ließ sich nicht holen oder nicht lesen. Trägt eine deutsche Meldung."""


# ═══════════════════════════════════════════════════════════════════════════════
# Infrastruktur
# ═══════════════════════════════════════════════════════════════════════════════

def _appmod():
    """app.py nur zur Laufzeit laden (kein zirkulärer Import auf Modulebene).
    Läuft app.py als __main__, liegt es unter sys.modules['__main__']; ein 'import app'
    würde die Datei ein zweites Mal ausführen. Darum zuerst __main__ und sys.modules."""
    main = sys.modules.get('__main__')
    if main is not None and hasattr(main, '_DATA_ROOT'):
        return main
    mod = sys.modules.get('app')
    if mod is not None:
        return mod
    try:
        return importlib.import_module('app')
    except Exception as ex:      # pragma: no cover — nur ohne lauffähige App
        log.warning(f'meme_katalog: app-Modul nicht ladbar: {ex}')
        return None


def _data_root(datenpfad=None):
    if datenpfad:
        return datenpfad
    appmod = _appmod()
    root = getattr(appmod, '_DATA_ROOT', None) if appmod else None
    return root or os.getenv('MEMEOS_DATA_ROOT') or os.path.join(_BASE_DIR, 'instance')


def offline():
    """True, wenn keine Netzaufrufe gemacht werden dürfen (MEMEOS_OFFLINE)."""
    return (os.getenv('MEMEOS_OFFLINE', '') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def formate_ordner(datenpfad=None):
    """<datenpfad>/formate/ — dort liegen die heruntergeladenen Katalogbilder."""
    d = os.path.join(_data_root(datenpfad), 'formate')
    os.makedirs(d, exist_ok=True)
    return d


def _holen(url, timeout=IMGFLIP_TIMEOUT, max_bytes=MAX_BILD_BYTES):
    """Bild über app._safe_fetch_url holen (SSRF-Schutz). Wirft KatalogFehler."""
    if offline():
        raise KatalogFehler('Ohne Netz (MEMEOS_OFFLINE=1)')
    appmod = _appmod()
    fetch = getattr(appmod, '_safe_fetch_url', None) if appmod else None
    if not fetch:
        raise KatalogFehler('app._safe_fetch_url nicht verfügbar')
    try:
        return fetch(url, timeout=timeout, max_bytes=max_bytes)
    except KatalogFehler:
        raise
    except Exception as ex:
        raise KatalogFehler(f'{type(ex).__name__}: {ex}')


# ═══════════════════════════════════════════════════════════════════════════════
# Wahrnehmungs-Hash (Differenzhash, nur Pillow)
# ═══════════════════════════════════════════════════════════════════════════════

def _als_bild(quelle):
    """PIL-Bild, Pfad, Bytes oder Datei-Objekt → geladenes PIL-Bild. Wirft KatalogFehler."""
    if isinstance(quelle, Image.Image):
        return quelle
    try:
        if isinstance(quelle, (bytes, bytearray, memoryview)):
            bild = Image.open(io.BytesIO(bytes(quelle)))
        elif isinstance(quelle, (str, os.PathLike)):
            bild = Image.open(os.fspath(quelle))
        elif hasattr(quelle, 'read'):
            bild = Image.open(quelle)
        else:
            raise KatalogFehler(f'Unbrauchbare Bildquelle: {type(quelle).__name__}')
        bild.load()
        return bild
    except KatalogFehler:
        raise
    except Exception as ex:
        raise KatalogFehler(f'Bild nicht lesbar ({type(ex).__name__}: {ex})')


def _punkte(bild, groesse):
    """Graustufen-Rasterpunkte (groesse+1) x groesse — die Grundlage des Hashes."""
    return list(bild.convert('L')
                    .resize((groesse + 1, groesse), Image.Resampling.LANCZOS)
                    .getdata())


def _bits(punkte, groesse):
    """Waagerechte Nachbarn vergleichen. Bit 1 heißt: der linke Punkt ist heller."""
    wert = 0
    breite = groesse + 1
    for y in range(groesse):
        zeile = y * breite
        for x in range(groesse):
            wert = (wert << 1) | (1 if punkte[zeile + x] > punkte[zeile + x + 1] else 0)
    return wert


def dhash(bild, groesse=HASH_GROESSE):
    """Differenzhash als Ganzzahl mit groesse*groesse Bit (Vorgabe 64).

    Das Bild wird graustufig auf (groesse+1) x groesse verkleinert; je Zeile wird
    jeder Bildpunkt mit seinem rechten Nachbarn verglichen. Bit 1 heißt: links heller.
    Unempfindlich gegen Größe, Helligkeit und Kompression, empfindlich gegen Struktur.
    """
    if groesse < 1:
        raise ValueError('groesse muss mindestens 1 sein')
    return _bits(_punkte(_als_bild(bild), groesse), groesse)


def hamming(a, b):
    """Anzahl der Bit, in denen sich zwei Hashes unterscheiden (0 = gleich)."""
    unterschied = int(a) ^ int(b)
    zaehler = getattr(unterschied, 'bit_count', None)      # ab Python 3.10
    if zaehler is not None:
        return zaehler()
    return bin(unterschied).count('1')


def phash_hex(bild, groesse=HASH_GROESSE):
    """Hash als feste Hex-Zeichenkette zum Speichern (bei groesse 8: 16 Zeichen)."""
    stellen = (groesse * groesse + 3) // 4
    return format(dhash(bild, groesse), f'0{stellen}x')


def hex_zu_int(text):
    """Gespeicherten Hex-Hash zurück in eine Ganzzahl. None, wenn leer oder kaputt."""
    text = (text or '').strip()
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _mittig(grau, flaechenanteil):
    """Mittiger Ausschnitt mit dem gewünschten Flächenanteil."""
    w, h = grau.size
    seite = flaechenanteil ** 0.5
    cw, ch = max(1, int(w * seite)), max(1, int(h * seite))
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return grau.crop((x0, y0, x0 + cw, y0 + ch))


def _teilbilder(bild):
    """Ganzes Bild plus die Ausschnitte, in denen ein Meme meist unberührt bleibt.

    'oben'/'unten' fangen den Fall ab, dass der Text nur an einem Rand sitzt.
    'mitte' (60 % der Fläche) übersteht Text mit Kontur und schmale Balken.
    'kern' (25 % der Fläche) ist gemessen dazugekommen: bei deckenden Balken über
    30 % der Höhe reicht 'mitte' noch in die Balken hinein — echte Paare lagen damit
    bei bis zu 18 Bit, mit 'kern' nur noch bei 6, ohne dass die Fehltrefferquote stieg.
    """
    grau = bild.convert('L') if bild.mode != 'L' else bild
    w, h = grau.size
    teile = {'voll': grau}
    if w >= 4 and h >= 4:
        halb = max(1, h // 2)
        teile['oben'] = grau.crop((0, 0, w, halb))
        teile['unten'] = grau.crop((0, h - halb, w, h))
        teile['mitte'] = _mittig(grau, MITTE_FLAECHE)
    if w >= 16 and h >= 16:
        teile['kern'] = _mittig(grau, KERN_FLAECHE)
    return teile


def hash_satz(bild, groesse=HASH_GROESSE):
    """{'voll', 'oben', 'unten', 'mitte', 'kern'} → je ein Hash. Siehe _teilbilder.

    Ausschnitte ohne Struktur (eine glatte Fläche, etwa ein weißer Rand oder ein
    deckender Balken) fallen raus: ihr Hash wäre bei jedem Bild gleich und würde
    zwei völlig verschiedene Vorlagen als Volltreffer erscheinen lassen.
    'voll' bleibt immer drin, weil dieser Wert in der Datenbank steht.
    """
    bild = _als_bild(bild)
    satz = {}
    for name, teil in _teilbilder(bild).items():
        punkte = _punkte(teil, groesse)
        if name != 'voll' and (max(punkte) - min(punkte)) < KONTRAST_MIN:
            continue
        satz[name] = _bits(punkte, groesse)
    return satz


def _hash_satz_datei(pfad):
    """hash_satz einer Datei, mit Zwischenspeicher über (Änderungszeit, Größe)."""
    try:
        st = os.stat(pfad)
    except OSError:
        return None
    schluessel = os.path.abspath(pfad)
    treffer = _TEIL_CACHE.get(schluessel)
    if treffer and treffer[0] == st.st_mtime and treffer[1] == st.st_size:
        return treffer[2]
    try:
        satz = hash_satz(pfad)
    except KatalogFehler as ex:
        log.debug(f'meme_katalog: {pfad} nicht hashbar ({ex})')
        return None
    if len(_TEIL_CACHE) >= CACHE_MAX:
        _TEIL_CACHE.clear()
    _TEIL_CACHE[schluessel] = (st.st_mtime, st.st_size, satz)
    return satz


def cache_leeren():
    """Zwischenspeicher der Teil-Hashes verwerfen (nach Umbenennen/Löschen von Dateien)."""
    _TEIL_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# Namensvergleich
# ═══════════════════════════════════════════════════════════════════════════════

def _name_normalisieren(text):
    """Kleinschreibung, Umlaute ausgeschrieben, alles außer a-z0-9 raus."""
    text = (text or '').strip().lower()
    for alt, neu in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss')):
        text = text.replace(alt, neu)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return _UNSAUBER_RE.sub(' ', text).strip()


def name_aehnlichkeit(a, b):
    """0..1 über difflib. 1.0, wenn der eine Name vollständig im anderen steckt."""
    a, b = _name_normalisieren(a), _name_normalisieren(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# ═══════════════════════════════════════════════════════════════════════════════
# Katalog holen
# ═══════════════════════════════════════════════════════════════════════════════

def _dateiname(vorsilbe, kennung, endung):
    kennung = _DATEINAME_RE.sub('_', str(kennung or ''))[:60] or 'ohne_id'
    return f'{vorsilbe}_{kennung}{endung}'


def _bild_ablegen(daten, ordner, vorsilbe, kennung):
    """Bytes prüfen, mit passender Endung ablegen. → (dateiname, phash, breite, hoehe)."""
    bild = _als_bild(daten)
    endung = BILD_ENDUNGEN.get((bild.format or '').upper(), '.jpg')
    name = _dateiname(vorsilbe, kennung, endung)
    ziel = os.path.join(ordner, name)
    with open(ziel, 'wb') as f:
        f.write(daten)
    return name, phash_hex(bild), bild.size[0], bild.size[1]


def _ganzzahl(wert, vorgabe=0):
    try:
        return int(wert)
    except (TypeError, ValueError):
        return vorgabe


def katalog_aktualisieren(datenpfad=None, laden=True):
    """Öffentlichen Imgflip-Katalog holen und als MemeFormat-Zeilen ablegen.

    laden=True lädt zusätzlich jedes Vorlagenbild nach <datenpfad>/formate/ und berechnet
    den Hash — erst danach ist der Bildvergleich möglich. laden=False legt nur die Namen an
    (dann findet finde_format diese Zeilen ausschließlich über den Namenshinweis).

    Ein Fehler bei einem einzelnen Eintrag wird gezählt und übersprungen, nie abgebrochen.

    → {'geholt': int, 'neu': int, 'aktualisiert': int, 'fehler': [str], 'meldung': str}
    """
    ergebnis = {'geholt': 0, 'neu': 0, 'aktualisiert': 0, 'fehler': [], 'meldung': ''}
    if offline():
        ergebnis['meldung'] = 'Katalog nicht geholt: kein Netz (MEMEOS_OFFLINE=1)'
        ergebnis['fehler'].append(ergebnis['meldung'])
        return ergebnis

    try:
        antwort = requests.get(IMGFLIP_URL, timeout=IMGFLIP_TIMEOUT)
        antwort.raise_for_status()
        daten = antwort.json()
    except Exception as ex:
        ergebnis['meldung'] = f'Katalog nicht erreichbar ({type(ex).__name__}: {ex})'
        ergebnis['fehler'].append(ergebnis['meldung'])
        return ergebnis

    if not isinstance(daten, dict) or not daten.get('success'):
        ergebnis['meldung'] = f'Katalog antwortete ohne Erfolg: {str(daten)[:200]}'
        ergebnis['fehler'].append(ergebnis['meldung'])
        return ergebnis

    eintraege = ((daten.get('data') or {}).get('memes') or [])
    ergebnis['geholt'] = len(eintraege)
    ordner = formate_ordner(datenpfad) if laden else None

    for eintrag in eintraege:
        try:
            extern_id = str(eintrag.get('id') or '').strip()
            name = (eintrag.get('name') or '').strip()
            if not extern_id or not name:
                ergebnis['fehler'].append(f'Eintrag ohne Kennung oder Namen: {str(eintrag)[:80]}')
                continue

            zeile = MemeFormat.query.filter_by(quelle='imgflip', extern_id=extern_id).first()
            neu = zeile is None
            if neu:
                zeile = MemeFormat(quelle='imgflip', extern_id=extern_id)
                db.session.add(zeile)

            zeile.name = name[:200]
            zeile.url = (eintrag.get('url') or '')[:1000]
            zeile.width = _ganzzahl(eintrag.get('width'))
            zeile.height = _ganzzahl(eintrag.get('height'))
            zeile.box_count = _ganzzahl(eintrag.get('box_count'), None)

            if laden and zeile.url:
                try:
                    rohdaten, _endgueltig, _typ = _holen(zeile.url)
                    dateiname, hashwert, breite, hoehe = _bild_ablegen(
                        rohdaten, ordner, 'imgflip', extern_id)
                    zeile.filename = dateiname
                    zeile.phash = hashwert
                    zeile.width = breite or zeile.width
                    zeile.height = hoehe or zeile.height
                except KatalogFehler as ex:
                    ergebnis['fehler'].append(f'{name}: Bild nicht geholt ({ex})')

            db.session.commit()
            ergebnis['neu' if neu else 'aktualisiert'] += 1
        except Exception as ex:
            db.session.rollback()
            ergebnis['fehler'].append(f'{str(eintrag.get("name") or eintrag)[:60]}: '
                                      f'{type(ex).__name__}: {ex}')

    ergebnis['meldung'] = (f'{ergebnis["geholt"]} Vorlagen geholt, {ergebnis["neu"]} neu, '
                           f'{ergebnis["aktualisiert"]} aufgefrischt, '
                           f'{len(ergebnis["fehler"])} Fehler')
    return ergebnis


# ═══════════════════════════════════════════════════════════════════════════════
# Eigene Bibliothek aufnehmen
# ═══════════════════════════════════════════════════════════════════════════════

def _lokale_datei(dateiname, datenpfad=None):
    """Dateinamen in den bekannten Ordnern suchen (formate, uploads, renders)."""
    if not dateiname:
        return None
    if os.path.isabs(dateiname) and os.path.exists(dateiname):
        return dateiname
    basis = os.path.basename(dateiname)
    wurzel = _data_root(datenpfad)
    for ordner in ('formate', 'uploads', 'renders'):
        kandidat = os.path.join(wurzel, ordner, basis)
        if os.path.exists(kandidat):
            return kandidat
    return None


def _eigene_zeile(datenpfad, extern_id, name, dateiname, url, ergebnis):
    """Eine MemeFormat-Zeile der Quelle 'eigen' anlegen oder auffrischen. Idempotent."""
    zeile = MemeFormat.query.filter_by(quelle='eigen', extern_id=extern_id).first()
    neu = zeile is None
    if neu:
        zeile = MemeFormat(quelle='eigen', extern_id=extern_id)
        db.session.add(zeile)

    alter_dateiname = None if neu else (zeile.filename or '')

    zeile.name = (name or extern_id)[:200]
    zeile.filename = (dateiname or '')[:300] or None
    zeile.url = (url or '')[:1000] or None

    # Wurde der Hintergrund einer Vorlage ausgetauscht, zeigt der alte Hash auf das alte Bild.
    # Deshalb bei geändertem Dateinamen neu messen, nicht nur wenn Hash oder Maße fehlen.
    dateiname_neu = (zeile.filename or '') != (alter_dateiname or '')
    pfad = _lokale_datei(dateiname, datenpfad)
    if pfad and (neu or dateiname_neu or not zeile.phash or not zeile.width):
        try:
            bild = _als_bild(pfad)
            zeile.phash = phash_hex(bild)
            zeile.width, zeile.height = bild.size
        except KatalogFehler as ex:
            ergebnis['fehler'].append(f'{zeile.name}: {ex}')
    db.session.commit()
    ergebnis['neu' if neu else 'aktualisiert'] += 1


def hintergrund_unsauber(template):
    """True, wenn der Hintergrund dieser Vorlage noch die fremde Schrift trägt.

    Der einfache Bild-Import legt das fremde Bild samt Text als Hintergrund ab und schreibt
    pil_config.meta.hintergrund_sauber = False; der Nachbau schreibt True. Ältere Vorlagen
    ohne meta erkennt man an der description "Importiert aus Bild ...". Solche Vorlagen
    gehören nicht in die Bibliothek: ein Hintergrund mit Schrift ist kein Hintergrund."""
    meta = None
    try:
        cfg = json.loads(template.pil_config or '{}')
        if isinstance(cfg, dict) and isinstance(cfg.get('meta'), dict):
            meta = cfg['meta']
    except Exception:
        meta = None
    if meta is not None and 'hintergrund_sauber' in meta:
        return meta.get('hintergrund_sauber') is False
    return str(template.description or '').strip().startswith('Importiert aus Bild')


def eigene_aufnehmen(datenpfad=None):
    """Eigene Hintergründe (Template-Vorschaubilder) und Aufkleber in den Katalog aufnehmen.

    Damit findet der Nutzer beim Nachbau auch seine eigenen Vorlagen wieder, nicht nur die
    öffentlichen. Rein lokal, kein Netz: der Hash wird nur berechnet, wenn die Datei da ist;
    sonst bleibt die URL stehen und format_bilddatei() lädt sie später bei Bedarf nach.

    Vorlagen, deren Hintergrund noch Schrift trägt (hintergrund_unsauber), werden übersprungen
    und in der Meldung gezählt.

    → {'geholt': int, 'neu': int, 'aktualisiert': int, 'uebersprungen': int,
       'fehler': [str], 'meldung': str}
    """
    ergebnis = {'geholt': 0, 'neu': 0, 'aktualisiert': 0, 'uebersprungen': 0, 'entfernt': 0,
                'fehler': [], 'meldung': ''}

    for t in MemeTemplate.query.order_by(MemeTemplate.id).all():
        if not (t.preview_image or t.preview_url):
            continue
        if hintergrund_unsauber(t):
            ergebnis['uebersprungen'] += 1
            # Vor dieser Regel aufgenommene Zeilen der Vorlage wieder aus der Bibliothek nehmen,
            # sonst holt der Nachbau weiter einen Hintergrund MIT Schrift aus der 'Bibliothek'.
            try:
                alt = MemeFormat.query.filter_by(quelle='eigen', extern_id=f'template:{t.id}').all()
                for zeile in alt:
                    db.session.delete(zeile)
                if alt:
                    db.session.commit()
                    ergebnis['entfernt'] += len(alt)
            except Exception as ex:
                db.session.rollback()
                ergebnis['fehler'].append(f'Template {t.id}: Altzeile nicht entfernt '
                                          f'({type(ex).__name__}: {ex})')
            continue
        ergebnis['geholt'] += 1
        try:
            _eigene_zeile(datenpfad, f'template:{t.id}', t.name,
                          t.preview_image, t.preview_url, ergebnis)
        except Exception as ex:
            db.session.rollback()
            ergebnis['fehler'].append(f'Template {t.id}: {type(ex).__name__}: {ex}')

    for a in StudioAsset.query.order_by(StudioAsset.id).all():
        if not (a.filename or a.url):
            continue
        ergebnis['geholt'] += 1
        try:
            _eigene_zeile(datenpfad, f'asset:{a.id}', a.name or a.filename,
                          a.filename, a.url, ergebnis)
        except Exception as ex:
            db.session.rollback()
            ergebnis['fehler'].append(f'Aufkleber {a.id}: {type(ex).__name__}: {ex}')

    ergebnis['meldung'] = (f'{ergebnis["geholt"]} eigene Bilder geprüft, {ergebnis["neu"]} neu, '
                           f'{ergebnis["aktualisiert"]} aufgefrischt, '
                           f'{len(ergebnis["fehler"])} Fehler')
    if ergebnis['uebersprungen']:
        ergebnis['meldung'] += (f', {ergebnis["uebersprungen"]} übersprungen '
                                f'(Hintergrund trägt noch Schrift)')
    if ergebnis['entfernt']:
        ergebnis['meldung'] += f', {ergebnis["entfernt"]} aus der Bibliothek entfernt'
    return ergebnis


# ═══════════════════════════════════════════════════════════════════════════════
# Suchen
# ═══════════════════════════════════════════════════════════════════════════════

def _guete(abstand, max_abstand, name_wert):
    """Bildabstand und Namensähnlichkeit zu einem Wert 0..1 zusammenfassen.

    Zwei unabhängige Belege (Bild UND Name) geben einen Bonus, weil ein zufälliges
    Zusammentreffen beider unwahrscheinlich ist. Ein reiner Namenstreffer kommt höchstens
    auf 0,50 und bleibt damit immer unter GUETE_SICHER: ein gleich heißendes Format ist
    ein Hinweis, aber kein Beleg, dass es dasselbe Bild ist.
    """
    if abstand is None:
        bild_anteil = 0.0
    else:
        bild_anteil = max(0.0, 1.0 - abstand / float(max(1, max_abstand) + 6))
    name_anteil = max(0.0, min(1.0, name_wert or 0.0))
    if abstand is not None and name_anteil:
        wert = 0.75 * bild_anteil + 0.25 * name_anteil + 0.15
    elif abstand is not None:
        wert = 0.85 * bild_anteil
    else:
        wert = 0.50 * name_anteil
    return round(max(0.0, min(1.0, wert)), 3)


def guete_label(guete):
    """'sicher' | 'unsicher' | 'schwach' — für die Beschriftung eines Treffers.

    Auch 'sicher' heißt nur: hier lohnt der Vergleich. Übernommen wird ein Hintergrund
    erst, wenn der Nutzer die Gegenüberstellung abgenommen hat.
    """
    if guete >= GUETE_SICHER:
        return 'sicher'
    if guete >= GUETE_UNSICHER:
        return 'unsicher'
    return 'schwach'


def _bester_teiltreffer(satz_a, satz_b):
    """Kleinster Abstand über alle Ausschnitte → (abstand, segment)."""
    bester, wo = None, ''
    for name in SEGMENTE:
        a, b = satz_a.get(name), satz_b.get(name)
        if a is None or b is None:
            continue
        d = hamming(a, b)
        if bester is None or d < bester:
            bester, wo = d, name
    return bester, wo


def finde_format(bild, name_hinweis=None, max_abstand=14, datenpfad=None, grenze=25):
    """Zu einem hochgeladenen Meme die passenden Katalogzeilen suchen.

    bild          PIL-Bild, Pfad, Bytes oder Datei-Objekt (das Meme MIT Text)
    name_hinweis  optionaler Name, z. B. aus dem Dateinamen oder der KI-Analyse
    max_abstand   Bit-Abstand, bis zu dem ein Bildtreffer gilt (0..64; 14 ist streng)
    datenpfad     Datenwurzel; ohne Angabe aus app._DATA_ROOT bzw. MEMEOS_DATA_ROOT
    grenze        wie viele Treffer höchstens zurückkommen

    → [{'format': dict, 'abstand': int|None, 'guete': float, 'grund': str,
        'segment': str, 'name_wert': float}], bester zuerst.

    grund ist 'bild', 'name' oder 'bild+name'. Der Bildvergleich läuft zusätzlich über
    Ausschnitte, weil der Meme-Text den Hash des ganzen Bildes verfälscht; gewertet wird
    der beste Ausschnitt und in 'segment' steht, welcher es war.

    Ohne Netz und ohne heruntergeladene Katalogbilder bleibt nur der Namensvergleich —
    dann ist 'abstand' None und die Güte niedrig. Das ist Absicht: der Nutzer soll den
    Nachbau abnehmen, nicht ein unsicherer Treffer still übernommen werden.
    """
    try:
        satz_frage = hash_satz(_als_bild(bild))
    except KatalogFehler as ex:
        log.warning(f'meme_katalog: Suchbild nicht lesbar ({ex})')
        return []

    zeilen = MemeFormat.query.all()
    hinweis = (name_hinweis or '').strip()

    # Vorsortieren: erst die Zeilen, deren gespeicherter Hash schon nah dran ist. Nur für
    # die vordersten wird die Bilddatei von Platte gelesen (Teilvergleich kostet I/O).
    vorsortiert = []
    for zeile in zeilen:
        gespeichert = hex_zu_int(zeile.phash)
        grob = hamming(satz_frage['voll'], gespeichert) if gespeichert is not None else 999
        vorsortiert.append((grob, zeile))
    vorsortiert.sort(key=lambda p: p[0])

    treffer = []
    gelesen = 0
    for grob, zeile in vorsortiert:
        name_wert = name_aehnlichkeit(hinweis, zeile.name) if hinweis else 0.0
        name_treffer = name_wert >= NAME_SCHWELLE

        abstand, segment = None, ''
        if grob < 999:
            abstand, segment = grob, 'voll'
        # Teilvergleich: nur für die vordersten Zeilen und für Namenstreffer, damit eine
        # Suche nicht den ganzen Katalog von Platte liest.
        if gelesen < TEILVERGLEICH_MAX or name_treffer:
            pfad = _lokale_datei(zeile.filename, datenpfad)
            if pfad:
                gelesen += 1
                satz_zeile = _hash_satz_datei(pfad)
                if satz_zeile:
                    d, wo = _bester_teiltreffer(satz_frage, satz_zeile)
                    if d is not None and (abstand is None or d < abstand):
                        abstand, segment = d, wo

        bild_treffer = abstand is not None and abstand <= max_abstand
        if not bild_treffer and not name_treffer:
            continue
        if bild_treffer and name_treffer:
            grund = 'bild+name'
        elif bild_treffer:
            grund = 'bild'
        else:
            grund = 'name'

        treffer.append({
            'format':    zeile.to_dict(),
            # gemessener bester Abstand; bei grund 'name' liegt er über max_abstand
            # (oder ist None, wenn gar kein Bild verglichen werden konnte)
            'abstand':   abstand,
            'guete':     _guete(abstand if bild_treffer else None, max_abstand,
                                name_wert if name_treffer else 0.0),
            'grund':     grund,
            'segment':   segment,
            'name_wert': round(name_wert, 3),
        })
        treffer[-1]['label'] = guete_label(treffer[-1]['guete'])

    treffer.sort(key=lambda t: (-t['guete'], t['abstand'] if t['abstand'] is not None else 999))
    return treffer[:max(1, grenze)]


# ═══════════════════════════════════════════════════════════════════════════════
# Bild holen
# ═══════════════════════════════════════════════════════════════════════════════

def format_bilddatei(format_zeile, datenpfad=None):
    """Lokalen Pfad zum Vorlagenbild liefern; bei Bedarf über die URL nachladen.

    Liefert None, wenn die Datei fehlt und nicht geholt werden kann (kein Netz, keine URL,
    Ziel abgelehnt). Wirft nie — der Aufrufer entscheidet, was er dem Nutzer zeigt.
    Ein erfolgreicher Nachschlag schreibt Dateiname, Hash und Maße in die Zeile.
    """
    if format_zeile is None:
        return None

    pfad = _lokale_datei(format_zeile.filename, datenpfad)
    if pfad:
        return pfad

    if not format_zeile.url:
        log.info(f'meme_katalog: Format {format_zeile.id} hat weder Datei noch URL')
        return None

    try:
        rohdaten, _endgueltig, _typ = _holen(format_zeile.url)
        kennung = format_zeile.extern_id or f'id{format_zeile.id}'
        dateiname, hashwert, breite, hoehe = _bild_ablegen(
            rohdaten, formate_ordner(datenpfad),
            (format_zeile.quelle or 'format'), kennung)
    except KatalogFehler as ex:
        log.warning(f'meme_katalog: Bild zu Format {format_zeile.id} nicht geholt ({ex})')
        return None

    try:
        format_zeile.filename = dateiname
        format_zeile.phash = hashwert
        format_zeile.width = breite or format_zeile.width
        format_zeile.height = hoehe or format_zeile.height
        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        log.warning(f'meme_katalog: Format {format_zeile.id} nicht gespeichert ({ex})')

    return os.path.join(formate_ordner(datenpfad), dateiname)
