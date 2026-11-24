#!/usr/bin/env python
"""
Behavioral tests for feed relocation: OPML imports must not sever tracked
history when a public subscription changes address.

Covers:
- OPML import registers a relocation on the existing Feed row (same Feed ID,
  article associations and read/processing progress preserved)
- Re-imports are idempotent; stale OPML entries are skipped, not duplicated
- Manual relocation registration and rollback (including chains)
- Ingestion falls back to the previous URL in the same batch when the new
  address serves nothing, and records which address actually delivered
"""

import pytest
from datetime import datetime
from sqlalchemy import create_engine, inspect, text

from rssidian import rss
from rssidian.models import Article, Feed, _migrate_schema, init_db
from rssidian.opml import import_feeds_from_opml
from rssidian.relocation import (
    get_url_history,
    normalize_feed_url,
    relocate_feed,
    rollback_feed_relocation,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@pytest.fixture
def db_session(tmp_path):
    session = init_db(str(tmp_path / "test.db"))
    yield session
    session.close()


def make_feed(session, title="Example Feed", url="https://old.example.com/feed",
              website_url="https://example.com"):
    feed = Feed(
        title=title,
        url=url,
        description="A feed",
        website_url=website_url,
        last_updated=datetime.utcnow(),
        last_checked=datetime.utcnow(),
    )
    session.add(feed)
    session.commit()
    return feed


def make_article(session, feed, guid="guid-1", processed=True):
    article = Article(
        feed_id=feed.id,
        title=f"Article {guid}",
        url=f"https://example.com/{guid}",
        guid=guid,
        content="some content",
        processed=processed,
        fetched_at=datetime.utcnow(),
    )
    session.add(article)
    session.commit()
    return article


def write_opml(path, feeds):
    outlines = "".join(
        f'<outline text="{f["title"]}" title="{f["title"]}" type="rss" '
        f'xmlUrl="{f["url"]}" htmlUrl="{f.get("website_url", "")}" '
        f'description="{f.get("description", "")}"/>'
        for f in feeds
    )
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<opml version="2.0"><head><title>Test</title></head><body>{outlines}</body></opml>'
    )
    return str(path)


def make_entry(guid, link):
    """A feedparser-like entry with enough content to skip Jina enrichment."""
    return {
        "id": guid,
        "link": link,
        "title": f"Article {guid}",
        "summary": "summary",
        "published": "Mon, 23 Nov 2026 10:00:00 GMT",
        "content": [{"value": "word " * 300}],
    }


# --------------------------------------------------------------------------
# OPML import: relocation instead of disconnect
# --------------------------------------------------------------------------

def test_opml_import_relocates_feed_that_changed_address(db_session, tmp_path):
    feed = make_feed(db_session)
    make_article(db_session, feed, processed=True)
    original_id = feed.id

    opml = write_opml(tmp_path / "feeds.opml", [
        {"title": "Example Feed", "url": "https://new.example.com/feed",
         "website_url": "https://example.com"},
    ])
    result = import_feeds_from_opml(opml, db_session)

    assert result["relocated"] == 1
    assert result["new"] == 0
    assert db_session.query(Feed).count() == 1

    feed = db_session.query(Feed).filter_by(title="Example Feed").one()
    # Original Feed ID preserved
    assert feed.id == original_id
    assert feed.url == "https://new.example.com/feed"
    assert feed.previous_url == "https://old.example.com/feed"
    assert feed.url_updated_at is not None

    # Article associations and read progress preserved
    articles = db_session.query(Article).filter_by(feed_id=original_id).all()
    assert len(articles) == 1
    assert articles[0].processed is True

    # Relocation registered in the history
    history = get_url_history(feed)
    assert len(history) == 1
    assert history[0]["action"] == "relocate"
    assert history[0]["old_url"] == "https://old.example.com/feed"
    assert history[0]["new_url"] == "https://new.example.com/feed"
    assert history[0]["source"] == "opml-import"


def test_opml_reimport_is_idempotent(db_session, tmp_path):
    make_feed(db_session)
    opml = write_opml(tmp_path / "feeds.opml", [
        {"title": "Example Feed", "url": "https://new.example.com/feed",
         "website_url": "https://example.com"},
    ])
    import_feeds_from_opml(opml, db_session)
    result = import_feeds_from_opml(opml, db_session)

    assert result["new"] == 0
    assert result["relocated"] == 0
    assert result["updated"] == 1
    assert db_session.query(Feed).count() == 1
    feed = db_session.query(Feed).one()
    assert feed.url == "https://new.example.com/feed"
    assert len(get_url_history(feed)) == 1


def test_stale_opml_entry_is_skipped_not_duplicated(db_session, tmp_path):
    make_feed(db_session)
    new_opml = write_opml(tmp_path / "new.opml", [
        {"title": "Example Feed", "url": "https://new.example.com/feed",
         "website_url": "https://example.com"},
    ])
    import_feeds_from_opml(new_opml, db_session)

    # An old OPML still listing the previous address must not create a
    # duplicate subscription or move the feed back
    old_opml = write_opml(tmp_path / "old.opml", [
        {"title": "Example Feed", "url": "https://old.example.com/feed",
         "website_url": "https://example.com"},
    ])
    result = import_feeds_from_opml(old_opml, db_session)

    assert result["new"] == 0
    assert result["skipped"] == 1
    assert db_session.query(Feed).count() == 1
    assert db_session.query(Feed).one().url == "https://new.example.com/feed"


def test_opml_import_records_cosmetic_url_change(db_session, tmp_path):
    feed = make_feed(db_session, url="http://example.com/feed")
    opml = write_opml(tmp_path / "feeds.opml", [
        {"title": "Example Feed", "url": "https://example.com/feed",
         "website_url": "https://example.com"},
    ])
    result = import_feeds_from_opml(opml, db_session)

    assert result["relocated"] == 1
    assert result["new"] == 0
    assert feed.url == "https://example.com/feed"
    assert feed.previous_url == "http://example.com/feed"


def test_opml_stale_cosmetic_variant_is_skipped(db_session, tmp_path):
    feed = make_feed(db_session, url="http://example.com/feed")
    new_opml = write_opml(tmp_path / "new.opml", [
        {"title": "Example Feed", "url": "https://example.com/feed",
         "website_url": "https://example.com"},
    ])
    import_feeds_from_opml(new_opml, db_session)

    # An old OPML still listing the http variant must not move the feed back
    old_opml = write_opml(tmp_path / "old.opml", [
        {"title": "Example Feed", "url": "http://example.com/feed",
         "website_url": "https://example.com"},
    ])
    result = import_feeds_from_opml(old_opml, db_session)

    assert result["skipped"] == 1
    assert feed.url == "https://example.com/feed"


def test_opml_import_still_creates_genuinely_new_feeds(db_session, tmp_path):
    make_feed(db_session)
    opml = write_opml(tmp_path / "feeds.opml", [
        {"title": "Example Feed", "url": "https://new.example.com/feed",
         "website_url": "https://example.com"},
        {"title": "Another Feed", "url": "https://other.example.com/rss",
         "website_url": "https://other.example.com"},
    ])
    result = import_feeds_from_opml(opml, db_session)

    assert result["relocated"] == 1
    assert result["new"] == 1
    assert db_session.query(Feed).count() == 2


# --------------------------------------------------------------------------
# Manual relocation and rollback
# --------------------------------------------------------------------------

def test_relocate_to_same_url_is_noop(db_session):
    feed = make_feed(db_session)
    assert relocate_feed(db_session, feed, feed.url) is False
    assert feed.previous_url is None


def test_relocate_rejects_url_of_another_feed(db_session):
    make_feed(db_session, title="Feed A", url="https://a.example.com/feed")
    feed_b = make_feed(db_session, title="Feed B", url="https://b.example.com/feed")

    with pytest.raises(ValueError, match="already used"):
        relocate_feed(db_session, feed_b, "https://a.example.com/feed")


def test_rollback_restores_previous_url_and_keeps_history(db_session, tmp_path):
    feed = make_feed(db_session)
    make_article(db_session, feed)
    original_id = feed.id

    opml = write_opml(tmp_path / "feeds.opml", [
        {"title": "Example Feed", "url": "https://new.example.com/feed",
         "website_url": "https://example.com"},
    ])
    import_feeds_from_opml(opml, db_session)

    restored = rollback_feed_relocation(db_session, feed)
    db_session.commit()

    assert restored == "https://old.example.com/feed"
    assert feed.id == original_id
    assert feed.url == "https://old.example.com/feed"
    assert feed.previous_url is None
    # Articles stay attached through the rollback
    assert db_session.query(Article).filter_by(feed_id=original_id).count() == 1
    # Both events are auditable
    assert [e["action"] for e in get_url_history(feed)] == ["relocate", "rollback"]


def test_rollback_chain_unwinds_multiple_relocations(db_session):
    feed = make_feed(db_session, url="https://a.example.com/feed")
    relocate_feed(db_session, feed, "https://b.example.com/feed")
    relocate_feed(db_session, feed, "https://c.example.com/feed")
    db_session.commit()

    assert feed.url == "https://c.example.com/feed"
    assert feed.previous_url == "https://b.example.com/feed"

    assert rollback_feed_relocation(db_session, feed) == "https://b.example.com/feed"
    assert feed.url == "https://b.example.com/feed"
    assert feed.previous_url == "https://a.example.com/feed"

    assert rollback_feed_relocation(db_session, feed) == "https://a.example.com/feed"
    assert feed.url == "https://a.example.com/feed"
    assert feed.previous_url is None

    # Nothing left to roll back
    assert rollback_feed_relocation(db_session, feed) is None


# --------------------------------------------------------------------------
# Ingestion: same-batch availability conflict resolution
# --------------------------------------------------------------------------

def test_ingest_falls_back_to_previous_url_in_same_batch(db_session, monkeypatch):
    feed = make_feed(db_session)
    relocate_feed(db_session, feed, "https://new.example.com/feed")
    db_session.commit()

    def fake_get_feed_entries(url, lookback_days=7, max_retries=3):
        if url == "https://old.example.com/feed":
            return [make_entry("guid-old-1", "https://example.com/article-1")]
        return []  # new address serves nothing

    monkeypatch.setattr(rss, "get_feed_entries", fake_get_feed_entries)

    new_articles, _, _, _, source_url = rss.process_feed(feed, db_session, analyze_content=False)

    # The batch makes explicit which address actually served data
    assert source_url == "https://old.example.com/feed"
    assert feed.last_successful_url == "https://old.example.com/feed"
    assert new_articles == 1

    # The article lands on the original Feed ID: history is not disconnected
    article = db_session.query(Article).filter_by(guid="guid-old-1").one()
    assert article.feed_id == feed.id


def test_ingest_prefers_new_url_when_both_serve(db_session, monkeypatch):
    feed = make_feed(db_session)
    relocate_feed(db_session, feed, "https://new.example.com/feed")
    db_session.commit()

    def fake_get_feed_entries(url, lookback_days=7, max_retries=3):
        return [make_entry(f"guid-via-{url}", "https://example.com/article-1")]

    monkeypatch.setattr(rss, "get_feed_entries", fake_get_feed_entries)
    _, _, _, _, source_url = rss.process_feed(feed, db_session, analyze_content=False)

    assert source_url == "https://new.example.com/feed"
    assert feed.last_successful_url == "https://new.example.com/feed"


def test_ingest_without_relocation_uses_current_url(db_session, monkeypatch):
    feed = make_feed(db_session)
    monkeypatch.setattr(
        rss, "get_feed_entries",
        lambda url, lookback_days=7, max_retries=3: [make_entry("g1", "https://example.com/a1")],
    )
    _, _, _, _, source_url = rss.process_feed(feed, db_session, analyze_content=False)

    assert source_url == "https://old.example.com/feed"
    assert feed.last_successful_url == "https://old.example.com/feed"


def test_ingest_records_nothing_when_no_address_serves(db_session, monkeypatch):
    feed = make_feed(db_session)
    relocate_feed(db_session, feed, "https://new.example.com/feed")
    db_session.commit()

    monkeypatch.setattr(rss, "get_feed_entries", lambda url, lookback_days=7, max_retries=3: [])
    _, _, _, _, source_url = rss.process_feed(feed, db_session, analyze_content=False)

    assert source_url is None
    assert feed.last_successful_url is None


def test_process_all_feeds_reports_fallback(db_session, monkeypatch):
    feed = make_feed(db_session)
    relocate_feed(db_session, feed, "https://new.example.com/feed")
    db_session.commit()

    def fake_get_feed_entries(url, lookback_days=7, max_retries=3):
        if url == "https://old.example.com/feed":
            return [make_entry("guid-old-1", "https://example.com/article-1")]
        return []

    monkeypatch.setattr(rss, "get_feed_entries", fake_get_feed_entries)
    result = rss.process_all_feeds(db_session, analyze_content=False)

    assert result["fallback_fetches"] == 1
    assert result["fallback_feeds"] == ["Example Feed"]


# --------------------------------------------------------------------------
# Schema migration and URL normalization
# --------------------------------------------------------------------------

def test_migration_adds_relocation_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE feeds (id INTEGER PRIMARY KEY, title VARCHAR(255), url VARCHAR(512))"
        ))
        conn.execute(text("CREATE TABLE articles (id INTEGER PRIMARY KEY)"))
        conn.commit()

    _migrate_schema(engine)

    feed_columns = {c["name"] for c in inspect(engine).get_columns("feeds")}
    assert {"previous_url", "url_updated_at", "url_history", "last_successful_url"} <= feed_columns
    article_columns = {c["name"] for c in inspect(engine).get_columns("articles")}
    assert "embedding_vector" in article_columns


def test_normalize_feed_url():
    assert normalize_feed_url("http://www.Example.com/feed/") == "example.com/feed"
    assert normalize_feed_url("https://example.com/feed") == "example.com/feed"
    assert normalize_feed_url("example.com/feed") == "example.com/feed"
    assert normalize_feed_url("") == ""
    assert normalize_feed_url(None) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
