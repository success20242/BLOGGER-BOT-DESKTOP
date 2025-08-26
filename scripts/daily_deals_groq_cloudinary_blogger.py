#!/usr/bin/env python3
# daily_deals_groq_cloudinary_blogger_fixed.py
# Fully integrated: Groq content + structured commentary + Cloudinary image + Blogger post
# Auto-refreshing Blogger token using token.pickle (self-healing + fallback flows)

import os
import json
import hashlib
import requests
import feedparser
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError  # <-- ADDED: to catch invalid_grant on refresh
import pickle
import sys  # <-- ADDED: for clear error messages

load_dotenv()

# ------------------- CONFIGURATION -------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

BLOG_ID = os.getenv("BLOG_ID")
# NOTE: We'll auto-detect client secret filename; this is kept for backwards-compat.
CLIENT_SECRET_FILE = os.getenv("GOOGLE_CLIENT_SECRET_FILE", "client_secret.json")
TOKEN_PICKLE = "token.pickle"

# OAuth scope for Blogger
SCOPES = ["https://www.googleapis.com/auth/blogger"]  # <-- ADDED: explicit scopes

FEEDS = [
    "https://slickdeals.net/newsearch.php?src=SearchBarV2&q=&mode=rss",
    "https://www.reddit.com/r/deals/.rss",
    "https://www.reddit.com/r/GameDeals/.rss",
    "https://camelcamelcamel.com/top_drops.rss",
]

MAX_POSTS = int(os.getenv("MAX_POSTS_PER_RUN", 1))
FEED_HOURS_BACK = int(os.getenv("FEED_HOURS_BACK", 72))
POSTED_LOG = "posted_links.json"

# ------------------- HELPERS (ADDED) -------------------

def _resolve_client_secret_file():
    """
    Find a valid Google OAuth client JSON. We try, in order:
    1) GOOGLE_CLIENT_SECRET_FILE env var (if points to an existing file)
    2) client_secret.json (default)
    3) credentials.json (common alternative name)
    """
    candidates = [
        os.getenv("GOOGLE_CLIENT_SECRET_FILE"),
        "client_secret.json",
        "credentials.json",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    raise FileNotFoundError(
        "Could not find Google OAuth client file. Expected one of: "
        "GOOGLE_CLIENT_SECRET_FILE, client_secret.json, credentials.json"
    )

def _save_creds(creds):
    try:
        with open(TOKEN_PICKLE, "wb") as f:
            pickle.dump(creds, f)
        print(f"[DEBUG] Saved new credentials to {TOKEN_PICKLE}")
    except Exception as e:
        print(f"[WARN] Failed to write {TOKEN_PICKLE}: {e}")

def _load_creds():
    if not os.path.exists(TOKEN_PICKLE):
        return None
    try:
        with open(TOKEN_PICKLE, "rb") as f:
            creds = pickle.load(f)
        return creds
    except Exception as e:
        print(f"[WARN] Failed to load {TOKEN_PICKLE}: {e}")
        return None

def _delete_token_pickle():
    try:
        if os.path.exists(TOKEN_PICKLE):
            os.remove(TOKEN_PICKLE)
            print(f"[DEBUG] Deleted stale {TOKEN_PICKLE}")
    except Exception as e:
        print(f"[WARN] Could not delete {TOKEN_PICKLE}: {e}")

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
        secure_url = response.json().get("secure_url")
        print(f"[DEBUG] Cloudinary URL: {secure_url}")
        return secure_url
    except Exception as e:
        print(f"[ERROR] Cloudinary upload failed: {e}")
        return None

# ------------------- GROQ API -------------------

def groq_generate(prompt, max_tokens=300):
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

# ------------------- BLOGGER AUTH (REWRITTEN) -------------------

def get_blogger_token():
    """
    Returns a valid OAuth access token for Blogger.
    - Loads token from token.pickle when available.
    - Refreshes when expired.
    - If refresh fails (invalid_grant / revoked), forces a clean re-auth.
    - Falls back to console flow if local server auth fails.
    """
    if not BLOG_ID:
        print("[ERROR] BLOG_ID is not set in environment variables.")
        sys.exit(1)

    creds = _load_creds()

    # If we already have valid creds, use them
    if creds and getattr(creds, "valid", False):
        return creds.token

    # Try to refresh existing creds
    if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        try:
            creds.refresh(Request())
            _save_creds(creds)
            return creds.token
        except RefreshError as e:
            # This is the classic: invalid_grant -> expired/revoked
            print(f"[WARN] Refresh failed (expired/revoked): {e}")
            _delete_token_pickle()
            creds = None
        except Exception as e:
            print(f"[WARN] Refresh failed: {e}")
            _delete_token_pickle()
            creds = None

    # If we reach here, we need a fresh authorization
    client_file = _resolve_client_secret_file()
    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)

    # Try local server flow first with offline access if supported by lib version
    try:
        try:
            # Some versions accept access_type/prompt directly
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        except TypeError:
            # Fallback: older versions without those kwargs
            creds = flow.run_local_server(port=0)
    except Exception as e:
        print(f"[WARN] Local-server OAuth failed, falling back to console flow: {e}")
        # Console fallback (works in headless / WSL / SSH)
        creds = flow.run_console()

    _save_creds(creds)
    return creds.token

# ------------------- BLOGGER POST (HARDENED) -------------------

def publish_to_blogger(title, content, labels=None):
    """
    Publishes a post to Blogger. If we hit a 401 (bad/expired token),
    we delete token.pickle and retry once automatically.
    """
    def _do_post(token):
        url = f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        data = {
            "kind": "blogger#post",
            "title": title,
            "content": content
        }
        if labels:
            data["labels"] = labels
        return requests.post(url, headers=headers, json=data)

    # First attempt
    token = get_blogger_token()
    response = _do_post(token)

    # If unauthorized, force re-auth once
    if response.status_code == 401:
        print("[WARN] Blogger returned 401. Forcing re-auth and retrying once.")
        _delete_token_pickle()
        token = get_blogger_token()
        response = _do_post(token)

    try:
        response.raise_for_status()
        post_data = response.json()
        print(f"[DEBUG] Published to Blogger: {post_data.get('url')}")
        return post_data
    except Exception as e:
        print(f"[ERROR] Blogger publish failed: {e} | Response: {getattr(response, 'text', '')}")
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
