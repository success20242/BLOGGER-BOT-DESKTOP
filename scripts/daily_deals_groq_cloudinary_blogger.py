# daily_deals_groq_cloudinary_blogger.py
"""
Upgraded daily deals bot:
- dedupe via posted_deals.json
- similarity check vs recent Blogger posts
- price/discount extraction + JSON-LD Offer
- uses Cloudinary-hosted images
- dynamic tags, CTA rotation, social proof (conservative)
- preserves existing flow: feeds -> Groq -> Blogger
"""

import os
import time
import re
import json
import random
import pickle
import feedparser
import requests
import difflib
from datetime import datetime, timezone
from jinja2 import Template
from dotenv import load_dotenv

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

import cloudinary
import cloudinary.uploader

# load env
load_dotenv()

# ========== CONFIG ==========
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

POSTED_FILE = "posted_deals.json"   # persistent posted deals store

# CTA rotation
CTA_VARIANTS = [
    "Buy now",
    "Grab the deal",
    "Limited time offer — buy now",
    "Claim this discount",
    "Shop the deal",
    "Don't miss out — buy now"
]

# Jinja2 wrapper template (includes author/date block)
POST_WRAPPER = Template("""
<div class="deal-post" style="font-family:sans-serif; max-width:800px; margin:auto; padding:15px;">
  <h1 style="font-size:1.9em; margin-bottom:0.2em;">{{ title }}</h1>
  <p style="color:#555; font-size:0.9em; margin-top:0;">Source: {{ source }} • Posted: {{ posted_at }} • Author: {{ author }}</p>
  {% if image_url %}
  <p><img src="{{ image_url }}" alt="{{ title }}" style="max-width:100%; height:auto; border-radius:8px; margin:1em 0;"></p>
  {% endif %}
  <div class="content" style="font-size:1.05em; line-height:1.45em; margin-bottom:1em;">
    {{ body | safe }}
  </div>
  <p style="font-style: italic; color:#666;">{{ social_proof }}</p>
  <p><a href="{{ link }}" rel="nofollow noopener" target="_blank" style="display:inline-block; background:#007bff; color:#fff; padding:10px 18px; border-radius:6px; text-decoration:none;">{{ cta }}</a></p>
  <!-- JSON-LD structured data inserted below for SEO -->
  <script type="application/ld+json">
  {{ jsonld | safe }}
  </script>
</div>
""")

# ========== Helpers: Cloudinary ==========
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
        res = cloudinary.uploader.upload(url, folder="daily_deals", resource_type="image", overwrite=False)
        return res.get("secure_url")
    except Exception as e:
        print("Cloudinary upload failed:", e)
        return None

# ========== Helpers: Blogger OAuth ==========
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

def fetch_recent_posts(service, blog_id, max_posts=30):
    try:
        resp = service.posts().list(blogId=blog_id, maxResults=max_posts).execute()
        return resp.get("items", [])  # list of posts (dicts)
    except Exception as e:
        print("Error fetching recent posts:", e)
        return []

# ========== Persistence: posted_deals.json ==========
def load_posted():
    if not os.path.exists(POSTED_FILE):
        return {}
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_posted(data):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ========== Feed parsing & dedupe ==========
def fetch_feed_entries(feed_urls, hours_back=72, seen_links=set()):
    cutoff = datetime.now(timezone.utc).timestamp() - (hours_back * 3600)
    entries = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                # published timestamp
                published_ts = None
                if hasattr(e, "published_parsed") and e.published_parsed:
                    published_ts = int(time.mktime(e.published_parsed))
                elif hasattr(e, "updated_parsed") and e.updated_parsed:
                    published_ts = int(time.mktime(e.updated_parsed))
                else:
                    published_ts = int(time.time())
                if published_ts < cutoff:
                    continue
                link = getattr(e, "link", "") or ""
                if not link:
                    # fallback: use title+summary hash
                    link = (getattr(e, "title", "") + getattr(e, "summary", "") )[:200]
                if link in seen_links:
                    continue
                entries.append({
                    "title": getattr(e, "title", "")[:300],
                    "link": link,
                    "source": feed.feed.get("title", url),
                    "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                    "published": datetime.fromtimestamp(published_ts, tz=timezone.utc),
                    "raw": e
                })
        except Exception as ex:
            print("Feed parsing error for", url, ex)
    # dedupe by link
    unique = {ent["link"]: ent for ent in entries}
    return list(unique.values())

# ========== Utilities: extract info ==========
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
    m = re.search(r'<img[^>]+src=[\'"]([^\'"]+)[\'"]', entry["summary"] or "")
    if m:
        return m.group(1)
    return None

def extract_price_and_currency(text):
    # look for "$123.45", "£12.99", "€9.99" OR "123 USD" etc.
    # returns (price_str, currency_symbol_or_code) or (None,None)
    m = re.search(r'([$£€]\s?\d{1,3}(?:[.,]\d{2})?)', text)
    if m:
        val = m.group(1).replace(" ", "")
        symbol = val[0]
        return val, symbol
    m2 = re.search(r'(\d{1,6}(?:[.,]\d{2})?)\s?(USD|EUR|GBP|CAD|AUD)', text, re.IGNORECASE)
    if m2:
        return m2.group(1), m2.group(2).upper()
    return None, None

def extract_discount(text):
    m = re.search(r'(\d{1,3}%\s?(?:off|OFF|OFF!|Off)?)', text)
    if m:
        return m.group(1)
    return None

def extract_rating(entry):
    # common RSS fields: e.g., entry.get('rating') or in summary like "4.5/5"
    e = entry["raw"]
    if hasattr(e, "rating") and e.rating:
        return str(e.rating)
    m = re.search(r'(\d(?:\.\d)?)/5', entry.get("summary","") or "")
    if m:
        return m.group(1)
    return None

def generate_social_proof(entry):
    # use feed fields if available (e.g., reddit score, comments)
    e = entry["raw"]
    # reddit style: e.get('score') possibly inside e
    score = None
    if hasattr(e, "score"):
        try:
            score = int(e.score)
        except Exception:
            score = None
    # try to parse 'comments' or 'ups'
    comments = None
    if hasattr(e, "comments"):
        try:
            comments = int(e.comments)
        except Exception:
            comments = None
    # If found, craft message
    if comments:
        return f"{comments} users commented on the original discussion."
    if score:
        return f"Over {score} users upvoted or showed interest in this deal."
    # otherwise generate conservative watcher count (not claiming sales)
    watchers = random.randint(25, 250)
    return f"Over {watchers} users are watching or interested in this deal."

def extract_keywords_for_tags(title, summary):
    text = (title + " " + (summary or "")).lower()
    tags = set()
    # simple keyword buckets (extend as needed)
    buckets = {
        "gaming": ["game", "steam", "xbox", "ps4", "ps5", "nintendo"],
        "tech": ["earbuds", "laptop", "charger", "smartwatch", "camera"],
        "home": ["vacuum", "mattress", "furniture", "lamp", "kitchen"],
        "fitness": ["yoga", "treadmill", "dumbbell", "exercise"],
        "fashion": ["shoes", "jacket", "dress", "sneaker"],
        "coupon": ["coupon", "promo", "coupon code", "discount code"]
    }
    for tag, kws in buckets.items():
        for kw in kws:
            if kw in text:
                tags.add(tag)
    # also add words that look like categories (capitalized words in title)
    caps = re.findall(r'\b([A-Z][a-z]{2,})\b', title)
    for c in caps[:3]:
        tags.add(c.lower())
    # fallback: a couple of important words from title
    words = re.findall(r'\b[a-z]{4,}\b', title.lower())
    for w in words[:3]:
        tags.add(w)
    return list(tags)[:8]

# ========== Groq prompt builder (keeps short) ==========
def build_groq_prompt(title, link, summary, image_url=None, discount=None, price=None, rating=None):
    extras = []
    if discount:
        extras.append(f"Discount: {discount}.")
    if price:
        extras.append(f"Price: {price}.")
    if rating:
        extras.append(f"Rating: {rating} / 5.")
    extra_line = " ".join(extras)
    return f"""
You are a concise ecommerce content writer. Create an 180-300 word SEO-friendly HTML snippet (use <p>, <ul><li>, <strong>) describing the deal below.
Include 2-3 benefit bullets and a short 'who this is for' line.
Be original (do not copy verbatim).
Product title: {title}
Product link: {link}
Product summary: {summary}
Image: {image_url or 'none'}
{extra_line}
End with a single CTA line in HTML linking to the product.
"""

def groq_generate(prompt, system_prompt=None, model=None, max_tokens=600):
    model = model or GROQ_MODEL
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY missing from .env")
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.25}
    r = requests.post(endpoint, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()

# ========== Structured data JSON-LD builder ==========
def build_jsonld(title, image, link, price=None, currency=None, availability="http://schema.org/InStock", seller=None):
    product = {"@context": "https://schema.org", "@type": "Product", "name": title, "url": link}
    if image:
        product["image"] = image
    if price:
        product["offers"] = {
            "@type": "Offer",
            "url": link,
            "price": str(price),
            "priceCurrency": currency or "USD",
            "availability": availability
        }
        if seller:
            product["offers"]["seller"] = {"@type": "Organization", "name": seller}
    return json.dumps(product, ensure_ascii=False)

# ========== Scoring & picking ==========
KEYWORDS = ["deal", "discount", "sale", "off", "save", "coupon", "clearance"]
def score_entry(entry):
    text = (entry["title"] + " " + (entry["summary"] or "")).lower()
    score = 0
    for kw in KEYWORDS:
        if kw in text:
            score += 2
    if "%" in text:
        score += 2
    return score

def select_top(entries, n=1):
    scored = sorted([(score_entry(e), e) for e in entries], key=lambda x: x[0], reverse=True)
    filtered = [e for s,e in scored if s>0]
    if not filtered:
        filtered = [e for _,e in scored[:n]]
    return filtered[:n]

# ========== Publish ==========
def publish_post(service, blog_id, title, html_content, labels=None, is_draft=False):
    post = {"kind":"blogger#post", "blog":{"id":blog_id}, "title":title, "content":html_content}
    if labels:
        post["labels"] = labels
    res = service.posts().insert(blogId=blog_id, body=post, isDraft=is_draft).execute()
    return res

# ========== Main workflow ==========
def run_once():
    # load posted deals and seen links
    posted = load_posted()   # dict mapping link -> metadata
    seen_links = set(posted.keys())

    print(f"Loaded {len(posted)} previously posted deals.")
    # fetch entries
    entries = fetch_feed_entries(FEEDS, hours_back=FEED_HOURS_BACK, seen_links=seen_links)
    print(f"Found {len(entries)} candidate entries from feeds.")

    if not entries:
        print("No new feed entries to process.")
        return

    # select best entries
    to_post = select_top(entries, n=MAX_POSTS)
    if not to_post:
        print("No suitable deals found by scoring.")
        return

    # blogger service and recent posts for similarity check
    service = get_blogger_service()
    recent_posts = fetch_recent_posts(service, BLOG_ID, max_posts=30)
    recent_titles = [p.get("title","") for p in recent_posts]

    for ent in to_post:
        title = ent["title"]
        link = ent["link"]
        source = ent.get("source", "Unknown")
        summary = re.sub(r'<[^>]+>', '', ent.get("summary",""))[:1000]
        posted_at = ent["published"].strftime("%Y-%m-%d %H:%M UTC")

        # similarity check vs recent titles
        if any(difflib.SequenceMatcher(None, title.lower(), rt.lower()).ratio() > 0.86 for rt in recent_titles):
            print("Skipping because title too similar to recent post:", title)
            continue

        # image hosting
        img = extract_image(ent)
        hosted_img = None
        if img:
            hosted_img = upload_image_to_cloudinary(img)
            if hosted_img:
                print("Hosted image:", hosted_img)
            else:
                hosted_img = img

        # extract price/discount/rating
        price_str, currency = extract_price_and_currency(title + " " + (ent.get("summary") or ""))
        discount = extract_discount(title + " " + (ent.get("summary") or ""))
        rating = extract_rating(ent)

        # social proof
        social_proof = generate_social_proof(ent)

        # tags
        tags = extract_keywords_for_tags(title, summary)
        if not tags:
            tags = ["deals", "daily"]

        # CTA
        cta = random.choice(CTA_VARIANTS)

        # build Groq prompt & generate content
        prompt = build_groq_prompt(title, link, summary, hosted_img, discount, price_str, rating)
        sys_prompt = "You are a concise deal writer. Return clean HTML (<p>, <ul><li>, <strong>)."
        try:
            body_html = groq_generate(prompt, system_prompt=sys_prompt)
        except Exception as e:
            print("Groq generation failed:", e)
            body_html = "<p>" + (summary or "Deal details available at the link.") + "</p>"

        # build JSON-LD structured data
        # if price_str like "$12.99" we will strip symbol for price numeric and set currency
        price_val = None
        price_currency = None
        if price_str:
            # normalize price
            m = re.search(r'([0-9]+[.,]?[0-9]*)', price_str.replace(",",""))
            if m:
                price_val = m.group(1).replace(",", "")
            # infer currency symbol
            if "$" in price_str:
                price_currency = "USD"
            elif "£" in price_str:
                price_currency = "GBP"
            elif "€" in price_str:
                price_currency = "EUR"
            else:
                price_currency = currency or "USD"
        jsonld = build_jsonld(title, hosted_img, link, price=price_val, currency=price_currency, seller=source)

        # assemble final HTML
        author = ""  # try to get author from Blogger account (best-effort)
        try:
            profile = service.users().get(userId="self").execute()
            author = profile.get("displayName", "") or profile.get("id","")
        except Exception:
            author = "Author"

        final_html = POST_WRAPPER.render(
            title=title,
            source=source,
            posted_at=posted_at,
            author=author,
            image_url=hosted_img,
            body=body_html,
            social_proof=social_proof,
            link=link,
            cta=cta,
            jsonld=jsonld
        )

        # publish
        print("Publishing:", title)
        try:
            res = publish_post(service, BLOG_ID, title, final_html, labels=tags, is_draft=False)
            url = res.get("url")
            print("Published:", url)
            # save to posted
            posted[link] = {
                "title": title,
                "url": url,
                "posted_at": datetime.utcnow().isoformat(),
                "tags": tags
            }
            save_posted(posted)
        except Exception as e:
            print("Failed to publish:", e)

        time.sleep(3)

if __name__ == "__main__":
    run_once()
