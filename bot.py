
import os
import re
import json
import html
import uuid
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin

# ==========================================
# NEXTBYTE SETTINGS
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
MAX_IMAGE_SIZE = 8 * 1024 * 1024


# ==========================================
# HTML PARSERS
# ==========================================

class FeedHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag.lower() == "img":
            src = attrs.get("src") or attrs.get("data-src")
            if src:
                self.images.append(src)

        if tag.lower() == "a":
            href = attrs.get("href")
            if href:
                self.links.append(href)


class ArticleMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = None
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()

        if tag == "meta":
            key = (
                attrs.get("property")
                or attrs.get("name")
                or ""
            ).lower()

            if key in ("og:image", "twitter:image", "twitter:image:src"):
                value = attrs.get("content", "").strip()

                if value and not self.image:
                    self.image = value

        if tag == "link":
            rel = attrs.get("rel", "").lower().split()

            if "canonical" in rel:
                self.canonical = attrs.get("href")


# ==========================================
# GENERAL HELPERS
# ==========================================

def local_name(tag):
    return tag.split("}")[-1].lower()


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def valid_url(value):
    if not value:
        return False

    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()

        if parsed.scheme not in ("http", "https"):
            return False

        if not host or "." not in host:
            return False

        blocked_hosts = (
            "abs.twimg.com",
            "pbs.twimg.com",
            "twemoji.maxcdn.com",
        )

        if any(host == h or host.endswith("." + h) for h in blocked_hosts):
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
# EXTRACT ARTICLE LINK
# ==========================================

def get_article_link(item, feed_url):
    candidates = []

    for element in item:
        name = local_name(element.tag)

        if name == "link":
            href = element.attrib.get("href", "").strip()
            rel = element.attrib.get("rel", "alternate").lower()
            media_type = element.attrib.get("type", "").lower()

            if href and rel == "alternate":
                if media_type in ("", "text/html", "application/xhtml+xml"):
                    candidates.append(href)

            text_value = (element.text or "").strip()
            if text_value:
                candidates.append(text_value)

        elif name == "guid":
            value = (element.text or "").strip()
            is_permalink = element.attrib.get(
                "isPermaLink", "true"
            ).lower()

            if value and is_permalink != "false":
                candidates.append(value)

        elif name == "id":
            value = (element.text or "").strip()
            if value:
                candidates.append(value)

    # Links embedded in the feed description are a last resort.
    description = ""

    for element in item:
        if local_name(element.tag) in (
            "description", "summary", "encoded", "content"
        ):
            description += element.text or ""

    parser = FeedHTMLParser()

    try:
        parser.feed(description)
        candidates.extend(parser.links)
    except Exception:
        pass

    for candidate in candidates:
        candidate = urljoin(feed_url, html.unescape(candidate.strip()))

        if valid_url(candidate):
            return candidate

    return None


# ==========================================
# EXTRACT RSS IMAGE
# ==========================================

def get_feed_image(item, description, feed_url):
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

            if image_url.startswith(("http://", "https://")):
                return image_url

    parser = FeedHTMLParser()

    try:
        parser.feed(description or "")
    except Exception:
        pass

    for image_url in parser.images:
        image_url = urljoin(feed_url, image_url)

        if image_url.startswith(("http://", "https://")):
            return image_url

    return None


# ==========================================
# ARTICLE PAGE IMAGE FALLBACK
# ==========================================

def get_article_image(article_url):
    try:
        request = urllib.request.Request(
            article_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NEXTBYTE/2.0)",
                "Accept": "text/html"
            }
        )

        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read(1_500_000).decode(
                "utf-8", errors="replace"
            )
            final_url = response.geturl()

        parser = ArticleMetaParser()
        parser.feed(page)

        if parser.image:
            image_url = urljoin(final_url, parser.image)

            if image_url.startswith(("http://", "https://")):
                return image_url

    except Exception as error:
        print(f"Article image lookup failed: {error}")

    return None


# ==========================================
# DOWNLOAD IMAGE
# ==========================================

def download_image(image_url):
    if not image_url:
        return None

    try:
        request = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NEXTBYTE/2.0)",
                "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*"
            }
        )

        with urllib.request.urlopen(request, timeout=25) as response:
            content_type = response.headers.get("Content-Type", "").lower()

            if not content_type.startswith("image/"):
                print(f"Not an image: {content_type}")
                return None

            image_data = response.read(MAX_IMAGE_SIZE + 1)

            if len(image_data) > MAX_IMAGE_SIZE:
                print("Image too large.")
                return None

            if not image_data:
                return None

            return image_data

    except Exception as error:
        print(f"Image download failed: {error}")
        return None


# ==========================================
# READ RSS FEED
# ==========================================

def fetch_feed(feed_url):
    request = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "Mozilla/5.0 NEXTBYTE/2.0"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())

    stories = []

    for item in root.iter():
        if local_name(item.tag) not in ("item", "entry"):
            continue

        title = ""
        description = ""

        for element in item:
            name = local_name(element.tag)

            if name == "title" and not title:
                title = element.text or ""

            if name in ("description", "summary", "encoded", "content"):
                if element.text:
                    description = element.text
                    break

        title = clean_text(title)
        description = clean_text(description)

        link = get_article_link(item, feed_url)

        if not title or not link:
            print(f"Skipped invalid story: {title}")
            continue

        image = get_feed_image(item, description, feed_url)

        stories.append({
            "title": title,
            "link": link,
            "description": description,
            "image": image
        })

    return stories


# ==========================================
# TELEGRAM API
# ==========================================

def telegram_request(method, fields):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    data = urllib.parse.urlencode(fields).encode()

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read())

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    return result


def telegram_send_photo(image_data, caption):
    boundary = uuid.uuid4().hex
    body = bytearray()

    def add_field(name, value):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(str(value).encode())
        body.extend(b"\r\n")

    add_field("chat_id", CHAT_ID)
    add_field("caption", caption)
    add_field("parse_mode", "HTML")

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        b'Content-Disposition: form-data; name="photo"; filename="news.jpg"\r\n'
    )
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(image_data)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"
        }
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read())

    if not result.get("ok"):
        raise RuntimeError(f"Telegram photo error: {result}")

    return result


# ==========================================
# SHORT CLICKABLE POST
# ==========================================

def make_caption(story, with_image=True):
    title = html.escape(story["title"])
    description = html.escape(
        story["description"][:MAX_DESCRIPTION]
    )

    link = html.escape(story["link"], quote=True)

    caption = (
        "⚡ <b>NEXTBYTE</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{description}\n\n"
        f'<a href="{link}">READ FULL STORY ↗</a>\n\n'
        "#AI #Technology"
    )

    limit = 1000 if with_image else 3900

    if len(caption) > limit:
        description = html.escape(story["description"][:50])

        caption = (
            "⚡ <b>NEXTBYTE</b>\n\n"
            f"<b>{title}</b>\n\n"
            f"{description}\n\n"
            f'<a href="{link}">READ FULL STORY ↗</a>\n\n'
            "#AI #Technology"
        )

    return caption


# ==========================================
# PUBLISH STORY
# ==========================================

def send_story(story):
    # Try the feed image first, then the article's OG image.
    image_url = story.get("image")

    image_data = download_image(image_url) if image_url else None

    if not image_data:
        fallback_url = get_article_image(story["link"])

        if fallback_url:
            image_data = download_image(fallback_url)

    if image_data:
        try:
            caption = make_caption(story, with_image=True)
            telegram_send_photo(image_data, caption)

            print(f"Published with image: {story['title']}")
            return True

        except Exception as error:
            print(f"Photo publishing failed: {error}")

    # Text fallback still has a clickable article link.
    caption = make_caption(story, with_image=False)

    telegram_request("sendMessage", {
        "chat_id": CHAT_ID,
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    })

    print(f"Published text only: {story['title']}")
    return True


# ==========================================
# MAIN
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

            print(f"Feed checked: {feed} ({len(stories)} stories)")

            for story in stories:
                if story["link"] not in seen:
                    new_stories[story["link"]] = story

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
            print(f"Publishing failed: {story['title']} — {error}")

    SEEN_FILE.write_text(
        json.dumps(sorted(seen), indent=2),
        encoding="utf-8"
    )

    print("NEXTBYTE finished.")


if __name__ == "__main__":
    main()
