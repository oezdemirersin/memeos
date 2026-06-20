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
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(100), nullable=False, unique=True)
    notes      = db.Column(db.Text)
    last_fetch = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    posts      = db.relationship('MemoInspirationPost', backref='source',
                                 lazy='dynamic', cascade='all,delete')

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
                'veroeffentlicht':'Veröffentlicht','archiviert':'Archiviert'}.get(self.status, self.status)

    @property
    def engagement_rate(self):
        reach = self.perf_reach or 0
        if not reach: return None
        interactions = (self.perf_likes or 0) + (self.perf_comments or 0) + (self.perf_saves or 0)
        return round(interactions / reach * 100, 2)

    def to_dict(self):
        city_name = self.city.name if self.city else ''
        city_color = self.city.accent_color if self.city else '#3b82f6'
        tmpl_name  = self.template.name if self.template else ''
        return {
            'id': self.id, 'city_id': self.city_id, 'city_name': city_name,
            'city_color': city_color, 'template_id': self.template_id,
            'template_name': tmpl_name, 'render_job_id': self.render_job_id,
            'title': self.title or '', 'image_url': self.image_url or self.image_path or '',
            'caption': self.caption or '', 'hashtags': self.hashtags or '',
            'post_type': self.post_type, 'status': self.status,
            'status_label': self.status_label,
            'scheduled_at': self.scheduled_at.isoformat() if self.scheduled_at else None,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'notes': self.notes or '',
            'perf_likes': self.perf_likes, 'perf_comments': self.perf_comments,
            'perf_saves': self.perf_saves, 'perf_reach': self.perf_reach,
            'perf_impressions': self.perf_impressions,
            'engagement_rate': self.engagement_rate,
            'created_at': self.created_at.isoformat(),
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
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
