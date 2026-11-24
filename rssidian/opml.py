import os
import xml.etree.ElementTree as ET
import xml.dom.minidom
from typing import List, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session

from .models import Feed
from .relocation import (
    find_relocation_candidate,
    get_url_history,
    normalize_feed_url,
    relocate_feed,
)


def parse_opml(file_path: str) -> List[Dict[str, Any]]:
    """
    Parse an OPML file and extract feed information.
    
    Args:
        file_path: Path to the OPML file
        
    Returns:
        List of dictionaries containing feed information
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"OPML file not found: {file_path}")
    
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        feeds = []
        
        # Find all outline elements that have an xmlUrl attribute
        for outline in root.findall(".//outline[@xmlUrl]"):
            feed = {
                "title": outline.get("title") or outline.get("text", "Unknown Feed"),
                "url": outline.get("xmlUrl"),
                "description": outline.get("description", ""),
                "website_url": outline.get("htmlUrl", "")
            }
            feeds.append(feed)
        
        return feeds
    
    except ET.ParseError as e:
        raise ValueError(f"Invalid OPML file: {e}")


def import_feeds_from_opml(file_path: str, db_session: Session) -> Dict[str, Any]:
    """
    Import feeds from an OPML file into the database.

    Feeds are matched to existing subscriptions in this order:

    1. Same URL (exact or cosmetically different) -> update in place. A
       cosmetic URL change (http->https, www, trailing slash) is registered
       as a relocation so the address history stays complete.
    2. URL known from a feed's relocation history -> the OPML is stale for
       this entry; skip it instead of creating a duplicate subscription.
    3. Same website URL or unique same title but an unseen URL -> the feed
       moved; register a relocation on the existing Feed row so its ID,
       articles and read progress carry over.
    4. Otherwise -> create a new feed.

    Args:
        file_path: Path to the OPML file
        db_session: SQLAlchemy database session

    Returns:
        Dict with counts (new, updated, relocated, skipped, errors) and a
        "relocations" list of {feed_id, title, old_url, new_url} details
    """
    feeds_data = parse_opml(file_path)

    stats = {"new": 0, "updated": 0, "relocated": 0, "skipped": 0, "errors": 0}
    relocations: List[Dict[str, Any]] = []

    # Index existing feeds once; updated as feeds are relocated/created.
    all_feeds: List[Feed] = db_session.query(Feed).all()
    by_current_url: Dict[str, Feed] = {}
    by_historical_url: Dict[str, Feed] = {}
    for feed in all_feeds:
        by_current_url.setdefault(normalize_feed_url(feed.url), feed)
        for event in get_url_history(feed):
            by_historical_url.setdefault(normalize_feed_url(event.get("old_url")), feed)
            by_historical_url.setdefault(normalize_feed_url(event.get("new_url")), feed)

    def register_current(feed: Feed, url: str) -> None:
        by_current_url[normalize_feed_url(url)] = feed
        by_historical_url.setdefault(normalize_feed_url(url), feed)

    def retire_current(feed: Feed, url: str) -> None:
        """Move a URL from the current index to the historical one."""
        key = normalize_feed_url(url)
        if by_current_url.get(key) is feed:
            del by_current_url[key]
        by_historical_url.setdefault(key, feed)

    def known_urls_of(feed: Feed) -> set:
        """Exact URLs this feed currently uses or ever moved away from."""
        urls = {feed.url}
        for event in get_url_history(feed):
            urls.add(event.get("old_url"))
            urls.add(event.get("new_url"))
        return urls

    def apply_relocation(feed: Feed, new_url: str, old_url: str) -> None:
        retire_current(feed, old_url)
        register_current(feed, new_url)
        stats["relocated"] += 1
        relocations.append({
            "feed_id": feed.id,
            "title": feed.title,
            "old_url": old_url,
            "new_url": new_url,
        })

    for feed_data in feeds_data:
        try:
            url = (feed_data["url"] or "").strip()
            normalized = normalize_feed_url(url)

            existing_feed = by_current_url.get(normalized)

            if existing_feed and existing_feed.url == url:
                # 1. Exact match: refresh metadata only
                existing_feed.title = feed_data["title"]
                existing_feed.description = feed_data["description"]
                existing_feed.website_url = feed_data["website_url"]
                stats["updated"] += 1
                db_session.commit()
            elif existing_feed and url in known_urls_of(existing_feed):
                # 2. Cosmetic variant of an address this feed already moved
                # away from: the OPML is stale for this entry
                print(
                    f"Skipping '{feed_data['title']}': {url} was already relocated "
                    f"(now tracked as {existing_feed.url})"
                )
                stats["skipped"] += 1
            elif existing_feed:
                # 3. Same address with cosmetic differences (scheme, www,
                # trailing slash): record the URL change on the same feed
                old_url = existing_feed.url
                relocate_feed(db_session, existing_feed, url, source="opml-import")
                existing_feed.title = feed_data["title"]
                existing_feed.description = feed_data["description"]
                existing_feed.website_url = feed_data["website_url"]
                apply_relocation(existing_feed, url, old_url)
                db_session.commit()
            elif normalized in by_historical_url:
                # 4. This address was already moved away from: stale OPML
                moved_to = by_historical_url[normalized]
                print(
                    f"Skipping '{feed_data['title']}': {url} was already relocated "
                    f"(now tracked as {moved_to.url})"
                )
                stats["skipped"] += 1
            else:
                # 5. Unseen URL: is this an existing feed that moved?
                candidate = find_relocation_candidate(
                    all_feeds, feed_data["title"], feed_data["website_url"]
                )
                if candidate:
                    old_url = candidate.url
                    relocate_feed(db_session, candidate, url, source="opml-import")
                    candidate.title = feed_data["title"]
                    candidate.description = feed_data["description"]
                    candidate.website_url = feed_data["website_url"]
                    apply_relocation(candidate, url, old_url)
                else:
                    # 6. Genuinely new feed
                    new_feed = Feed(
                        title=feed_data["title"],
                        url=url,
                        description=feed_data["description"],
                        website_url=feed_data["website_url"],
                        last_updated=datetime.utcnow(),
                        last_checked=datetime.utcnow()
                    )
                    db_session.add(new_feed)
                    all_feeds.append(new_feed)
                    register_current(new_feed, url)
                    stats["new"] += 1
                db_session.commit()
        except Exception as e:
            # Roll back the transaction if there's an error
            db_session.rollback()
            stats["errors"] += 1
            print(f"Error importing feed {feed_data['title']}: {e}")
            # Continue with the next feed

    stats["relocations"] = relocations
    return stats


def export_feeds_to_opml(db_session: Session, include_muted: bool = True) -> str:
    """
    Export feeds from the database to OPML format.
    
    Args:
        db_session: SQLAlchemy database session
        include_muted: Whether to include muted feeds in the export
        
    Returns:
        OPML content as a string
    """
    # Create the OPML structure
    root = ET.Element("opml")
    root.set("version", "2.0")
    
    # Add the head element
    head = ET.SubElement(root, "head")
    title = ET.SubElement(head, "title")
    title.text = "RSSidian Feed Export"
    date_created = ET.SubElement(head, "dateCreated")
    date_created.text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S %z")
    
    # Add the body element
    body = ET.SubElement(root, "body")
    
    # Query feeds from the database
    query = db_session.query(Feed)
    if not include_muted:
        query = query.filter_by(muted=False)
    
    feeds = query.order_by(Feed.title).all()
    
    # Add feeds to the OPML file
    for feed in feeds:
        outline = ET.SubElement(body, "outline")
        outline.set("text", feed.title)
        outline.set("title", feed.title)
        outline.set("type", "rss")
        outline.set("xmlUrl", feed.url)
        
        if feed.website_url:
            outline.set("htmlUrl", feed.website_url)
        
        if feed.description:
            outline.set("description", feed.description)
        
        # Add custom attributes for RSSidian-specific features
        if feed.muted:
            outline.set("muted", "true")
            if feed.muted_reason:
                outline.set("mutedReason", feed.muted_reason)
        
        if feed.peer_through:
            outline.set("peerThrough", "true")
    
    # Format the XML with proper indentation
    rough_string = ET.tostring(root, encoding="unicode", method="xml")
    reparsed = xml.dom.minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")