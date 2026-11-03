
import os
import json
import re
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from html.parser import HTMLParser

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

FEEDS = [
    "https://openai.com/news/rss.xml",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
    "https://blog.research.google/feeds/posts/default?alt=rss",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
]

SEEN_FILE = Path("seen.json")
MAX_STORIES = 5


class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            attrs = dict(attrs)
            src = attrs.get("src") or attrs.get("data-src")
            if src:
                self.images.append(src)


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def local_name(tag):
    return tag.split("}")[-1].lower()


def find_image(item, description):
    # Look for RSS media:content, media:thumbnail and enclosure images
    for element in item.iter():
        name = local_name(element.tag)

        if name in ("content", "thumbnail", "enclosure"):
            url = element.attrib.get("url") or element.attrib.get("href")
            media_type = element.attrib.get("type", "")

            if url and (
                name != "enclosure"
                or media_type.startswith("image/")
                or re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.I)
            ):
                return url

    # Look for an image embedded in the article description
    parser = ImageParser()
    try:
        parser.feed(description or "")
    except Exception:
        pass

    if parser.images:
        return parser.images[0]

    return None


def get_field(item, names):
    for element in item:
        if local_name(element.tag) in names:
            if element.text:
                return element.text.strip()

    return ""


def fetch_feed(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NEXTBYTE-NewsBot/1.0"}
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        root = ET.fromstring(response.read())

    stories = []

    for item in root.iter():
        if local_name(item.tag) not in ("item", "entry"):
            continue

        title = get_field(item, ["title"])
        link = get_field(item, ["link", "guid"])
        description = get_field(
            item, ["description", "summary", "encoded", "content"]
        )

        if not link:
            for element in item:
                if local_name(element.tag) == "link":
                    link = element.attrib.get("href", "")
                    if link:
                        break

        if title and link:
            stories.append({
                "title": clean_text(title),
                "link": link,
                "description": clean_text(description),
                "image": find_image(item, description)
            })

    return stories


def telegram_request(method, fields):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(fields).encode()

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=40) as response:
        result = json.loads(response.read())

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    return result


def send_story(story):
    title = story["title"]
    description = story["description"][:450]
    link = story["link"]

    caption = (
        f"⚡ NEXTBYTE | TECHNOLOGY BRIEFING\n\n"
        f"{title}\n\n"
        f"{description}\n\n"
        f"READ THE ORIGINAL REPORT:\n{link}\n\n"
        f"#AI #Technology"
    )

    # Telegram photo captions have a 1024-character limit
    caption = caption[:1000]

    image_url = story.get("image")

    if image_url:
        try:
            telegram_request("sendPhoto", {
                "chat_id": CHAT_ID,
                "photo": image_url,
                "caption": caption
            })
            print(f"Sent image: {title}")
            return
        except Exception as error:
            print(f"Image unavailable, sending text instead: {error}")

    telegram_request("sendMessage", {
        "chat_id": CHAT_ID,
        "text": caption,
        "disable_web_page_preview": "false"
    })

    print(f"Sent text: {title}")


def main():
    seen = set()

    if SEEN_FILE.exists():
        seen = set(json.loads(SEEN_FILE.read_text()))

    new_stories = {}

    for feed in FEEDS:
        try:
            for story in fetch_feed(feed):
                if story["link"] not in seen:
                    new_stories[story["link"]] = story
        except Exception as error:
            print(f"Feed error: {feed} — {error}")

    stories = list(new_stories.values())[:MAX_STORIES]

    if not stories:
        print("No new stories found.")
        return

    for story in stories:
        try:
            send_story(story)
            seen.add(story["link"])
        except Exception as error:
            print(f"Could not send story: {error}")

    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


if __name__ == "__main__":
    main()
