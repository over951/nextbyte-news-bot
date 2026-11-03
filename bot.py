import os
import re
import html
import json
import time
import hashlib
import logging
from urllib.parse import urlparse, urljoin

import requests
import feedparser
from bs4 import BeautifulSoup

# ---------------- SETTINGS ----------------

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen.json"
MAX_STORIES = 10
REQUEST_TIMEOUT = 20
MAX_IMAGE_SIZE = 8 * 1024 * 1024
X_CHARACTER_LIMIT = 280

FEEDS = [
    "https://openai.com/news/rss.xml",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
    "https://blog.research.google/feeds/posts/default?alt=rss",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NEXTBYTE-NewsBot/1.0)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)


# ---------------- TEXT HELPERS ----------------

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

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    host = parsed.netloc.lower()

    blocked_hosts = (
        "abs.twimg.com",
        "pbs.twimg.com",
        "twemoji.maxcdn.com",
        "telegram.org",
    )

    if any(host.endswith(domain) for domain in blocked_hosts):
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

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    if parsed.path.lower().endswith(".svg"):
        return False

    return True


# ---------------- X CHARACTER COUNT ----------------

def x_weighted_length(text):
    """
    Approximate X's post length:
    - URLs count as 23 characters.
    - Non-ASCII characters are conservatively counted as 2.
    """

    url_pattern = r"https?://[^\s]+"
    urls = re.findall(url_pattern, text)

    text_without_urls = re.sub(url_pattern, "", text)

    length = len(urls) * 23

    for char in text_without_urls:
        if ord(char) > 127:
            length += 2
        else:
            length += 1

    return length


def make_post_text(story):
    """
    Create a short post suitable for copying into X.
    Keep the original article URL intact.
    """

    title = clean_text(story.get("title", "NEXTBYTE News"))
    description = clean_text(story.get("description", ""))
    link = story["link"]

    title = title[:160]
    description = description[:120]

    hashtags = "#AI #Tech"

    def build():
        return (
            f"⚡ NEXTBYTE\n\n"
            f"{title}\n\n"
            f"{description}\n\n"
            f"READ FULL STORY ↗\n"
            f"{link}\n\n"
            f"{hashtags}"
        )

    # Shorten the description first.
    while x_weighted_length(build()) > X_CHARACTER_LIMIT and description:
        description = description[:-5].rstrip()

    # Then shorten the title if necessary.
    while x_weighted_length(build()) > X_CHARACTER_LIMIT and title:
        title = title[:-5].rstrip()

    # Last-resort compact format if the original URL is unusually long.
    if x_weighted_length(build()) > X_CHARACTER_LIMIT:
        title = title[:70]
        description = ""

    post = build()

    logging.info(
        "X post length estimate: %s",
        x_weighted_length(post)
    )

    return post


# ---------------- SEEN STORIES ----------------

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            return set(json.load(file))
    except Exception:
        logging.warning("Could not read seen.json.")
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen), file, indent=2)


def story_id(link, title):
    value = link or title
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


# ---------------- ARTICLE LINKS ----------------

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


# ---------------- IMAGE EXTRACTION ----------------

def image_from_html(value):
    if not value:
        return None

    soup = BeautifulSoup(str(value), "html.parser")

    for img in soup.find_all("img"):
        for attribute in ("src", "data-src", "data-original"):
            url = img.get(attribute)

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
            "Could not extract article image: %s",
            error
        )

    return None


def download_image(image_url):
    if not valid_image_url(image_url):
        return None

    try:
        response = requests.get(
            image_url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            stream=True
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
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = ".png"
        else:
            logging.info("Unsupported image format.")
            return None

        return content, extension

    except Exception as error:
        logging.warning("Image download failed: %s", error)
        return None


# ---------------- TELEGRAM FORMATTING ----------------

def make_caption(story):
    post = make_post_text(story)

    title = html.escape(clean_text(story.get("title", "")))
    link = html.escape(story["link"], quote=True)

    # Keep the same short post text for X.
    # Make the READ FULL STORY label clickable in Telegram.
    escaped_post = html.escape(post)

    linked_post = escaped_post.replace(
        "READ FULL STORY ↗",
        f'<a href="{link}">READ FULL STORY ↗</a>',
        1
    )

    return linked_post


def make_text_message(story):
    return make_caption(story)


# ---------------- TELEGRAM PUBLISHING ----------------

def telegram_send_photo(image_data, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    content, extension = image_data

    mime_type = (
        "image/jpeg" if extension == ".jpg"
        else "image/png"
    )

    files = {
        "photo": (
            f"nextbyte{extension}",
            content,
            mime_type
        )
    }

    data = {
        "chat_id": CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML",
    }

    response = requests.post(
        url,
        data=data,
        files=files,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram photo error"))

    return True


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
        raise RuntimeError(result.get("description", "Telegram message error"))

    return True


# ---------------- RSS PROCESSING ----------------

def fetch_stories():
    stories = []

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

            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))

                if not title:
                    continue

                link = get_article_link(entry)

                if not link:
                    logging.warning(
                        "Skipping story without a valid article URL: %s",
                        title
                    )
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
                    "image": get_feed_image(entry),
                })

        except Exception as error:
            logging.error(
                "Feed failed: %s | %s",
                feed_url,
                error
            )

    return stories


# ---------------- MAIN ----------------

def main():
    logging.info("NEXTBYTE bot started.")

    seen = load_seen()
    stories = fetch_stories()

    logging.info(
        "Found %s stories in RSS feeds.",
        len(stories)
    )

    published = 0

    for story in stories:
        if published >= MAX_STORIES:
            break

        identifier = story_id(
            story["link"],
            story["title"]
        )

        if identifier in seen:
            continue

        logging.info(
            "Preparing story: %s",
            story["title"]
        )

        image_url = story.get("image")

        if not image_url:
            image_url = get_article_image(story["link"])

        sent = False

        if image_url:
            image_data = download_image(image_url)

            if image_data:
                try:
                    telegram_send_photo(
                        image_data,
                        make_caption(story)
                    )

                    sent = True
                    logging.info("Published with image.")

                except Exception as error:
                    logging.warning(
                        "Photo post failed: %s",
                        error
                    )

        if not sent:
            try:
                telegram_send_message(
                    make_text_message(story)
                )

                sent = True
                logging.info("Published as text.")

            except Exception as error:
                logging.error(
                    "Text post failed: %s",
                    error
                )

        if sent:
            seen.add(identifier)
            published += 1
            save_seen(seen)
            time.sleep(2)

    logging.info(
        "Finished. Published %s new stories.",
        published
    )


if __name__ == "__main__":
    main()
