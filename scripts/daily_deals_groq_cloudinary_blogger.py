#!/usr/bin/env python3
# daily_deals_groq_cloudinary_blogger_auto_model.py
# Fully integrated: Groq content + structured commentary + Cloudinary image + Blogger post
# Only posts RSS items from the last FEED_HOURS_BACK hours

import os
import json
import hashlib
import requests
import feedparser
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ------------------- CONFIGURATION -------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"  # OpenAI-compatible endpoint

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

BLOG_ID = os.getenv("BLOG_ID")
BLOGGER_OAUTH_TOKEN = os.getenv("BLOGGER_OAUTH_TOKEN")

FEEDS = [
    "https://slickdeals.net/newsearch.php?src=SearchBarV2&q=&mode=rss",
    "https://www.reddit.com/r/deals/.rss",
    "https://www.reddit.com/r/GameDeals/.rss",
    "https://camelcamelcamel.com/top_drops.rss",
]

MAX_POSTS = int(os.getenv("MAX_POSTS_PER_RUN", 1))
FEED_HOURS_BACK = int(os.getenv("FEED_HOURS_BACK", 72))
POSTED_LOG = "posted_links.json"

# ------------------- GROQ MODEL DYNAMIC SELECTION -------------------

def get_first_available_model():
    """
    Fetches available Groq models dynamically and returns the first one.
    """
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    models_url = "https://api.groq.com/openai/v1/models"
    try:
        response = requests.get(models_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        models = [m["id"] for m in data.get("data", [])]
        if models:
            print(f"[DEBUG] Available models: {models}")
            return models[0]  # pick the first available model
        else:
            print("[ERROR] No models returned by Groq API; defaulting to 'openai/gpt-oss-20b'")
            return "openai/gpt-oss-20b"
    except Exception as e:
        print(f"[ERROR] Failed to fetch Groq models: {e}")
        return "openai/gpt-oss-20b"

GROQ_MODEL = get_first_available_model()
print(f"[DEBUG] Using Groq model: {GROQ_MODEL}")

# ------------------- UTILITIES -------------------

def load_posted_links():
    if not os.path.exists(POSTED_LOG):
        return set()
    with open(POSTED_LOG, "r") as f:
        links = json.load(f)
        print(f"[DEBUG] Loaded {len(links)} posted links")
        return set(links)

def save_posted_link(link):
    posted = load_posted_links()
    posted.add(link)
    with open(POSTED_LOG, "w") as f:
        json.dump(list(posted), f)
    print(f"[DEBUG] Saved posted link: {link}")

def hash_text(text):
    import hashlib
    return hashlib.md5(text.encode()).hexdigest()

# ------------------- CLOUDINARY -------------------

def upload_image_to_cloudinary(image_url):
    print(f"[DEBUG] Uploading image to Cloudinary: {image_url}")
    upload_url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"
    try:
        response = requests.post(
            upload_url,
            data={"file": image_url, "upload_preset": "ml_default"},
            auth=(CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)
        )
        response.raise_for_status()
        result = response.json()
        secure_url = result.get("secure_url")
        print(f"[DEBUG] Cloudinary URL: {secure_url}")
        return secure_url
    except Exception as e:
        print(f"[ERROR] Cloudinary upload failed: {e}")
        return None

# ------------------- GROQ API -------------------

def groq_generate(prompt, max_tokens=300):
    """
    Generates text using the Groq OpenAI-compatible API endpoint
    """
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }

    try:
        response = requests.post(GROQ_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[ERROR] Groq API request failed: {e}")
        return "<p>Error generating content.</p>"

def generate_groq_content(title, summary, link):
    prompt = f"""
You are a professional e-commerce writer.
Generate a short, punchy headline for this deal and a clear description.
Make it engaging and concise in HTML format.
Product title: {title}
Summary: {summary}
Link: {link}
"""
    content = groq_generate(prompt, max_tokens=300)
    print(f"[DEBUG] Generated main content for {title}")
    return content

def generate_structured_commentary(title, summary, link):
    prompt = f"""
You are a human-like, persuasive e-commerce writer.
Write a 250-word original commentary for this product, summarizing:
- Top 3 pros
- Top 3 cons
- Who it is for
- Usage tips
Do NOT copy content from anywhere; write in your own words.
Product title: {title}
Summary: {summary}
Link: {link}
Output in HTML format with <ul><li>...</li></ul> for pros/cons
"""
    content = groq_generate(prompt, max_tokens=500)
    print(f"[DEBUG] Generated commentary for {title}")
    return content

# ------------------- BLOGGER POST -------------------

def publish_to_blogger(title, content, labels=None):
    url = f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/"
    headers = {
        "Authorization": f"Bearer {BLOGGER_OAUTH_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "kind": "blogger#post",
        "title": title,
        "content": content
    }
    if labels:
        data["labels"] = labels
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        post_data = response.json()
        print(f"[DEBUG] Published to Blogger: {post_data.get('url')}")
        return post_data
    except Exception as e:
        print(f"[ERROR] Blogger publish failed: {e}")
        return {}

# ------------------- MAIN SCRIPT -------------------

def run_once():
    posted_links = load_posted_links()
    posts_count = 0
    cutoff_time = datetime.utcnow() - timedelta(hours=FEED_HOURS_BACK)

    for feed_url in FEEDS:
        print(f"[DEBUG] Fetching feed: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[ERROR] Failed to parse feed {feed_url}: {e}")
            continue

        print(f"[DEBUG] Found {len(feed.entries)} entries in feed")
        for entry in feed.entries:
            if posts_count >= MAX_POSTS:
                print("[DEBUG] Reached max posts limit for this run")
                return

            # Skip old posts
            published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            if not published_parsed:
                print(f"[DEBUG] No timestamp found for {entry.get('title','Unknown')}. Skipping.")
                continue
            entry_time = datetime(*published_parsed[:6])
            if entry_time < cutoff_time:
                print(f"[DEBUG] Skipping old post: {entry.title} ({entry_time})")
                continue

            link = entry.link
            title = entry.title
            summary = entry.get("summary", "")

            if link in posted_links:
                print(f"[DEBUG] Already posted {link}. Skipping.")
                continue

            image_url = None
            if "media_content" in entry:
                image_url = entry.media_content[0].get("url")
            elif "media_thumbnail" in entry:
                image_url = entry.media_thumbnail[0].get("url")

            img_html = ""
            if image_url:
                cloud_image = upload_image_to_cloudinary(image_url)
                if cloud_image:
                    img_html = f'<img src="{cloud_image}" alt="{title}" style="max-width:100%;">'

            main_content = generate_groq_content(title, summary, link)
            commentary_html = generate_structured_commentary(title, summary, link)

            full_post_html = f"""
{img_html}
<h2>{title}</h2>
{main_content}
<div style="border:1px solid #ccc; padding:10px; margin-top:15px;">
    <h3>Commentary & Tips</h3>
    {commentary_html}
</div>
<p><a href="{link}" target="_blank">Check Deal</a></p>
"""

            response = publish_to_blogger(title, full_post_html, labels=["Deals", "Daily Deals"])
            if response.get("url"):
                save_posted_link(link)
                posts_count += 1
            else:
                print(f"[ERROR] Failed to save link for {title}")

if __name__ == "__main__":
    run_once()
