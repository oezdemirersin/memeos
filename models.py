from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os
import json
import hashlib
import secrets

db = SQLAlchemy()

# Wird von app.py nach dem Bestimmen des Datenpfads gesetzt (Punkt 13).
# Dient nur der Auflösung lokaler Slide-Dateinamen zu /uploads/… bzw. /renders/….
UPLOAD_DIR = ''

_VIDEO_EXT = ('mp4', 'mov', 'webm')


def slide_kind(url_or_name):
    """'video' für mp4/mov/webm, sonst 'image' (nach Dateiendung, Query-String ignoriert)."""
    base = (url_or_name or '').split('?')[0].lower()
    ext = base.rsplit('.', 1)[-1] if '.' in base else ''
    return 'video' if ext in _VIDEO_EXT else 'image'


def slide_url(name):
    """Dateiname oder URL → ausspielbare URL.
    http(s)-URLs und bereits relative Pfade (/uploads/…, /renders/…) bleiben unverändert;
    ein nackter Dateiname wird /uploads/<f>, wenn er im Upload-Ordner liegt, sonst /renders/<f>."""
    if not name:
        return ''
    if name.startswith('http://') or name.startswith('https://') or name.startswith('/'):
        return name
    base = os.path.basename(name)
    if UPLOAD_DIR and os.path.exists(os.path.join(UPLOAD_DIR, base)):
        return '/uploads/' + base
    return '/renders/' + base


KNOWLEDGE_CATEGORIES = [
    ("problem_place",    "🚨 Problemort / Kriminalität",  "#ef4444"),
    ("landmark",         "🗺️ Wahrzeichen",                "#3b82f6"),
    ("youth_spot",       "👶 Jugendtreff",                "#22c55e"),
    ("food_spot",        "🍔 Essensort",                  "#f59e0b"),
    ("traffic_spot",     "🚗 Verkehrspunkt",              "#8b5cf6"),
    ("school",           "🎓 Schule / Uni",               "#06b6d4"),
    ("event",            "🎉 Event / Tradition",          "#ec4899"),
    ("klischee",         "😏 Klischee",                   "#f97316"),
    ("stadtteil_arm",    "🏚️ Problemviertel",             "#dc2626"),
    ("stadtteil_reich",  "🏘️ Reiches Viertel",            "#16a34a"),
    ("stadtteil_student","📚 Studentenviertel",           "#7c3aed"),
    ("local_meme",       "😂 Lokales Meme / Running Gag", "#0ea5e9"),
    ("sport",            "⚽ Sportverein",                "#84cc16"),
    ("dialect",          "💬 Dialekt / Slang",            "#d97706"),
    ("person",           "👤 Bekannte Person",            "#6366f1"),
    ("rivalitaet",       "⚔️ Stadt-Rivalität",            "#f43f5e"),
    ("witz",             "🤣 Stadt-Witz",                 "#14b8a6"),
    ("viertel",          "📍 Wichtiges Viertel",          "#0891b2"),
    ("sonstiges",        "📝 Sonstiges",                  "#94a3b8"),
]

CATEGORY_MAP = {k: (label, color) for k, label, color in KNOWLEDGE_CATEGORIES}

# Alte Schlüssel der Stadtwissen-Seite / des KI-Sorts → Registry-Schlüssel.
# event, viertel, rivalitaet, witz, person, sonstiges heißen in beiden Welten gleich.
LEGACY_CATEGORY_MAP = {
    'reichenviertel':   'stadtteil_reich',
    'problemviertel':   'stadtteil_arm',
    'kriminalitaet':    'problem_place',
    'running_gag':      'local_meme',
    'slang':            'dialect',
    'sehenswuerdigkeit':'landmark',
}


def normalize_category(cat, default='sonstiges'):
    """Beliebigen Kategorie-String auf einen Registry-Schlüssel abbilden; unbekannt → default."""
    key = (cat or '').strip().lower()
    key = LEGACY_CATEGORY_MAP.get(key, key)
    return key if key in CATEGORY_MAP else default


class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(100), unique=True, nullable=False)
    email         = db.Column(db.String(200), unique=True)
    password_hash = db.Column(db.String(300), nullable=False)
    role          = db.Column(db.String(20), default='admin')
    active        = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    last_login    = db.Column(db.DateTime)

    def set_password(self, password):
        salt = secrets.token_hex(16)
        self.password_hash = salt + ':' + hashlib.sha256((salt + password).encode()).hexdigest()

    def check_password(self, password):
        if ':' not in (self.password_hash or ''):
            return False
        salt, hashed = self.password_hash.split(':', 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == hashed


class City(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(200), nullable=False, unique=True)
    state            = db.Column(db.String(100))
    population       = db.Column(db.Integer)
    instagram_handle = db.Column(db.String(200))
    tiktok_handle    = db.Column(db.String(200))
    accent_color     = db.Column(db.String(20), default='#3b82f6')
    brand_bg         = db.Column(db.String(20), default='#ffffff')
    brand_text_color = db.Column(db.String(20), default='#000000')
    brand_font       = db.Column(db.String(100), default='Arial')
    rss_url          = db.Column(db.String(500))
    lat              = db.Column(db.Float)   # Koordinaten für Wetter-Events (Open-Meteo), scheduler.py
    lon              = db.Column(db.Float)
    notes            = db.Column(db.Text)
    active           = db.Column(db.Boolean, default=True, index=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    knowledge   = db.relationship('CityKnowledge', backref='city', lazy='dynamic',
                                   cascade='all,delete')
    render_jobs = db.relationship('RenderJob', backref='city', lazy='dynamic',
                                   cascade='all,delete')
    news_items  = db.relationship('NewsItem', backref='city', lazy='dynamic',
                                   cascade='all,delete')
    surveys     = db.relationship('ResidentSurvey', backref='city', lazy='dynamic',
                                   cascade='all,delete')

    def knowledge_by_category(self):
        entries = self.knowledge.filter_by(active=True)\
                      .order_by(CityKnowledge.confidence.desc()).all()
        result = {}
        for e in entries:
            result.setdefault(e.category, []).append(e)
        return result

    follower_snapshots = db.relationship('CityFollowerSnapshot', backref='city',
                                          lazy='dynamic', cascade='all,delete')

    def knowledge_count(self):
        return self.knowledge.filter_by(active=True).count()

    def render_count(self):
        return self.render_jobs.filter_by(status='done').count()

    def latest_followers(self):
        snap = self.follower_snapshots.order_by(
            CityFollowerSnapshot.recorded_at.desc()
        ).first()
        return snap.count if snap else None


# ═══════════════════════════════════════════════════════════════════════════════
# STADT-STIL – jede Stadt bekommt eine eigene Handschrift
# ═══════════════════════════════════════════════════════════════════════════════
# Die vier alten Felder der Stadt (accent_color, brand_bg, brand_text_color, brand_font)
# reichen nicht, damit 100 Seiten unterschiedlich aussehen: sie ändern nur die Farbe,
# nicht die Anmutung. CityStyle trägt deshalb eine ganze Palette, ein Schriftpaar und
# die Entscheidungen, die den Charakter ausmachen (Schreibweise, Kastenart, Radius,
# Kontur, Schatten, Ecke des Wasserzeichens).
#
# Gelesen wird der Stil von memeos_render: das Dict aus to_dict() bzw. fuer() geht
# unverändert als `brand` in render() und löst dort 'stil:*' und die Rollen auf.
# Die alten Schlüssel bg/text/accent/font stehen weiterhin darin, 'brand:*' bleibt gültig.

STIL_SCHREIBWEISEN       = ('gross', 'normal')
STIL_KASTEN_ARTEN        = ('keiner', 'voll', 'weich', 'umriss')
STIL_KONTUR_ARTEN        = ('keine', 'dunkel', 'palette')
STIL_WASSERZEICHEN_ECKEN = ('unten_links', 'unten_rechts', 'oben_rechts', 'keins')

# Vorgabe-Handschrift für Städte ohne eigenen Stil.
# ACHTUNG: identisch mit memeos_render.STIL_STANDARD – tests/test_i1_stil.py prüft das.
CITY_STYLE_DEFAULTS = {
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


def _stil_abdunkeln(farbe, faktor=0.62):
    """'#rrggbb' auf faktor seiner Helligkeit ziehen. Unbrauchbar -> Vorgabe."""
    s = str(farbe or '').strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    if len(s) < 6:
        return CITY_STYLE_DEFAULTS['secondary']
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return CITY_STYLE_DEFAULTS['secondary']
    f = max(0.0, min(1.0, float(faktor)))
    return '#%02x%02x%02x' % (int(round(r * f)), int(round(g * f)), int(round(b * f)))


# Acht Handschriften, keine acht Farbschemata: sie unterscheiden sich in der Anmutung,
# nicht nur im Farbton. Wer eine Stadt anlegt, wählt eine davon – und ändert danach
# jeden Einzelwert, wenn er will.
STIL_PAKETE = {
    'boulevard': {
        'name': 'Boulevard',
        'beschreibung': 'Laut und plakativ: Signalgelb, volle Balken hinter jeder Zeile, alles in Großbuchstaben.',
        'primary': '#ffd400', 'secondary': '#a88b00', 'accent': '#e11d48',
        'dark': '#111111', 'light': '#ffffff',
        'font_headline': 'anton', 'font_body': 'bold',
        'schreibweise': 'gross', 'kasten': 'voll', 'radius': 0,
        # Kontur 'keine', weil der volle Balken die Lesbarkeit schon traegt –
        # Balken UND dicke Kontur zusammen wirken schmutzig.
        'kontur': 'keine', 'schatten': True, 'wasserzeichen_ecke': 'unten_rechts',
    },
    'klar': {
        'name': 'Klar',
        'beschreibung': 'Ruhig und aufgeräumt: viel Weiß, weiche Kästen mit runden Ecken, normale Schreibweise.',
        'primary': '#2563eb', 'secondary': '#1e3a8a', 'accent': '#f97316',
        'dark': '#0f172a', 'light': '#f8fafc',
        'font_headline': 'bold', 'font_body': 'bold',
        'schreibweise': 'normal', 'kasten': 'weich', 'radius': 24,
        'kontur': 'keine', 'schatten': False, 'wasserzeichen_ecke': 'unten_rechts',
    },
    'wucht': {
        'name': 'Wucht',
        'beschreibung': 'Kräftig mit dicker dunkler Kontur: der Text steht auch auf dem unruhigsten Foto.',
        'primary': '#ff3b30', 'secondary': '#9e241c', 'accent': '#ffcc00',
        'dark': '#000000', 'light': '#ffffff',
        'font_headline': 'anton', 'font_body': 'anton',
        'schreibweise': 'gross', 'kasten': 'keiner', 'radius': 0,
        'kontur': 'dunkel', 'schatten': True, 'wasserzeichen_ecke': 'unten_links',
    },
    'freundlich': {
        'name': 'Freundlich',
        'beschreibung': 'Hell und warm: runde Schrift, stark gerundete Kästen, gemischte Schreibweise.',
        'primary': '#fb7185', 'secondary': '#9b4653', 'accent': '#22c55e',
        'dark': '#3f3f46', 'light': '#fff7ed',
        'font_headline': 'arial_rounded_bold', 'font_body': 'arial_rounded_bold',
        'schreibweise': 'normal', 'kasten': 'weich', 'radius': 32,
        'kontur': 'keine', 'schatten': False, 'wasserzeichen_ecke': 'unten_rechts',
    },
    'nacht': {
        'name': 'Nacht',
        'beschreibung': 'Dunkler Grund mit Neon-Akzent: Umrisskästen statt Flächen, kühle Farben.',
        'primary': '#22d3ee', 'secondary': '#0e7490', 'accent': '#a3e635',
        'dark': '#0b1020', 'light': '#e2e8f0',
        'font_headline': 'anton', 'font_body': 'bold',
        'schreibweise': 'gross', 'kasten': 'umriss', 'radius': 8,
        'kontur': 'palette', 'schatten': True, 'wasserzeichen_ecke': 'oben_rechts',
    },
    'sachlich': {
        'name': 'Sachlich',
        'beschreibung': 'Nachrichtlich ohne Kästen: strenge Farben, feine dunkle Kontur, normale Schreibweise.',
        'primary': '#0f766e', 'secondary': '#0a4a45', 'accent': '#eab308',
        'dark': '#111827', 'light': '#f1f5f9',
        'font_headline': 'bold', 'font_body': 'bold',
        'schreibweise': 'normal', 'kasten': 'keiner', 'radius': 0,
        'kontur': 'dunkel', 'schatten': False, 'wasserzeichen_ecke': 'unten_rechts',
    },
    'sonne': {
        'name': 'Sonne',
        'beschreibung': 'Sommerlich warm: orange Balken, große Schrift, weicher Schatten.',
        'primary': '#f59e0b', 'secondary': '#94620a', 'accent': '#0ea5e9',
        'dark': '#1c1917', 'light': '#fffbeb',
        'font_headline': 'anton', 'font_body': 'bold',
        'schreibweise': 'gross', 'kasten': 'voll', 'radius': 12,
        'kontur': 'keine', 'schatten': True, 'wasserzeichen_ecke': 'unten_links',
    },
    'beton': {
        'name': 'Beton',
        'beschreibung': 'Nüchtern grau mit einem Farbschlag: Umriss statt Fläche, kaum Rundung.',
        'primary': '#64748b', 'secondary': '#3e4856', 'accent': '#ef4444',
        'dark': '#020617', 'light': '#e5e7eb',
        'font_headline': 'bold', 'font_body': 'bold',
        'schreibweise': 'normal', 'kasten': 'umriss', 'radius': 4,
        'kontur': 'dunkel', 'schatten': False, 'wasserzeichen_ecke': 'unten_rechts',
    },
}

STIL_PAKET_NAMEN = tuple(STIL_PAKETE.keys())


def stil_paket_liste():
    """Für die Oberfläche: [{'key','name','beschreibung', …vollständige Werte…}, …]."""
    out = []
    for key, p in STIL_PAKETE.items():
        eintrag = {'key': key}
        eintrag.update(p)
        out.append(eintrag)
    return out


class CityStyle(db.Model):
    """Genau eine Zeile je Stadt: ihre Handschrift.

    Alle Felder haben Vorgaben – eine Stadt ohne Zeile läuft mit fuer() trotzdem,
    dann wird der Stil aus den alten brand-Feldern abgeleitet."""
    __tablename__ = 'city_style'

    id      = db.Column(db.Integer, primary_key=True)
    city_id = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False,
                        unique=True, index=True)

    # Palette
    primary   = db.Column(db.String(20), default=CITY_STYLE_DEFAULTS['primary'])
    secondary = db.Column(db.String(20), default=CITY_STYLE_DEFAULTS['secondary'])
    accent    = db.Column(db.String(20), default=CITY_STYLE_DEFAULTS['accent'])
    dark      = db.Column(db.String(20), default=CITY_STYLE_DEFAULTS['dark'])
    light     = db.Column(db.String(20), default=CITY_STYLE_DEFAULTS['light'])

    # Schriftpaar (Schlüssel aus der Schriftbibliothek, memeos_render.list_fonts)
    font_headline = db.Column(db.String(100), default=CITY_STYLE_DEFAULTS['headline'])
    font_body     = db.Column(db.String(100), default=CITY_STYLE_DEFAULTS['body'])

    # Anmutung
    schreibweise = db.Column(db.String(10), default=CITY_STYLE_DEFAULTS['schreibweise'])
    kasten       = db.Column(db.String(10), default=CITY_STYLE_DEFAULTS['kasten'])
    radius       = db.Column(db.Integer,    default=CITY_STYLE_DEFAULTS['radius'])
    kontur       = db.Column(db.String(10), default=CITY_STYLE_DEFAULTS['kontur'])
    schatten     = db.Column(db.Boolean,    default=CITY_STYLE_DEFAULTS['schatten'])

    paket              = db.Column(db.String(50),  default='')
    wasserzeichen_ecke = db.Column(db.String(20),
                                   default=CITY_STYLE_DEFAULTS['wasserzeichen_ecke'])

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    city = db.relationship('City', backref=db.backref('style', uselist=False,
                                                      cascade='all,delete-orphan'))

    # ── Werte lesen ──────────────────────────────────────────────────────────────
    def _wahl(self, wert, erlaubt, schluessel):
        w = str(wert or '').strip().lower()
        return w if w in erlaubt else CITY_STYLE_DEFAULTS[schluessel]

    def to_dict(self):
        """Vollständiges Stil-Dict – so, wie memeos_render es als `brand` erwartet.

        Enthält absichtlich beides: die Renderer-Schlüssel (headline/body) und die
        Modellnamen (font_headline/font_body), dazu die alten brand-Schlüssel
        bg/text/font, damit 'brand:*' in Bestandsvorlagen weiterläuft."""
        headline = str(self.font_headline or CITY_STYLE_DEFAULTS['headline'])
        body     = str(self.font_body or headline)
        primary  = self.primary or CITY_STYLE_DEFAULTS['primary']
        dark     = self.dark or CITY_STYLE_DEFAULTS['dark']
        light    = self.light or CITY_STYLE_DEFAULTS['light']
        return {
            'city_id':   self.city_id,
            'primary':   primary,
            'secondary': self.secondary or _stil_abdunkeln(primary),
            'accent':    self.accent or primary,
            'dark':      dark,
            'light':     light,
            'headline':  headline,
            'body':      body,
            'font_headline': headline,
            'font_body':     body,
            'schreibweise': self._wahl(self.schreibweise, STIL_SCHREIBWEISEN, 'schreibweise'),
            'kasten':       self._wahl(self.kasten, STIL_KASTEN_ARTEN, 'kasten'),
            'radius':       max(0, min(40, int(self.radius if self.radius is not None
                                               else CITY_STYLE_DEFAULTS['radius']))),
            'kontur':       self._wahl(self.kontur, STIL_KONTUR_ARTEN, 'kontur'),
            'schatten':     bool(self.schatten) if self.schatten is not None
                            else CITY_STYLE_DEFAULTS['schatten'],
            'wasserzeichen_ecke': self._wahl(self.wasserzeichen_ecke,
                                             STIL_WASSERZEICHEN_ECKEN, 'wasserzeichen_ecke'),
            'paket': self.paket or '',
            # Altbestand
            'bg':   light,
            'text': dark,
            'font': headline,
        }

    # ── Stil einer Stadt besorgen ────────────────────────────────────────────────
    @classmethod
    def fuer(cls, city):
        """Der Stil einer Stadt als vollständiges Dict (nie None).

        Hat die Stadt eine Zeile, gilt die. Sonst wird ein Standardstil aus den alten
        brand-Feldern abgeleitet: primary = accent_color, dark = brand_text_color,
        light = brand_bg, secondary = abgedunkeltes primary, accent = primary,
        Schriftpaar = brand_font. Ohne Stadt: reine Vorgaben."""
        zeile = getattr(city, 'style', None) if city is not None else None
        if zeile is not None:
            return zeile.to_dict()

        primary = (getattr(city, 'accent_color', None) or CITY_STYLE_DEFAULTS['primary'])
        dark    = (getattr(city, 'brand_text_color', None) or CITY_STYLE_DEFAULTS['dark'])
        light   = (getattr(city, 'brand_bg', None) or CITY_STYLE_DEFAULTS['light'])
        font    = (getattr(city, 'brand_font', None) or CITY_STYLE_DEFAULTS['headline'])
        return {
            'city_id':   getattr(city, 'id', None),
            'primary':   primary,
            'secondary': _stil_abdunkeln(primary),
            'accent':    primary,
            'dark':      dark,
            'light':     light,
            'headline':  font,
            'body':      font,
            'font_headline': font,
            'font_body':     font,
            'schreibweise': CITY_STYLE_DEFAULTS['schreibweise'],
            'kasten':       CITY_STYLE_DEFAULTS['kasten'],
            'radius':       CITY_STYLE_DEFAULTS['radius'],
            'kontur':       CITY_STYLE_DEFAULTS['kontur'],
            'schatten':     CITY_STYLE_DEFAULTS['schatten'],
            'wasserzeichen_ecke': CITY_STYLE_DEFAULTS['wasserzeichen_ecke'],
            'paket':     '',
            'bg':   light,
            'text': dark,
            'font': font,
        }

    def paket_setzen(self, key):
        """Alle Werte aus einem Stil-Paket übernehmen. Unbekannter Schlüssel -> False."""
        p = STIL_PAKETE.get(str(key or '').strip().lower())
        if not p:
            return False
        for feld in ('primary', 'secondary', 'accent', 'dark', 'light',
                     'font_headline', 'font_body', 'schreibweise', 'kasten',
                     'radius', 'kontur', 'schatten', 'wasserzeichen_ecke'):
            if feld in p:
                setattr(self, feld, p[feld])
        self.paket = str(key).strip().lower()
        return True

    @classmethod
    def aus_paket(cls, city_id, key):
        """Neue (noch nicht gespeicherte) Stil-Zeile aus einem Paket. Unbekannt -> None."""
        zeile = cls(city_id=city_id)
        return zeile if zeile.paket_setzen(key) else None


class CityFollowerSnapshot(db.Model):
    __tablename__ = 'city_follower_snapshot'
    id          = db.Column(db.Integer, primary_key=True)
    city_id     = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    count       = db.Column(db.Integer, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class CityKnowledge(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    city_id        = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    category       = db.Column(db.String(50), nullable=False, index=True)
    name           = db.Column(db.String(200), nullable=False)
    description    = db.Column(db.Text)
    confidence     = db.Column(db.Integer, default=70)   # 0–100
    source         = db.Column(db.String(30), default='ai')  # ai | resident | verified | manual
    source_post_id = db.Column(db.Integer, db.ForeignKey('memo_inspiration_post.id'), nullable=True)
    used_count     = db.Column(db.Integer, default=0)
    last_used_at   = db.Column(db.DateTime)
    cooldown_until = db.Column(db.DateTime)
    active         = db.Column(db.Boolean, default=True, index=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source_post = db.relationship('MemoInspirationPost',
                                  backref=db.backref('knowledge_extracted', lazy='dynamic'),
                                  foreign_keys=[source_post_id])

    @property
    def on_cooldown(self):
        return bool(self.cooldown_until and self.cooldown_until > datetime.utcnow())

    @property
    def category_label(self):
        return CATEGORY_MAP.get(self.category, (self.category, '#64748b'))[0]

    @property
    def category_color(self):
        return CATEGORY_MAP.get(self.category, (self.category, '#64748b'))[1]

    @property
    def source_badge(self):
        return {'ai': 'KI', 'resident': 'Einwohner', 'verified': 'Verifiziert', 'manual': 'Manuell'}.get(self.source, self.source)

    @property
    def verified(self):
        return self.source == 'verified' or self.source == 'manual'

    def to_dict(self):
        cat_label = self.category_label
        return {
            'id':             self.id,
            'city_id':        self.city_id,
            'city_name':      self.city.name if hasattr(self, 'city') and self.city else '',
            'category':       self.category,
            'category_label': cat_label,
            'content':        (self.name or '') + (('\n' + self.description) if self.description else ''),
            'name':           self.name or '',
            'description':    self.description or '',
            'confidence':     self.confidence or 70,
            'source':         self.source or 'ai',
            'source_post_id': self.source_post_id,
            'verified':       self.verified,
            'active':         self.active,
            'created_at':     self.created_at.isoformat() if self.created_at else '',
        }


TEMPLATE_CATEGORIES = [
    # Saisonal
    ('sommer',       'Sommer',         '☀️',  'seasonal'),
    ('winter',       'Winter',         '❄️',  'seasonal'),
    ('weihnachten',  'Weihnachten',    '🎄',  'seasonal'),
    ('ostern',       'Ostern',         '🐣',  'seasonal'),
    ('halloween',    'Halloween',      '🎃',  'seasonal'),
    ('karneval',     'Karneval',       '🎭',  'seasonal'),
    ('silvester',    'Silvester',      '🎆',  'seasonal'),
    ('valentinstag', 'Valentinstag',   '❤️',  'seasonal'),
    ('fruehling',    'Frühling',       '🌸',  'seasonal'),
    ('herbst',       'Herbst',         '🍂',  'seasonal'),
    # Wetter
    ('hitze',        'Hitze',          '🌡️', 'wetter'),
    ('regen',        'Regen',          '🌧️', 'wetter'),
    ('schnee',       'Schnee',         '☃️', 'wetter'),
    ('gewitter',     'Gewitter',       '⛈️', 'wetter'),
    # Themen
    ('uni',          'Uni / Studenten','🎓',  'thema'),
    ('pendler',      'Pendler',        '🚇',  'thema'),
    ('opnv',         'ÖPNV',           '🚌',  'thema'),
    ('baustelle',    'Baustelle',      '🚧',  'thema'),
    ('verkehr',      'Verkehr',        '🚗',  'thema'),
    ('sport',        'Sport',          '⚽',  'thema'),
    ('party',        'Party / Nacht',  '🎉',  'thema'),
    ('stadtleben',   'Stadtleben',     '🏙️', 'thema'),
    ('fussball',     'Fußball',        '⚽',  'thema'),
    # Formate
    ('pov',          'POV',            '📸',  'format'),
    ('ranking',      'Ranking',        '🏆',  'format'),
    ('vergleich',    'Vergleich',      '⚖️', 'format'),
    ('klischee',     'Klischee',       '🎭',  'format'),
    ('news',         'News-Meme',      '📰',  'format'),
    ('viral',        'Viral-Format',   '🔥',  'format'),
    ('allgemein',    'Allgemein',      '📋',  'format'),
]

TEMPLATE_CAT_MAP = {k: (label, emoji, group) for k, label, emoji, group in TEMPLATE_CATEGORIES}

TEMPLATE_GROUPS = [
    ('seasonal', 'Saisonal',  '☀️'),
    ('wetter',   'Wetter',    '🌧️'),
    ('thema',    'Themen',    '🏙️'),
    ('format',   'Formate',   '📋'),
]


class TemplateCategory(db.Model):
    __tablename__ = 'template_category'
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(50), unique=True, nullable=False)
    label      = db.Column(db.String(100), nullable=False)
    emoji      = db.Column(db.String(10), default='📋')
    group      = db.Column(db.String(20), default='format')
    sort_order = db.Column(db.Integer, default=100)
    active     = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {'id': self.id, 'key': self.key, 'label': self.label,
                'emoji': self.emoji, 'group': self.group,
                'sort_order': self.sort_order, 'active': self.active}


class MemeTemplate(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(200), nullable=False)
    description      = db.Column(db.Text)
    canva_template_id= db.Column(db.String(100))         # LEGACY (Autofill entfernt) – wird nicht mehr geschrieben/ausgegeben
    canva_url        = db.Column(db.String(500))         # Direktlink zum Canva-Design (nur Referenz)
    render_type      = db.Column(db.String(20), default='pil')  # pil | manual
    pil_config       = db.Column(db.Text, default='{}')  # JSON-Konfiguration für PIL-Rendering
    required_vars    = db.Column(db.Text, default='[]')  # JSON: ["problem_place", "landmark"]
    canva_field_map  = db.Column(db.Text, default='{}')  # LEGACY (Autofill entfernt) – wird nicht mehr geschrieben/ausgegeben
    tags             = db.Column(db.Text, default='[]')  # JSON: ["pov", "klischee"]
    category         = db.Column(db.String(50), default='allgemein', index=True)
    rating           = db.Column(db.Integer, default=0)  # 0–5 Sterne
    preview_image    = db.Column(db.String(500))         # Dateiname des Hintergrundbilds im Upload-Ordner
    preview_url      = db.Column(db.String(1000))        # Cloudinary-URL des Hintergrundbilds (Deploy-sicher)
    example_text     = db.Column(db.Text)                # Beispiel-Text wie das Template aussieht
    notes            = db.Column(db.Text)                # Interne Notizen
    seasonal_from    = db.Column(db.String(5))           # "12-01"
    seasonal_to      = db.Column(db.String(5))           # "12-31"
    min_population   = db.Column(db.Integer, default=0)
    use_count        = db.Column(db.Integer, default=0)
    active           = db.Column(db.Boolean, default=True, index=True)
    series           = db.Column(db.String(200))   # Serien-Name, z.B. "SpongeBob Edition"
    series_position  = db.Column(db.Integer)       # Position in der Serie (1, 2, 3 …)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    render_jobs = db.relationship('RenderJob', backref='template', lazy='dynamic')

    def get_required_vars(self):
        try: return json.loads(self.required_vars or '[]')
        except: return []

    def get_tags(self):
        try: return json.loads(self.tags or '[]')
        except: return []

    def has_canva(self):
        """Bedeutet seit Wegfall des Autofills nur noch: ein Canva-Link ist hinterlegt."""
        return bool(self.canva_url and self.canva_url.strip())


class RenderJob(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    template_id     = db.Column(db.Integer, db.ForeignKey('meme_template.id'), nullable=False, index=True)
    city_id         = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    status          = db.Column(db.String(20), default='pending', index=True)
    # pending | running | done | failed | review | approved | rejected | sent
    fit_score       = db.Column(db.Integer)          # 0–100 von Claude
    fit_reasoning   = db.Column(db.Text)             # Claude-Begründung
    vars_used       = db.Column(db.Text, default='{}') # JSON: {"problem_place": "Hauptbahnhof"}
    manual_brief    = db.Column(db.Text)             # Text-Brief für manuelle Erstellung
    canva_design_id = db.Column(db.String(200))
    image_url       = db.Column(db.String(500))      # Lokaler Pfad oder externe URL
    image_filename  = db.Column(db.String(300))
    error_message   = db.Column(db.Text)
    review_note     = db.Column(db.Text)
    reviewed_at     = db.Column(db.DateTime)
    sent_to_content_os = db.Column(db.Boolean, default=False)
    sent_at         = db.Column(db.DateTime)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completed_at    = db.Column(db.DateTime)

    def get_vars(self):
        try: return json.loads(self.vars_used or '{}')
        except: return {}

    @property
    def fit_color(self):
        if not self.fit_score: return '#64748b'
        if self.fit_score >= 70: return '#22c55e'
        if self.fit_score >= 40: return '#f59e0b'
        return '#ef4444'

    @property
    def status_label(self):
        return {
            'pending': 'Wartend', 'running': 'Läuft', 'done': 'Fertig',
            'failed': 'Fehler', 'review': 'Review', 'approved': 'Freigegeben',
            'rejected': 'Abgelehnt', 'sent': 'Gesendet'
        }.get(self.status, self.status)


class NewsItem(db.Model):
    id                   = db.Column(db.Integer, primary_key=True)
    city_id              = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    headline             = db.Column(db.String(500), nullable=False)
    url                  = db.Column(db.String(1000))
    source_name          = db.Column(db.String(200))
    published_at         = db.Column(db.DateTime)
    fetched_at           = db.Column(db.DateTime, default=datetime.utcnow)
    meme_score           = db.Column(db.Integer)          # 0–100 Claude-Bewertung
    meme_reasoning       = db.Column(db.Text)
    suggested_template_id= db.Column(db.Integer, db.ForeignKey('meme_template.id'), nullable=True)
    status               = db.Column(db.String(20), default='new', index=True)
    # new | scored | used | skipped
    suggested_template   = db.relationship('MemeTemplate', backref='news_suggestions', foreign_keys=[suggested_template_id])

    @property
    def meme_score_color(self):
        if not self.meme_score: return '#64748b'
        if self.meme_score >= 70: return '#22c55e'
        if self.meme_score >= 40: return '#f59e0b'
        return '#ef4444'


class ResidentSurvey(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    city_id      = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    token        = db.Column(db.String(64), unique=True, nullable=False)
    respondent   = db.Column(db.String(200))
    answers      = db.Column(db.Text, default='{}')   # JSON
    status       = db.Column(db.String(20), default='pending')  # pending | completed | imported
    submitted_at = db.Column(db.DateTime)
    imported_at  = db.Column(db.DateTime)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def get_answers(self):
        try: return json.loads(self.answers or '{}')
        except: return {}


class AppSettings(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = str(value) if value is not None else None
            row.updated_at = datetime.utcnow()
        else:
            row = cls(key=key, value=str(value) if value is not None else None)
            db.session.add(row)
        db.session.commit()


class ExportJob(db.Model):
    """ZIP-Export-Auftrag – persistent statt im Prozess-Speicher (überlebt mehrere Gunicorn-Worker)."""
    __tablename__ = 'export_job'
    id         = db.Column(db.String(36), primary_key=True)
    status     = db.Column(db.String(20), default='pending', index=True)  # pending | building | ready | error
    path       = db.Column(db.String(1000))
    filename   = db.Column(db.String(300))
    post_count = db.Column(db.Integer, default=0)
    error      = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            'job_id':     self.id,
            'status':     self.status,
            'ready':      self.status == 'ready',
            'filename':   self.filename,
            'post_count': self.post_count or 0,
            'error':      self.error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class AppTodo(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    text       = db.Column(db.Text, nullable=False)
    category   = db.Column(db.String(50), default='idee')  # idee | feature | bug | notiz
    done       = db.Column(db.Boolean, default=False)
    priority   = db.Column(db.Integer, default=0)           # 0=normal, 1=hoch
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AiUsageLog(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    feature       = db.Column(db.String(60), nullable=False)
    model         = db.Column(db.String(80), nullable=False)
    input_tokens  = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    # Prompt-Caching: Schreiben in den Cache kostet mehr als normale Eingabe, Lesen deutlich
    # weniger. Beide Mengen werden getrennt erfasst, damit die Ersparnis sichtbar bleibt.
    # nullable + Standard 0, damit Bestandszeilen ohne diese Werte weiterlaufen.
    cache_creation_input_tokens = db.Column(db.Integer, default=0, nullable=True)
    cache_read_input_tokens     = db.Column(db.Integer, default=0, nullable=True)
    provider      = db.Column(db.String(20), default='anthropic', nullable=True)
    cost_eur      = db.Column(db.Float, default=0.0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class CityMarketEntry(db.Model):
    """100 größte deutsche Städte — Markt-Übersicht für Page-Akquise."""
    __tablename__ = 'city_market_entry'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200), nullable=False, unique=True)
    state      = db.Column(db.String(100))
    population = db.Column(db.Integer)
    rank       = db.Column(db.Integer)
    status     = db.Column(db.String(30), default='none', index=True)
    # none | owned | want_to_buy | found_pages
    city_id    = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=True)
    notes      = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    city         = db.relationship('City', backref='market_entry')
    buyable_pages= db.relationship('BuyablePage', backref='market_entry',
                                   lazy='dynamic', cascade='all,delete')

    @property
    def status_color(self):
        return {'owned':'#22c55e','want_to_buy':'#f59e0b',
                'found_pages':'#8b5cf6','none':'#374151'}.get(self.status,'#374151')

    @property
    def status_label(self):
        return {'owned':'Besitzen wir','want_to_buy':'Kaufen?',
                'found_pages':'Seiten gefunden','none':'—'}.get(self.status,'—')


class BuyablePage(db.Model):
    """Kaufbare Instagram-Seite für eine Stadt."""
    __tablename__ = 'buyable_page'
    id               = db.Column(db.Integer, primary_key=True)
    market_entry_id  = db.Column(db.Integer, db.ForeignKey('city_market_entry.id'), nullable=False, index=True)
    instagram_url    = db.Column(db.String(500))
    handle           = db.Column(db.String(200))
    followers        = db.Column(db.Integer)
    price_ask        = db.Column(db.Float)
    contact_status   = db.Column(db.String(30), default='neu')
    # neu | antwortet | aktiv | in_verhandlung | inaktiv | antwortet_nicht | gekauft | abgelehnt
    contact_notes    = db.Column(db.Text)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def contact_color(self):
        return {
            'neu':'#64748b','antwortet':'#22c55e','aktiv':'#3b82f6',
            'in_verhandlung':'#f59e0b','inaktiv':'#374151',
            'antwortet_nicht':'#ef4444','gekauft':'#22c55e','abgelehnt':'#dc2626'
        }.get(self.contact_status,'#64748b')

    @property
    def contact_label(self):
        return {
            'neu':'Neu','antwortet':'Antwortet','aktiv':'Aktiv',
            'in_verhandlung':'In Verhandlung','inaktiv':'Inaktiv',
            'antwortet_nicht':'Antwortet nicht','gekauft':'Gekauft','abgelehnt':'Abgelehnt'
        }.get(self.contact_status, self.contact_status)


class MemoInspirationSource(db.Model):
    """Instagram-Seiten die wir für Meme-Inspiration beobachten."""
    __tablename__ = 'memo_inspiration_source'
    # Derselbe Handle kann auf mehreren Plattformen existieren (@stadt auf Instagram UND TikTok),
    # deshalb ist erst Name + Plattform eindeutig – nicht der Name allein.
    __table_args__ = (db.UniqueConstraint('username', 'platform',
                                          name='uq_inspo_source_user_platform'),)
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(100), nullable=False)
    city_id    = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=True, index=True)
    platform   = db.Column(db.String(20), default='instagram')  # instagram | tiktok | …
    notes      = db.Column(db.Text)
    last_fetch = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    city       = db.relationship('City', backref=db.backref('inspiration_sources', lazy='dynamic'))
    posts      = db.relationship('MemoInspirationPost', backref='source',
                                 lazy='dynamic', cascade='all,delete')

    def to_dict(self):
        return {
            'id': self.id, 'username': self.username, 'notes': self.notes or '',
            'city_id': self.city_id,
            'city_name': self.city.name if self.city else '',
            'platform': self.platform or 'instagram',
            'post_count': self.post_count(), 'new_count': self.new_count(),
            'last_fetch': self.last_fetch.isoformat() if self.last_fetch else None,
        }

    def post_count(self):
        return self.posts.count()

    def new_count(self):
        return self.posts.filter_by(status='new').count()


class MemePost(db.Model):
    """Vorrat: Fertige/geplante Meme-Posts — Herzstück des Publishing-Flows."""
    __tablename__ = 'meme_post'
    id               = db.Column(db.Integer, primary_key=True)
    city_id          = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    render_job_id    = db.Column(db.Integer, db.ForeignKey('render_job.id'), nullable=True)
    template_id      = db.Column(db.Integer, db.ForeignKey('meme_template.id'), nullable=True)

    title            = db.Column(db.String(300))
    image_path       = db.Column(db.String(500))
    image_url        = db.Column(db.String(1000))
    carousel_paths   = db.Column(db.Text)  # JSON-Liste: ["a.png", "b.png", ...] in Post-Reihenfolge, nur bei post_type='carousel'

    caption          = db.Column(db.Text)
    hashtags         = db.Column(db.Text)
    post_type        = db.Column(db.String(20), default='feed', index=True)
    # feed | reel | story | carousel

    status           = db.Column(db.String(20), default='entwurf', index=True)
    # entwurf | bereit | geplant | veroeffentlicht | archiviert

    scheduled_at     = db.Column(db.DateTime, nullable=True, index=True)
    published_at     = db.Column(db.DateTime)
    notes            = db.Column(db.Text)

    perf_likes       = db.Column(db.Integer)
    perf_comments    = db.Column(db.Integer)
    perf_saves       = db.Column(db.Integer)
    perf_reach       = db.Column(db.Integer)
    perf_impressions = db.Column(db.Integer)
    perf_updated_at  = db.Column(db.DateTime)

    recycle_count    = db.Column(db.Integer, default=0)
    last_recycled_at = db.Column(db.DateTime)

    created_at       = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    city     = db.relationship('City',         backref='meme_posts')
    template = db.relationship('MemeTemplate', backref='meme_posts')

    @property
    def status_label(self):
        return {'entwurf':'Entwurf','bereit':'Bereit','geplant':'Geplant',
                'veroeffentlicht':'Veröffentlicht','archiviert':'Archiviert',
                'rendering':'Wird gerendert','failed':'Fehlgeschlagen'}.get(self.status, self.status)

    def get_carousel_paths(self):
        try: return json.loads(self.carousel_paths or '[]')
        except Exception: return []

    @property
    def engagement_rate(self):
        reach = self.perf_reach or 0
        if not reach: return None
        interactions = (self.perf_likes or 0) + (self.perf_comments or 0) + (self.perf_saves or 0)
        return round(interactions / reach * 100, 2)

    def get_slides(self):
        """Liste von {'url', 'kind'} – bei Karussells aus carousel_paths, sonst ein Eintrag aus image_url."""
        slides = []
        if self.post_type == 'carousel':
            for name in self.get_carousel_paths():
                url = slide_url(name)
                if url:
                    slides.append({'url': url, 'kind': slide_kind(url)})
        if not slides:
            single = self.image_url or (slide_url(self.image_path) if self.image_path else '')
            if single:
                single = slide_url(single)
                slides.append({'url': single, 'kind': slide_kind(single)})
        return slides

    # ── Notizen und Metadaten ────────────────────────────────────────────────
    # notes trägt zwei Dinge: die maschinenlesbaren Metadaten der Render-Queue
    # (missing_vars, quality, task_kind, series, slides) und die Notiz, die ein Mensch
    # im Bearbeiten-Dialog schreibt. Damit das eine das andere nicht überschreibt,
    # liegt beides als ein JSON-Objekt in notes: die Notiz unter dem Schlüssel 'notiz',
    # alles Übrige sind Metadaten. Altbestand (reiner Text in notes) gilt komplett als Notiz.

    def _notes_objekt(self):
        """notes als Dict, wenn dort ein JSON-Objekt steht – sonst None (reiner Text)."""
        raw = (self.notes or '').strip()
        if not raw or not raw.startswith('{'):
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def meta_dict(self):
        """Die Metadaten aus notes OHNE den lesbaren Anteil 'notiz'.
        Leeres Dict, wenn notes kein JSON-Objekt ist (dann gibt es keine Metadaten)."""
        data = self._notes_objekt()
        if not data:
            return {}
        return {k: v for k, v in data.items() if k != 'notiz'}

    def notiz_text(self):
        """Der menschenlesbare Anteil von notes als Text.
        Ist notes kein JSON-Objekt, gilt der ganze Text als Notiz."""
        data = self._notes_objekt()
        if data is None:
            return self.notes or ''
        wert = data.get('notiz')
        if wert is None:
            return ''
        return wert if isinstance(wert, str) else str(wert)

    def to_dict(self):
        city_name = self.city.name if self.city else ''
        city_color = self.city.accent_color if self.city else '#3b82f6'
        tmpl_name  = self.template.name if self.template else ''
        slides = self.get_slides()
        return {
            'id': self.id, 'city_id': self.city_id, 'city_name': city_name,
            'city_color': city_color, 'template_id': self.template_id,
            'template_name': tmpl_name, 'render_job_id': self.render_job_id,
            'title': self.title or '', 'image_url': self.image_url or self.image_path or '',
            'slides': slides, 'slide_count': len(slides),
            'caption': self.caption or '', 'hashtags': self.hashtags or '',
            'post_type': self.post_type, 'status': self.status,
            'status_label': self.status_label,
            'scheduled_at': self.scheduled_at.isoformat() if self.scheduled_at else None,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'notes': self.notes or '',          # unverändert für Altcode
            'notiz': self.notiz_text(),         # nur der lesbare Teil (Bearbeiten-Dialog)
            'meta': self.meta_dict(),           # Metadaten der Queue ohne 'notiz'
            'perf_likes': self.perf_likes, 'perf_comments': self.perf_comments,
            'perf_saves': self.perf_saves, 'perf_reach': self.perf_reach,
            'perf_impressions': self.perf_impressions,
            'engagement_rate': self.engagement_rate,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class TrendingTopic(db.Model):
    """Trending-Themen pro Stadt — aus RSS extrahiert oder manuell."""
    __tablename__ = 'trending_topic'
    id          = db.Column(db.Integer, primary_key=True)
    city_id     = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=True, index=True)
    keyword     = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    trend_score = db.Column(db.Integer, default=50)   # 0–100 Meme-Potenzial
    source      = db.Column(db.String(20), default='rss')  # rss | manual
    meme_idea   = db.Column(db.Text)
    used_in_post_id = db.Column(db.Integer, db.ForeignKey('meme_post.id'), nullable=True)
    ignored     = db.Column(db.Boolean, default=False, index=True)
    fetched_at  = db.Column(db.DateTime, default=datetime.utcnow)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    city         = db.relationship('City', backref='trending_topics', foreign_keys=[city_id])
    used_in_post = db.relationship('MemePost', backref='used_for_trend', foreign_keys=[used_in_post_id])

    def to_dict(self):
        return {
            'id': self.id, 'city_id': self.city_id,
            'city_name': self.city.name if self.city else 'Überregional',
            'city_color': self.city.accent_color if self.city else '#3b82f6',
            'keyword': self.keyword, 'description': self.description or '',
            'trend_score': self.trend_score, 'source': self.source,
            'meme_idea': self.meme_idea or '', 'ignored': self.ignored,
            'used_in_post_id': self.used_in_post_id,
            'fetched_at': self.fetched_at.isoformat() if self.fetched_at else None,
            'created_at': self.created_at.isoformat(),
        }


class RecycleJob(db.Model):
    """Recycling-Vorschlag: ein veröffentlichter Post soll nochmal gepostet werden."""
    __tablename__ = 'recycle_job'
    id               = db.Column(db.Integer, primary_key=True)
    source_post_id   = db.Column(db.Integer, db.ForeignKey('meme_post.id'), nullable=False, index=True)
    target_post_id   = db.Column(db.Integer, db.ForeignKey('meme_post.id'), nullable=True)
    city_id          = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False)
    status           = db.Column(db.String(20), default='vorschlag', index=True)
    # vorschlag | geplant | veroeffentlicht | abgelehnt
    scheduled_for    = db.Column(db.DateTime)
    new_caption      = db.Column(db.Text)
    recycle_score    = db.Column(db.Integer)
    rejection_reason = db.Column(db.String(200))
    notes            = db.Column(db.Text)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source_post = db.relationship('MemePost', foreign_keys=[source_post_id], backref='recycle_jobs_as_source')
    target_post = db.relationship('MemePost', foreign_keys=[target_post_id], backref='recycle_job_as_target')
    city        = db.relationship('City', backref='recycle_jobs')

    def to_dict(self):
        sp = self.source_post
        tp = self.target_post
        return {
            'id': self.id,
            'source_post_id': self.source_post_id,
            'source_title': sp.title if sp else '',
            'source_image': (sp.image_url or sp.image_path or '') if sp else '',
            'source_city': sp.city.name if sp and sp.city else '',
            'source_city_color': sp.city.accent_color if sp and sp.city else '#3b82f6',
            'source_published_at': sp.published_at.isoformat() if sp and sp.published_at else None,
            'source_perf': {
                'likes': sp.perf_likes, 'comments': sp.perf_comments,
                'saves': sp.perf_saves, 'reach': sp.perf_reach,
                'er': sp.engagement_rate,
            } if sp else {},
            'target_post_id': self.target_post_id,
            'target_perf': {
                'likes': tp.perf_likes, 'comments': tp.perf_comments,
                'saves': tp.perf_saves, 'reach': tp.perf_reach,
                'er': tp.engagement_rate,
            } if tp else {},
            'city_id': self.city_id,
            'city_name': self.city.name if self.city else '',
            'city_color': self.city.accent_color if self.city else '#3b82f6',
            'status': self.status,
            'scheduled_for': self.scheduled_for.isoformat() if self.scheduled_for else None,
            'new_caption': self.new_caption or '',
            'recycle_score': self.recycle_score,
            'rejection_reason': self.rejection_reason or '',
            'notes': self.notes or '',
            'created_at': self.created_at.isoformat(),
        }


class MemeEvent(db.Model):
    """Meme-relevante Events: Saisonal, Wetter, Lokal, Wiederkehrend."""
    __tablename__ = 'meme_event'
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(200), nullable=False)
    description    = db.Column(db.Text)
    event_type     = db.Column(db.String(30), default='saisonal', index=True)
    # saisonal | wetter | lokal | sport | breaking | recurring
    date_from      = db.Column(db.String(10))   # YYYY-MM-DD oder MM-DD wenn recurring
    date_to        = db.Column(db.String(10))
    recurring      = db.Column(db.Boolean, default=False)  # jährlich wiederkehrend
    lead_days      = db.Column(db.Integer, default=7)      # X Tage vor Event erinnern
    city_scope     = db.Column(db.Text, default='[]')      # JSON: [] = alle Städte, [id,...] = bestimmte
    meme_relevance = db.Column(db.Integer, default=3)      # 1-5 Sterne wie gut eignet sich das für Memes
    suggested_cats = db.Column(db.Text, default='[]')      # empfohlene Template-Kategorien JSON
    emoji          = db.Column(db.String(10), default='📅')
    notes          = db.Column(db.Text)
    notified_at    = db.Column(db.DateTime)                # wann wurde zuletzt Benachrichtigung gesendet
    active         = db.Column(db.Boolean, default=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def get_city_scope(self):
        try: return json.loads(self.city_scope or '[]')
        except: return []

    def get_suggested_cats(self):
        try: return json.loads(self.suggested_cats or '[]')
        except: return []

    @staticmethod
    def _month_day(s):
        """'MM-DD' oder 'YYYY-MM-DD' → (monat, tag)."""
        s = (s or '').strip()
        if len(s) == 5:
            return int(s[:2]), int(s[3:])
        from datetime import date
        d = date.fromisoformat(s)
        return d.month, d.day

    @staticmethod
    def _safe_date(year, month, day):
        """date(), das den 29.02. in Nicht-Schaltjahren auf den 28.02. legt (statt ValueError zu
        werfen – dadurch war ein solches Event drei von vier Jahren unsichtbar). Gleiches gilt
        für einen 31., den es im Monat nicht gibt."""
        from datetime import date
        d = int(day or 1)
        while d > 1:
            try:
                return date(year, month, d)
            except ValueError:
                d -= 1
        return date(year, month, 1)

    def _window(self, today):
        """(start, ende, wiederkehrend) als date-Objekte für das aktuelle Jahr bzw. das feste Datum.
        None, wenn kein/ungültiges Datum."""
        from datetime import date
        if not self.date_from:
            return None
        try:
            if self.recurring:
                mf, df_ = self._month_day(self.date_from)
                start = self._safe_date(today.year, mf, df_)
                if self.date_to:
                    mt, dt_ = self._month_day(self.date_to)
                    end = self._safe_date(today.year, mt, dt_)
                else:
                    end = start
                return start, end, True
            start = date.fromisoformat(self.date_from)
            end = date.fromisoformat(self.date_to) if self.date_to else start
            if end < start:
                end = start
            return start, end, False
        except Exception:
            return None

    def is_active_today(self):
        """Läuft das Event heute? Berücksichtigt date_to und bei recurring den Jahreswechsel
        (z. B. Winter 12-21..03-20)."""
        from datetime import date
        today = date.today()
        w = self._window(today)
        if not w:
            return False
        start, end, rec = w
        if rec and end < start:            # über den Jahreswechsel hinweg
            return today >= start or today <= end
        return start <= today <= end

    def days_until(self):
        """Tage bis zum nächsten Beginn (None wenn kein Datum, 0 wenn das Event gerade läuft,
        negativ wenn ein einmaliges Event vorbei ist)."""
        from datetime import date
        today = date.today()
        w = self._window(today)
        if not w:
            return None
        start, end, rec = w
        if self.is_active_today():
            return 0
        if rec and start < today:
            try:
                start = self._safe_date(today.year + 1, start.month, start.day)
            except Exception:
                return None
        return (start - today).days

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'description': self.description or '',
            'event_type': self.event_type, 'date_from': self.date_from or '',
            'date_to': self.date_to or '', 'recurring': self.recurring,
            'lead_days': self.lead_days, 'city_scope': self.get_city_scope(),
            'meme_relevance': self.meme_relevance,
            'suggested_cats': self.get_suggested_cats(),
            'emoji': self.emoji or '📅', 'notes': self.notes or '',
            'active': self.active, 'days_until': self.days_until(),
            'active_now': self.is_active_today(),
            'notified_at': self.notified_at.isoformat() if self.notified_at else None,
        }


class MemoInspirationPost(db.Model):
    """Heruntergeladener Inspirations-Post."""
    __tablename__ = 'memo_inspiration_post'
    id             = db.Column(db.Integer, primary_key=True)
    source_id      = db.Column(db.Integer, db.ForeignKey('memo_inspiration_source.id'), nullable=False, index=True)
    instagram_code = db.Column(db.String(50), unique=True)
    image_url      = db.Column(db.String(1000))
    caption        = db.Column(db.Text)
    post_date      = db.Column(db.DateTime)
    like_count     = db.Column(db.Integer)
    media_type     = db.Column(db.String(20), default='image')
    status         = db.Column(db.String(20), default='new', index=True)
    # new | saved | ignored | used
    is_saved       = db.Column(db.Boolean, default=False)
    meme_idea      = db.Column(db.Text)
    carousel_urls  = db.Column(db.Text)
    ai_theme       = db.Column(db.String(100))
    ai_relevant    = db.Column(db.Boolean)   # None = not yet checked
    ai_reasoning   = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)


# ═══════════════════════════════════════════════════════════
# KOOPERATIONEN
# ═══════════════════════════════════════════════════════════

class CollabNiche(db.Model):
    """Nischen/Branchen für Kooperationen (z.B. Gastronomie, Sport, Beauty)."""
    __tablename__ = 'collab_niche'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False, unique=True)
    emoji       = db.Column(db.String(10), default='🤝')
    description = db.Column(db.Text)
    active      = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    ideas       = db.relationship('CollabIdea', backref='niche', lazy='dynamic',
                                  cascade='all,delete')

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'emoji': self.emoji or '🤝',
                'description': self.description or '', 'active': self.active,
                'idea_count': self.ideas.filter_by(active=True).count()}


class CollabIdea(db.Model):
    """Kooperationsidee, die auf beliebig viele Städte angewendet werden kann."""
    __tablename__ = 'collab_idea'
    id            = db.Column(db.Integer, primary_key=True)
    niche_id      = db.Column(db.Integer, db.ForeignKey('collab_niche.id'), nullable=False, index=True)
    title         = db.Column(db.String(200), nullable=False)
    description   = db.Column(db.Text)
    # Template-Text mit {partner_name} und {city_name} als Platzhalter
    template_text = db.Column(db.Text)
    example_image = db.Column(db.String(500))
    active        = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    city_collabs  = db.relationship('CityCollab', backref='idea', lazy='dynamic',
                                    cascade='all,delete')

    def to_dict(self):
        assigned = self.city_collabs.count()
        return {
            'id': self.id, 'niche_id': self.niche_id,
            'niche_name': self.niche.name if self.niche else '',
            'niche_emoji': self.niche.emoji if self.niche else '🤝',
            'title': self.title, 'description': self.description or '',
            'template_text': self.template_text or '',
            'example_image': self.example_image or '',
            'active': self.active, 'assigned_cities': assigned,
        }


class CityCollab(db.Model):
    """Konkrete Kooperation: Idee × Stadt × Partner."""
    __tablename__ = 'city_collab'
    id           = db.Column(db.Integer, primary_key=True)
    idea_id      = db.Column(db.Integer, db.ForeignKey('collab_idea.id'), nullable=False, index=True)
    city_id      = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    partner_name = db.Column(db.String(200), nullable=False)  # z.B. "Café Luisa"
    partner_ig   = db.Column(db.String(100))
    status       = db.Column(db.String(20), default='aktiv')  # aktiv | pausiert | abgeschlossen
    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    city         = db.relationship('City', backref=db.backref('collabs', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id, 'idea_id': self.idea_id,
            'city_id': self.city_id, 'city_name': self.city.name if self.city else '',
            'partner_name': self.partner_name, 'partner_ig': self.partner_ig or '',
            'status': self.status, 'notes': self.notes or '',
            'created_at': self.created_at.isoformat() if self.created_at else '',
        }


# ═══════════════════════════════════════════════════════════════════════════════
# STUDIO – Stadtbilder, Aufkleber-Bibliothek, Textstile
# ═══════════════════════════════════════════════════════════════════════════════
# Drei kleine Tabellen für den Bau eigener Vorlagen im Studio (G3):
#   CityAsset    – Bilder, die je Stadt anders sind (Wappen, Logo, Skyline …)
#   StudioAsset  – stadtunabhängige Aufkleber/Rahmen/Logos
#   StudioPreset – gespeicherte Textstile ("so sieht unsere Überschrift aus")
# Die Dateien liegen unter <MEMEOS_DATA_ROOT>/assets/; die Spalte url trägt die
# Cloudinary-Fassung, falls hochgeladen (deploy-sicher, siehe project_persistent_media).

CITY_ASSET_KINDS   = ('wappen', 'logo', 'foto', 'sonstige')
STUDIO_ASSET_KINDS = ('sticker', 'logo', 'rahmen', 'sonstige')

# Vorlagen-Variablen mit diesem Präfix werden aus den Stadtbildern gefüllt:
# ein image-Element mit var='bild_wappen' zieht das CityAsset mit key='wappen'.
CITY_ASSET_VAR_PREFIX = 'bild_'


class CityAsset(db.Model):
    """Ein Bild, das genau einer Stadt gehört. Der Schlüssel ist der Variablenname
    ohne das Präfix 'bild_': key='wappen' füllt var='bild_wappen'."""
    __tablename__ = 'city_asset'
    __table_args__ = (db.UniqueConstraint('city_id', 'key', name='uq_city_asset_city_key'),)

    id         = db.Column(db.Integer, primary_key=True)
    city_id    = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    key        = db.Column(db.String(40), nullable=False, index=True)
    name       = db.Column(db.String(200))
    filename   = db.Column(db.String(300))
    url        = db.Column(db.String(1000))
    kind       = db.Column(db.String(20), default='sonstige')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    city = db.relationship('City', backref=db.backref('assets', lazy='dynamic',
                                                      cascade='all,delete'))

    @property
    def var_name(self):
        return CITY_ASSET_VAR_PREFIX + (self.key or '')

    def to_dict(self):
        return {
            'id':         self.id,
            'city_id':    self.city_id,
            'city_name':  self.city.name if self.city else '',
            'key':        self.key or '',
            'var':        self.var_name,
            'name':       self.name or (self.key or ''),
            'filename':   self.filename or '',
            'url':        self.url or '',
            'kind':       self.kind or 'sonstige',
            'created_at': self.created_at.isoformat() if self.created_at else '',
        }


class StudioAsset(db.Model):
    """Aufkleber-Bibliothek: Bilder, die in jeder Stadt gleich sind."""
    __tablename__ = 'studio_asset'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200))
    filename   = db.Column(db.String(300))
    url        = db.Column(db.String(1000))
    kind       = db.Column(db.String(20), default='sticker', index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':         self.id,
            'name':       self.name or (self.filename or ''),
            'filename':   self.filename or '',
            'url':        self.url or '',
            'kind':       self.kind or 'sticker',
            'created_at': self.created_at.isoformat() if self.created_at else '',
        }


class StudioPreset(db.Model):
    """Gespeicherter Textstil: die Eigenschaften eines text-Elements ohne Position."""
    __tablename__ = 'studio_preset'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200), nullable=False)
    props      = db.Column(db.Text, default='{}')     # JSON-Objekt
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_props(self):
        try:
            data = json.loads(self.props or '{}')
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def to_dict(self):
        return {
            'id':         self.id,
            'name':       self.name or '',
            'props':      self.get_props(),
            'created_at': self.created_at.isoformat() if self.created_at else '',
        }


class MemeFormat(db.Model):
    """Bekannte Meme-Vorlage: der saubere Hintergrund OHNE Text (H1 Vorlagen-Katalog).

    Damit ein hochgeladenes fremdes Meme nachgebaut werden kann, ohne die Schrift nur
    wegzuretuschieren, wird hier nachgeschlagen, ob das Originalformat bekannt ist.

    quelle 'imgflip' – aus dem öffentlichen Katalog api.imgflip.com/get_memes
    quelle 'eigen'   – aus der eigenen Bibliothek (Template-Hintergründe, Aufkleber);
                       extern_id ist dort 'template:<id>' bzw. 'asset:<id>'
    phash            – Differenzhash (dhash, 64 Bit) als 16-stellige Hex-Zeichenkette;
                       leer, solange das Bild noch nicht lokal vorliegt
    filename         – lokale Kopie in <DATA_ROOT>/formate/ (bei 'eigen' auch uploads/)

    (quelle, extern_id) ist eindeutig; Zeilen ohne extern_id sind davon unberührt,
    weil NULL in einem eindeutigen Index nicht mit NULL kollidiert.
    """
    __tablename__ = 'meme_format'
    __table_args__ = (
        db.UniqueConstraint('quelle', 'extern_id', name='uq_meme_format_quelle_extern'),
    )

    id         = db.Column(db.Integer, primary_key=True)
    quelle     = db.Column(db.String(20), nullable=False, default='imgflip', index=True)
    extern_id  = db.Column(db.String(80))
    name       = db.Column(db.String(200), nullable=False, default='')
    url        = db.Column(db.String(1000))
    filename   = db.Column(db.String(300))
    width      = db.Column(db.Integer, default=0)
    height     = db.Column(db.Integer, default=0)
    box_count  = db.Column(db.Integer)
    phash      = db.Column(db.String(16), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':         self.id,
            'quelle':     self.quelle or '',
            'extern_id':  self.extern_id or '',
            'name':       self.name or '',
            'url':        self.url or '',
            'filename':   self.filename or '',
            'width':      self.width or 0,
            'height':     self.height or 0,
            'box_count':  self.box_count,
            'phash':      self.phash or '',
            'hat_bild':   bool(self.filename),
            'created_at': self.created_at.isoformat() if self.created_at else '',
        }
