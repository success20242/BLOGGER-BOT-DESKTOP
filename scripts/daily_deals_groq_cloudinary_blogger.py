"""
daily_deals_groq_cloudinary_blogger.py

- Aggregates deal feeds from multiple sources
- Uploads deal images to Cloudinary
- Generates SEO-optimized posts via Groq LLM with strict mode always on
- Publishes posts to Google Blogger
- Uses environment variables for config
"""

import os
import time
import re
import pickle
import feedparser
import requests
import difflib
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

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

BLOG_ID = os.getenv("BLOG_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = "token.pickle"
POSTED_LINKS_FILE = "posted_links.txt"

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

# -------- Persistent posted links cache --------
def load_posted_links():
    if not os.path.exists(POSTED_LINKS_FILE):
        return set()
    with open(POSTED_LINKS_FILE, "r") as f:
        return set(line.strip() for line in f.readlines())

def save_posted_link(link):
    with open(POSTED_LINKS_FILE, "a") as f:
        f.write(link + "\n")

# -------- Google Blogger OAuth --------
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

# -------- Fetch recent post titles for similarity checks --------
def fetch_recent_post_titles(service, blog_id, max_posts=20):
    try:
        posts = service.posts().list(blogId=blog_id, maxResults=max_posts).execute()
        return [post["title"] for post in posts.get("items", [])]
    except Exception as e:
        print(f"Error fetching recent posts: {e}")
        return []

# -------- Check similarity --------
def is_similar(text1, text2, threshold=0.85):
    return difflib.SequenceMatcher(None, text1.lower(), text2.lower()).ratio() > threshold

# -------- Feed parsing with deduplication --------
def fetch_feed_entries(feed_urls, hours_back=72, seen_links=set()):
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
                if published_ts < cutoff:
                    continue
                link = getattr(e, "link", "")
                if link in seen_links:
                    continue
                entries.append({
                    "title": getattr(e, "title", "")[:250],
                    "link": link,
                    "source": feed.feed.get("title", url),
                    "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                    "published": datetime.fromtimestamp(published_ts, tz=timezone.utc),
                    "raw": e
                })
        except Exception as ex:
            print(f"Feed parsing error for {url}: {ex}")
    return entries

# -------- Image extraction --------
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

# -------- Cloudinary --------
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

# -------- Groq API call --------
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

# -------- Extract discount info for enrichment --------
def extract_discount_info(title):
    m = re.findall(r"(\d{1,3}% off|\d{1,3}% OFF|\d{1,3}% discount|\d{1,3}% SALE)", title, re.IGNORECASE)
    if m:
        print(f"Discount info found: {m}")
    else:
        print("No explicit discount info found; skipping discount mention.")
    return ", ".join(m) if m else None

# -------- Extract ratings or social proof --------
def extract_social_proof(text):
    rating_pattern = re.compile(r"(\d(\.\d)?\s?stars?)|(\d{2,}\s?reviews?)", re.IGNORECASE)
    matches = rating_pattern.findall(text)
    if matches:
        print(f"Social proof found: {matches}")
        return True
    else:
        print("No valid social proof found; skipping.")
        return False

# -------- Build Groq prompt --------
def build_groq_prompt(title, link, summary, image_url=None):
    discount_info = extract_discount_info(title)
    social_proof_present = extract_social_proof(summary)
    discount_line = f"Highlight discounts: {discount_info}." if discount_info else ""
    social_proof_line = "Include social proof such as ratings or reviews only if explicitly present." if social_proof_present else ""

    prompt = f"""
You are a helpful, concise ecommerce content writer specialized in short deal posts. 
Given the product info below, write a short, SEO-friendly post of 180-350 words in HTML (use <p>, <ul><li>, and <strong> for emphasis). 
Include a short catchy 8-12 word headline (on its own line), then the body. Keep tone persuasive and factual. 
At the end, include a one-line CTA: 'Buy now' with the product link.

Product title: {title}
Product link: {link}
Product summary / excerpt: {summary}
Image url: {image_url or 'none'}

{discount_line}
{social_proof_line}

Make sure to:
- Avoid copying long chunks verbatim from the source; summarize in your own words.
- Include 2–3 quick bullets of benefits.
- Provide one short 'who this is for' line.
- Do NOT fabricate any ratings, social proof, or numeric claims.
- End with: <p><a href=\"{link}\" rel=\"nofollow noopener\" target=\"_blank\">Buy / View Deal</a></p>
"""
    print("Groq prompt built with strict mode.")
    return prompt

# -------- Generate tags strictly from keywords found --------
KEYWORDS = ["deal", "discount", "sale", "off", "save", "coupon", "clearance"]

def extract_tags(text):
    tags = set()
    text_lower = text.lower()
    for kw in KEYWORDS:
        if kw in text_lower:
            tags.add(kw)
    if tags:
        print(f"Tags extracted: {tags}")
    else:
        print("No tags extracted; no keywords found.")
    return list(tags)

# -------- Publish to Blogger --------
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

# -------- Score & select entries --------
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
        filtered = [e for _, e in scored[:max_posts]]
    return filtered[:max_posts]

# -------- MAIN --------
def run_once():
    print("Loading posted links...")
    posted_links = load_posted_links()
    print(f"Loaded {len(posted_links)} posted links.")

    print("Fetching feed entries...")
    entries = fetch_feed_entries(FEEDS, hours_back=FEED_HOURS_BACK, seen_links=posted_links)
    print(f"Found {len(entries)} new entries after filtering posted.")

    if not entries:
        print("No new entries to post. Exiting.")
        return

    top_entries = select_top_entries(entries, MAX_POSTS)
    if not top_entries:
        print("No suitable entries after scoring. Exiting.")
        return

    service = get_blogger_service()
    recent_titles = fetch_recent_post_titles(service, BLOG_ID, max_posts=20)

    for entry in top_entries:
        title = entry["title"]
        link = entry["link"]
        source = entry.get("source", "Unknown Source")
        summary = re.sub(r'<[^>]+>', '', entry.get("summary", ""))[:800]

        # Check similarity with recent posts
        if any(is_similar(title, old_title) for old_title in recent_titles):
            print(f"Skipping similar post: {title}")
            continue

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

        # Generate content with Groq LLM using strict prompt
        prompt = build_groq_prompt(title, link, summary, hosted_img_url)
        sys_prompt = "You are a concise, factual deal-writer. Produce clean HTML with a short headline line, then body in <p>, <ul><li> format. Strict mode on: no fabricated social proof."
        print(f"Generating content for: {title}")
        try:
            generated_content = groq_generate(prompt, system_prompt=sys_prompt, model=GROQ_MODEL)
        except Exception as e:
            print(f"Groq generation failed: {e}")
            generated_content = f"<p>{summary}</p>"

        # Extract tags strictly from text
        tags = extract_tags(title + " " + summary)

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
        print(f"Publishing post: {title} with tags: {tags}")
        try:
            result = publish_post(service, BLOG_ID, title, post_html, labels=tags if tags else ["deals", "daily"], is_draft=False)
            print(f"Published successfully: {result.get('url')}")
            save_posted_link(link)  # Save posted link after success
        except Exception as e:
            print(f"Publishing failed: {e}")

        time.sleep(5)  # avoid rate limits

if __name__ == "__main__":
    run_once()
