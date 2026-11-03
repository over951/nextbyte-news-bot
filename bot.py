import os
import re
import html
import json
import time
import hashlib
import logging
from urllib.parse import urlparse

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

# ---------------- HELPERS ----------------

def clean_text(value):
    if not value:
        return ""

    soup = BeautifulSoup(str(value), "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def valid_article_url(url):
    if not url:
        return False

    url = url.strip()

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    if not parsed.netloc:
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

    path = parsed.path.lower()

    if path.endswith((
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

    host = parsed.netloc.lower()

    blocked_hosts = (
        "abs.twimg.com",
        "twemoji.maxcdn.com",
    )

    if any(host.endswith(domain) for domain in blocked_hosts):
        return False

    if parsed.path.lower().endswith(".svg"):
        return False

    return True


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return set(data)
    except Exception:
        logging.warning("Could not read seen.json. Starting with an empty list.")
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen), file, indent=2)


def story_id(link, title):
    value = link or title
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------- ARTICLE LINKS ----------------

def get_article_link(entry):
    candidates = []

    # Prefer an RSS/Atom alternate article link.
    for item in entry.get("links", []):
        if item.get("rel", "alternate") == "alternate":
            candidates.append(item.get("href", ""))

    # Standard RSS link.
    candidates.append(entry.get("link", ""))

    # Some feeds use a permalink GUID.
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
    # RSS media:content and media:thumbnail
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key, []):
            url = item.get("url")

            if valid_image_url(url):
                return html.unescape(url)

    # RSS enclosure
    for item in entry.get("enclosures", []):
        url = item.get("href") or item.get("url")
        media_type = item.get("type", "")

        if url and (
            media_type.startswith("image/")
            or valid_image_url(url)
        ):
            if valid_image_url(url):
                return html.unescape(url)

    # Images inside article summary/content
    for item in entry.get("content", []):
        image = image_from_html(item.get("value", ""))
        if image:
            return image

    image = image_from_html(entry.get("summary", ""))
    if image:
        return image

    return None


def get_article_image(article_url):
    try:
        response = requests.get(
            article_url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Prefer publisher's social preview image.
        for attribute, key in (
            ("property", "og:image"),
            ("name", "twitter:image"),
        ):
            tag = soup.find("meta", attrs={attribute: key})

            if tag:
                image_url = tag.get("content", "").strip()

                if valid_image_url(image_url):
                    return requests.compat.urljoin(
                        article_url, image_url
                    )

    except Exception as error:
        logging.warning("Could not extract article image: %s", error)

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

        content_type = response.headers.get("Content-Type", "").lower()

        if not content_type.startswith("image/"):
            return None

        content = response.content

        if not content or len(content) > MAX_IMAGE_SIZE:
            return None

        # Telegram photo uploads should be standard image formats.
        if not content.startswith((
            b"\xff\xd8\xff",       # JPEG
            b"\x89PNG\r\n\x1a\n",  # PNG
        )):
            logging.info("Image is not JPEG or PNG; skipping photo upload.")
            return None

        extension = ".jpg" if content.startswith(b"\xff\xd8\xff") else ".png"

        return content, extension

    except Exception as error:
        logging.warning("Image download failed: %s", error)
        return None


# ---------------- TELEGRAM CAPTION ----------------

def make_caption(story):
    title = clean_text(story["title"])
    description = clean_text(story.get("description", ""))
    link = story["link"]

    # Keep the caption within Telegram's photo-caption limit.
    title = title[:180]
    description = description[:180]

    safe_title = html.escape(title)
    safe_description = html.escape(description)
    safe_link = html.escape(link, quote=True)

    caption = (
        f"⚡ <b>NEXTBYTE</b>\n\n"
        f"<b>{safe_title}</b>\n\n"
        f"{safe_description}\n\n"
        f'🔗 <a href="{safe_link}">READ FULL STORY ↗</a>\n\n'
        f"🔗 {safe_link}\n\n"
        f"#AI #Technology"
    )

    return caption


def make_text_message(story):
    title = clean_text(story["title"])[:180]
    description = clean_text(story.get("description", ""))[:300]
    link = story["link"]

    safe_title = html.escape(title)
    safe_description = html.escape(description)
    safe_link = html.escape(link, quote=True)

    return (
        f"⚡ <b>NEXTBYTE</b>\n\n"
        f"<b>{safe_title}</b>\n\n"
        f"{safe_description}\n\n"
        f'🔗 <a href="{safe_link}">READ FULL STORY ↗</a>\n\n'
        f"🔗 {safe_link}\n\n"
        f"#AI #Technology"
    )


# ---------------- TELEGRAM PUBLISHING ----------------

def telegram_send_photo(image_data, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    content, extension = image_data

    files = {
        "photo": (
            f"nextbyte{extension}",
            content,
            "image/jpeg" if extension == ".jpg" else "image/png"
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
                    logging.warning("Skipping story with no valid article URL: %s", title)
                    continue

                description = clean_text(
                    entry.get("summary", "")
                )

                if not description and entry.get("content"):
                    description = clean_text(
                        entry["content"][0].get("value", "")
                    )

                image = get_feed_image(entry)

                stories.append({
                    "title": title,
                    "description": description,
                    "link": link,
                    "image": image,
                })

        except Exception as error:
            logging.error("Feed failed: %s | %s", feed_url, error)

    return stories


# ---------------- MAIN ----------------

def main():
    logging.info("NEXTBYTE bot started.")

    seen = load_seen()
    stories = fetch_stories()

    logging.info("Found %s stories in RSS feeds.", len(stories))

    published = 0

    for story in stories:
        if published >= MAX_STORIES:
            break

        identifier = story_id(story["link"], story["title"])

        if identifier in seen:
            continue

        logging.info("Preparing: %s", story["title"])

        image_url = story.get("image")

        # If RSS has no image, look for the publisher's article preview image.
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
                    logging.warning("Photo post failed: %s", error)

        # If there is no usable image, publish a text-only post.
        if not sent:
            try:
                telegram_send_message(make_text_message(story))
                sent = True
                logging.info("Published as text.")
            except Exception as error:
                logging.error("Text post failed: %s", error)

        if sent:
            seen.add(identifier)
            published += 1
            save_seen(seen)
            time.sleep(2)

    logging.info("Finished. Published %s new stories.", published)


if __name__ == "__main__":
    main()
