from datetime import datetime
from typing import Optional, List, Dict, Any

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Float, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, Session

Base = declarative_base()


class Feed(Base):
    """Model for RSS feed subscriptions."""
    __tablename__ = "feeds"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    url = Column(String(512), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    website_url = Column(String(512), nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow)
    last_checked = Column(DateTime, default=datetime.utcnow)
    muted = Column(Boolean, default=False)
    muted_reason = Column(Text, nullable=True)  # Reason for muting the feed
    peer_through = Column(Boolean, default=False)  # Whether to peer through aggregator to origin article
    error_count = Column(Integer, default=0)  # Count of consecutive errors
    no_entries_count = Column(Integer, default=0)  # Count of consecutive checks with no entries
    last_error = Column(Text, nullable=True)

    # Relocation tracking: when a feed moves to a new address, the URL is
    # updated in place so the Feed ID, article associations and read/processing
    # progress survive. The old address is kept for rollback and as a fetch
    # fallback while both addresses are reachable.
    previous_url = Column(String(512), nullable=True)  # URL relocated from (fallback candidate)
    url_updated_at = Column(DateTime, nullable=True)  # When the current URL became effective
    url_history = Column(Text, nullable=True)  # JSON list of relocation/rollback events
    last_successful_url = Column(String(512), nullable=True)  # Address that actually served data last batch
    
    # Statistics
    article_count = Column(Integer, default=0)  # Total count of ingested articles
    avg_quality_score = Column(Float, nullable=True)  # Average quality score of articles
    quality_tier_counts = Column(Text, nullable=True)  # JSON string of quality tier counts {"S": 5, "A": 10, ...}
    
    # Relationships
    articles = relationship("Article", back_populates="feed", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Feed(id={self.id}, title='{self.title}', muted={self.muted}, peer_through={self.peer_through})>"


class Article(Base):
    """Model for articles from RSS feeds."""
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    feed_id = Column(Integer, ForeignKey("feeds.id"), nullable=False)
    title = Column(String(512), nullable=False)
    url = Column(String(512), nullable=False)
    guid = Column(String(512), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    author = Column(String(255), nullable=True)
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    processed = Column(Boolean, default=False)
    processed_at = Column(DateTime, nullable=True)
    summary = Column(Text, nullable=True)
    quality_tier = Column(String(1), nullable=True)  # S, A, B, C, D
    quality_score = Column(Integer, nullable=True)  # 1-100
    labels = Column(String(255), nullable=True)
    embedding_generated = Column(Boolean, default=False)
    embedding_vector = Column(Text, nullable=True)  # JSON-serialized embedding vector
    word_count = Column(Integer, nullable=True)
    jina_enhanced = Column(Boolean, default=False)  # Flag to indicate if content was fetched from Jina.ai
    
    # Relationships
    feed = relationship("Feed", back_populates="articles")

    def __repr__(self) -> str:
        return f"<Article(id={self.id}, title='{self.title[:30]}...', quality_tier='{self.quality_tier or 'None'}')>"


def _migrate_schema(engine) -> None:
    """Add columns introduced after the initial schema to existing databases.

    New databases get every column from Base.metadata.create_all(); this
    upgrades databases created by older versions in place.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)

    # Columns added per table: {table: {column: SQL type}}
    migrations = {
        "articles": {
            "embedding_vector": "TEXT",
        },
        "feeds": {
            "previous_url": "VARCHAR(512)",
            "url_updated_at": "DATETIME",
            "url_history": "TEXT",
            "last_successful_url": "VARCHAR(512)",
        },
    }

    with engine.connect() as conn:
        for table, columns in migrations.items():
            if not inspector.has_table(table):
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        conn.commit()


def init_db(db_path: str) -> Session:
    """Initialize database and return session."""
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    _migrate_schema(engine)

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()


def get_db_session(db_path: str) -> Session:
    """Get a database session."""
    engine = create_engine(f"sqlite:///{db_path}")
    _migrate_schema(engine)

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()