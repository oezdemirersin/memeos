# -*- coding: utf-8 -*-
"""Muster: ganze Vorlagen auf einen Klick.

Ein Baustein ist ein Stück (eine Überschrift, ein Wasserzeichen). Ein Muster ist eine
fertige Vorlage: alle Elemente, an den richtigen Stellen, mit Rollen statt fester Farben
und mit den Stadt-Variablen, die das Format braucht. Aus „Vorlage anlegen und dann zwanzig
Regler stellen“ wird damit „Muster wählen, Texte tauschen“.

Warum hier und nicht im Browser: Die Vorlagen-Prüfung, der Editor und das Bedienteil
sollen dieselben Muster sehen. Steht die Tabelle im Browser, kennt der Server sie nicht.

Aufbau eines Musters
    key          eindeutiger Schlüssel
    name         Klarname für die Auswahl
    info         ein Satz: wofür ist das gut
    braucht_bild True, wenn das Format ohne Hintergrundfoto sinnlos ist
    grund        Leinwandfarbe, falls das Muster ohne Foto lebt (sonst None)
    format       'feed' (1080x1350), 'quadrat' (1080x1080) oder 'story' (1080x1920)
    variablen    die Felder, die das Muster füllt – mit Beispiel für die Vorschau
    els          die Elemente in Anteilen der Leinwand (0..1), gleiche Sprache wie
                 die Bausteine im Editor: xp/yp/wp/hp, role, varname, fallback, …

Die Anteile werden erst in muster_config() zu Pixeln – ein Muster passt dadurch auf jedes
Format. Farben stehen als stil:… drin, nie als Hexwert: sonst sähe jede Stadt gleich aus.
"""

import copy

# Stadtwissen, das die Muster benutzen. Die Schlüssel stammen aus
# models.KNOWLEDGE_CATEGORIES; hier steht nur, was für die Beispielvorschau nötig ist.
BEISPIELE = {
    'city_name':     'Musterstadt',
    'city_handle':   '@musterstadt',
    'city_state':    'Hessen',
    'problem_place': 'am Hauptbahnhof',
    'landmark':      'dem alten Wasserturm',
    'food_spot':     'der Dönerbude am Markt',
    'traffic_spot':  'der Baustelle am Ring',
    'klischee':      'alle fahren Fahrrad',
    'local_meme':    'die kaputte Rolltreppe',
    'rivalitaet':    'Nachbarstadt',
    'dialect':       'Ei gude',
    'event':         'dem Stadtfest',
    'youth_spot':    'dem Skatepark',
}

# Leinwandmaße je Format
FORMATE = {
    'feed':    (1080, 1350),
    'quadrat': (1080, 1080),
    'story':   (1080, 1920),
}


def _var(key, label, beispiel=None):
    return {'key': key, 'label': label, 'beispiel': beispiel or BEISPIELE.get(key, '')}


MUSTER = [
    # ── 1. Der Klassiker ──────────────────────────────────────────────────────
    {
        'key': 'oben_unten',
        'name': 'Oben und unten',
        'info': 'Der Klassiker: eine Zeile über dem Bild, die Pointe darunter.',
        'braucht_bild': True, 'grund': None, 'format': 'feed',
        'variablen': [_var('oben', 'Obere Zeile', 'Wenn du sagst, du kommst gleich'),
                      _var('unten', 'Untere Zeile', 'und dann noch zweimal duschst')],
        'els': [
            {'type': 'text', 'role': 'memezeile', 'varname': 'oben',
             'fallback': 'Obere Zeile', 'xp': 0.05, 'yp': 0.04, 'wp': 0.90, 'hp': 0.17,
             'align': 'center', 'valign': 'top'},
            {'type': 'text', 'role': 'memezeile', 'varname': 'unten',
             'fallback': 'Untere Zeile', 'xp': 0.05, 'yp': 0.76, 'wp': 0.90, 'hp': 0.17,
             'align': 'center', 'valign': 'bottom'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
    # ── 2. POV ────────────────────────────────────────────────────────────────
    {
        'key': 'pov',
        'name': 'POV',
        'info': '„POV: Du wohnst in …“ – die Stadt steht schon drin, die Pointe kommt unten.',
        'braucht_bild': True, 'grund': None, 'format': 'feed',
        'variablen': [_var('city_name', 'Stadtname'),
                      _var('pointe', 'Pointe unten', 'und der Bus kommt trotzdem nicht')],
        'els': [
            {'type': 'text', 'role': 'ueberschrift', 'text': 'POV: Du wohnst in {city_name}',
             'xp': 0.06, 'yp': 0.05, 'wp': 0.88, 'hp': 0.16,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'pointe', 'varname': 'pointe',
             'fallback': 'und der Bus kommt trotzdem nicht',
             'xp': 0.06, 'yp': 0.74, 'wp': 0.88, 'hp': 0.19,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
    # ── 3. Niemand ────────────────────────────────────────────────────────────
    {
        'key': 'niemand',
        'name': 'Niemand / Absolut niemand',
        'info': 'Die Dreier-Anlaufform: Niemand, absolut niemand, dann die Stadt.',
        'braucht_bild': True, 'grund': None, 'format': 'feed',
        'variablen': [_var('city_name', 'Stadtname'),
                      _var('pointe', 'Was die Stadt sagt', 'Baustelle bis 2031')],
        'els': [
            {'type': 'text', 'role': 'memezeile', 'text': 'Niemand:',
             'xp': 0.06, 'yp': 0.05, 'wp': 0.88, 'hp': 0.07,
             'align': 'left', 'valign': 'middle'},
            {'type': 'text', 'role': 'memezeile', 'text': 'Absolut niemand:',
             'xp': 0.06, 'yp': 0.13, 'wp': 0.88, 'hp': 0.07,
             'align': 'left', 'valign': 'middle'},
            {'type': 'text', 'role': 'ort', 'varname': 'city_name', 'fallback': 'Stadt',
             'xp': 0.06, 'yp': 0.22, 'wp': 0.88, 'hp': 0.09,
             'align': 'left', 'valign': 'middle'},
            {'type': 'text', 'role': 'pointe', 'varname': 'pointe',
             'fallback': 'Baustelle bis 2031',
             'xp': 0.06, 'yp': 0.74, 'wp': 0.88, 'hp': 0.19,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
    # ── 4. Zwei Bilder ────────────────────────────────────────────────────────
    {
        'key': 'zwei_bilder',
        'name': 'Zwei Bilder',
        'info': 'Vergleich: oben das eine, unten das andere – je mit Beschriftung.',
        'braucht_bild': False, 'grund': 'stil:dark', 'format': 'feed',
        'variablen': [_var('bild_oben', 'Bild oben (Variable oder Stadtbild)', ''),
                      _var('bild_unten', 'Bild unten (Variable oder Stadtbild)', ''),
                      _var('label_oben', 'Beschriftung oben', 'Was du erwartest'),
                      _var('label_unten', 'Beschriftung unten', 'Was du bekommst')],
        'els': [
            {'type': 'image', 'varname': 'bild_oben',
             'xp': 0, 'yp': 0, 'wp': 1, 'hp': 0.5},
            {'type': 'image', 'varname': 'bild_unten',
             'xp': 0, 'yp': 0.5, 'wp': 1, 'hp': 0.5},
            {'type': 'text', 'role': 'memezeile', 'varname': 'label_oben',
             'fallback': 'Was du erwartest', 'xp': 0.05, 'yp': 0.36, 'wp': 0.90, 'hp': 0.11,
             'align': 'center', 'valign': 'bottom'},
            {'type': 'text', 'role': 'memezeile', 'varname': 'label_unten',
             'fallback': 'Was du bekommst', 'xp': 0.05, 'yp': 0.86, 'wp': 0.90, 'hp': 0.11,
             'align': 'center', 'valign': 'bottom'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
    # ── 5. Zitatkarte ─────────────────────────────────────────────────────────
    {
        'key': 'zitat',
        'name': 'Zitatkarte',
        'info': 'Ohne Foto: eine Aussage groß auf der Hauptfarbe, darunter die Stadt.',
        'braucht_bild': False, 'grund': 'stil:primary', 'format': 'quadrat',
        'variablen': [_var('spruch', 'Die Aussage', 'Hier kennt jeder jeden. Leider.'),
                      _var('city_name', 'Stadtname')],
        'els': [
            {'type': 'text', 'role': 'ueberschrift', 'varname': 'spruch',
             'fallback': 'Hier kennt jeder jeden. Leider.',
             'xp': 0.08, 'yp': 0.16, 'wp': 0.84, 'hp': 0.46,
             'align': 'center', 'valign': 'middle'},
            # Die Karte steht auf der Hauptfarbe. Die Ortszeile darf hier NICHT die
            # Rollenfarbe (ebenfalls Hauptfarbe) behalten – sonst Farbe auf Farbe.
            {'type': 'text', 'role': 'ort', 'color': 'stil:auto',
             'varname': 'city_name', 'fallback': 'Stadt',
             'xp': 0.08, 'yp': 0.71, 'wp': 0.84, 'hp': 0.10,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
    # ── 6. Liste ──────────────────────────────────────────────────────────────
    {
        'key': 'liste',
        'name': 'Dreier-Liste',
        'info': 'Überschrift und drei Punkte – gut für „Diese drei Dinge …“.',
        'braucht_bild': False, 'grund': 'stil:dark', 'format': 'feed',
        'variablen': [_var('city_name', 'Stadtname'),
                      _var('punkt1', 'Punkt 1', 'Der Ring ist immer dicht'),
                      _var('punkt2', 'Punkt 2', 'Der Bahnhof ist immer laut'),
                      _var('punkt3', 'Punkt 3', 'Die Rolltreppe ist immer kaputt')],
        'els': [
            {'type': 'text', 'role': 'ueberschrift',
             'text': 'Drei Dinge, die es nur in {city_name} gibt',
             'xp': 0.07, 'yp': 0.08, 'wp': 0.86, 'hp': 0.18,
             'align': 'center', 'valign': 'middle'},
            {'type': 'shape', 'shape': 'rect', 'fill': 'stil:primary',
             'xp': 0.07, 'yp': 0.34, 'wp': 0.04, 'hp': 0.04},
            {'type': 'text', 'role': 'unterzeile', 'varname': 'punkt1',
             'fallback': 'Der Ring ist immer dicht',
             'xp': 0.15, 'yp': 0.33, 'wp': 0.78, 'hp': 0.07,
             'align': 'left', 'valign': 'middle'},
            {'type': 'shape', 'shape': 'rect', 'fill': 'stil:primary',
             'xp': 0.07, 'yp': 0.47, 'wp': 0.04, 'hp': 0.04},
            {'type': 'text', 'role': 'unterzeile', 'varname': 'punkt2',
             'fallback': 'Der Bahnhof ist immer laut',
             'xp': 0.15, 'yp': 0.46, 'wp': 0.78, 'hp': 0.07,
             'align': 'left', 'valign': 'middle'},
            {'type': 'shape', 'shape': 'rect', 'fill': 'stil:primary',
             'xp': 0.07, 'yp': 0.60, 'wp': 0.04, 'hp': 0.04},
            {'type': 'text', 'role': 'unterzeile', 'varname': 'punkt3',
             'fallback': 'Die Rolltreppe ist immer kaputt',
             'xp': 0.15, 'yp': 0.59, 'wp': 0.78, 'hp': 0.07,
             'align': 'left', 'valign': 'middle'},
            {'type': 'text', 'role': 'ort', 'varname': 'city_name', 'fallback': 'Stadt',
             'xp': 0.07, 'yp': 0.80, 'wp': 0.86, 'hp': 0.09,
             'align': 'left', 'valign': 'middle'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
    # ── 7. Wenn / dann ────────────────────────────────────────────────────────
    {
        'key': 'wenn_dann',
        'name': 'Wenn – dann',
        'info': 'Aufbau und Auflösung auf zwei Balken über dem Foto.',
        'braucht_bild': True, 'grund': None, 'format': 'feed',
        'variablen': [_var('wenn', 'Der Aufbau', 'Wenn du in {city_name} pünktlich sein willst'),
                      _var('dann', 'Die Auflösung', 'aber der Ring dich anders sieht')],
        'els': [
            {'type': 'shape', 'shape': 'rect', 'fill': 'stil:dark',
             'xp': 0, 'yp': 0.04, 'wp': 1, 'hp': 0.15, 'opacity': 0.82},
            {'type': 'text', 'role': 'unterzeile', 'color': 'stil:light', 'varname': 'wenn',
             'fallback': 'Wenn du pünktlich sein willst',
             'xp': 0.06, 'yp': 0.05, 'wp': 0.88, 'hp': 0.13,
             'align': 'center', 'valign': 'middle'},
            {'type': 'shape', 'shape': 'rect', 'fill': 'stil:primary',
             'xp': 0, 'yp': 0.76, 'wp': 1, 'hp': 0.18},
            # 'stil:auto' liest den Balken, der eben gezeichnet wurde: bei heller Hauptfarbe
            # wird die Schrift dunkel, bei dunkler hell.
            {'type': 'text', 'role': 'pointe', 'color': 'stil:auto', 'varname': 'dann',
             'fallback': 'aber der Ring dich anders sieht',
             'xp': 0.06, 'yp': 0.77, 'wp': 0.88, 'hp': 0.16,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
    # ── 8. Duell ──────────────────────────────────────────────────────────────
    {
        'key': 'duell',
        'name': 'Duell',
        'info': 'Die Stadt gegen die Nachbarstadt – zieht das Stadtwissen „Rivalität“.',
        'braucht_bild': True, 'grund': None, 'format': 'quadrat',
        'variablen': [_var('city_name', 'Stadtname'), _var('rivalitaet', 'Rivalin'),
                      _var('pointe', 'Der Seitenhieb', 'ratet mal, wer gewinnt')],
        'els': [
            {'type': 'text', 'role': 'ort', 'varname': 'city_name', 'fallback': 'Stadt',
             'xp': 0.04, 'yp': 0.08, 'wp': 0.40, 'hp': 0.12,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'pointe', 'text': 'vs',
             'xp': 0.44, 'yp': 0.09, 'wp': 0.12, 'hp': 0.10,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'memezeile', 'varname': 'rivalitaet',
             'fallback': 'Nachbarstadt', 'xp': 0.56, 'yp': 0.08, 'wp': 0.40, 'hp': 0.12,
             'align': 'center', 'valign': 'middle'},
            {'type': 'text', 'role': 'memezeile', 'varname': 'pointe',
             'fallback': 'ratet mal, wer gewinnt',
             'xp': 0.06, 'yp': 0.80, 'wp': 0.88, 'hp': 0.13,
             'align': 'center', 'valign': 'bottom'},
            {'type': 'text', 'role': 'wasserzeichen', 'varname': 'city_handle',
             'fallback': '@stadtseite', 'ecke': True, 'wp': 0.44, 'hp': 0.045},
        ],
    },
]

_NACH_KEY = {m['key']: m for m in MUSTER}

# Ecke des Wasserzeichens -> Anteilposition. Gleiche Rechnung wie eckeProzent() im Editor.
_RAND = 0.04


def _ecke_anteile(ecke, wp, hp):
    if ecke == 'unten_links':
        return _RAND, 1 - _RAND - hp, 'left'
    if ecke == 'oben_rechts':
        return 1 - _RAND - wp, _RAND, 'right'
    return 1 - _RAND - wp, 1 - _RAND - hp, 'right'      # unten_rechts


def muster_liste():
    """Der Katalog für die Auswahl – ohne die Elementliste, die kommt beim Anwenden."""
    return [{'key': m['key'], 'name': m['name'], 'info': m['info'],
             'braucht_bild': bool(m['braucht_bild']), 'grund': m['grund'],
             'format': m['format'],
             'masse': {'width': FORMATE[m['format']][0], 'height': FORMATE[m['format']][1]},
             'variablen': [dict(v) for v in m['variablen']],
             'elemente': len(m['els'])}
            for m in MUSTER]


def muster_holen(key):
    """Ein Muster oder None."""
    m = _NACH_KEY.get(str(key or '').strip().lower())
    return copy.deepcopy(m) if m else None


def muster_masse(key):
    """(breite, hoehe) des Musters, oder None."""
    m = _NACH_KEY.get(str(key or '').strip().lower())
    return FORMATE[m['format']] if m else None


def muster_elemente(key, breite, hoehe, wasserzeichen_ecke='unten_rechts', praefix='m'):
    """Die Elemente eines Musters in Pixeln. Leere Liste, wenn es den Schlüssel nicht gibt.

    wasserzeichen_ecke kommt aus dem Stil der Stadt; 'keins' lässt das Wasserzeichen weg.
    praefix wird für die Element-Ids benutzt (m1, m2, …)."""
    m = _NACH_KEY.get(str(key or '').strip().lower())
    if not m:
        return []
    W = max(1, int(breite or 0))
    H = max(1, int(hoehe or 0))
    ecke = str(wasserzeichen_ecke or 'unten_rechts').strip().lower()
    out = []
    for spec in m['els']:
        xp, yp = float(spec.get('xp', 0)), float(spec.get('yp', 0))
        wp, hp = float(spec.get('wp', 0.5)), float(spec.get('hp', 0.1))
        align = spec.get('align', 'left')
        if spec.get('ecke'):
            if ecke == 'keins':
                continue
            xp, yp, align = _ecke_anteile(ecke, wp, hp)
        el = {'id': f'{praefix}{len(out) + 1}',
              'type': spec.get('type', 'text'),
              'x': int(round(xp * W)), 'y': int(round(yp * H)),
              'width': int(round(wp * W)), 'height': int(round(hp * H))}
        for feld, ziel in (('role', 'role'), ('varname', 'var'), ('fallback', 'fallback'),
                           ('text', 'text'), ('shape', 'shape'), ('fill', 'fill'),
                           ('color', 'color'), ('opacity', 'opacity')):
            if spec.get(feld) is not None:
                el[ziel] = spec[feld]
        if el['type'] != 'shape':
            el['align'] = align
            el['valign'] = spec.get('valign', 'middle')
        out.append(el)
    return out


def muster_config(key, breite=None, hoehe=None, wasserzeichen_ecke='unten_rechts'):
    """Fertige Config eines Musters. None, wenn es den Schlüssel nicht gibt.

    Ohne Maße gilt das Format des Musters. Ein Muster ohne Foto bringt seine Leinwandfarbe
    als stil:… mit – die Stadt entscheidet dann, welche Farbe das wird."""
    m = _NACH_KEY.get(str(key or '').strip().lower())
    if not m:
        return None
    vor_b, vor_h = FORMATE[m['format']]
    W = int(breite or vor_b)
    H = int(hoehe or vor_h)
    canvas = {'width': W, 'height': H}
    if m['grund']:
        canvas['background'] = m['grund']
    return {'canvas': canvas,
            'elements': muster_elemente(key, W, H, wasserzeichen_ecke)}


def muster_variablen(key):
    """Die Variablennamen, die ein Muster füllt – für required_vars."""
    m = _NACH_KEY.get(str(key or '').strip().lower())
    if not m:
        return []
    namen = []
    for spec in m['els']:
        v = spec.get('varname')
        if v and v not in namen:
            namen.append(v)
    for spec in m['els']:
        for stueck in str(spec.get('text') or '').split('{')[1:]:
            v = stueck.split('}')[0].strip()
            if v and v not in namen:
                namen.append(v)
    return namen
