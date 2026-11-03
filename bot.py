
import os
import re
import html
import json
import time
import hashlib
import logging
from collections import defaultdict, deque
from urllib.parse import urlparse, urljoin

import requests
import feedparser
from bs4 import BeautifulSoup

# ==========================================
# NEXTBYTE NEWS BOT
# ==========================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen.json"
MAX_STORIES = 10
REQUEST_TIMEOUT = 20
MAX_IMAGE_SIZE = 8 * 1024 * 1024
X_LIMIT = 280

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NEXTBYTE-NewsBot/1.0)"
}

# ==========================================
# NEWS SOURCES
# ==========================================

FEEDS = [
    # AI companies and research
    "https://openai.com/news/rss.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://huggingface.co/blog/feed.xml",
    "https://blog.google/technology/ai/rss/",

    # Independent AI journalism
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://arstechnica.com/ai/feed/",
    "https://www.wired.com/feed/tag/ai/latest/rss",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
    "https://the-decoder.com/feed/",

    # General technology news
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://www.wired.com/feed/rss",
    "https://www.engadget.com/rss.xml",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# ==========================================
# PUBLISHER NAMES
# ==========================================

PUBLISHERS = {
    "openai.com": "OpenAI",
    "deepmind.google": "Google DeepMind",
    "huggingface.co": "Hugging Face",
    "blog.google": "Google",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "arstechnica.com": "Ars Technica",
    "wired.com": "WIRED",
    "technologyreview.com": "MIT Technology Review",
    "the-decoder.com": "The Decoder",
    "engadget.com": "Engadget",
}


def get_publisher(url):
    host = urlparse(url).netloc.lower()
    host = host.removeprefix("www.")

    for domain, name in PUBLISHERS.items():
        if host == domain or host.endswith("." + domain):
            return name

    return host


# ==========================================
# TEXT HELPERS
# ==========================================

def clean_text(value):
    if not value:
        return ""

    soup = BeautifulSoup(str(value), "html.parser")
    text = soup.get_text(" ", strip=True)

    return re.sub(r"\s+", " ", text).strip()


def valid_article_url(url):
    if not url:
        return False

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    if not parsed.netloc:
        return False

    host = parsed.netloc.lower()

    blocked = (
        "abs.twimg.com",
        "pbs.twimg.com",
        "twemoji.maxcdn.com",
        "telegram.org",
    )

    if any(host.endswith(domain) for domain in blocked):
        return False

    if parsed.path.lower().endswith((
        ".jpg", ".jpeg", ".png", ".gif",
        ".webp", ".svg", ".avif", ".mp4"
    )):
        return False

    return True


def valid_image_url(url):
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    if not parsed.netloc:
        return False

    if parsed.path.lower().endswith(".svg"):
        return False

    return True


# ==========================================
# X CHARACTER COUNTER
# ==========================================

def x_weighted_length(text):
    # X counts recognized URLs as 23 characters.
    url_pattern = r"https?://[^\s]+"
    urls = re.findall(url_pattern, text)

    text_without_urls = re.sub(
        url_pattern, "", text
    )

    length = len(urls) * 23

    for char in text_without_urls:
        length += 1 if ord(char) <= 127 else 2

    return length


def make_x_post(story):
    title = clean_text(story["title"])
    description = clean_text(story.get("description", ""))
    publisher = story["publisher"]
    link = story["link"]

    title = title[:150]
    description = description[:100]

    def build():
        return (
            f"⚡ NEXTBYTE\n\n"
            f"{title}\n\n"
            f"{description}\n\n"
            f"Source: {publisher}\n"
            f"READ FULL STORY ↗\n"
            f"{link}\n\n"
            f"#AI #Tech"
        )

    # Shorten the description first.
    while x_weighted_length(build()) > X_LIMIT and description:
        description = description[:-5].rstrip()

    # Then shorten the title.
    while x_weighted_length(build()) > X_LIMIT and title:
        title = title[:-5].rstrip()

    # Compact fallback for unusually long URLs.
    if x_weighted_length(build()) > X_LIMIT:
        description = ""
        title = title[:60]

    return build()


# ==========================================
# SEEN STORIES
# ==========================================

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            return set(json.load(file))
    except Exception as error:
        logging.warning("Could not load seen.json: %s", error)
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen), file, indent=2)


def story_id(link):
    return hashlib.sha256(
        link.encode("utf-8")
    ).hexdigest()


# ==========================================
# ARTICLE LINKS
# ==========================================

def get_article_link(entry):
    candidates = []

    for item in entry.get("links", []):
        if item.get("rel", "alternate") == "alternate":
            candidates.append(item.get("href", ""))

    candidates.append(entry.get("link", ""))

    guid = entry.get("id", "")

    if guid.startswith("http"):
        candidates.append(guid)

    for candidate in candidates:
        candidate = html.unescape(str(candidate)).strip()

        if valid_article_url(candidate):
            return candidate

    return None


# ==========================================
# ARTICLE IMAGES
# ==========================================

def image_from_html(value):
    if not value:
        return None

    soup = BeautifulSoup(str(value), "html.parser")

    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original"):
            url = img.get(attr)

            if valid_image_url(url):
                return html.unescape(url)

    return None


def get_feed_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key, []):
            url = item.get("url")

            if valid_image_url(url):
                return html.unescape(url)

    for item in entry.get("enclosures", []):
        url = item.get("href") or item.get("url")

        if valid_image_url(url):
            return html.unescape(url)

    for item in entry.get("content", []):
        image = image_from_html(item.get("value", ""))

        if image:
            return image

    return image_from_html(entry.get("summary", ""))


def get_article_image(article_url):
    try:
        response = requests.get(
            article_url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for attribute, key in (
            ("property", "og:image"),
            ("name", "twitter:image"),
        ):
            tag = soup.find("meta", attrs={attribute: key})

            if tag:
                image_url = tag.get("content", "").strip()

                if valid_image_url(image_url):
                    return urljoin(article_url, image_url)

    except Exception as error:
        logging.warning(
            "Article image extraction failed: %s", error
        )

    return None


def download_image(image_url):
    if not valid_image_url(image_url):
        return None

    try:
        response = requests.get(
            image_url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if not content_type.startswith("image/"):
            return None

        content = response.content

        if not content or len(content) > MAX_IMAGE_SIZE:
            return None

        if content.startswith(b"\xff\xd8\xff"):
            extension = ".jpg"
            mime = "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = ".png"
            mime = "image/png"
        else:
            return None

        return content, extension, mime

    except Exception as error:
        logging.warning("Image download failed: %s", error)
        return None


# ==========================================
# TELEGRAM MESSAGE
# ==========================================

def make_telegram_post(story):
    title = clean_text(story["title"])
    description = clean_text(story.get("description", ""))

    # Keep the message short and readable.
    title = title[:180]
    description = description[:160]

    publisher = story["publisher"]
    link = story["link"]

    safe_title = html.escape(title)
    safe_description = html.escape(description)
    safe_publisher = html.escape(publisher)
    safe_link = html.escape(link, quote=True)

    return (
        f"⚡ <b>NEXTBYTE</b>\n\n"
        f"<b>{safe_title}</b>\n\n"
        f"{safe_description}\n\n"
        f"📰 Source: <b>{safe_publisher}</b>\n\n"
        f'🔗 <a href="{safe_link}">READ FULL STORY ↗</a>\n'
        f"{safe_link}\n\n"
        f"#AI #Tech"
    )


def telegram_send_photo(image_data, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    content, extension, mime = image_data

    files = {
        "photo": (
            f"nextbyte{extension}",
            content,
            mime
        )
    }

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML",
        },
        files=files,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Photo sending failed"))


def telegram_send_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Message sending failed"))


# ==========================================
# FETCH ALL NEWS
# ==========================================

def get_published_time(entry):
    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if parsed:
        try:
            return time.mktime(parsed)
        except Exception:
            pass

    return 0


def fetch_stories():
    stories = []
    failed_feeds = []

    for feed_url in FEEDS:
        logging.info("Checking feed: %s", feed_url)

        try:
            response = requests.get(
                feed_url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            if not feed.entries:
                logging.warning("Feed returned no entries: %s", feed_url)
                continue

            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))

                if not title:
                    continue

                link = get_article_link(entry)

                if not link:
                    continue

                description = clean_text(
                    entry.get("summary", "")
                )

                if not description and entry.get("content"):
                    description = clean_text(
                        entry["content"][0].get("value", "")
                    )

                stories.append({
                    "title": title,
                    "description": description,
                    "link": link,
                    "publisher": get_publisher(link),
                    "image": get_feed_image(entry),
                    "published_timestamp": get_published_time(entry),
                })

            logging.info(
                "Feed successful: %s (%s articles)",
                feed_url,
                len(feed.entries)
            )

        except Exception as error:
            failed_feeds.append(feed_url)
            logging.error(
                "Feed failed: %s | %s",
                feed_url,
                error
            )

    logging.info(
        "Feed results: %s successful, %s failed",
        len(FEEDS) - len(failed_feeds),
        len(failed_feeds)
    )

    return stories


# ==========================================
# DIVERSIFY PUBLISHERS
# ==========================================

def select_diverse_stories(stories, seen):
    """
    Sort newest stories first, then rotate publishers
    so one publisher does not dominate a batch.
    """

    unique = {}
    for story in stories:
        identifier = story_id(story["link"])

        if identifier in seen:
            continue

        unique[identifier] = story

    grouped = defaultdict(deque)

    ordered = sorted(
        unique.values(),
        key=lambda story: story["published_timestamp"],
        reverse=True
    )

    for story in ordered:
        grouped[story["publisher"]].append(story)

    selected = []

    while grouped and len(selected) < MAX_STORIES:
        for publisher in list(grouped.keys()):
            if grouped[publisher]:
                selected.append(grouped[publisher].popleft())

            if not grouped[publisher]:
                del grouped[publisher]

            if len(selected) >= MAX_STORIES:
                break

    return selected


# ==========================================
# PUBLISH STORIES
# ==========================================

def publish_story(story):
    caption = make_telegram_post(story)
    image_url = story.get("image")

    if not image_url:
        image_url = get_article_image(story["link"])

    if image_url:
        image_data = download_image(image_url)

        if image_data:
            try:
                telegram_send_photo(image_data, caption)
                logging.info(
                    "Published with image: %s",
                    story["title"]
                )
                return True

            except Exception as error:
                logging.warning(
                    "Photo publishing failed: %s",
                    error
                )

    try:
        telegram_send_message(caption)

        logging.info(
            "Published as text: %s",
            story["title"]
        )
        return True

    except Exception as error:
        logging.error(
            "Telegram publishing failed: %s",
            error
        )
        return False


# ==========================================
# MAIN
# ==========================================

def main():
    logging.info("================================")
    logging.info("NEXTBYTE NEWS BOT STARTED")
    logging.info("================================")

    seen = load_seen()

    stories = fetch_stories()

    logging.info(
        "Total articles collected: %s",
        len(stories)
    )

    selected = select_diverse_stories(stories, seen)

    logging.info(
        "New stories selected: %s",
        len(selected)
    )

    published = 0

    for story in selected:
        logging.info(
            "Publishing from %s: %s",
            story["publisher"],
            story["title"]
        )

        if publish_story(story):
            seen.add(story_id(story["link"]))
            save_seen(seen)
            published += 1
            time.sleep(2)

    logging.info("================================")
    logging.info("PUBLISHED: %s", published)
    logging.info("NEXTBYTE BOT FINISHED")
    logging.info("================================")


if __name__ == "__main__":
    main()
