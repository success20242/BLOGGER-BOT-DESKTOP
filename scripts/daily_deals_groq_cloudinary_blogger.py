#!/usr/bin/env python3
# daily_deals_groq_cloudinary_blogger.py
# Fully integrated: Groq content + structured commentary + Cloudinary image + Blogger post

import os
import json
import requests
import hashlib
import feedparser
from datetime import datetime
from openai import OpenAI

# ------------------- CONFIGURATION -------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # Groq/LLM key
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
BLOGGER_BLOG_ID = os.getenv("BLOGGER_BLOG_ID")
BLOGGER_API_KEY = os.getenv("BLOGGER_API_KEY")
BLOGGER_OAUTH_TOKEN = os.getenv("BLOGGER_OAUTH_TOKEN")

FEED_URLS = [
    "https://example.com/daily-deals-feed.xml"
]

POSTED_LOG = "posted_links.json"

# ------------------- UTILITIES -------------------

def load_posted_links():
    if not os.path.exists(POSTED_LOG):
        return set()
    with open(POSTED_LOG, "r") as f:
        return set(json.load(f))

def save_posted_link(link):
    posted = load_posted_links()
    posted.add(link)
    with open(POSTED_LOG, "w") as f:
        json.dump(list(posted), f)

def hash_text(text):
    return hashlib.md5(text.encode()).hexdigest()

# ------------------- CLOUDINARY -------------------

def upload_image_to_cloudinary(image_url):
    upload_url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"
    response = requests.post(
        upload_url,
        data={
            "file": image_url,
            "upload_preset": "ml_default"
        },
        auth=(CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)
    )
    result = response.json()
    return result.get("secure_url")

# ------------------- GROQ / OpenAI -------------------

client = OpenAI(api_key=OPENAI_API_KEY)

def generate_groq_content(title, summary, link):
    prompt = f"""
    You are a professional e-commerce writer.
    Generate a short, punchy headline for this deal and a clear description.
    Make it engaging and concise in HTML format.
    Product title: {title}
    Summary: {summary}
    Link: {link}
    """
    response = client.chat.completions.create(
        model="groq",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300
    )
    return response.choices[0].message.content.strip()

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
    response = client.chat.completions.create(
        model="groq",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500
    )
    return response.choices[0].message.content.strip()

# ------------------- BLOGGER POST -------------------

def publish_to_blogger(title, content, labels=None):
    url = f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/posts/"
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
    response = requests.post(url, headers=headers, json=data)
    return response.json()

# ------------------- MAIN SCRIPT -------------------

def run_once():
    posted_links = load_posted_links()
    for feed_url in FEED_URLS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            if entry.link in posted_links:
                continue

            title = entry.title
            summary = entry.get("summary", "")
            link = entry.link
            image_url = entry.get("media_content", [{}])[0].get("url")

            if image_url:
                cloud_image = upload_image_to_cloudinary(image_url)
                img_html = f'<img src="{cloud_image}" alt="{title}" style="max-width:100%;">'
            else:
                img_html = ""

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
            print(f"Posted: {title} -> {response.get('url')}")
            save_posted_link(link)

if __name__ == "__main__":
    run_once()
