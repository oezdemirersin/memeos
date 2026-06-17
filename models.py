from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json
import hashlib
import secrets

db = SQLAlchemy()

KNOWLEDGE_CATEGORIES = [
    ("problem_place",    "Problemort",         "#ef4444"),
    ("landmark",         "Wahrzeichen",         "#3b82f6"),
    ("youth_spot",       "Jugendtreff",         "#22c55e"),
    ("food_spot",        "Essensort",           "#f59e0b"),
    ("traffic_spot",     "Verkehrspunkt",       "#8b5cf6"),
    ("school",           "Schule / Uni",        "#06b6d4"),
    ("event",            "Event / Fest",        "#ec4899"),
    ("klischee",         "Klischee",            "#f97316"),
    ("stadtteil_arm",    "Problemviertel",      "#dc2626"),
    ("stadtteil_reich",  "Reiches Viertel",     "#16a34a"),
    ("stadtteil_student","Studentenviertel",    "#7c3aed"),
    ("local_meme",       "Lokales Meme",        "#0ea5e9"),
    ("sport",            "Sportverein",         "#84cc16"),
    ("dialect",          "Dialekt / Ausdruck",  "#d97706"),
]

CATEGORY_MAP = {k: (label, color) for k, label, color in KNOWLEDGE_CATEGORIES}


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
    rss_url          = db.Column(db.String(500))
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

    def knowledge_count(self):
        return self.knowledge.filter_by(active=True).count()

    def render_count(self):
        return self.render_jobs.filter_by(status='done').count()


class CityKnowledge(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    city_id        = db.Column(db.Integer, db.ForeignKey('city.id'), nullable=False, index=True)
    category       = db.Column(db.String(50), nullable=False, index=True)
    name           = db.Column(db.String(200), nullable=False)
    description    = db.Column(db.Text)
    confidence     = db.Column(db.Integer, default=70)   # 0–100
    source         = db.Column(db.String(30), default='ai')  # ai | resident | verified | manual
    used_count     = db.Column(db.Integer, default=0)
    last_used_at   = db.Column(db.DateTime)
    cooldown_until = db.Column(db.DateTime)
    active         = db.Column(db.Boolean, default=True, index=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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


class MemeTemplate(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(200), nullable=False)
    description      = db.Column(db.Text)
    canva_template_id= db.Column(db.String(100))         # Canva Brand Template ID
    required_vars    = db.Column(db.Text, default='[]')  # JSON: ["problem_place", "landmark"]
    canva_field_map  = db.Column(db.Text, default='{}')  # JSON: {"problem_place": "feld_in_canva"}
    tags             = db.Column(db.Text, default='[]')  # JSON: ["pov", "klischee"]
    category         = db.Column(db.String(50), default='allgemein')
    preview_image    = db.Column(db.String(500))
    example_text     = db.Column(db.Text)                # Beispiel-Text wie das Template aussieht
    seasonal_from    = db.Column(db.String(5))           # "12-01"
    seasonal_to      = db.Column(db.String(5))           # "12-31"
    min_population   = db.Column(db.Integer, default=0)
    use_count        = db.Column(db.Integer, default=0)
    active           = db.Column(db.Boolean, default=True, index=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    render_jobs = db.relationship('RenderJob', backref='template', lazy='dynamic')

    def get_required_vars(self):
        try: return json.loads(self.required_vars or '[]')
        except: return []

    def get_canva_field_map(self):
        try: return json.loads(self.canva_field_map or '{}')
        except: return {}

    def get_tags(self):
        try: return json.loads(self.tags or '[]')
        except: return []

    def has_canva(self):
        return bool(self.canva_template_id and self.canva_template_id.strip())


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
    cost_eur      = db.Column(db.Float, default=0.0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
