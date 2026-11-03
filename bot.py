
import os
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

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


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def get_text(element, names):
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def fetch_feed(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NEXTBYTE-NewsBot/1.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())

    stories = []

    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue

        title = get_text(item, ["title"])
        link = get_text(item, ["link", "guid"])
        description = get_text(
            item, ["description", "summary", "content"]
        )

        if not link:
            link_element = item.find("link")
            if link_element is not None:
                link = link_element.attrib.get("href", "")

        if title and link:
            stories.append({
                "title": title,
                "link": link,
                "description": description
            })

    return stories


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": "true"
    }).encode()

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read())

    if not result.get("ok"):
        raise RuntimeError("Telegram did not accept the message")


def main():
    seen = load_seen()
    new_stories = []

    for feed in FEEDS:
        try:
            for story in fetch_feed(feed):
                if story["link"] not in seen:
                    new_stories.append(story)
        except Exception as error:
            print(f"Feed error: {feed} — {error}")

    # Remove duplicate links
    unique = {}
    for story in new_stories:
        unique[story["link"]] = story

    stories = list(unique.values())[:MAX_STORIES]

    if not stories:
        print("No new stories found.")
        return

    for story in stories:
        description = story["description"][:350].strip()
        message = (
            f"⚡ NEXTBYTE NEWS\n\n"
            f"{story['title']}\n\n"
            f"{description}\n\n"
            f"Source: {story['link']}\n\n"
            f"#AI #Technology"
        )

        send_telegram(message)
        seen.add(story["link"])
        print(f"Sent: {story['title']}")

    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


if __name__ == "__main__":
    main()
