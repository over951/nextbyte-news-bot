
import os
import re
import json
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin

# ==========================================
# NEXTBYTE CONFIGURATION
# ==========================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

FEEDS = [
    "https://openai.com/news/rss.xml",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
    "https://blog.research.google/feeds/posts/default?alt=rss",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
]

SEEN_FILE = Path("seen.json")
MAX_STORIES = 10
MAX_DESCRIPTION = 150


# ==========================================
# HTML IMAGE PARSER
# ==========================================

class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return

        attrs = dict(attrs)
        src = attrs.get("src") or attrs.get("data-src")

        if src:
            self.images.append(src)


# ==========================================
# CLEAN TEXT
# ==========================================

def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def local_name(tag):
    return tag.split("}")[-1].lower()


# ==========================================
# ARTICLE LINK VALIDATION
# ==========================================

def valid_article_url(value):
    if not value:
        return False

    value = html.unescape(value.strip())

    try:
        parsed = urlparse(value)

        if parsed.scheme not in ("http", "https"):
            return False

        host = (parsed.hostname or "").lower()

        if not host or "." not in host:
            return False

        # Reject images, emoji files and other media.
        if host in (
            "abs.twimg.com",
            "pbs.twimg.com",
            "twemoji.maxcdn.com",
        ):
            return False

        if re.search(
            r"\.(svg|png|jpg|jpeg|gif|webp|ico)(?:$|\?)",
            parsed.path,
            re.I
        ):
            return False

        return True

    except Exception:
        return False


# ==========================================
# GET ARTICLE TITLE AND DESCRIPTION
# ==========================================

def get_field(item, names):
    for element in item:
        if local_name(element.tag) in names:
            if element.text:
                return element.text.strip()

    return ""


# ==========================================
# GET REAL ARTICLE LINK
# ==========================================

def get_link(item, feed_url):
    candidates = []

    # Atom feeds: prefer alternate HTML links.
    for element in item:
        if local_name(element.tag) == "link":
            href = element.attrib.get("href", "").strip()
            rel = element.attrib.get("rel", "alternate").lower()
            media_type = element.attrib.get("type", "").lower()

            if href and rel == "alternate":
                if media_type in (
                    "",
                    "text/html",
                    "application/xhtml+xml"
                ):
                    candidates.append(href)

    # RSS feeds: collect text links and GUIDs.
    for element in item:
        name = local_name(element.tag)

        if name in ("link", "guid", "id"):
            value = (element.text or "").strip()

            if name == "guid" and element.attrib.get("isPermaLink", "true").lower() == "false":
                continue

            if value:
                candidates.append(value)

    for candidate in candidates:
        candidate = urljoin(feed_url, candidate)

        if valid_article_url(candidate):
            return candidate

    return None


# ==========================================
# GET PUBLISHER IMAGE
# ==========================================

def find_image(item, description, feed_url):
    # Look for RSS media images.
    for element in item.iter():
        name = local_name(element.tag)

        if name in ("content", "thumbnail", "enclosure"):
            image_url = (
                element.attrib.get("url")
                or element.attrib.get("href")
            )

            media_type = element.attrib.get("type", "")

            if not image_url:
                continue

            if name == "enclosure":
                if not media_type.startswith("image/"):
                    if not re.search(
                        r"\.(jpg|jpeg|png|webp)(\?|$)",
                        image_url,
                        re.I
                    ):
                        continue

            image_url = urljoin(feed_url, image_url)

            if image_url.startswith(("https://", "http://")):
                return image_url

    # Look for images inside HTML descriptions.
    parser = ImageParser()

    try:
        parser.feed(description or "")
    except Exception:
        pass

    for image_url in parser.images:
        image_url = urljoin(feed_url, image_url)

        if image_url.startswith(("https://", "http://")):
            return image_url

    return None


# ==========================================
# READ RSS FEED
# ==========================================

def fetch_feed(feed_url):
    request = urllib.request.Request(
        feed_url,
        headers={
            "User-Agent": "Mozilla/5.0 NEXTBYTE-NewsBot/2.0"
        }
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())

    stories = []

    for item in root.iter():
        if local_name(item.tag) not in ("item", "entry"):
            continue

        title = get_field(item, ["title"])

        description = get_field(
            item,
            ["description", "summary", "encoded", "content"]
        )

        link = get_link(item, feed_url)

        if not title or not link:
            print(f"Skipped item with invalid article link: {title}")
            continue

        stories.append({
            "title": clean_text(title),
            "link": link,
            "description": clean_text(description),
            "image": find_image(item, description, feed_url)
        })

    return stories


# ==========================================
# TELEGRAM API
# ==========================================

def telegram_request(method, fields):
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    data = urllib.parse.urlencode(fields).encode()

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read())

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    return result


# ==========================================
# SHORT PROFESSIONAL CAPTION
# ==========================================

def make_caption(story):
    title = html.escape(story["title"])

    description = story["description"][:MAX_DESCRIPTION]
    description = html.escape(description)

    link = html.escape(story["link"], quote=True)

    caption = (
        "⚡ <b>NEXTBYTE</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{description}\n\n"
        f'<a href="{link}">READ FULL STORY ↗</a>\n\n'
        "#AI #Technology"
    )

    # Telegram photo captions: maximum 1024 characters.
    if len(caption) > 1000:
        description = html.escape(
            story["description"][:50]
        )

        caption = (
            "⚡ <b>NEXTBYTE</b>\n\n"
            f"<b>{title}</b>\n\n"
            f"{description}\n\n"
            f'<a href="{link}">READ FULL STORY ↗</a>\n\n'
            "#AI #Technology"
        )

    return caption[:1024]


# ==========================================
# PUBLISH STORY
# ==========================================

def send_story(story):
    caption = make_caption(story)

    image_url = story.get("image")

    if image_url:
        try:
            telegram_request("sendPhoto", {
                "chat_id": CHAT_ID,
                "photo": image_url,
                "caption": caption,
                "parse_mode": "HTML"
            })

            print(f"Published image: {story['title']}")
            return True

        except Exception as error:
            print(f"Image failed; trying text: {error}")

    telegram_request("sendMessage", {
        "chat_id": CHAT_ID,
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    })

    print(f"Published text: {story['title']}")
    return True


# ==========================================
# MAIN AUTOMATION
# ==========================================

def main():
    if SEEN_FILE.exists():
        try:
            seen = set(json.loads(
                SEEN_FILE.read_text(encoding="utf-8")
            ))
        except Exception:
            seen = set()
    else:
        seen = set()

    new_stories = {}

    for feed in FEEDS:
        try:
            stories = fetch_feed(feed)

            for story in stories:
                if story["link"] not in seen:
                    new_stories[story["link"]] = story

            print(f"Feed checked: {feed}")

        except Exception as error:
            print(f"Feed error: {feed} — {error}")

    stories = list(new_stories.values())[:MAX_STORIES]

    if not stories:
        print("No new stories found.")
        return

    print(f"Found {len(stories)} new stories.")

    for story in stories:
        try:
            if send_story(story):
                seen.add(story["link"])

        except Exception as error:
            print(f"Publishing failed: {error}")

    SEEN_FILE.write_text(
        json.dumps(sorted(seen), indent=2),
        encoding="utf-8"
    )

    print("NEXTBYTE automation finished.")


if __name__ == "__main__":
    main()
