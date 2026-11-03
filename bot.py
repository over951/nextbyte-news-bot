
import os
import json
import re
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlparse

# ==================================================
# NEXTBYTE CONFIGURATION
# ==================================================

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


# ==================================================
# IMAGE EXTRACTION
# ==================================================

class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            attrs = dict(attrs)
            image = attrs.get("src") or attrs.get("data-src")
            if image:
                self.images.append(image)


# ==================================================
# TEXT HELPERS
# ==================================================

def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def local_name(tag):
    return tag.split("}")[-1].lower()


def get_field(item, names):
    for element in item:
        if local_name(element.tag) in names:
            if element.text:
                return element.text.strip()
    return ""


def get_link(item):
    for element in item:
        if local_name(element.tag) == "link":
            href = element.attrib.get("href")
            if href:
                return href

            if element.text and element.text.strip():
                return element.text.strip()

    return get_field(item, ["guid", "id"])


def find_image(item, description):
    for element in item.iter():
        name = local_name(element.tag)

        if name in ("content", "thumbnail", "enclosure"):
            image_url = (
                element.attrib.get("url")
                or element.attrib.get("href")
            )

            media_type = element.attrib.get("type", "")

            if image_url and (
                name != "enclosure"
                or media_type.startswith("image/")
                or re.search(
                    r"\.(jpg|jpeg|png|webp)(\?|$)",
                    image_url,
                    re.I
                )
            ):
                return image_url

    parser = ImageParser()

    try:
        parser.feed(description or "")
    except Exception:
        pass

    if parser.images:
        return parser.images[0]

    return None


# ==================================================
# RSS FEED READER
# ==================================================

def fetch_feed(feed_url):
    request = urllib.request.Request(
        feed_url,
        headers={
            "User-Agent": "NEXTBYTE-NewsBot/1.0"
        }
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())

    stories = []

    for item in root.iter():
        if local_name(item.tag) not in ("item", "entry"):
            continue

        title = get_field(item, ["title"])
        link = get_link(item)

        description = get_field(
            item,
            ["description", "summary", "encoded", "content"]
        )

        if not title or not link:
            continue

        if not link.startswith(("http://", "https://")):
            continue

        stories.append({
            "title": clean_text(title),
            "link": link,
            "description": clean_text(description),
            "image": find_image(item, description)
        })

    return stories


# ==================================================
# TELEGRAM API
# ==================================================

def telegram_request(method, fields):
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    data = urllib.parse.urlencode(fields).encode()

    request = urllib.request.Request(
        url,
        data=data
    )

    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read())

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result


# ==================================================
# PROFESSIONAL NEXTBYTE POST
# ==================================================

def make_caption(story, description_limit=350):
    title = html.escape(story["title"])

    description = story["description"][:description_limit]
    description = html.escape(description)

    link = story["link"]
    safe_link = html.escape(link, quote=True)

    domain = urlparse(link).netloc.replace("www.", "")
    safe_domain = html.escape(domain)

    caption = (
        "⚡ <b>NEXTBYTE</b>\n"
        "<i>TECHNOLOGY INTELLIGENCE</i>\n\n"
        "🔵 <b>LATEST REPORT</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{description}\n\n"
        f'🔗 <a href="{safe_link}">READ THE FULL STORY</a>\n'
        f"<i>Source: {safe_domain}</i>\n\n"
        "<b>THE FUTURE, BEFORE EVERYONE.</b>\n"
        "#AI #Technology #NEXTBYTE"
    )

    # Telegram photo captions have a 1024-character limit.
    if len(caption) > 1000:
        return make_caption(story, max(50, description_limit - 50))

    return caption


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

            print(f"Published with image: {story['title']}")
            return True

        except Exception as error:
            print(f"Image failed; using text: {error}")

    telegram_request("sendMessage", {
        "chat_id": CHAT_ID,
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    })

    print(f"Published text: {story['title']}")
    return True


# ==================================================
# MAIN AUTOMATION
# ==================================================

def main():
    if SEEN_FILE.exists():
        try:
            seen = set(json.loads(SEEN_FILE.read_text()))
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

            print(f"Checked feed: {feed}")

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
            print(
                f"Could not publish "
                f"{story['title']}: {error}"
            )

    SEEN_FILE.write_text(
        json.dumps(sorted(seen), indent=2),
        encoding="utf-8"
    )

    print("NEXTBYTE automation completed.")


if __name__ == "__main__":
    main()
