"""Feed relocation tracking.

When a public feed moves to a new address, the subscription's tracked
history (article associations, read/processing progress, statistics) must
stay attached to the original Feed record. This module registers such
relocations in place, supports rolling them back, and matches incoming
OPML entries to feeds that moved.

URL changes are applied to the existing Feed row (never delete + recreate),
so ``Feed.id`` and every ``Article.feed_id`` pointing at it stay valid.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from .models import Feed

logger = logging.getLogger(__name__)


def normalize_feed_url(url: Optional[str]) -> str:
    """Normalize a feed or website URL for comparison.

    Ignores scheme (http/https), a leading ``www.``, trailing slashes and
    host case, so cosmetic variants of the same address compare equal.
    """
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    candidate = url if "://" in url else f"http://{url}"
    parsed = urlparse(candidate)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    normalized = f"{netloc}{parsed.path.rstrip('/')}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized


def get_url_history(feed: Feed) -> List[Dict[str, Any]]:
    """Return the feed's relocation history as a list of event dicts."""
    if not feed.url_history:
        return []
    try:
        history = json.loads(feed.url_history)
        return history if isinstance(history, list) else []
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Feed {feed.id} has unreadable url_history; treating as empty")
        return []


def relocate_feed(
    db_session: Session,
    feed: Feed,
    new_url: str,
    source: str = "manual",
    reason: Optional[str] = None,
) -> bool:
    """Register the feed's move to a new address, preserving its identity.

    The Feed row keeps its ID, articles, read/processing progress and
    statistics; only the URL and relocation bookkeeping change. The old
    address is remembered as ``previous_url`` so ingestion can fall back
    to it and the move can be rolled back.

    Args:
        db_session: Database session (caller commits)
        feed: Feed to relocate
        new_url: The feed's new address
        source: Origin of the relocation ("manual", "opml-import", ...)
        reason: Optional free-text reason recorded in the history

    Returns:
        True if the relocation was applied, False if it was a no-op

    Raises:
        ValueError: If the new URL is empty or already used by another feed
    """
    new_url = (new_url or "").strip()
    if not new_url:
        raise ValueError("New URL must not be empty")
    if new_url == feed.url:
        return False

    conflict = db_session.query(Feed).filter(Feed.url == new_url, Feed.id != feed.id).first()
    if conflict:
        raise ValueError(
            f"Cannot relocate '{feed.title}': URL is already used by "
            f"feed '{conflict.title}' (id={conflict.id})"
        )

    now = datetime.utcnow()
    history = get_url_history(feed)
    history.append({
        "action": "relocate",
        "old_url": feed.url,
        "new_url": new_url,
        "changed_at": now.isoformat(),
        "source": source,
        "reason": reason,
    })

    logger.info(f"Relocating feed '{feed.title}' (id={feed.id}): {feed.url} -> {new_url}")
    feed.previous_url = feed.url
    feed.url = new_url
    feed.url_updated_at = now
    feed.url_history = json.dumps(history)
    # Neither address has served data in the new era yet; the next ingest
    # batch records which one actually delivers.
    feed.last_successful_url = None
    return True


def rollback_feed_relocation(db_session: Session, feed: Feed) -> Optional[str]:
    """Roll back the most recent relocation, restoring the previous URL.

    The abandoned URL is kept in the history, and the fallback pointer is
    moved one step further back along the relocation chain, so repeated
    rollbacks unwind multiple moves in order.

    Args:
        db_session: Database session (caller commits)
        feed: Feed to roll back

    Returns:
        The restored URL, or None if there is nothing to roll back to
    """
    if not feed.previous_url:
        return None

    now = datetime.utcnow()
    restored_url = feed.previous_url
    abandoned_url = feed.url

    history = get_url_history(feed)
    history.append({
        "action": "rollback",
        "old_url": abandoned_url,
        "new_url": restored_url,
        "changed_at": now.isoformat(),
        "source": "manual",
        "reason": None,
    })

    # The URL that preceded the restored one becomes the new fallback:
    # find the relocation that originally arrived at the restored URL.
    earlier_url = None
    for entry in reversed(history):
        if entry.get("action") == "relocate" and entry.get("new_url") == restored_url:
            earlier_url = entry.get("old_url")
            break

    logger.info(f"Rolling back feed '{feed.title}' (id={feed.id}): {abandoned_url} -> {restored_url}")
    feed.url = restored_url
    feed.previous_url = earlier_url if earlier_url != restored_url else None
    feed.url_updated_at = now
    feed.url_history = json.dumps(history)
    feed.last_successful_url = None
    return restored_url


def find_relocation_candidate(
    feeds: List[Feed],
    title: Optional[str],
    website_url: Optional[str],
) -> Optional[Feed]:
    """Match an OPML entry with an unknown URL to at most one existing feed.

    A feed qualifies if its website URL matches the entry's (both
    non-empty), or its title matches exactly and uniquely. Ambiguous
    matches return None rather than guessing wrong.

    Args:
        feeds: All existing feeds
        title: Entry title from the OPML outline
        website_url: Entry htmlUrl from the OPML outline

    Returns:
        The single matching Feed, or None
    """
    normalized_site = normalize_feed_url(website_url)
    if normalized_site:
        matches = [f for f in feeds if normalize_feed_url(f.website_url) == normalized_site]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                f"Website URL '{website_url}' matches {len(matches)} feeds; "
                "not guessing a relocation candidate"
            )
            return None

    normalized_title = (title or "").strip().lower()
    if normalized_title and normalized_title != "unknown feed":
        matches = [f for f in feeds if (f.title or "").strip().lower() == normalized_title]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                f"Title '{title}' matches {len(matches)} feeds; "
                "not guessing a relocation candidate"
            )

    return None
