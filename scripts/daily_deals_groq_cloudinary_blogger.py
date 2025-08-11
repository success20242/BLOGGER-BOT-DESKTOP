"""
daily_deals_groq_cloudinary_blogger.py

- Aggregates deal feeds from multiple sources
- Uploads deal images to Cloudinary
- Generates SEO-optimized posts via Groq LLM
- Publishes posts to Google Blogger
- Uses environment variables for config

Instructions:
- pip install python-dotenv feedparser requests google-auth-oauthlib google-api-python-client jinja2 cloudinary
- Create a .env file with required keys (see README)
- Place credentials.json (Google OAuth client) in the same folder
- Run once to authenticate with Google OAuth
"""

import os
import time
import re
import pickle
import feedparser
import requests
from datetime import datetime, timezone
from jinja2 import Template
from dotenv import load_dotenv

# Google API imports
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# Cloudinary SDK
import cloudinary
import cloudinary.uploader

# Load environment variables from .env file
load_dotenv()

# ========== CONFIG from .env ==========
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

BLOG_ID = os.getenv("BLOG_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = "token.pickle"

FEEDS = [
    "https://slickdeals.net/newsearch.php?src=SearchBarV2&q=&mode=rss",
    "https://www.reddit.com/r/deals/.rss",
    "https://www.reddit.com/r/GameDeals/.rss",
    "https://camelcamelcamel.com/top_drops.rss",
]

FEED_HOURS_BACK = int(os.getenv("FEED_HOURS_BACK", 72))
MAX_POSTS = int(os.getenv("MAX_POSTS_PER_RUN", 1))
SCOPES = ["https://www.googleapis.com/auth/blogger"]

POST_WRAPPER = Template("""
<div class="deal-post" style="font-family:sans-serif; max-width:700px; margin:auto; padding:15px; border:1px solid #ccc; border-radius:8px;">
  <h1 style="font-size:1.8em; margin-bottom:0.3em;">{{ title }}</h1>
  <p style="color:#555; font-size:0.9em; margin-top:0;">Source: {{ source }} • Posted: {{ posted_at }}</p>
  {% if image_url %}
  <p><img src="{{ image_url }}" alt="{{ title }}" style="max-width:100%; height:auto; border-radius:8px; margin-bottom:1em;"></p>
  {% endif %}
  <div class="content" style="font-size:1.1em; line-height:1.4em; margin-bottom:1em;">
    {{ body | safe }}
  </div>
  <p style="font-style: italic; color:#666;">Check the deal link below to buy / view more details.</p>
  <p><a href="{{ link }}" rel="nofollow noopener" target="_blank" style="display:inline-block; background:#007bff; color:#fff; padding:10px 20px; border-radius:6px; text-decoration:none;">View Deal</a></p>
</div>
""")

# ===================== Google Blogger OAuth =====================
def get_blogger_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not getattr(creds, "valid", False):
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("blogger", "v3", credentials=creds)

# ===================== Feed parsing =====================
def fetch_feed_entries(feed_urls, hours_back=72):
    cutoff = datetime.now(timezone.utc).timestamp() - (hours_back * 3600)
    entries = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                published_ts = None
                if hasattr(e, "published_parsed") and e.published_parsed:
                    published_ts = int(time.mktime(e.published_parsed))
                elif hasattr(e, "updated_parsed") and e.updated_parsed:
                    published_ts = int(time.mktime(e.updated_parsed))
                else:
                    published_ts = int(time.time())
                if published_ts >= cutoff:
                    entries.append({
                        "title": getattr(e, "title", "")[:250],
                        "link": getattr(e, "link", ""),
                        "source": feed.feed.get("title", url),
                        "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                        "published": datetime.fromtimestamp(published_ts, tz=timezone.utc),
                        "raw": e
                    })
        except Exception as ex:
            print(f"Feed parsing error for {url}: {ex}")
    # Deduplicate by link
    unique = {ent["link"]: ent for ent in entries}
    return list(unique.values())

# ===================== Image extraction =====================
def extract_image(entry):
    e = entry["raw"]
    if hasattr(e, "media_content"):
        m = e.media_content
        if isinstance(m, (list, tuple)) and m:
            url = m[0].get("url")
            if url:
                return url
    if hasattr(e, "links"):
        for l in e.links:
            if l.get("rel") == "enclosure" and l.get("type", "").startswith("image"):
                return l.get("href")
    import re
    m = re.search(r'<img[^>]+src=[\'"]([^\'"]+)[\'"]', entry["summary"] or "")
    if m:
        return m.group(1)
    return None

# ===================== Cloudinary image upload =====================
def init_cloudinary():
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
        raise RuntimeError("Cloudinary credentials missing in .env")
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True
    )

def upload_image_to_cloudinary(url):
    try:
        init_cloudinary()
        res = cloudinary.uploader.upload(url, folder="daily_deals", resource_type="image")
        return res.get("secure_url")
    except Exception as e:
        print(f"Cloudinary upload failed: {e}")
        return None

# ===================== Groq API call =====================
def groq_generate(prompt, system_prompt=None, model=None, max_tokens=600):
    model = model or GROQ_MODEL
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY missing in .env")
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    messages = []
    if system_prompt:
        messages.append({"role":"system", "content": system_prompt})
    messages.append({"role":"user", "content": prompt})
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3
    }
    response = requests.post(endpoint, headers=headers, json=body, timeout=60)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()

def build_groq_prompt(title, link, summary, image_url=None):
    return f"""
You are a helpful, concise ecommerce content writer specialized in short deal posts. 
Given the product info below, write a short, SEO-friendly post of 180-350 words in HTML (use <p>, <ul><li>, and <strong> for emphasis). 
Include a short catchy 8-12 word headline (on its own line), then the body. Keep tone persuasive and factual. 
At the end, include a one-line CTA: 'Buy now' with the product link.

Product title: {title}
Product link: {link}
Product summary / excerpt: {summary}
Image url: {image_url or 'none'}

Make sure to:
- Avoid copying long chunks verbatim from the source; summarize in your own words.
- Include 2–3 quick bullets of benefits.
- Provide one short 'who this is for' line.
- End with: <p><a href=\"{link}\" rel=\"nofollow noopener\" target=\"_blank\">Buy / View Deal</a></p>
"""

# ===================== Publish to Blogger =====================
def publish_post(service, blog_id, title, content_html, labels=None, is_draft=False):
    post = {
        "kind": "blogger#post",
        "blog": {"id": blog_id},
        "title": title,
        "content": content_html,
    }
    if labels:
        post["labels"] = labels
    result = service.posts().insert(blogId=blog_id, body=post, isDraft=is_draft).execute()
    return result

# ===================== Scoring & selection =====================
KEYWORDS = ["deal", "discount", "sale", "off", "save", "coupon", "clearance"]

def score_entry(entry):
    text = (entry["title"] + " " + entry["summary"]).lower()
    score = 0
    for kw in KEYWORDS:
        if kw in text:
            score += 2
    if "%" in text:
        score += 2
    return score

def select_top_entries(entries, max_posts):
    scored = [(score_entry(e), e) for e in entries]
    scored.sort(key=lambda x: x[0], reverse=True)
    filtered = [e for s,e in scored if s > 0]
    if not filtered:
        # fallback to top N even if score is 0
        filtered = [e for _, e in scored[:max_posts]]
    return filtered[:max_posts]

# ===================== MAIN WORKFLOW =====================
def run_once():
    print("Fetching feed entries...")
    entries = fetch_feed_entries(FEEDS, hours_back=FEED_HOURS_BACK)
    print(f"Found {len(entries)} entries.")
    if not entries:
        print("No entries found. Exiting.")
        return

    top_entries = select_top_entries(entries, MAX_POSTS)
    if not top_entries:
        print("No suitable entries after scoring. Exiting.")
        return

    service = get_blogger_service()

    for entry in top_entries:
        title = entry["title"]
        link = entry["link"]
        source = entry.get("source", "Unknown Source")
        summary = re.sub(r'<[^>]+>', '', entry.get("summary", ""))[:800]

        # Image hosting
        img_url = extract_image(entry)
        hosted_img_url = None
        if img_url:
            print(f"Uploading image to Cloudinary: {img_url}")
            hosted_img_url = upload_image_to_cloudinary(img_url)
            if hosted_img_url:
                print(f"Image uploaded: {hosted_img_url}")
            else:
                print("Cloudinary upload failed, using original image URL")
                hosted_img_url = img_url

        # Generate content with Groq LLM
        prompt = build_groq_prompt(title, link, summary, hosted_img_url)
        sys_prompt = "You are a concise, factual deal-writer. Produce clean HTML with a short headline line, then body in <p>, <ul><li> format."
        print(f"Generating content for: {title}")
        try:
            generated_content = groq_generate(prompt, system_prompt=sys_prompt, model=GROQ_MODEL)
        except Exception as e:
            print(f"Groq generation failed: {e}")
            generated_content = f"<p>{summary}</p>"

        # Wrap content in template
        post_html = POST_WRAPPER.render(
            title=title,
            source=source,
            posted_at=entry["published"].strftime("%Y-%m-%d %H:%M UTC"),
            image_url=hosted_img_url,
            body=generated_content,
            link=link,
        )

        # Publish to Blogger
        print(f"Publishing post: {title}")
        try:
            result = publish_post(service, BLOG_ID, title, post_html, labels=["deals","daily"], is_draft=False)
            print(f"Published successfully: {result.get('url')}")
        except Exception as e:
            print(f"Publishing failed: {e}")
        time.sleep(5)

if __name__ == "__main__":
    run_once()
