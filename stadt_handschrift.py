# -*- coding: utf-8 -*-
"""Aus der Markenfarbe einer Stadt eine vollständige Handschrift ableiten.

Eine Stadtseite hat in aller Regel schon eine Farbe – die, in der ihr Logo und ihre
Beiträge stehen. Daraus lässt sich der Rest einer Handschrift errechnen, statt für jede
neue Stadt dreizehn Regler von Hand zu stellen. Bei hundert Städten ist das der
Unterschied zwischen einem Knopf und einem Nachmittag.

Zwei Dinge macht dieses Modul:

1. `palette(marke, logo)` – aus einer hellen und (optional) einer dunklen Markenfarbe die
   fünf Farben, die MemeOS braucht. Die Markenfarbe selbst bleibt unangetastet: sie IST
   die Identität der Seite, nur alles um sie herum wird gerechnet.

2. `anmutungen_verteilen(...)` – Schriftpaar, Schreibweise, Kasten und Kontur so auf die
   Städte verteilen, dass zwei Seiten, die dieselben Leute im Feed erreichen, nie gleich
   aussehen. Entfernung entscheidet, nicht die Reihenfolge in der Liste.

Alle Farben kommen als '#rrggbb' heraus. Kontrastwerte folgen der WCAG-Formel, dieselbe
wie in memeos_render.kontrast – hier noch einmal eigenständig, damit das Modul ohne den
Renderer benutzbar bleibt (Skripte, Migrationen, Tests).
"""

import colorsys
from math import radians, sin, cos, asin, sqrt

# Ab hier gilt eine Pointe als vom dunklen Grund unterscheidbar. Darunter verschmilzt sie
# mit der dunklen Kontur, die ihre Rolle ihr mitgibt, und wird zum Klumpen.
POINTE_MIN_KONTRAST = 2.8

# Schriftpaar + Anmutung. Jede Zeile ist ein eigener Auftritt; mehr Schriften in der
# Bibliothek heißt, dass hier mehr Zeilen möglich werden (siehe schriftpaare_moeglich).
ANMUTUNGEN = [
    # Schmal und laut – der Boulevard-Ton.
    {'headline': 'anton',              'body': 'poppins_semibold',    'schreibweise': 'gross',
     'kasten': 'voll',    'radius': 0,  'kontur': 'keine',   'schatten': True},
    {'headline': 'bebas_neue',         'body': 'bold',                'schreibweise': 'gross',
     'kasten': 'keiner',  'radius': 0,  'kontur': 'dunkel',  'schatten': False},
    {'headline': 'oswald_bold',        'body': 'poppins_semibold',    'schreibweise': 'gross',
     'kasten': 'umriss',  'radius': 6,  'kontur': 'palette', 'schatten': True},
    # Fett und breit – steht auch auf unruhigen Fotos.
    {'headline': 'archivo_black',      'body': 'poppins_semibold',    'schreibweise': 'normal',
     'kasten': 'weich',   'radius': 18, 'kontur': 'keine',   'schatten': True},
    {'headline': 'poppins_black',      'body': 'poppins_semibold',    'schreibweise': 'normal',
     'kasten': 'voll',    'radius': 12, 'kontur': 'keine',   'schatten': False},
    {'headline': 'passion_one_black',  'body': 'bold',                'schreibweise': 'gross',
     'kasten': 'keiner',  'radius': 0,  'kontur': 'dunkel',  'schatten': True},
    # Rund und freundlich.
    {'headline': 'titan_one',          'body': 'arial_rounded_bold',  'schreibweise': 'normal',
     'kasten': 'weich',   'radius': 28, 'kontur': 'keine',   'schatten': False},
    {'headline': 'lilita_one',         'body': 'poppins_semibold',    'schreibweise': 'normal',
     'kasten': 'voll',    'radius': 22, 'kontur': 'keine',   'schatten': True},
    {'headline': 'arial_rounded_bold', 'body': 'arial_rounded_bold',  'schreibweise': 'normal',
     'kasten': 'weich',   'radius': 26, 'kontur': 'keine',   'schatten': False},
    # Comic und handgemacht – die laute Ecke.
    {'headline': 'luckiest_guy',       'body': 'poppins_semibold',    'schreibweise': 'normal',
     'kasten': 'keiner',  'radius': 0,  'kontur': 'dunkel',  'schatten': True},
    {'headline': 'bangers',            'body': 'bold',                'schreibweise': 'gross',
     'kasten': 'keiner',  'radius': 0,  'kontur': 'dunkel',  'schatten': False},
    {'headline': 'permanent_marker',   'body': 'arial_rounded_bold',  'schreibweise': 'normal',
     'kasten': 'weich',   'radius': 14, 'kontur': 'keine',   'schatten': True},
    {'headline': 'kalam_bold',         'body': 'poppins_semibold',    'schreibweise': 'normal',
     'kasten': 'voll',    'radius': 16, 'kontur': 'keine',   'schatten': False},
    # Kantig und technisch.
    {'headline': 'alfa_slab_one',      'body': 'bold',                'schreibweise': 'normal',
     'kasten': 'umriss',  'radius': 4,  'kontur': 'dunkel',  'schatten': True},
    {'headline': 'rubik_mono_one',     'body': 'poppins_semibold',    'schreibweise': 'normal',
     'kasten': 'keiner',  'radius': 0,  'kontur': 'palette', 'schatten': False},
    {'headline': 'anton',              'body': 'anton',               'schreibweise': 'gross',
     'kasten': 'keiner',  'radius': 0,  'kontur': 'dunkel',  'schatten': False},
    # Die drei eingebauten Schriften allein – diese Anmutungen tragen auch eine Anlage,
    # in der die Schriftbibliothek noch nicht nachgeladen wurde.
    {'headline': 'bold',               'body': 'bold',                'schreibweise': 'normal',
     'kasten': 'weich',   'radius': 20, 'kontur': 'keine',   'schatten': True},
    {'headline': 'anton',              'body': 'bold',                'schreibweise': 'gross',
     'kasten': 'umriss',  'radius': 10, 'kontur': 'palette', 'schatten': True},
    {'headline': 'anton',              'body': 'arial_rounded_bold',  'schreibweise': 'gross',
     'kasten': 'voll',    'radius': 14, 'kontur': 'keine',   'schatten': False},
    {'headline': 'bold',               'body': 'arial_rounded_bold',  'schreibweise': 'normal',
     'kasten': 'keiner',  'radius': 0,  'kontur': 'dunkel',  'schatten': False},
]

# Die Ecke wandert mit, damit nicht jede Seite ihr Wasserzeichen an derselben Stelle hat.
ECKEN = ('unten_rechts', 'unten_links', 'unten_rechts', 'oben_rechts')

# Ab dieser Entfernung gelten zwei Städte als „sieht nicht dieselbe Zielgruppe".
NACHBAR_KM = 90


# ═══════════════════════════════════════════════════════════════════════════════
# Farbe
# ═══════════════════════════════════════════════════════════════════════════════

def hex_rgb(wert):
    """'#rrggbb' oder '#rgb' -> (r, g, b). Unlesbares ergibt Schwarz."""
    h = str(wert or '').strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) < 6:
        return (0, 0, 0)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def rgb_hex(rgb):
    return '#%02x%02x%02x' % tuple(max(0, min(255, int(round(v)))) for v in tuple(rgb)[:3])


def _hls(wert):
    r, g, b = (v / 255 for v in hex_rgb(wert))
    return colorsys.rgb_to_hls(r, g, b)


def _aus_hls(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return rgb_hex((r * 255, g * 255, b * 255))


def _linear(kanal):
    k = kanal / 255.0
    return k / 12.92 if k <= 0.04045 else ((k + 0.055) / 1.055) ** 2.4


def leuchtdichte(wert):
    """Relative Leuchtdichte 0..1 (WCAG)."""
    r, g, b = hex_rgb(wert)
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def kontrast(a, b):
    """Kontrastverhältnis zweier Farben, 1.0 .. 21.0."""
    la, lb = leuchtdichte(a), leuchtdichte(b)
    hell, dunkel = (la, lb) if la >= lb else (lb, la)
    return (hell + 0.05) / (dunkel + 0.05)


def _farbe_gueltig(wert):
    h = str(wert or '').strip()
    if not h.startswith('#'):
        return False
    rest = h[1:]
    if len(rest) not in (3, 6):
        return False
    try:
        int(rest, 16)
        return True
    except ValueError:
        return False


def palette(marke, logo=None):
    """Die fünf Farben einer Handschrift aus ein bis zwei Markenfarben.

    marke: die helle Markenfarbe (wird zu 'primary' und bleibt unverändert)
    logo:  die dunkle Fassung, falls es eine gibt (wird zu 'secondary');
           ohne sie wird sie aus der Markenfarbe abgedunkelt

    Gibt {} zurück, wenn `marke` keine lesbare Farbe ist – der Aufrufer entscheidet dann,
    ob er ein fertiges Paket nimmt oder nichts tut."""
    if not _farbe_gueltig(marke):
        return {}
    primary = str(marke).strip()
    h_m, l_m, s_m = _hls(primary)

    if _farbe_gueltig(logo):
        secondary = str(logo).strip()
    else:
        # Keine zweite Markenfarbe: dieselbe Farbe, deutlich tiefer und etwas satter.
        secondary = _aus_hls(h_m, max(0.16, l_m * 0.45), min(0.95, s_m + 0.12))
    h_s, l_s, s_s = _hls(secondary)

    # 'dark' trägt helle Schrift und steht als Leinwandgrund. Tief genug für Kontrast,
    # aber im Farbton der Marke – sonst sieht jede Stadt nach Standard-Schwarz aus.
    dark = _aus_hls(h_s, 0.055, min(0.62, max(0.25, s_s)))

    # 'light' ist fast weiß mit einem Hauch der Marke.
    light = _aus_hls(h_m, 0.965, min(0.45, max(0.12, s_m)))

    # Die Pointe steht in 'accent' und bekommt von ihrer Rolle eine dunkle Kontur mit –
    # gegen den Untergrund trägt also die Kontur, nicht die Farbe. Die Farbe selbst muss
    # knallen: in den eingebauten Paketen ist die Pointe immer eine kräftige Farbe auf
    # ÄHNLICHER Helligkeit wie die Hauptfarbe, nur im Gegenton (boulevard #ffd400/#e11d48,
    # sonne #f59e0b/#0ea5e9). Genau so wird sie hier gebaut, nicht kontrastoptimiert.
    sat = max(0.72, min(0.95, s_m + 0.08))
    hell = max(0.42, min(0.60, l_m))
    accent, bester_abstand = None, -1.0
    for versatz in (0.50, 0.46, 0.54, 0.42, 0.58):
        kandidat = _aus_hls(h_m + versatz, hell, sat)
        if kontrast(kandidat, dark) < POINTE_MIN_KONTRAST:
            continue
        h_k = _hls(kandidat)[0]
        abstand = abs(((h_k - h_m) + 0.5) % 1.0 - 0.5)
        if abstand > bester_abstand:
            accent, bester_abstand = kandidat, abstand
    if accent is None:
        # Dunkle Markenfarbe: der Gegenton muss eine Stufe heller werden, sonst
        # verschwindet er im dunklen Grund.
        accent = _aus_hls(h_m + 0.5, min(0.68, hell + 0.18), sat)

    return {'primary': primary, 'secondary': secondary,
            'accent': accent, 'dark': dark, 'light': light}


# ═══════════════════════════════════════════════════════════════════════════════
# Verteilung
# ═══════════════════════════════════════════════════════════════════════════════

def entfernung_km(a, b):
    """Luftlinie zwischen (lat, lon) und (lat, lon). Ohne Koordinaten: sehr weit."""
    try:
        if None in tuple(a) or None in tuple(b):
            return 1e9
        la1, lo1, la2, lo2 = map(radians, [float(a[0]), float(a[1]), float(b[0]), float(b[1])])
    except (TypeError, ValueError):
        return 1e9
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def anmutungen_verteilen(staedte, nachbar_km=NACHBAR_KM, tabelle=None):
    """staedte: [(id, name, lat, lon)] -> {id: Index in ANMUTUNGEN}

    Zwei Seiten, die dieselben Leute erreichen, dürfen nicht gleich aussehen. Jede Stadt
    nimmt deshalb die erste Anmutung, die keiner ihrer Nachbarn schon hat. Ist in einer
    dichten Gegend keine mehr frei, wird die genommen, deren nächster Träger am weitesten
    weg ist – besser eine Wiederholung in achtzig Kilometern als eine direkt nebenan.
    Sortiert wird nach Namen, damit dieselbe Eingabe immer dieselbe Zuteilung ergibt."""
    tabelle = tabelle or ANMUTUNGEN
    if not tabelle:
        return {}
    folge = sorted(staedte, key=lambda s: str(s[1]))
    koord = {s[0]: (s[2], s[3]) for s in folge}
    zuteilung = {}
    for i, stadt in enumerate(folge):
        sid = stadt[0]
        nachbarn = [t[0] for t in folge
                    if t[0] in zuteilung
                    and entfernung_km(koord[sid], koord[t[0]]) <= nachbar_km]
        belegt = {zuteilung[n] for n in nachbarn}
        frei = [j for j in range(len(tabelle)) if j not in belegt]
        if frei:
            # Reihum durchstarten, sonst bekämen alle isolierten Städte die Nummer 0.
            zuteilung[sid] = min(frei, key=lambda j: (j - i) % len(tabelle))
        else:
            zuteilung[sid] = max(
                range(len(tabelle)),
                key=lambda j: min([entfernung_km(koord[sid], koord[n])
                                   for n in nachbarn if zuteilung[n] == j] or [1e9]))
    return zuteilung


def handschrift(marke, logo=None, anmutung=0):
    """Eine vollständige Handschrift als Dict – Palette plus Anmutung plus Ecke.

    Die Schlüssel entsprechen den Feldern von models.CityStyle (ohne font_-Präfix:
    'headline'/'body', wie memeos_render sie erwartet). Leeres Dict bei unlesbarer Farbe."""
    farben = palette(marke, logo)
    if not farben:
        return {}
    i = int(anmutung) % len(ANMUTUNGEN)
    ergebnis = dict(farben)
    ergebnis.update(ANMUTUNGEN[i])
    ergebnis['wasserzeichen_ecke'] = ECKEN[i % len(ECKEN)]
    return ergebnis


def schriftpaare_moeglich(schriften):
    """Wie viele Anmutungen sich mit diesen Schriftschlüsseln überhaupt bauen lassen.

    Dient der ehrlichen Auskunft im Bedienteil: mit drei Schriften sind es zwölf, und in
    einer dichten Gegend reicht das irgendwann nicht mehr für lauter verschiedene Städte."""
    vorhanden = {str(s).strip().lower() for s in (schriften or []) if str(s).strip()}
    if not vorhanden:
        return 0
    return sum(1 for a in ANMUTUNGEN
               if a['headline'] in vorhanden and a['body'] in vorhanden)
