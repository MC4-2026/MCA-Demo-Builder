#!/usr/bin/env python3
"""
MCA Demo Brand Builder — Backend Server
Fetches websites server-side, extracts brand assets (colors, fonts, tone, images).
"""

_ENGINE_REV = 'mc4-lr-bbr-2026'  # build revision tag

import os
import re
import io
import json
import sys
import time
import uuid
import zipfile
import base64
import colorsys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from collections import Counter
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS

app = Flask(__name__, static_folder='.')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-prod')
CORS(app)

# ─── OAuth Configuration ───
SF_CLIENT_ID = os.environ.get('SF_CLIENT_ID', '')
SF_CLIENT_SECRET = os.environ.get('SF_CLIENT_SECRET', '')
SF_CALLBACK_URL = os.environ.get('SF_CALLBACK_URL', 'http://localhost:5111/oauth/callback')

# In-memory job store for async analysis
jobs = {}  # job_id -> {'status': 'pending'|'done'|'error', 'result': ..., 'error': ...}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'identity',
    'Cache-Control': 'no-cache',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}

# ─── Helpers ───

def fetch_page(url, timeout=10):
    """Fetch a URL with browser-like headers."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, resp.url

def fetch_css(url, timeout=5):
    """Fetch an external CSS file."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        return resp.text if resp.ok else ''
    except:
        return ''

def hex_to_rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except:
        return None

def rgb_to_hex(r, g, b):
    return '#{:02x}{:02x}{:02x}'.format(max(0,min(255,r)), max(0,min(255,g)), max(0,min(255,b)))

def brightness(r, g, b):
    return (r * 299 + g * 587 + b * 114) / 1000

def color_distance(c1, c2):
    return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5

def is_near_white_or_black(r, g, b):
    br = brightness(r, g, b)
    return br > 235 or br < 20

def is_near_gray(r, g, b):
    """Check if a color is essentially gray (low saturation)."""
    max_c = max(r, g, b)
    min_c = min(r, g, b)
    return (max_c - min_c) < 20

def resolve_url(src, base_url):
    if not src or src.startswith('data:'):
        return None
    try:
        return urljoin(base_url, src)
    except:
        return None


# ─── Color Extraction ───

def extract_colors(soup, raw_html, css_texts):
    """Extract color palette from HTML + CSS."""
    all_text = raw_html + '\n'.join(css_texts)

    color_counts = Counter()

    # Hex colors
    for m in re.finditer(r'#([0-9a-fA-F]{3,6})\b', all_text):
        h = m.group(1)
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        if len(h) == 6:
            rgb = hex_to_rgb('#' + h)
            if rgb and not is_near_white_or_black(*rgb) and not is_near_gray(*rgb):
                color_counts['#' + h.lower()] += 1

    # rgb/rgba colors
    for m in re.finditer(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', all_text):
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not is_near_white_or_black(r, g, b) and not is_near_gray(r, g, b):
            color_counts[rgb_to_hex(r, g, b)] += 1

    # hsl colors
    for m in re.finditer(r'hsl\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%', all_text):
        h_val, s, l = float(m.group(1))/360, float(m.group(2))/100, float(m.group(3))/100
        if s > 0.1 and 0.1 < l < 0.9:
            r, g, b = [int(c * 255) for c in colorsys.hls_to_rgb(h_val, l, s)]
            color_counts[rgb_to_hex(r, g, b)] += 1

    # Sort by frequency
    sorted_colors = sorted(color_counts.items(), key=lambda x: -x[1])

    # Deduplicate similar colors
    deduped = []
    for hex_val, count in sorted_colors:
        rgb = hex_to_rgb(hex_val)
        if not rgb:
            continue
        if not any(color_distance(rgb, hex_to_rgb(c)) < 35 for c in deduped):
            deduped.append(hex_val)
        if len(deduped) >= 6:
            break

    labels = ['Primary', 'Secondary', 'Accent', 'Highlight', 'Neutral 1', 'Neutral 2']
    return [{'label': labels[i] if i < len(labels) else f'Color {i+1}', 'hex': c} for i, c in enumerate(deduped)]


# ─── Font Extraction ───

def extract_fonts(soup, raw_html, css_texts):
    """Extract heading and body fonts."""
    all_css = raw_html + '\n'.join(css_texts)

    # Resolve CSS custom properties for fonts
    css_vars = {}
    for m in re.finditer(r'--([\w-]+)\s*:\s*([^;}{]+)', all_css):
        val = m.group(2).strip().strip('"\'')
        css_vars['--' + m.group(1)] = val
    # Also match vars with quoted values like --font-family:"Open Sans",...
    for m in re.finditer(r'--([\w-]+)\s*:\s*"([^"]+)"', all_css):
        css_vars['--' + m.group(1)] = m.group(2).strip()

    def resolve_font(raw):
        """Resolve a font value, handling CSS vars and system fonts."""
        raw = raw.strip()
        # Resolve var() references
        var_match = re.search(r'var\((--[\w-]+)', raw)
        if var_match:
            var_name = var_match.group(1)
            if var_name in css_vars:
                raw = css_vars[var_name]
            else:
                return None  # unresolvable

        first = raw.split(',')[0].strip().strip('"\'').strip()
        # Map generic system fonts to readable names
        system_map = {
            'system-ui': 'System Default (San Francisco / Segoe UI)',
            '-apple-system': 'San Francisco',
            'BlinkMacSystemFont': 'San Francisco',
            'Segoe UI': 'Segoe UI',
            'ui-sans-serif': 'System Sans-Serif',
            'ui-serif': 'System Serif',
            'ui-monospace': 'System Monospace',
        }
        if first in system_map:
            return system_map[first]
        if first and not re.search(r'icon|glyph|symbol|awesome|material|fa-|^var\(', first, re.I):
            return first
        return None

    font_mentions = []
    for m in re.finditer(r'font-family\s*:\s*([^;}"\']+)', all_css, re.IGNORECASE):
        resolved = resolve_font(m.group(1))
        if resolved:
            font_mentions.append(resolved)

    # Google Fonts
    google_fonts = []
    for link in soup.find_all('link', href=True):
        href = link['href']
        if 'fonts.googleapis.com' in href:
            fm = re.search(r'family=([^&:]+)', href)
            if fm:
                fname = fm.group(1).replace('+', ' ').split('|')[0].split(',')[0]
                google_fonts.append(fname)

    # Also check @import in CSS
    for css in css_texts:
        for m in re.finditer(r'@import\s+url\(["\']?([^"\')\s]+)["\']?\)', css):
            url = m.group(1)
            if 'fonts.googleapis.com' in url:
                fm = re.search(r'family=([^&:]+)', url)
                if fm:
                    fname = fm.group(1).replace('+', ' ').split('|')[0].split(',')[0]
                    google_fonts.append(fname)

    # Check @font-face declarations for custom fonts
    custom_fonts = []
    for m in re.finditer(r'@font-face\s*\{[^}]*font-family\s*:\s*["\']?([^"\';\}]+)', all_css, re.I):
        fname = m.group(1).strip()
        if fname and not re.search(r'icon|glyph|symbol|awesome|material', fname, re.I):
            custom_fonts.append(fname)

    # Heading fonts: check heading elements for inline styles
    heading_fonts = []
    for tag in soup.find_all(['h1', 'h2', 'h3']):
        style = tag.get('style', '')
        fm = re.search(r'font-family\s*:\s*([^;]+)', style)
        if fm:
            resolved = resolve_font(fm.group(1))
            if resolved:
                heading_fonts.append(resolved)

    # Priority: Google Fonts > Custom @font-face > CSS mentions > fallback
    all_fonts = google_fonts + custom_fonts + font_mentions
    # Deduplicate while preserving order
    seen = set()
    unique_fonts = []
    for f in all_fonts:
        key = f.lower()
        if key not in seen:
            seen.add(key)
            unique_fonts.append(f)

    body_font = unique_fonts[0] if unique_fonts else 'Arial'
    heading_font = heading_fonts[0] if heading_fonts else (unique_fonts[0] if unique_fonts else body_font)

    return {
        'heading': heading_font,
        'body': body_font
    }


# ─── FSI Industry Tone Profiles ───

INDUSTRY_TONES = {
    'general': {
        'label': 'General Business',
        'industry': 'General',
        'description': 'Clear, professional language that focuses on the value your business delivers. Approachable and confident without industry-specific jargon.',
        'identity_template': '{name} is a business committed to delivering value and exceptional service. They communicate with clarity and confidence, focusing on customer needs and practical solutions.',
        'customer_term': 'customers'
    },
    'banking_retail': {
        'label': 'Retail Banking',
        'industry': 'Banking',
        'description': 'Clear, reassuring language that builds trust with everyday banking customers. Emphasizes convenience, security, and personal financial wellness.',
        'identity_template': '{name} is a retail banking institution committed to helping customers manage their finances with confidence. They communicate with clarity and warmth, making banking accessible and straightforward.',
        'customer_term': 'customers'
    },
    'banking_commercial': {
        'label': 'Commercial / B2B Banking',
        'industry': 'Banking',
        'description': 'Authoritative, solutions-oriented language for business and corporate clients. Focuses on strategic partnership, capital markets expertise, and driving business growth.',
        'identity_template': '{name} is a commercial banking partner focused on empowering businesses with tailored financial solutions. They speak with authority and industry expertise, positioning themselves as strategic advisors.',
        'customer_term': 'clients'
    },
    'banking_community': {
        'label': 'Community Banks',
        'industry': 'Banking',
        'description': 'Warm, relationship-driven language rooted in local community values. Emphasizes personal service, neighborly trust, and reinvesting in the communities they serve.',
        'identity_template': '{name} is a community bank built on personal relationships and local roots. They communicate with genuine warmth, emphasizing their commitment to the neighborhoods and people they serve.',
        'customer_term': 'customers'
    },
    'credit_unions': {
        'label': 'Credit Unions',
        'industry': 'Banking',
        'description': 'Member-focused, cooperative language that emphasizes shared ownership and community benefit. Highlights that members are owners, not just account holders.',
        'identity_template': '{name} is a member-owned credit union dedicated to putting members first. As a cooperative, they reinvest in their community and communicate with an inclusive, people-first voice that reflects their commitment to shared financial well-being.',
        'customer_term': 'members'
    },
    'wealth_management': {
        'label': 'Wealth Management',
        'industry': 'Wealth & Asset Management',
        'description': 'Refined, consultative language conveying deep expertise and discretion. Focuses on legacy planning, personalized strategies, and long-term financial stewardship.',
        'identity_template': '{name} is a wealth management firm dedicated to preserving and growing their clients\' financial legacies. They communicate with sophistication and discretion, reflecting their commitment to personalized, long-term financial stewardship.',
        'customer_term': 'clients'
    },
    'asset_management': {
        'label': 'Asset Management',
        'industry': 'Wealth & Asset Management',
        'description': 'Data-driven, institutional language focused on portfolio performance, risk management, and market insight. Balances technical precision with strategic vision.',
        'identity_template': '{name} is an asset management firm delivering institutional-grade investment strategies. They communicate with analytical precision and market authority, providing investors with transparent, performance-focused insights.',
        'customer_term': 'investors'
    },
    'insurance_pc': {
        'label': 'Property & Casualty Insurance',
        'industry': 'Insurance',
        'description': 'Protective, reassuring language focused on risk mitigation and peace of mind. Balances technical coverage details with empathetic, human-centered messaging.',
        'identity_template': '{name} provides property and casualty insurance solutions designed to protect what matters most. They communicate with a balance of expertise and empathy, helping customers feel secure and prepared.',
        'customer_term': 'policyholders'
    },
    'insurance_life': {
        'label': 'Life Insurance',
        'industry': 'Insurance',
        'description': 'Thoughtful, future-oriented language centered on family security and legacy protection. Balances sensitivity with confidence in long-term planning.',
        'identity_template': '{name} offers life insurance solutions that help families plan for the future with confidence. They communicate thoughtfully, balancing sensitivity with assurance to help customers protect the people who matter most.',
        'customer_term': 'policyholders'
    },
    'insurance_group': {
        'label': 'Group Benefits',
        'industry': 'Insurance',
        'description': 'Professional, employer-focused language that emphasizes employee well-being, retention, and competitive benefits packages.',
        'identity_template': '{name} delivers group benefits solutions that help employers attract and retain top talent. They communicate professionally, emphasizing employee well-being, comprehensive coverage, and streamlined administration.',
        'customer_term': 'employers and employees'
    },
    'insurance_brokerage': {
        'label': 'Insurance Brokerage',
        'industry': 'Insurance',
        'description': 'Advisory, market-savvy language positioned as an independent expert navigating options on behalf of clients. Emphasizes choice, advocacy, and tailored coverage.',
        'identity_template': '{name} is an independent insurance brokerage that advocates for clients by navigating the market to find optimal coverage. They communicate as trusted advisors, emphasizing choice, transparency, and client-first service.',
        'customer_term': 'clients'
    },
    'lending_mortgage': {
        'label': 'Lending & Mortgage',
        'industry': 'Lending',
        'description': 'Guiding, milestone-oriented language that helps borrowers navigate the path to homeownership or capital access. Balances technical loan details with excitement about life goals.',
        'identity_template': '{name} is a lending institution dedicated to making homeownership and financial goals achievable. They communicate with a guiding, encouraging voice that simplifies complex lending processes and celebrates customer milestones.',
        'customer_term': 'borrowers'
    },
    'payments': {
        'label': 'Payments & Fintech',
        'industry': 'Payments',
        'description': 'Modern, efficiency-driven language focused on speed, innovation, and seamless transactions. Speaks to both businesses and consumers with a forward-thinking voice.',
        'identity_template': '{name} is a payments technology company enabling fast, secure, and seamless transactions. They communicate with a modern, forward-thinking voice, emphasizing innovation, reliability, and the power of frictionless commerce.',
        'customer_term': 'customers'
    }
}

def get_industry_list():
    """Return structured industry/sub-industry list for the frontend dropdown."""
    industries = {}
    for key, tone in INDUSTRY_TONES.items():
        ind = tone['industry']
        if ind not in industries:
            industries[ind] = []
        industries[ind].append({
            'key': key,
            'label': tone['label']
        })
    return industries

def analyze_tone(soup, industry_key=None):
    """Analyze tone — if an industry is selected, use that profile. Otherwise auto-detect."""
    if industry_key and industry_key in INDUSTRY_TONES:
        tone = INDUSTRY_TONES[industry_key]
        return {
            'key': industry_key,
            'label': tone['label'],
            'industry': tone['industry'],
            'description': tone['description'],
            'customer_term': tone['customer_term']
        }

    # Auto-detect: score page text against FSI keywords
    text_parts = []
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        text_parts.append(meta['content'])
    og = soup.find('meta', attrs={'property': 'og:description'})
    if og and og.get('content'):
        text_parts.append(og['content'])
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4']):
        text_parts.append(tag.get_text(strip=True))
    for p in soup.find_all('p'):
        text_parts.append(p.get_text(strip=True))
    for cls in ['hero', 'tagline', 'subtitle', 'banner', 'headline', 'slogan']:
        for el in soup.find_all(class_=re.compile(cls, re.I)):
            text_parts.append(el.get_text(strip=True))

    all_text = ' '.join(text_parts).lower()[:8000]

    # Industry detection keywords
    INDUSTRY_KEYWORDS = {
        'banking_retail': ['checking', 'savings', 'debit card', 'atm', 'mobile banking', 'personal banking',
                           'direct deposit', 'online banking', 'bank account', 'personal finance'],
        'banking_commercial': ['commercial', 'treasury', 'capital markets', 'corporate banking', 'business banking',
                               'trade finance', 'cash management', 'commercial lending', 'business solutions'],
        'banking_community': ['community bank', 'local bank', 'neighborhood', 'community', 'locally owned',
                              'hometown', 'community development', 'small business lending'],
        'credit_unions': ['credit union', 'member', 'membership', 'member-owned', 'cooperative', 'share account',
                          'member services', 'join today', 'become a member', 'member benefits'],
        'wealth_management': ['wealth', 'portfolio', 'estate planning', 'financial planning', 'advisor',
                              'high net worth', 'family office', 'trust', 'legacy', 'fiduciary'],
        'asset_management': ['fund', 'etf', 'institutional', 'asset management', 'portfolio management',
                             'investment strategy', 'risk management', 'alpha', 'benchmark'],
        'insurance_pc': ['property', 'casualty', 'auto insurance', 'home insurance', 'claims',
                         'coverage', 'policy', 'deductible', 'liability', 'renters insurance'],
        'insurance_life': ['life insurance', 'term life', 'whole life', 'beneficiary', 'death benefit',
                           'annuity', 'universal life', 'life protection', 'life coverage'],
        'insurance_group': ['group benefits', 'employee benefits', 'health plan', 'dental', 'vision',
                            'disability', 'voluntary benefits', 'open enrollment', 'employer'],
        'insurance_brokerage': ['brokerage', 'broker', 'independent agent', 'insurance market',
                                'compare quotes', 'coverage options', 'shop insurance'],
        'lending_mortgage': ['mortgage', 'home loan', 'refinance', 'preapproval', 'loan officer',
                             'closing costs', 'interest rate', 'home equity', 'fha', 'va loan'],
        'payments': ['payments', 'fintech', 'transaction', 'checkout', 'payment processing',
                     'digital wallet', 'contactless', 'point of sale', 'merchant', 'real-time payments']
    }

    scores = {}
    for key, keywords in INDUSTRY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            score += len(re.findall(re.escape(kw), all_text, re.I))
        scores[key] = score

    # Only use industry match if score is meaningful (≥2 keyword hits)
    # Otherwise fall back to 'general' for non-FSI businesses
    if any(scores.values()) and max(scores.values()) >= 2:
        best = max(scores, key=scores.get)
    else:
        best = 'general'
    tone = INDUSTRY_TONES[best]

    return {
        'key': best,
        'label': tone['label'],
        'industry': tone['industry'],
        'description': tone['description'],
        'customer_term': tone['customer_term']
    }


# ─── Identity Generation ───

def extract_website_text(soup):
    """Extract meaningful brand text from the website (meta desc, tagline, key headings)."""
    text_pieces = []

    # Meta description
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        text_pieces.append(meta['content'].strip())

    # OG description as fallback
    og = soup.find('meta', attrs={'property': 'og:description'})
    if og and og.get('content') and og['content'].strip() not in text_pieces:
        text_pieces.append(og['content'].strip())

    # Look for real taglines/slogans
    for cls in ['tagline', 'slogan', 'hero-text', 'hero-title', 'banner-title',
                'hero-subtitle', 'hero-description', 'hero-copy', 'headline']:
        el = soup.find(class_=re.compile(cls, re.I))
        if el:
            t = el.get_text(strip=True)
            if 10 < len(t) < 200 and t not in text_pieces:
                text_pieces.append(t)

    # Filter out generic/useless page titles
    generic_patterns = ['home page', 'homepage', 'welcome to', 'official site',
                        'official website', 'log in', 'sign in', 'page not found',
                        'cookie', 'privacy', 'accept all']
    text_pieces = [t for t in text_pieces
                   if not any(p in t.lower() for p in generic_patterns)]

    return text_pieces


def generate_identity(name, soup, tone):
    """Generate brand identity blending industry profile with scraped website text."""
    website_text = extract_website_text(soup)
    tone_key = tone.get('key', 'banking_retail')
    industry_tone = INDUSTRY_TONES.get(tone_key, INDUSTRY_TONES['banking_retail'])

    # Start with the industry-specific identity template
    identity = industry_tone['identity_template'].format(name=name)

    # Blend in the actual website text to make it specific
    if website_text:
        # Use the best piece of scraped text (meta description or tagline)
        best_text = website_text[0]
        if len(best_text) > 20:
            identity += f' In their own words: "{best_text[:250]}"'

    return identity


# ─── Image Extraction ───

def get_best_src(img):
    """Get the best image source from an element, checking multiple lazy-loading patterns."""
    # Priority order: actual src, then various lazy-load data attributes
    for attr in ['src', 'data-src', 'data-lazy-src', 'data-original', 'data-lazy',
                 'data-srcset', 'data-bg', 'data-image', 'data-full-src', 'data-hi-res-src',
                 'data-large-file', 'data-orig-file', 'loading-src']:
        val = img.get(attr, '')
        if val and not val.startswith('data:') and val.strip():
            # For srcset-like attributes, pick the best (largest) URL
            if 'srcset' in attr.lower():
                return parse_srcset_best(val)
            return val.strip()
    return ''


def parse_srcset_best(srcset_val):
    """Parse a srcset attribute and return the URL of the largest image."""
    if not srcset_val:
        return ''
    best_url = ''
    best_width = 0
    for entry in srcset_val.split(','):
        parts = entry.strip().split()
        if not parts:
            continue
        url = parts[0]
        width = 0
        if len(parts) > 1:
            w_match = re.search(r'(\d+)w', parts[1])
            x_match = re.search(r'([\d.]+)x', parts[1])
            if w_match:
                width = int(w_match.group(1))
            elif x_match:
                width = int(float(x_match.group(1)) * 1000)
        if width >= best_width and url and not url.startswith('data:'):
            best_width = width
            best_url = url
    return best_url or ''


def extract_images(soup, base_url):
    """LC/MC brand-analysis pipeline — image extraction stage."""
    images = []
    seen = set()
    parsed_base = urlparse(base_url)

    def add_image(src, img_type, alt):
        if not src or src.startswith('data:'):
            return
        url = resolve_url(src, base_url)
        if not url or url in seen:
            return
        # Skip tiny tracking pixels, svgs with data URIs, icons, etc.
        skip_patterns = ['pixel', 'tracking', 'spacer', '1x1', 'blank.gif', 'beacon',
                         'icon-', 'favicon', 'spinner', 'loading.', 'placeholder']
        if any(x in url.lower() for x in skip_patterns):
            return
        # Skip very small SVGs that are likely icons
        if url.lower().endswith('.svg') and img_type != 'logo':
            return
        seen.add(url)
        # Derive a readable display label — prefer alt text, fall back to URL filename
        display_label = alt.strip() if alt else ''
        generic_alts = ('', 'image', 'hero image', 'background image', 'hero', 'logo')
        if not display_label or display_label.lower() in generic_alts:
            try:
                path_part = urlparse(url).path
                fname = path_part.split('/')[-1].rsplit('.', 1)[0] if '/' in path_part else ''
                if fname and len(fname) > 2:
                    cleaned = re.sub(r'[-_]+', ' ', fname).strip().title()
                    # Remove common noise like dimensions (e.g. "1200x600")
                    cleaned = re.sub(r'\b\d{3,4}x\d{3,4}\b', '', cleaned).strip()
                    if cleaned and len(cleaned) > 2:
                        display_label = cleaned[:50]
            except Exception:
                pass
        if not display_label or display_label.lower() in generic_alts:
            display_label = f'{img_type.title()} {len(images) + 1}'
        images.append({'url': url, 'type': img_type, 'alt': alt or img_type.title(), 'label': display_label, 'selected': True})

    # Logo candidates
    logo_selectors = [
        ('header img', 'logo'),
        ('nav img', 'logo'),
        ('[class*="logo"] img', 'logo'),
        ('img[class*="logo"]', 'logo'),
        ('img[alt*="logo"]', 'logo'),
        ('img[src*="logo"]', 'logo'),
        ('a[class*="logo"] img', 'logo'),
        ('[id*="logo"] img', 'logo'),
        ('img[id*="logo"]', 'logo'),
        ('.navbar-brand img', 'logo'),
        ('[class*="brand"] img', 'logo'),
        # Also check <picture> inside logo areas
        ('header picture source', 'logo'),
        ('[class*="logo"] picture source', 'logo'),
    ]
    for selector, img_type in logo_selectors:
        for el in soup.select(selector):
            if el.name == 'source':
                src = el.get('srcset', '')
                if src:
                    src = parse_srcset_best(src) or src.split(',')[0].strip().split()[0]
                alt = 'Logo'
            else:
                src = get_best_src(el)
                alt = el.get('alt', 'Logo')
            add_image(src, img_type, alt)

    # SVG logos — check for SVG <img> with logo in src
    for img in soup.select('header img[src$=".svg"], nav img[src$=".svg"]'):
        add_image(img.get('src', ''), 'logo', img.get('alt', 'Logo'))

    # <picture> elements — extract best source
    for picture in soup.find_all('picture'):
        sources = picture.find_all('source')
        img_el = picture.find('img')
        best_src = ''
        alt = ''

        # Try sources first (usually higher quality)
        for source in sources:
            srcset = source.get('srcset', '')
            if srcset:
                candidate = parse_srcset_best(srcset)
                if candidate:
                    best_src = candidate
                    break

        # Fallback to the <img> inside <picture>
        if not best_src and img_el:
            best_src = get_best_src(img_el)
            alt = img_el.get('alt', '')

        if best_src:
            # Determine context
            parent_class = ' '.join(picture.parent.get('class', [])) if picture.parent else ''
            context = parent_class.lower()
            is_hero = bool(re.search(r'hero|banner|jumbotron|splash|featured|carousel|slider|masthead', context))
            add_image(best_src, 'hero' if is_hero else 'hero', alt or 'Image')

    # Hero / large images — all <img> tags
    for img in soup.find_all('img'):
        src = get_best_src(img)
        alt = img.get('alt', '')
        if not src:
            # Try srcset on the img itself
            srcset = img.get('srcset', '')
            if srcset:
                src = parse_srcset_best(srcset)
        if not src:
            continue

        try:
            w = int(img.get('width', 0) or 0)
        except (ValueError, TypeError):
            w = 0
        try:
            h = int(img.get('height', 0) or 0)
        except (ValueError, TypeError):
            h = 0
        is_large = w > 400 or h > 200

        # Check CSS classes for size hints
        img_classes = ' '.join(img.get('class', [])).lower() if img.get('class') else ''
        is_large = is_large or bool(re.search(r'full|large|wide|cover|hero|banner|featured', img_classes))

        parent_class = ' '.join(img.parent.get('class', [])) if img.parent else ''
        grandparent_class = ' '.join(img.parent.parent.get('class', [])) if img.parent and img.parent.parent else ''
        context = (parent_class + ' ' + grandparent_class).lower()
        in_hero = bool(re.search(r'hero|banner|jumbotron|splash|featured|carousel|slider|masthead|promo|spotlight', context))

        src_hint = bool(re.search(r'hero|banner|featured|cover|main|splash|carousel|promo|spotlight|header', src, re.I))

        if is_large or in_hero or src_hint:
            add_image(src, 'hero', alt or 'Hero Image')

    # Inline style background images
    for el in soup.find_all(style=re.compile(r'background')):
        style = el.get('style', '')
        for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', style):
            add_image(m.group(1), 'hero', 'Background Image')

    # CSS background-image in <style> blocks
    for style_tag in soup.find_all('style'):
        css_text = style_tag.get_text()
        for m in re.finditer(r'background(?:-image)?\s*:\s*[^;]*url\(["\']?([^"\')\s]+)["\']?\)', css_text):
            src = m.group(1)
            # Only include if it looks like a real image (not a gradient or icon)
            if re.search(r'\.(jpg|jpeg|png|webp|gif)', src, re.I):
                add_image(src, 'hero', 'Background Image')

    # data-background attributes (common in slider/parallax plugins)
    for el in soup.find_all(attrs={'data-background': True}):
        add_image(el['data-background'], 'hero', 'Background Image')
    for el in soup.find_all(attrs={'data-bg': True}):
        add_image(el['data-bg'], 'hero', 'Background Image')
    for el in soup.find_all(attrs={'data-bg-src': True}):
        add_image(el['data-bg-src'], 'hero', 'Background Image')

    return images[:12]


# ─── Button Style ───

def extract_button_style(soup, raw_html, css_texts, colors):
    all_css = raw_html + '\n'.join(css_texts)
    radius = 4
    color = colors[0]['hex'] if colors else '#0176d3'

    # Look for border-radius on button-like selectors
    for m in re.finditer(r'(?:\.btn|\.button|\.cta|button)[^{]*\{[^}]*border-radius\s*:\s*(\d+)', all_css, re.I):
        radius = int(m.group(1))
        break

    # Check inline button styles
    for btn in soup.select('button, a[class*="btn"], a[class*="cta"], [class*="button"]'):
        style = btn.get('style', '')
        rm = re.search(r'border-radius\s*:\s*(\d+)', style)
        if rm:
            radius = int(rm.group(1))
        bgm = re.search(r'background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,6})', style)
        if bgm:
            color = bgm.group(1)

    return {'color': color, 'radius': radius}


# ─── Sub-page Discovery ───

def find_sub_pages(soup, base_url, max_pages=3):
    """Find internal sub-page links to crawl for additional images."""
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc
    sub_urls = []
    seen = {base_url.rstrip('/')}

    # Look for main nav links
    nav_links = soup.select('nav a[href], header a[href], [class*="nav"] a[href]')
    all_links = nav_links if nav_links else soup.find_all('a', href=True)

    for a in all_links:
        href = a.get('href', '')
        if not href or href.startswith('#') or href.startswith('javascript:'):
            continue
        full_url = resolve_url(href, base_url)
        if not full_url:
            continue
        parsed = urlparse(full_url)
        # Same domain only, skip login/account/api pages
        if parsed.netloc != base_domain:
            continue
        normalized = full_url.split('?')[0].split('#')[0].rstrip('/')
        if normalized in seen:
            continue
        # Skip non-content pages
        path = parsed.path.lower()
        if any(x in path for x in ['login', 'signin', 'signup', 'account', 'cart',
                                     'checkout', 'api', 'admin', 'privacy', 'terms',
                                     'cookie', 'contact', 'sitemap', '.pdf', '.xml']):
            continue
        seen.add(normalized)
        sub_urls.append(full_url)
        if len(sub_urls) >= max_pages:
            break

    return sub_urls


# ─── Background Analysis Worker ───

def run_analysis(job_id, name, url):
    """Run website analysis in background thread."""
    try:
        print(f'[analyze] Starting analysis for {name} @ {url}', flush=True)
        html, final_url = fetch_page(url)
        print(f'[analyze] Fetched {final_url} ({len(html)} bytes)', flush=True)

        soup = BeautifulSoup(html, 'html.parser')

        # Fetch external CSS (limit to 5 to keep response fast)
        css_texts = []
        css_count = 0
        for link in soup.find_all('link', rel='stylesheet'):
            if css_count >= 5:
                break
            href = link.get('href')
            if href:
                css_url = resolve_url(href, final_url)
                if css_url:
                    css_texts.append(fetch_css(css_url))
                    css_count += 1

        # Also inline styles
        for style_tag in soup.find_all('style'):
            css_texts.append(style_tag.get_text())

        colors = extract_colors(soup, html, css_texts)
        fonts = extract_fonts(soup, html, css_texts)
        tone = analyze_tone(soup)  # auto-detect industry from page text
        identity = generate_identity(name, soup, tone)
        images = extract_images(soup, final_url)
        btn_style = extract_button_style(soup, html, css_texts, colors)

        # Crawl 1-2 sub-pages for additional images
        sub_pages = find_sub_pages(soup, final_url, max_pages=2)
        for sub_url in sub_pages:
            try:
                sub_html, sub_final = fetch_page(sub_url, timeout=5)
                sub_soup = BeautifulSoup(sub_html, 'html.parser')
                sub_images = extract_images(sub_soup, sub_final)
                existing_urls = {img['url'] for img in images}
                for img in sub_images:
                    if img['url'] not in existing_urls and len(images) < 12:
                        img['alt'] = img['alt'] + ' (sub-page)'
                        images.append(img)
                        existing_urls.add(img['url'])
            except:
                pass

        title_tag = soup.find('title')
        page_title = title_tag.get_text(strip=True) if title_tag else ''

        result = {
            'brandName': name,
            'description': f'Brand identity for {name}, derived from {urlparse(final_url).hostname}',
            'identity': identity,
            'toneKey': tone['key'],
            'toneLabel': tone['label'],
            'toneIndustry': tone.get('industry', ''),
            'toneDescription': tone['description'],
            'customerTerm': tone.get('customer_term', 'customers'),
            'colors': colors,
            'headingFont': fonts['heading'],
            'bodyFont': fonts['body'],
            'buttonColor': btn_style['color'],
            'buttonRadius': btn_style['radius'],
            'images': images,
            'sourceUrl': final_url,
            'pageTitle': page_title
        }

        jobs[job_id] = {'status': 'done', 'result': result}
        print(f'[analyze] Job {job_id} complete', flush=True)

    except Exception as e:
        print(f'[analyze] Job {job_id} failed: {e}', flush=True)
        jobs[job_id] = {'status': 'error', 'error': str(e)}


# ─── API Endpoints ───

@app.route('/api/analyze', methods=['GET', 'POST'])
def analyze():
    """Start analysis — returns a job ID immediately."""
    if request.method == 'GET':
        url = request.args.get('url', '').strip()
        name = request.args.get('name', '').strip()
    else:
        data = request.json or {}
        url = data.get('url', '').strip()
        name = data.get('name', '').strip()

    if not url or not name:
        return jsonify({'error': 'Both name and url are required'}), 400

    if not url.startswith('http'):
        url = 'https://' + url

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'pending'}

    t = threading.Thread(target=run_analysis, args=(job_id, name, url), daemon=True)
    t.start()

    return jsonify({'jobId': job_id, 'status': 'pending'})


@app.route('/api/status/<job_id>')
def job_status(job_id):
    """Poll for job completion."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    resp = jsonify(job)
    resp.headers['X-Engine'] = _ENGINE_REV
    return resp


@app.route('/api/more-images', methods=['POST'])
def scan_more_images():
    """Crawl additional sub-pages for more images."""
    data = request.json or {}
    source_url = data.get('sourceUrl', '').strip()
    existing_urls = set(data.get('existingImageUrls', []))
    max_pages = min(int(data.get('maxPages', 5)), 10)

    if not source_url:
        return jsonify({'error': 'sourceUrl is required'}), 400

    try:
        html, final_url = fetch_page(source_url)
        soup = BeautifulSoup(html, 'html.parser')
        sub_pages = find_sub_pages(soup, final_url, max_pages=max_pages + 3)

        # Skip pages already crawled in initial analysis (first 2)
        new_images = []
        pages_scanned = 0
        for sub_url in sub_pages[2:]:
            try:
                sub_html, sub_final = fetch_page(sub_url, timeout=5)
                sub_soup = BeautifulSoup(sub_html, 'html.parser')
                page_images = extract_images(sub_soup, sub_final)
                pages_scanned += 1
                for img in page_images:
                    if img['url'] not in existing_urls and len(new_images) < 20:
                        img['alt'] = img.get('alt', 'Image') + ' (additional)'
                        new_images.append(img)
                        existing_urls.add(img['url'])
            except:
                pages_scanned += 1

        return jsonify({
            'images': new_images,
            'pagesScanned': pages_scanned,
            'totalFound': len(new_images)
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500


@app.route('/api/scan-page', methods=['POST'])
def scan_specific_page():
    """Scan a specific URL for images."""
    data = request.json or {}
    page_url = data.get('pageUrl', '').strip()
    existing_urls = set(data.get('existingImageUrls', []))

    if not page_url:
        return jsonify({'error': 'pageUrl is required'}), 400

    if not page_url.startswith('http'):
        page_url = 'https://' + page_url

    try:
        html, final_url = fetch_page(page_url, timeout=10)
        soup = BeautifulSoup(html, 'html.parser')
        page_images = extract_images(soup, final_url)

        new_images = []
        for img in page_images:
            if img['url'] not in existing_urls:
                img['alt'] = img.get('alt', 'Image') + ' (scanned)'
                new_images.append(img)
                existing_urls.add(img['url'])

        return jsonify({
            'images': new_images,
            'totalFound': len(new_images),
            'pageTitle': (soup.find('title').get_text(strip=True) if soup.find('title') else page_url)
        })
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500


@app.route('/api/industries')
def get_industries():
    """Return the list of FSI industries and sub-industries for the frontend dropdown."""
    return jsonify(get_industry_list())


@app.route('/api/regenerate-identity', methods=['POST'])
def regenerate_identity():
    """Re-generate brand identity when user changes industry selection."""
    data = request.json or {}
    brand_name = data.get('brandName', 'Brand')
    industry_key = data.get('industryKey', '')
    source_url = data.get('sourceUrl', '')

    if not industry_key or industry_key not in INDUSTRY_TONES:
        return jsonify({'error': 'Invalid industry key'}), 400

    tone = INDUSTRY_TONES[industry_key]

    # If we have a source URL, re-fetch for website text
    soup = None
    if source_url:
        try:
            html, _ = fetch_page(source_url, timeout=8)
            soup = BeautifulSoup(html, 'html.parser')
        except:
            pass

    tone_data = {
        'key': industry_key,
        'label': tone['label'],
        'industry': tone['industry'],
        'description': tone['description'],
        'customer_term': tone['customer_term']
    }

    if soup:
        identity = generate_identity(brand_name, soup, tone_data)
    else:
        identity = tone['identity_template'].format(name=brand_name)

    return jsonify({
        'identity': identity,
        'toneLabel': tone['label'],
        'toneDescription': tone['description'],
        'customerTerm': tone['customer_term']
    })


@app.route('/')
def index():
    return send_from_directory('.', 'brand-builder.html')


@app.route('/download-email-series.html')
def download_page():
    return send_from_directory('.', 'download-email-series.html')


# ─── OAuth & Salesforce Deploy ───

FONT_MAP = {
    'arial': 'arial', 'arial black': 'arialBlack', 'calibri': 'calibri',
    'comic sans ms': 'comicSansMs', 'courier new': 'courierNew', 'georgia': 'georgia',
    'impact': 'impact', 'lucida console': 'lucidaConsole',
    'lucida sans unicode': 'lucidaSansUnicode', 'palatino linotype': 'palatinoLinotype',
    'tahoma': 'tahoma', 'times new roman': 'timesNewRoman', 'trebuchet ms': 'trebuchetMs',
    'verdana': 'verdana', 'century gothic': 'verdana', 'helvetica': 'arial',
    'segoe ui': 'calibri', 'system default': 'arial', 'san francisco': 'arial',
    'open sans': 'arial', 'roboto': 'arial', 'lato': 'arial', 'montserrat': 'arial',
    'poppins': 'arial', 'inter': 'arial', 'nunito': 'arial', 'raleway': 'arial',
}


def map_font_key(font_name):
    """Map a font name to the closest available CMS brand font key."""
    key = font_name.lower().strip()
    if key in FONT_MAP:
        return FONT_MAP[key]
    # Try partial match
    for k, v in FONT_MAP.items():
        if k in key or key in k:
            return v
    return 'arial'


def darken_hex(hex_color, factor=0.15):
    """Darken a hex color by a factor."""
    rgb = hex_to_rgb(hex_color)
    if not rgb:
        return hex_color
    r, g, b = rgb
    r = max(0, int(r * (1 - factor)))
    g = max(0, int(g * (1 - factor)))
    b = max(0, int(b * (1 - factor)))
    return rgb_to_hex(r, g, b)


def build_brand_content_body(config):
    """Build the sfdc_cms__brand contentBody from app's brand config JSON."""
    colors = config.get('colors', [])
    primary = colors[0]['hex'] if len(colors) > 0 else '#0176d3'
    secondary = colors[1]['hex'] if len(colors) > 1 else darken_hex(primary)
    accent = colors[2]['hex'] if len(colors) > 2 else '#333333'

    font_key = map_font_key(config.get('typography', {}).get('bodyFont', 'Arial'))
    btn_radius_px = config.get('buttonStyle', {}).get('borderRadius', 4)
    btn_radius_rem = round(btn_radius_px / 16, 4)

    tone = config.get('tone', {})
    identity = config.get('identity', '')

    return {
        "sfdc_cms:title": config.get('brandName', 'Brand'),
        "sfdc_cms:description": config.get('description', ''),
        "sfdc_cms:einsteinBrandProperties": {
            "identity": identity,
            "personality": {
                "defaultPersonality": "casual",
                "personalities": {
                    "casual": {
                        "label": tone.get('label', 'Friendly'),
                        "value": tone.get('description', 'A warm, conversational style.')
                    },
                    "professional": {
                        "label": "Professional",
                        "value": "A formal style that gets straight to the point."
                    },
                    "plain": {
                        "label": "Plain",
                        "value": "A style that uses brief, declarative sentences in an active voice."
                    },
                    "inquisitive": {
                        "label": "Inquisitive",
                        "value": "A style that poses open-ended questions relevant to the recipient's interests."
                    },
                    "urgent": {
                        "label": "Urgent",
                        "value": "A style that uses clear, concise language and time-sensitive words."
                    }
                }
            }
        },
        "baseFontFamily": "{!$brand.fontFamily." + font_key + "}",
        "baseFontSize": {"unit": "px", "value": 16},
        "colorScheme": {
            "contrast": accent,
            "neutral": "#f5f5f0",
            "primaryAccent": primary,
            "primaryAccentContrast": "#ffffff",
            "primaryAccentContrastDerived": "#e5e5e5",
            "primaryAccentDerived": secondary,
            "root": "#ffffff"
        },
        "borderRadius": {
            "round": {"unit": "rem", "value": btn_radius_rem},
            "square": {"unit": "rem", "value": 0}
        },
        "borderWeight": {
            "medium": {"unit": "rem", "value": 0.125},
            "none": {"unit": "rem", "value": 0},
            "thick": {"unit": "rem", "value": 0.1875},
            "thin": {"unit": "rem", "value": 0.0625}
        },
        "buttonStyleGroup": {
            "primary": {
                "lightning:borderRadius": "{!$brand.borderRadius.round}",
                "lightning:borderWidth": "{!$brand.borderWeight.thin}",
                "lightning:buttonColorGroup": {
                    "backgroundColor": "{!$brand.colorScheme.primaryAccent}",
                    "backgroundHoverColor": "{!$brand.colorScheme.primaryAccentDerived}",
                    "borderColor": "{!$brand.colorScheme.primaryAccent}",
                    "borderHoverColor": "{!$brand.colorScheme.primaryAccentDerived}",
                    "textColor": "{!$brand.colorScheme.primaryAccentContrast}",
                    "textHoverColor": "{!$brand.colorScheme.primaryAccentContrastDerived}"
                },
                "lightning:padding": {"bottom": {"unit": "rem", "value": 0.5}, "left": {"unit": "rem", "value": 1}, "right": {"unit": "rem", "value": 1}, "top": {"unit": "rem", "value": 0.5}},
                "lightning:typography": "{!$brand.typography.button.button1}"
            },
            "secondary": {
                "lightning:borderRadius": "{!$brand.borderRadius.round}",
                "lightning:borderWidth": "{!$brand.borderWeight.thin}",
                "lightning:buttonColorGroup": {
                    "backgroundColor": "{!$brand.colorScheme.primaryAccentContrast}",
                    "backgroundHoverColor": "{!$brand.colorScheme.primaryAccentContrastDerived}",
                    "borderColor": "{!$brand.colorScheme.primaryAccent}",
                    "borderHoverColor": "{!$brand.colorScheme.primaryAccentDerived}",
                    "textColor": "{!$brand.colorScheme.primaryAccent}",
                    "textHoverColor": "{!$brand.colorScheme.primaryAccentDerived}"
                },
                "lightning:padding": {"bottom": {"unit": "rem", "value": 0.5}, "left": {"unit": "rem", "value": 1}, "right": {"unit": "rem", "value": 1}, "top": {"unit": "rem", "value": 0.5}},
                "lightning:typography": "{!$brand.typography.button.button1}"
            },
            "tertiary": {
                "lightning:borderRadius": "{!$brand.borderRadius.round}",
                "lightning:borderWidth": "{!$brand.borderWeight.none}",
                "lightning:buttonColorGroup": {
                    "textColor": "{!$brand.colorScheme.primaryAccent}",
                    "textHoverColor": "{!$brand.colorScheme.primaryAccentDerived}"
                },
                "lightning:padding": {"bottom": {"unit": "rem", "value": 0.5}, "left": {"unit": "rem", "value": 1}, "right": {"unit": "rem", "value": 1}, "top": {"unit": "rem", "value": 0.5}},
                "lightning:typography": "{!$brand.typography.button.button1}"
            }
        },
        "fontFamily": {
            "arial": {"category": "sans-serif", "fallbacks": ["Helvetica"], "name": "Arial"},
            "arialBlack": {"category": "sans-serif", "fallbacks": ["Gadget"], "name": "Arial Black"},
            "calibri": {"category": "sans-serif", "fallbacks": ["Candara", "Segoe", "Segoe UI", "Optima", "Arial"], "name": "Calibri"},
            "comicSansMs": {"category": "sans-serif", "fallbacks": ["cursive"], "name": "Comic Sans MS"},
            "courierNew": {"category": "monospace", "name": "Courier New"},
            "georgia": {"category": "serif", "name": "Georgia"},
            "impact": {"category": "sans-serif", "fallbacks": ["Charcoal"], "name": "Impact"},
            "lucidaConsole": {"category": "monospace", "fallbacks": ["Monaco"], "name": "Lucida Console"},
            "lucidaSansUnicode": {"category": "sans-serif", "fallbacks": ["Lucida Grande"], "name": "Lucida Sans Unicode"},
            "palatinoLinotype": {"category": "serif", "fallbacks": ["Book Antiqua", "Palatino"], "name": "Palatino Linotype"},
            "tahoma": {"category": "sans-serif", "fallbacks": ["Geneva"], "name": "Tahoma"},
            "timesNewRoman": {"category": "serif", "fallbacks": ["Times"], "name": "Times New Roman"},
            "trebuchetMs": {"category": "sans-serif", "name": "Trebuchet MS"},
            "verdana": {"category": "sans-serif", "fallbacks": ["Geneva"], "name": "Verdana"}
        },
        "fontSize": {
            "large": {"unit": "rem", "value": 1.125}, "medium": {"unit": "rem", "value": 1},
            "small": {"unit": "rem", "value": 0.8125}, "xLarge": {"unit": "rem", "value": 1.5},
            "xSmall": {"unit": "rem", "value": 0.625}, "xxLarge": {"unit": "rem", "value": 2}
        },
        "fontWeight": {"bold": 700, "light": 300, "normal": 400},
        "letterSpacing": {"compact": {"unit": "px", "value": -1}, "normal": "normal", "wide": {"unit": "px", "value": 6}},
        "spacing": {
            "large": {"bottom": {"unit": "rem", "value": 1.5}, "left": {"unit": "rem", "value": 1.5}, "right": {"unit": "rem", "value": 1.5}, "top": {"unit": "rem", "value": 1.5}},
            "medium": {"bottom": {"unit": "rem", "value": 1}, "left": {"unit": "rem", "value": 1}, "right": {"unit": "rem", "value": 1}, "top": {"unit": "rem", "value": 1}},
            "none": {"bottom": {"unit": "rem", "value": 0}, "left": {"unit": "rem", "value": 0}, "right": {"unit": "rem", "value": 0}, "top": {"unit": "rem", "value": 0}},
            "small": {"bottom": {"unit": "rem", "value": 0.75}, "left": {"unit": "rem", "value": 0.75}, "right": {"unit": "rem", "value": 0.75}, "top": {"unit": "rem", "value": 0.75}},
            "xLarge": {"bottom": {"unit": "rem", "value": 2}, "left": {"unit": "rem", "value": 2}, "right": {"unit": "rem", "value": 2}, "top": {"unit": "rem", "value": 2}},
            "xSmall": {"bottom": {"unit": "rem", "value": 0.5}, "left": {"unit": "rem", "value": 0.5}, "right": {"unit": "rem", "value": 0.5}, "top": {"unit": "rem", "value": 0.5}}
        },
        "typography": {
            "button": {"button1": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.medium}", "fontWeight": "{!$brand.fontWeight.normal}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"}},
            "heading": {
                "heading1": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.xxLarge}", "fontWeight": "{!$brand.fontWeight.bold}", "letterSpacing": "normal", "lineHeight": 1.3, "textTransform": "none"},
                "heading2": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.xLarge}", "fontWeight": "{!$brand.fontWeight.bold}", "letterSpacing": "normal", "lineHeight": 1.4, "textTransform": "none"},
                "heading3": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.large}", "fontWeight": "{!$brand.fontWeight.bold}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"},
                "heading4": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.medium}", "fontWeight": "{!$brand.fontWeight.bold}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"},
                "heading5": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.small}", "fontWeight": "{!$brand.fontWeight.bold}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"},
                "heading6": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.xSmall}", "fontWeight": "{!$brand.fontWeight.bold}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"}
            },
            "input": {"input1": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.medium}", "fontWeight": "{!$brand.fontWeight.normal}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"}},
            "label": {"label1": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.small}", "fontWeight": "{!$brand.fontWeight.normal}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"}},
            "paragraph": {
                "paragraph1": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.medium}", "fontWeight": "{!$brand.fontWeight.normal}", "letterSpacing": "normal", "lineHeight": 1.6, "textTransform": "none"},
                "paragraph2": {"fontFamily": "{!$brand.baseFontFamily}", "fontSize": "{!$brand.fontSize.small}", "fontWeight": "{!$brand.fontWeight.normal}", "letterSpacing": "normal", "lineHeight": 1.5, "textTransform": "none"}
            }
        },
        "lightning:dataProviders": [],
        "sfdc_cms:variants": []
    }


def sf_api(method, path, access_token, instance_url, body=None, _retried=False):
    """Make an authenticated Salesforce REST API call with auto-refresh on 401."""
    url = instance_url.rstrip('/') + path
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    if method == 'GET':
        resp = requests.get(url, headers=headers, timeout=30)
    elif method == 'POST':
        resp = requests.post(url, headers=headers, json=body, timeout=30)
    else:
        resp = requests.request(method, url, headers=headers, json=body, timeout=30)

    # Auto-refresh on 401 (expired token)
    if resp.status_code == 401 and not _retried:
        if try_refresh_token():
            new_token = session.get('sf_access_token')
            return sf_api(method, path, new_token, instance_url, body=body, _retried=True)

    return resp


@app.route('/oauth/login')
def oauth_login():
    """Redirect user to Salesforce OAuth login."""
    if not SF_CLIENT_ID:
        return jsonify({'error': 'OAuth not configured. Set SF_CLIENT_ID environment variable.'}), 500

    env = request.args.get('env', 'production')
    login_host = 'test.salesforce.com' if env == 'sandbox' else 'login.salesforce.com'

    auth_url = (
        f'https://{login_host}/services/oauth2/authorize'
        f'?response_type=code'
        f'&client_id={quote(SF_CLIENT_ID)}'
        f'&redirect_uri={quote(SF_CALLBACK_URL)}'
        f'&scope=api+refresh_token'
    )
    return redirect(auth_url)


@app.route('/oauth/callback')
def oauth_callback():
    """Handle OAuth callback — exchange code for tokens."""
    code = request.args.get('code')
    error = request.args.get('error')
    error_desc = request.args.get('error_description', '')

    if error:
        return f"""<script>
            window.opener ? window.opener.postMessage({{type:'oauth_error',error:'{error_desc}'}}, '*') : null;
            window.close();
        </script>""", 200

    if not code:
        return jsonify({'error': 'No authorization code received'}), 400

    # Try production first, then sandbox
    for host in ['login.salesforce.com', 'test.salesforce.com']:
        token_resp = requests.post(f'https://{host}/services/oauth2/token', data={
            'grant_type': 'authorization_code',
            'client_id': SF_CLIENT_ID,
            'client_secret': SF_CLIENT_SECRET,
            'redirect_uri': SF_CALLBACK_URL,
            'code': code
        }, timeout=30)
        if token_resp.ok:
            break

    if not token_resp.ok:
        err_msg = token_resp.json().get('error_description', 'Token exchange failed')
        return f"""<script>
            window.opener ? window.opener.postMessage({{type:'oauth_error',error:'{err_msg}'}}, '*') : null;
            window.close();
        </script>""", 200

    tokens = token_resp.json()
    session['sf_access_token'] = tokens['access_token']
    session['sf_instance_url'] = tokens['instance_url']
    session['sf_refresh_token'] = tokens.get('refresh_token', '')

    # Post success message back to opener and close popup
    return """<!DOCTYPE html>
<html><head><title>Connected!</title></head>
<body>
<script>
    if (window.opener) {
        window.opener.postMessage({type:'oauth_success'}, '*');
        window.close();
    } else {
        // Fallback — redirected in same window
        window.location.href = '/?connected=1';
    }
</script>
<p>Connected to Salesforce! You can close this window.</p>
</body></html>"""


def check_brand_support(workspace_id, token, instance):
    """Test if a workspace supports sfdc_cms__brand by sending a minimal create request.
    Returns True if the workspace supports brand content type (even if required fields are missing)."""
    try:
        resp = sf_api('POST', '/services/data/v62.0/connect/cms/contents', token, instance, {
            'contentSpaceOrFolderId': workspace_id,
            'contentType': 'sfdc_cms__brand',
            'title': '__brand_support_check__',
            'contentBody': {}
        })
        if not resp.ok:
            err = resp.text
            if 'not supported by this space' in err:
                return False
            return True
        return True
    except:
        return False


def find_marketing_workspace(token, instance):
    """Find the 'Content Workspace for Marketing Cloud' or any marketing-type workspace."""
    resp = sf_api('GET',
        '/services/data/v62.0/query?q=' + quote("SELECT Id, Name FROM ManagedContentSpace ORDER BY Name"),
        token, instance)
    if not resp.ok:
        return None, []

    records = resp.json().get('records', [])
    all_workspaces = [{'id': r['Id'], 'name': r['Name']} for r in records]

    # Check each workspace's spaceType via the Connect API
    for r in records:
        try:
            space_resp = sf_api('GET', f'/services/data/v62.0/connect/cms/spaces/{r["Id"]}', token, instance)
            if space_resp.ok:
                space_data = space_resp.json()
                space_type = space_data.get('spaceType', {}).get('apiName', '')
                if space_type == 'marketing':
                    return {'id': r['Id'], 'name': r['Name']}, all_workspaces
        except:
            pass

    # Fallback: find any workspace that supports brand content
    for r in records:
        if check_brand_support(r['Id'], token, instance):
            return {'id': r['Id'], 'name': r['Name']}, all_workspaces

    return None, all_workspaces


@app.route('/api/sf/workspaces')
def get_workspaces():
    """Find the marketing workspace for brand deployment."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    if not token or not instance:
        return jsonify({'error': 'Not connected to Salesforce'}), 401

    resp = sf_api('GET',
        '/services/data/v62.0/query?q=' + quote("SELECT Id, Name FROM ManagedContentSpace ORDER BY Name"),
        token, instance)

    if resp.status_code == 401:
        return jsonify({'error': 'Session expired. Please reconnect.'}), 401
    if not resp.ok:
        return jsonify({'error': 'Failed to fetch workspaces'}), 500

    records = resp.json().get('records', [])
    all_workspaces = [{'id': r['Id'], 'name': r['Name']} for r in records]

    # Try to auto-detect the marketing workspace
    marketing_ws = None
    for r in records:
        try:
            space_resp = sf_api('GET', f'/services/data/v62.0/connect/cms/spaces/{r["Id"]}', token, instance)
            if space_resp.ok:
                space_data = space_resp.json()
                space_type = space_data.get('spaceType', {}).get('apiName', '')
                if space_type == 'marketing':
                    marketing_ws = {'id': r['Id'], 'name': r['Name']}
                    break
        except:
            pass

    # If no marketing workspace found, check for any brand-compatible workspace
    brand_compatible = []
    if not marketing_ws:
        for r in records:
            if check_brand_support(r['Id'], token, instance):
                brand_compatible.append({'id': r['Id'], 'name': r['Name']})

    return jsonify({
        'marketingWorkspace': marketing_ws,
        'brandCompatible': brand_compatible,
        'allWorkspaces': all_workspaces,
        'totalSpaces': len(records)
    })


@app.route('/api/sf/workspaces', methods=['POST'])
def create_workspace():
    """Create a new CMS workspace in the connected Salesforce org."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    if not token or not instance:
        return jsonify({'error': 'Not connected to Salesforce'}), 401

    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Workspace name is required'}), 400

    # Sanitize name — replace spaces with underscores for API compatibility
    api_name = re.sub(r'[^a-zA-Z0-9_]', '_', name)

    resp = sf_api('POST', '/services/data/v62.0/connect/cms/spaces', token, instance, {
        'name': api_name
    })

    if resp.status_code == 401:
        return jsonify({'error': 'Session expired. Please reconnect.'}), 401
    if not resp.ok:
        err_detail = resp.text[:300]
        return jsonify({'error': f'Failed to create workspace: {err_detail}'}), 500

    result = resp.json()
    ws_id = result.get('id', '')

    # Check if the new workspace supports brand content type
    supports_brand = check_brand_support(ws_id, token, instance) if ws_id else False

    return jsonify({
        'success': True,
        'workspace': {
            'id': ws_id,
            'name': result.get('name', api_name)
        },
        'supportsBrand': supports_brand,
        'warning': '' if supports_brand else 'This workspace was created but may not support Brand content. You may need to enable the Brand content type in Salesforce Setup > Digital Experiences > CMS.'
    })


def _deploy_brand_internal(token, instance, config, workspace_id):
    """Core brand deploy logic: uploads images + creates brand + publishes.
    Returns dict with brandId, contentIds, totalCreated, errors, success."""
    content_ids = []
    errors = []
    brand_id = ''

    # 1. Upload images as sfdc_cms__image items
    images = config.get('images', [])
    for img in images:
        try:
            img_resp = sf_api('POST', '/services/data/v62.0/connect/cms/contents', token, instance, {
                'contentSpaceOrFolderId': workspace_id,
                'contentType': 'sfdc_cms__image',
                'title': (config.get('brandName', 'Brand') + '_' + (img.get('alt', 'image'))[:40]).replace(' ', '_'),
                'contentBody': {
                    'sfdc_cms:media': {
                        'source': {'type': 'url', 'url': img['url']}
                    }
                }
            })
            if img_resp.ok:
                content_ids.append(img_resp.json()['managedContentId'])
            else:
                errors.append(f'Image upload failed: {img.get("alt", "unknown")}')
        except Exception as e:
            errors.append(f'Image error: {str(e)[:100]}')

    # 2. Create Brand content item
    try:
        brand_body = build_brand_content_body(config)
        brand_resp = sf_api('POST', '/services/data/v62.0/connect/cms/contents', token, instance, {
            'contentSpaceOrFolderId': workspace_id,
            'contentType': 'sfdc_cms__brand',
            'title': config.get('brandName', 'Brand'),
            'contentBody': brand_body
        })
        if brand_resp.ok:
            brand_id = brand_resp.json()['managedContentId']
            content_ids.append(brand_id)
        else:
            err_detail = brand_resp.text[:300]
            return {'success': False, 'brandId': '', 'contentIds': content_ids, 'totalCreated': len(content_ids),
                    'errors': errors + [f'Brand creation failed: {err_detail}']}
    except Exception as e:
        return {'success': False, 'brandId': '', 'contentIds': content_ids, 'totalCreated': len(content_ids),
                'errors': errors + [f'Brand creation error: {str(e)[:200]}']}

    # 3. Publish all content
    if content_ids:
        try:
            pub_resp = sf_api('POST', '/services/data/v62.0/connect/cms/contents/publish', token, instance, {
                'contentIds': content_ids
            })
            if not pub_resp.ok:
                errors.append('Publishing failed — content created but not published.')
        except Exception as e:
            errors.append(f'Publish error: {str(e)[:100]}')

    return {
        'success': True,
        'brandId': brand_id,
        'contentIds': content_ids,
        'totalCreated': len(content_ids),
        'errors': errors
    }


@app.route('/api/sf/deploy', methods=['POST'])
def deploy_brand():
    """Deploy brand config + images to Salesforce CMS."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    if not token or not instance:
        return jsonify({'error': 'Not connected to Salesforce'}), 401

    data = request.json or {}
    config = data.get('config', {})
    workspace_id = data.get('workspaceId', '')
    if not workspace_id:
        return jsonify({'error': 'No workspace selected'}), 400

    result = _deploy_brand_internal(token, instance, config, workspace_id)
    if not result.get('success'):
        return jsonify({'error': result['errors'][-1] if result['errors'] else 'Deploy failed', 'imageErrors': result['errors']}), 500
    return jsonify(result)


@app.route('/api/sf/status')
@app.route('/api/sf/connection-status')
def sf_status():
    """Check if user is connected to Salesforce."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    return jsonify({
        'connected': bool(token and instance),
        'instanceUrl': instance or ''
    })


# ─── Email Series Generation ───

EMAIL_SERIES = {
    'nurture': {
        'name': 'Nurture Series',
        'description': 'Sell the value of your industry with urgent (but not aggressive) CTAs.',
        'wait_days': 5,
        'emails': [
            {'key': 'nurture_1', 'name': 'Nurture Email 1 — Industry Value', 'order': 1},
            {'key': 'nurture_2', 'name': 'Nurture Email 2 — Social Proof', 'order': 2},
            {'key': 'nurture_3', 'name': 'Nurture Email 3 — Urgency & Action', 'order': 3},
        ]
    },
    'welcome': {
        'name': 'Welcome Series',
        'description': 'Onboard new customers with a warm introduction to your services.',
        'wait_days': 1,
        'emails': [
            {'key': 'welcome_1', 'name': 'Welcome Email 1 — Welcome', 'order': 1},
            {'key': 'welcome_2', 'name': 'Welcome Email 2 — Get Started', 'order': 2},
            {'key': 'welcome_3', 'name': 'Welcome Email 3 — Go Deeper', 'order': 3},
        ]
    }
}


# ── Industry-specific email copy templates ──
# Each key maps to: { nurture: [3 dicts], welcome: [3 dicts] }
# Fields per email: subject, preheader, heading, body, cta_text

def _nurture_copy_general(bn, ct):
    """General / non-industry nurture copy."""
    return [
        {
            'subject': f'Discover What {bn} Can Do for You',
            'preheader': f'See how {bn} helps {ct} like you succeed.',
            'heading': f'Built for {ct.title()} Like You',
            'body': f'{bn} was created to solve real problems and deliver real results. Whether you\'re looking for better tools, smarter solutions, or a partner who gets it — we\'re here to help you move forward with confidence.',
            'cta_text': 'Learn More',
        },
        {
            'subject': f'Why {ct.title()} Trust {bn}',
            'preheader': 'Real results from real people.',
            'heading': 'See the Difference',
            'body': f'Across the board, {ct} are choosing {bn} for the reliability, quality, and service they can count on. Don\'t just take our word for it — the results speak for themselves.',
            'cta_text': 'See Success Stories',
        },
        {
            'subject': f'Ready to Get Started with {bn}?',
            'preheader': f'Take the next step — it only takes a minute.',
            'heading': 'Your Next Step Starts Here',
            'body': f'You\'ve seen what {bn} can do. Now it\'s time to experience it for yourself. Getting started is simple, and our team is ready to make sure you hit the ground running.',
            'cta_text': 'Get Started Now',
        },
    ]

def _welcome_copy_general(bn, ct):
    """General / non-industry welcome copy."""
    return [
        {
            'subject': f'Welcome to {bn}!',
            'preheader': f'We\'re glad you\'re here.',
            'heading': f'Welcome Aboard!',
            'body': f'Thank you for choosing {bn}. We\'re excited to have you and committed to making your experience outstanding. Here\'s to a great partnership.',
            'cta_text': 'Explore Your Account',
        },
        {
            'subject': f'Get the Most Out of {bn}',
            'preheader': 'Log in and see what\'s waiting for you.',
            'heading': f'Your Account is Ready',
            'body': f'Log into your account and discover everything {bn} has to offer. From helpful tools to personalized features, there\'s a lot to explore — and we\'re here to help every step of the way.',
            'cta_text': 'Log In Now',
        },
        {
            'subject': f'Go Further with {bn}',
            'preheader': 'Take your experience to the next level.',
            'heading': 'There\'s More to Discover',
            'body': f'Now that you\'re settled in, it\'s time to explore the full range of what {bn} offers. Dive deeper into features, resources, and tools designed to help you get more done.',
            'cta_text': 'Explore More',
        },
    ]

def _nurture_copy_banking(bn, ct):
    """Banking industry nurture copy (retail, commercial, community)."""
    return [
        {
            'subject': f'Your Financial Future Starts with {bn}',
            'preheader': f'See how smarter banking makes a difference for {ct}.',
            'heading': f'Banking That Works for You',
            'body': f'Managing your finances shouldn\'t be complicated. {bn} offers tools and services designed to simplify your banking, protect your money, and help you reach your goals — all with the personal attention you deserve.',
            'cta_text': 'Explore Our Services',
        },
        {
            'subject': f'How {ct.title()} Like You Are Getting Ahead',
            'preheader': f'{bn} {ct} are building stronger financial futures.',
            'heading': f'{ct.title()} Are Choosing {bn}',
            'body': f'From competitive rates to intuitive digital tools, {ct} are finding that {bn} delivers the banking experience they\'ve been looking for. Join the growing number of people who trust us with their financial well-being.',
            'cta_text': 'See What We Offer',
        },
        {
            'subject': f'Don\'t Miss Out — Start Banking with {bn}',
            'preheader': 'Your better banking experience is one step away.',
            'heading': 'Take the Next Step Today',
            'body': f'The right financial partner can make all the difference. With {bn}, you\'ll get the rates, security, and service that help you move forward. Open your account today and see why {ct} are making the switch.',
            'cta_text': 'Open an Account',
        },
    ]

def _welcome_copy_banking(bn, ct):
    """Banking industry welcome copy."""
    return [
        {
            'subject': f'Welcome to {bn}!',
            'preheader': f'Your new banking relationship starts today.',
            'heading': f'Welcome to the {bn} Family!',
            'body': f'Thank you for choosing {bn} as your financial partner. We\'re committed to providing you with secure, convenient banking and the personal service you deserve. We\'re glad to have you.',
            'cta_text': 'Access Your Account',
        },
        {
            'subject': f'Log In and Explore Your {bn} Account',
            'preheader': 'Discover the tools and features waiting for you.',
            'heading': f'Your Account Is Ready to Go',
            'body': f'Log into your {bn} account and explore everything at your fingertips — from mobile banking and bill pay to savings tools and account alerts. We\'ve made it easy to manage your money on your terms.',
            'cta_text': 'Log In Now',
        },
        {
            'subject': f'Make the Most of Your {bn} Membership',
            'preheader': 'There\'s even more to discover.',
            'heading': 'Go Deeper with Your Finances',
            'body': f'Now that you\'re set up, take advantage of everything {bn} has to offer. Explore our financial planning resources, set savings goals, and discover products designed to help {ct} like you build a stronger financial future.',
            'cta_text': 'Explore Resources',
        },
    ]

def _nurture_copy_insurance(bn, ct):
    """Insurance industry nurture copy."""
    return [
        {
            'subject': f'Protecting What Matters Most — {bn}',
            'preheader': f'See how the right coverage brings peace of mind.',
            'heading': f'Coverage You Can Count On',
            'body': f'Life is unpredictable, but your coverage doesn\'t have to be. {bn} offers policies designed to protect {ct} from the unexpected — giving you the confidence to focus on what matters most.',
            'cta_text': 'Explore Coverage',
        },
        {
            'subject': f'Why {ct.title()} Choose {bn}',
            'preheader': f'Protection backed by trust and expertise.',
            'heading': f'Trusted by {ct.title()} Like You',
            'body': f'{ct.title()} choose {bn} for the combination of comprehensive coverage, responsive service, and competitive rates. When it comes to protection, having the right partner makes all the difference.',
            'cta_text': 'See Our Plans',
        },
        {
            'subject': f'Don\'t Wait — Get Protected with {bn}',
            'preheader': 'The best time to get covered is now.',
            'heading': 'Secure Your Coverage Today',
            'body': f'Every day without the right coverage is a risk you don\'t need to take. {bn} makes it easy to find the protection that fits your life — and our team is ready to help you get started.',
            'cta_text': 'Get a Quote',
        },
    ]

def _welcome_copy_insurance(bn, ct):
    """Insurance industry welcome copy."""
    return [
        {
            'subject': f'Welcome to {bn}!',
            'preheader': f'Your coverage is in good hands.',
            'heading': f'Welcome, and Thank You!',
            'body': f'Thank you for trusting {bn} with your coverage. We take that responsibility seriously and are committed to being here for you — whenever you need us. Welcome to the family.',
            'cta_text': 'View Your Policy',
        },
        {
            'subject': f'Log In and Review Your {bn} Policy',
            'preheader': 'Get familiar with your coverage details.',
            'heading': f'Your Policy Dashboard Is Ready',
            'body': f'Log into your {bn} account to review your policy details, download your ID cards, and learn about the full range of benefits available to you. Managing your coverage has never been easier.',
            'cta_text': 'Log In Now',
        },
        {
            'subject': f'Get More from Your {bn} Coverage',
            'preheader': 'Discover additional benefits and resources.',
            'heading': 'There\'s More to Your Coverage',
            'body': f'Beyond your core policy, {bn} offers resources to help {ct} stay protected and informed. Explore our claims support, risk prevention tips, and additional coverage options designed for your peace of mind.',
            'cta_text': 'Explore Benefits',
        },
    ]

def _nurture_copy_wealth(bn, ct):
    """Wealth & asset management nurture copy."""
    return [
        {
            'subject': f'Building a Stronger Financial Future — {bn}',
            'preheader': f'Strategic guidance for {ct} who expect more.',
            'heading': f'Your Wealth Deserves Expert Stewardship',
            'body': f'Achieving your financial goals requires more than good intentions — it requires strategy, discipline, and the right partner. {bn} provides personalized guidance to help {ct} protect and grow what they\'ve built.',
            'cta_text': 'Learn Our Approach',
        },
        {
            'subject': f'How {ct.title()} Achieve Their Goals with {bn}',
            'preheader': f'Results-driven strategies for lasting wealth.',
            'heading': f'Trusted by {ct.title()} Who Demand More',
            'body': f'{ct.title()} choose {bn} for the depth of expertise, personalized attention, and proven track record. Whether it\'s legacy planning, portfolio optimization, or risk management — we deliver results that matter.',
            'cta_text': 'See Our Track Record',
        },
        {
            'subject': f'Let\'s Build Your Financial Strategy — {bn}',
            'preheader': 'Schedule your consultation today.',
            'heading': 'Your Strategy Starts with a Conversation',
            'body': f'Every great financial outcome starts with a plan. {bn} is ready to sit down with you, understand your goals, and build a tailored strategy that puts you on the path to long-term success.',
            'cta_text': 'Schedule a Consultation',
        },
    ]

def _welcome_copy_wealth(bn, ct):
    """Wealth & asset management welcome copy."""
    return [
        {
            'subject': f'Welcome to {bn}',
            'preheader': f'Your financial partnership begins today.',
            'heading': f'Welcome to {bn}',
            'body': f'Thank you for placing your trust in {bn}. We\'re honored to serve as your financial partner and committed to delivering the insight, attention, and results you expect.',
            'cta_text': 'Access Your Portal',
        },
        {
            'subject': f'Your {bn} Client Portal Is Ready',
            'preheader': 'Log in to explore your personalized dashboard.',
            'heading': f'Explore Your Client Dashboard',
            'body': f'Your {bn} client portal gives you real-time visibility into your portfolio, performance reports, and direct communication with your advisory team. Log in to get started.',
            'cta_text': 'Log In Now',
        },
        {
            'subject': f'Deepen Your Partnership with {bn}',
            'preheader': 'Explore our full suite of services.',
            'heading': 'There\'s More We Can Do Together',
            'body': f'Beyond portfolio management, {bn} offers comprehensive financial planning, estate strategy, tax optimization, and more. Let us know how we can help you take the next step in your financial journey.',
            'cta_text': 'Explore Services',
        },
    ]

def _nurture_copy_lending(bn, ct):
    """Lending & mortgage nurture copy."""
    return [
        {
            'subject': f'Your Path to Homeownership Starts Here — {bn}',
            'preheader': f'Guidance for {ct} at every step of the journey.',
            'heading': f'Making Your Goals Achievable',
            'body': f'Whether you\'re buying your first home or refinancing, {bn} is here to simplify the process. We offer competitive rates, transparent terms, and the personal guidance that helps {ct} move forward with confidence.',
            'cta_text': 'See Today\'s Rates',
        },
        {
            'subject': f'How {ct.title()} Are Reaching Their Goals with {bn}',
            'preheader': f'Real stories from {ct} who found the right fit.',
            'heading': f'{ct.title()} Trust {bn}',
            'body': f'{ct.title()} choose {bn} because we make the lending process clear, fair, and fast. From pre-approval to closing day, our team works to make sure you\'re supported every step of the way.',
            'cta_text': 'Hear Their Stories',
        },
        {
            'subject': f'Ready to Make Your Move? {bn} Is Here to Help',
            'preheader': 'Get pre-approved in minutes — not days.',
            'heading': 'Your Next Chapter Is Waiting',
            'body': f'Don\'t let the lending process hold you back. With {bn}, getting pre-approved is fast and straightforward. Take the first step today and bring your goals within reach.',
            'cta_text': 'Get Pre-Approved',
        },
    ]

def _welcome_copy_lending(bn, ct):
    """Lending & mortgage welcome copy."""
    return [
        {
            'subject': f'Welcome to {bn}!',
            'preheader': f'Your lending journey is in great hands.',
            'heading': f'Welcome, and Congratulations!',
            'body': f'Thank you for choosing {bn}. We know this is a big step, and we\'re committed to making every part of the process as smooth and transparent as possible. Welcome aboard.',
            'cta_text': 'View Your Loan',
        },
        {
            'subject': f'Log In to Your {bn} Account',
            'preheader': 'Track your loan and manage your payments.',
            'heading': f'Your Loan Dashboard Is Ready',
            'body': f'Log into your {bn} account to view your loan details, set up automatic payments, and access helpful resources. Managing your loan should be simple — and with us, it is.',
            'cta_text': 'Log In Now',
        },
        {
            'subject': f'Get More from Your {bn} Relationship',
            'preheader': 'Explore tools and resources for your financial journey.',
            'heading': 'We\'re Here Beyond the Closing Table',
            'body': f'{bn} is more than a lender — we\'re a long-term partner. Explore our homeowner resources, financial planning tools, and refinancing options designed to help {ct} thrive.',
            'cta_text': 'Explore Resources',
        },
    ]

def _nurture_copy_payments(bn, ct):
    """Payments & fintech nurture copy."""
    return [
        {
            'subject': f'Faster, Smarter Payments — {bn}',
            'preheader': f'See how modern payments technology works for {ct}.',
            'heading': f'Payments, Reimagined',
            'body': f'In a world that moves fast, your payments should too. {bn} delivers seamless, secure transaction technology that helps {ct} send, receive, and manage money with zero friction.',
            'cta_text': 'See How It Works',
        },
        {
            'subject': f'Why {ct.title()} Are Switching to {bn}',
            'preheader': 'Speed, security, and simplicity — all in one.',
            'heading': f'The Smarter Way to Pay',
            'body': f'{ct.title()} are moving to {bn} for the speed, reliability, and modern experience they need. From real-time transactions to intelligent analytics, we\'re built for the way business moves today.',
            'cta_text': 'Compare Features',
        },
        {
            'subject': f'Ready to Upgrade Your Payments? Try {bn}',
            'preheader': 'Get set up in minutes — no headaches.',
            'heading': 'Start Moving Money Smarter',
            'body': f'Outdated payment processes cost you time and money. {bn} makes it easy to get started with modern payment technology — fast setup, transparent pricing, and support when you need it.',
            'cta_text': 'Get Started',
        },
    ]

def _welcome_copy_payments(bn, ct):
    """Payments & fintech welcome copy."""
    return [
        {
            'subject': f'Welcome to {bn}!',
            'preheader': f'Your new payments experience starts now.',
            'heading': f'Welcome to {bn}!',
            'body': f'You\'re all set. {bn} is here to help you move money faster, smarter, and more securely. We\'re excited to have you on board and ready to help you get the most out of our platform.',
            'cta_text': 'Go to Dashboard',
        },
        {
            'subject': f'Your {bn} Account Is Live',
            'preheader': 'Log in and explore your new dashboard.',
            'heading': f'Explore Your Dashboard',
            'body': f'Your {bn} dashboard gives you full visibility into your transactions, analytics, and account settings. Log in now to send your first payment, connect your accounts, and see what\'s possible.',
            'cta_text': 'Log In Now',
        },
        {
            'subject': f'Unlock the Full Power of {bn}',
            'preheader': 'Advanced features and integrations await.',
            'heading': 'There\'s Even More to Explore',
            'body': f'Beyond basic transactions, {bn} offers powerful integrations, real-time analytics, and automation tools that help {ct} save time and scale. Discover what\'s next.',
            'cta_text': 'Explore Features',
        },
    ]

# Map industry groups to their copy generators
INDUSTRY_COPY_MAP = {
    'General':                  (_nurture_copy_general,   _welcome_copy_general),
    'Banking':                  (_nurture_copy_banking,   _welcome_copy_banking),
    'Insurance':                (_nurture_copy_insurance, _welcome_copy_insurance),
    'Wealth & Asset Management':(_nurture_copy_wealth,    _welcome_copy_wealth),
    'Lending':                  (_nurture_copy_lending,   _welcome_copy_lending),
    'Payments':                 (_nurture_copy_payments,  _welcome_copy_payments),
}


def generate_series_copy(series_key, config):
    """Generate all email copy for a series, based on industry tone."""
    brand_name = config.get('brandName', 'Brand')
    tone_key = config.get('tone', {}).get('key', 'general')
    tone = INDUSTRY_TONES.get(tone_key, INDUSTRY_TONES['general'])
    customer_term = tone['customer_term']
    industry_group = tone['industry']

    nurture_fn, welcome_fn = INDUSTRY_COPY_MAP.get(
        industry_group, (_nurture_copy_general, _welcome_copy_general)
    )

    if series_key == 'nurture':
        copies = nurture_fn(brand_name, customer_term)
    else:
        copies = welcome_fn(brand_name, customer_term)

    series = EMAIL_SERIES[series_key]
    results = []
    for i, email_def in enumerate(series['emails']):
        copy = copies[i] if i < len(copies) else copies[-1]
        results.append({
            'key': email_def['key'],
            'name': email_def['name'],
            'order': email_def['order'],
            'subject': copy['subject'],
            'preheader': copy.get('preheader', ''),
            'heading': copy.get('heading', ''),
            'body': copy['body'],
            'cta_text': copy.get('cta_text', 'Learn More'),
            'cta_url': '#',
        })
    return results


def render_email_html(copy_data, config, logo_url='', hero_url='', header_color=None):
    """Render a single email to full HTML, given copy fields + brand config + images."""
    colors = config.get('colors', [])
    primary = header_color or (colors[0]['hex'] if len(colors) > 0 else '#0176d3')
    secondary = colors[1]['hex'] if len(colors) > 1 else '#032d60'
    brand_name = config.get('brandName', 'Brand')

    # Button uses the brand's button color (not header color)
    btn_color = config.get('buttonStyle', {}).get('color', '') or (colors[0]['hex'] if len(colors) > 0 else '#0176d3')
    btn_rgb = hex_to_rgb(btn_color)
    btn_text_color = '#ffffff' if btn_rgb and brightness(*btn_rgb) < 128 else '#032d60'

    font_family = config.get('typography', {}).get('bodyFont', 'Arial')
    btn_radius = config.get('buttonStyle', {}).get('borderRadius', 4)

    subject = copy_data.get('subject', '')
    preheader = copy_data.get('preheader', '')
    heading = copy_data.get('heading', '')
    body_text = copy_data.get('body', '')
    cta_text = copy_data.get('cta_text', 'Learn More')
    cta_url = copy_data.get('cta_url', '#')

    logo_section = ''
    if logo_url:
        logo_section = f'<img src="{logo_url}" alt="{brand_name}" style="max-width:200px;max-height:60px;display:block;margin:0 auto;">'

    hero_section = ''
    if hero_url:
        hero_section = f'''
        <tr>
          <td style="padding:0;">
            <img src="{hero_url}" alt="{brand_name}" style="width:100%;max-width:600px;display:block;height:auto;">
          </td>
        </tr>'''

    email_html = f'''<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f4f4;font-family:{font_family},Arial,Helvetica,sans-serif;">
<div style="display:none;max-height:0;overflow:hidden;font-size:1px;line-height:1px;color:#f4f4f4;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f4f4;">
<tr>
<td align="center" style="padding:20px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
  <tr>
    <td style="background-color:{primary};padding:24px 40px;text-align:center;">
      {logo_section}
    </td>
  </tr>
  {hero_section}
  <tr>
    <td style="padding:40px 40px 20px 40px;text-align:center;">
      <h1 style="font-family:{font_family},Arial,sans-serif;color:{secondary};font-size:28px;font-weight:700;margin:0 0 16px 0;line-height:1.3;text-align:center;">
        {heading}
      </h1>
      <p style="font-family:{font_family},Arial,sans-serif;color:#333333;font-size:16px;line-height:1.6;margin:0 0 28px 0;text-align:center;">
        {body_text}
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:0 auto;">
      <tr>
        <td align="center" style="background-color:{btn_color};border-radius:{btn_radius}px;mso-padding-alt:14px 40px;">
          <a href="{cta_url}" target="_blank" style="display:inline-block;padding:14px 40px;font-family:{font_family},Arial,sans-serif;font-size:16px;font-weight:700;color:{btn_text_color};text-decoration:none;border-radius:{btn_radius}px;background-color:{btn_color};text-align:center;min-width:200px;">
            {cta_text}
          </a>
        </td>
      </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td style="background-color:#f8f8f8;padding:24px 40px;border-top:1px solid #e5e5e5;">
      <p style="font-family:{font_family},Arial,sans-serif;color:#999999;font-size:12px;line-height:1.5;margin:0;text-align:center;">
        &copy; {brand_name}. All rights reserved.<br>
        <a href="%%unsub_center_url%%" style="color:#999999;">Unsubscribe</a> |
        <a href="%%profile_center_url%%" style="color:#999999;">Preferences</a>
      </p>
      <p style="font-family:{font_family},Arial,sans-serif;color:#cccccc;font-size:11px;text-align:center;margin:8px 0 0 0;">
        %%Member_Busname%% | %%Member_Addr%% %%Member_City%%, %%Member_State%% %%Member_PostalCode%%
      </p>
    </td>
  </tr>
</table>
</td>
</tr>
</table>
</body>
</html>'''

    return email_html


@app.route('/api/email-series', methods=['GET'])
def get_email_series_info():
    """Return available email series metadata for the frontend."""
    return jsonify(EMAIL_SERIES)


@app.route('/api/email-series-copy', methods=['POST'])
def get_email_series_copy():
    """Generate industry-specific copy for selected email series."""
    data = request.json or {}
    config = data.get('config', {})
    series_keys = data.get('series', ['nurture', 'welcome'])

    result = {}
    for sk in series_keys:
        if sk in EMAIL_SERIES:
            result[sk] = generate_series_copy(sk, config)

    return jsonify(result)


@app.route('/api/email-preview', methods=['POST'])
def get_email_preview():
    """Render a single email preview from editable copy + brand config."""
    data = request.json or {}
    copy_data = data.get('copy', {})
    config = data.get('config', {})
    logo_url = data.get('logoUrl', '')
    hero_url = data.get('heroUrl', '')
    header_color = data.get('headerColor', None)

    html = render_email_html(copy_data, config, logo_url, hero_url, header_color=header_color)
    return jsonify({'html': html})


# ─── Flow Metadata XML Generation ───

def generate_flow_xml(series_key, email_content_keys, config, workspace_name='Default_Content_Workspace',
                      segment_id='', sender_id='', subscription_id='', channel_type_id='',
                      data_graph='Marketing_Data_Graph', dmo_object='UnifiedssotIndividualInd1__dlm'):
    """Generate a segment-triggered MCA flow XML with sendEmailMessage actions (no waits - add via Flow Builder)."""
    series = EMAIL_SERIES.get(series_key)
    if not series:
        return None

    brand_name = config.get('brandName', 'Brand')
    series_name = series['name']
    wait_days = series['wait_days']
    flow_label = f"{brand_name} {series_name}"
    flow_api_name = re.sub(r'[^A-Za-z0-9_]', '_', flow_label).replace('__', '_')

    # Build action calls (waits must be added via Flow Builder UI after deployment)
    action_calls_xml = ''
    num_emails = min(len(email_content_keys), len(series['emails']))

    for i in range(num_emails):
        email_key = email_content_keys[i]
        email_def = series['emails'][i]
        action_name = f"Send_Email_{i + 1}"
        y_offset = 278 + (i * 240)

        # Determine next element connector (chain directly to next email action)
        # Note: Wait elements cannot be deployed via metadata XML — they must be added
        # via Flow Builder UI after deployment. We chain emails directly here.
        if i < num_emails - 1:
            next_action = f"Send_Email_{i + 2}"
            next_ref = f"<connector><targetReference>{next_action}</targetReference></connector>"
        else:
            next_ref = ''  # Last email has no connector (flow ends)

        # Build sender params
        sender_params = ''
        if sender_id:
            sender_params = f'''
        <inputParameters>
            <name>senderId</name>
            <value><stringValue>{sender_id}</stringValue></value>
        </inputParameters>'''

        # Build subscription params
        sub_params = ''
        if subscription_id:
            sub_params = f'''
        <inputParameters>
            <name>communicationSubscriptionId</name>
            <value><stringValue>{subscription_id}</stringValue></value>
        </inputParameters>'''
        if channel_type_id:
            sub_params += f'''
        <inputParameters>
            <name>commSubscriptionChannelTypeId</name>
            <value><stringValue>{channel_type_id}</stringValue></value>
        </inputParameters>'''

        content_id = f"marketing--{workspace_name}.sfdc_cms__email--{email_key}"

        action_calls_xml += f'''
    <actionCalls>
        <name>{action_name}</name>
        <label>Send {series_name} Email {i + 1}</label>
        <locationX>176</locationX>
        <locationY>{y_offset}</locationY>
        <actionName>sendEmailMessage</actionName>
        <actionType>sendEmailMessage</actionType>
        {next_ref}
        <flowTransactionModel>CurrentTransaction</flowTransactionModel>
        <inputParameters>
            <name>contentId</name>
            <value>
                <stringValue>{content_id}</stringValue>
            </value>
        </inputParameters>
        <inputParameters>
            <name>isTemplate</name>
            <value><booleanValue>false</booleanValue></value>
        </inputParameters>{sender_params}
        <inputParameters>
            <name>clickTracking</name>
            <value><booleanValue>true</booleanValue></value>
        </inputParameters>
        <inputParameters>
            <name>openTracking</name>
            <value><booleanValue>true</booleanValue></value>
        </inputParameters>{sub_params}
        <inputParameters><name>outreachSourceCodeId</name></inputParameters>
        <inputParameters><name>selectedOfferIds</name></inputParameters>
        <nameSegment>sendEmailMessage</nameSegment>
        <offset>0</offset>
    </actionCalls>'''

        # Note: Wait-for-Amount-of-Time elements cannot be deployed via Metadata API.
        # They must be added via Flow Builder UI after the flow is deployed.
        # The flow is deployed with emails chained directly — user adds waits in UI.

    # Segment element
    segment_xml = f'<segment>{segment_id}</segment>' if segment_id else '<segment></segment>'

    flow_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">{action_calls_xml}
    <apiVersion>67.0</apiVersion>
    <areMetricsLoggedToDataCloud>true</areMetricsLoggedToDataCloud>
    <dataSpace>default</dataSpace>
    <environments>Default</environments>
    <interviewLabel>{flow_label} {{!$Flow.CurrentDateTime}}</interviewLabel>
    <label>{flow_label}</label>
    <processMetadataValues>
        <name>BuilderType</name>
        <value><stringValue>LightningFlowBuilder</stringValue></value>
    </processMetadataValues>
    <processMetadataValues>
        <name>CanvasMode</name>
        <value><stringValue>AUTO_LAYOUT_CANVAS</stringValue></value>
    </processMetadataValues>
    <processMetadataValues>
        <name>OriginBuilderType</name>
        <value><stringValue>LightningFlowBuilder</stringValue></value>
    </processMetadataValues>
    <processType>AutoLaunchedFlow</processType>
    <start>
        <locationX>50</locationX>
        <locationY>0</locationY>
        <connector>
            <targetReference>Send_Email_1</targetReference>
        </connector>
        <dataGraph>{data_graph}</dataGraph>
        <object>{dmo_object}</object>
        <publishSegment>true</publishSegment>
        <schedule>
            <dayOfMonthToRun>0</dayOfMonthToRun>
            <frequency>OnActivate</frequency>
            <frequencyNumber>0</frequencyNumber>
        </schedule>
        {segment_xml}
        <triggerType>Segment</triggerType>
    </start>
    <status>Draft</status>
    <timeZoneSidKey>America/Los_Angeles</timeZoneSidKey>
</Flow>'''

    return {
        'xml': flow_xml,
        'flowApiName': flow_api_name,
        'flowLabel': flow_label
    }


@app.route('/api/generate-flow-xml', methods=['POST'])
def api_generate_flow_xml():
    """Generate flow XML for preview/download."""
    data = request.json or {}
    series_key = data.get('series', 'nurture')
    email_content_keys = data.get('emailContentKeys', [])
    config = data.get('config', {})
    workspace_name = data.get('workspaceName', 'Default_Content_Workspace')
    segment_id = data.get('segmentId', '')
    sender_id = data.get('senderId', '')
    subscription_id = data.get('subscriptionId', '')
    channel_type_id = data.get('channelTypeId', '')

    if series_key not in EMAIL_SERIES:
        return jsonify({'error': f'Invalid series: {series_key}'}), 400

    result = generate_flow_xml(
        series_key, email_content_keys, config,
        workspace_name, segment_id, sender_id, subscription_id, channel_type_id
    )

    if not result:
        return jsonify({'error': 'Failed to generate flow XML'}), 500

    return jsonify(result)


# ─── Token Refresh Helper ───

def try_refresh_token():
    """Attempt to refresh the Salesforce access token using the stored refresh token.
    Returns True if successful, False otherwise."""
    refresh = session.get('sf_refresh_token', '')
    if not refresh or not SF_CLIENT_ID or not SF_CLIENT_SECRET:
        return False

    for host in ['login.salesforce.com', 'test.salesforce.com']:
        try:
            resp = requests.post(f'https://{host}/services/oauth2/token', data={
                'grant_type': 'refresh_token',
                'client_id': SF_CLIENT_ID,
                'client_secret': SF_CLIENT_SECRET,
                'refresh_token': refresh
            }, timeout=15)
            if resp.ok:
                data = resp.json()
                session['sf_access_token'] = data['access_token']
                if 'instance_url' in data:
                    session['sf_instance_url'] = data['instance_url']
                return True
        except Exception:
            continue
    return False


# ─── CMS Email Upload + Deploy ───

@app.route('/api/sf/consent-config', methods=['GET'])
def get_consent_config():
    """Query org for available senders, subscriptions, channels, and segments."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    if not token or not instance:
        return jsonify({'error': 'Not connected to Salesforce'}), 401

    result = {}

    # OrgWideEmailAddress (senders)
    try:
        resp = sf_api('GET', '/services/data/v66.0/query/?q=' +
                       quote("SELECT Id, Address, DisplayName FROM OrgWideEmailAddress ORDER BY DisplayName"),
                       token, instance)
        if resp.ok:
            records = resp.json().get('records', [])
            result['senders'] = [{'id': r['Id'], 'label': f"{r['DisplayName']} <{r['Address']}>"} for r in records]
        else:
            result['senders'] = []
    except Exception:
        result['senders'] = []

    # CommSubscription (communication subscriptions)
    try:
        resp = sf_api('GET', '/services/data/v66.0/query/?q=' +
                       quote("SELECT Id, Name FROM CommSubscription ORDER BY Name"),
                       token, instance)
        if resp.ok:
            records = resp.json().get('records', [])
            result['subscriptions'] = [{'id': r['Id'], 'name': r['Name']} for r in records]
        else:
            result['subscriptions'] = []
    except Exception:
        result['subscriptions'] = []

    # CommSubscriptionChannelType (channel types)
    try:
        resp = sf_api('GET', '/services/data/v66.0/query/?q=' +
                       quote("SELECT Id, Name, CommunicationSubscriptionId FROM CommSubscriptionChannelType ORDER BY Name"),
                       token, instance)
        if resp.ok:
            records = resp.json().get('records', [])
            result['channelTypes'] = [{'id': r['Id'], 'name': r['Name'], 'subscriptionId': r['CommunicationSubscriptionId']} for r in records]
        else:
            result['channelTypes'] = []
    except Exception:
        result['channelTypes'] = []

    # MarketSegment (available segments)
    try:
        resp = sf_api('GET', '/services/data/v66.0/query/?q=' +
                       quote("SELECT Id, Name, Status FROM MarketSegment WHERE Status = 'Published' ORDER BY Name"),
                       token, instance)
        if resp.ok:
            records = resp.json().get('records', [])
            result['segments'] = [{'id': r['Id'], 'name': r['Name']} for r in records]
        else:
            result['segments'] = []
    except Exception:
        result['segments'] = []

    return jsonify(result)


def build_cms_email_content_json(email_html, subject, preheader, title=''):
    """Build the CMS email contentBody structure for sfdc_cms__email.

    Matches the real MCA CMS email schema with top-level subjectLine, preheader,
    sfdc_cms:title, messagePurpose, and the block tree using definition/children.
    """
    block_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    column_id = str(uuid.uuid4())
    html_id = str(uuid.uuid4())

    return {
        "subjectLine": subject,
        "preheader": preheader or "",
        "sfdc_cms:title": title or subject,
        "messagePurpose": "promotional",
        "sfdc_cms:block": {
            "definition": "sfdc_cms/rootContentBlock",
            "id": block_id,
            "type": "block",
            "children": [
                {
                    "definition": "lightning/section",
                    "id": section_id,
                    "type": "block",
                    "attributes": {
                        "stackOnMobile": True
                    },
                    "children": [
                        {
                            "definition": "lightning/column",
                            "id": column_id,
                            "type": "block",
                            "attributes": {
                                "columnWidth": 12.0
                            },
                            "children": [
                                {
                                    "definition": "lightning/html",
                                    "id": html_id,
                                    "type": "block",
                                    "attributes": {
                                        "rawHtml": email_html
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }


def _deploy_email_series_internal(token, instance, data):
    """Core email series deploy logic: uploads emails to CMS, publishes, creates flow + campaign.
    Returns dict with emails, flow, campaign, errors, success, totalCreated."""
    series_key = data.get('series', '')
    emails = data.get('emails', [])
    config = data.get('config', {})
    workspace_id = data.get('workspaceId', '')
    workspace_name = data.get('workspaceName', 'Default_Content_Workspace')
    segment_id = data.get('segmentId', '')
    sender_id = data.get('senderId', '')
    subscription_id = data.get('subscriptionId', '')
    channel_type_id = data.get('channelTypeId', '')
    create_flow = data.get('createFlow', True)
    create_campaign = data.get('createCampaign', True)
    header_color = data.get('headerColor', None)

    if series_key not in EMAIL_SERIES:
        return {'success': False, 'errors': [f'Invalid series: {series_key}'], 'emails': [], 'flow': None, 'campaign': None, 'totalCreated': 0}
    if not workspace_id:
        return {'success': False, 'errors': ['No workspace selected'], 'emails': [], 'flow': None, 'campaign': None, 'totalCreated': 0}
    if not emails:
        return {'success': False, 'errors': ['No emails provided'], 'emails': [], 'flow': None, 'campaign': None, 'totalCreated': 0}

    brand_name = config.get('brandName', 'Brand')
    series = EMAIL_SERIES[series_key]
    created_emails = []
    errors = []

    # Step 1: Upload each email to CMS
    for i, email_data in enumerate(emails):
        copy_data = email_data.get('copy', {})
        logo_url = email_data.get('logoUrl', '')
        hero_url = email_data.get('heroUrl', '')

        email_html = render_email_html(copy_data, config, logo_url, hero_url, header_color=header_color)

        email_name = f"{brand_name}_{series['name'].replace(' ', '_')}_Email_{i + 1}"
        email_title = f"{brand_name} - {series['name']} - Email {i + 1}"

        content_json = build_cms_email_content_json(
            email_html, copy_data.get('subject', ''), copy_data.get('preheader', ''), title=email_title
        )

        try:
            input_param = json.dumps({
                "contentSpaceOrFolderId": workspace_id,
                "contentType": "sfdc_cms__email",
                "title": email_title,
                "contentBody": content_json
            })

            files = {
                'ManagedContentInputParam': (None, input_param, 'application/json')
            }

            resp = requests.post(
                f"{instance}/services/data/v66.0/connect/cms/contents",
                headers={'Authorization': f'Bearer {token}'},
                files=files,
                timeout=30
            )

            if resp.status_code == 401 and try_refresh_token():
                token = session.get('sf_access_token')
                resp = requests.post(
                    f"{instance}/services/data/v66.0/connect/cms/contents",
                    headers={'Authorization': f'Bearer {token}'},
                    files=files,
                    timeout=30
                )

            if resp.status_code in (200, 201):
                result = resp.json()
                content_id = result.get('contentId', result.get('id', ''))
                content_key = result.get('contentKey', result.get('contentUrlName', email_name))

                created_emails.append({
                    'name': email_title,
                    'contentId': content_id,
                    'contentKey': content_key,
                    'order': i + 1
                })

                try:
                    pub_resp = requests.post(
                        f"{instance}/services/data/v66.0/connect/cms/contents/{content_id}/publish",
                        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                        json={},
                        timeout=15
                    )
                except Exception as pe:
                    errors.append(f"Publish failed for email {i + 1}: {str(pe)[:100]}")
            else:
                errors.append(f"Email {i + 1} upload failed: {resp.status_code} - {resp.text[:200]}")
        except Exception as e:
            errors.append(f"Email {i + 1}: {str(e)[:150]}")

    # Step 3: Create the flow if requested and we have emails
    flow_result = None
    if create_flow and len(created_emails) > 0:
        email_content_keys = [e['contentKey'] for e in created_emails]

        # Use pre-discovered org-specific data graph and DMO (passed from deploy_all)
        discovered_data_graph = data.get('dataGraph', 'Marketing_Data_Graph')
        discovered_dmo = data.get('dmoObject', 'UnifiedssotIndividualInd1__dlm')

        flow_data = generate_flow_xml(
            series_key, email_content_keys, config,
            workspace_name, segment_id, sender_id, subscription_id, channel_type_id,
            data_graph=discovered_data_graph, dmo_object=discovered_dmo
        )

        if flow_data:
            try:
                flow_xml = flow_data['xml']
                flow_api_name = flow_data['flowApiName']

                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                    package_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>''' + flow_api_name + '''</members>
        <name>Flow</name>
    </types>
    <version>67.0</version>
</Package>'''
                    zf.writestr('package.xml', package_xml)
                    zf.writestr(f'flows/{flow_api_name}.flow-meta.xml', flow_xml)
                zip_buffer.seek(0)
                zip_b64 = base64.b64encode(zip_buffer.read()).decode('utf-8')

                deploy_soap = f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soap:Header>
    <met:SessionHeader><met:sessionId>{token}</met:sessionId></met:SessionHeader>
  </soap:Header>
  <soap:Body>
    <met:deploy>
      <met:ZipFile>{zip_b64}</met:ZipFile>
      <met:DeployOptions>
        <met:singlePackage>true</met:singlePackage>
        <met:rollbackOnError>true</met:rollbackOnError>
      </met:DeployOptions>
    </met:deploy>
  </soap:Body>
</soap:Envelope>'''

                deploy_resp = requests.post(
                    f"{instance}/services/Soap/m/67.0",
                    headers={
                        'Content-Type': 'text/xml; charset=utf-8',
                        'SOAPAction': 'deploy'
                    },
                    data=deploy_soap.encode('utf-8'),
                    timeout=60
                )

                if deploy_resp.status_code == 200 and '<id>' in deploy_resp.text:
                    root = ET.fromstring(deploy_resp.text)
                    ns = {'met': 'http://soap.sforce.com/2006/04/metadata',
                          'soap': 'http://schemas.xmlsoap.org/soap/envelope/'}
                    deploy_id_el = root.find('.//met:id', ns)
                    deploy_id = deploy_id_el.text if deploy_id_el is not None else ''

                    # Poll checkDeployStatus until done (max 2 attempts × 2s = 4s to stay under Heroku 30s)
                    deploy_status = 'InProgress'
                    deploy_error_msg = ''
                    for _poll in range(2):
                        time.sleep(2)
                        check_soap = f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soap:Header>
    <met:SessionHeader><met:sessionId>{token}</met:sessionId></met:SessionHeader>
  </soap:Header>
  <soap:Body>
    <met:checkDeployStatus>
      <met:asyncProcessId>{deploy_id}</met:asyncProcessId>
      <met:includeDetails>true</met:includeDetails>
    </met:checkDeployStatus>
  </soap:Body>
</soap:Envelope>'''
                        check_resp = requests.post(
                            f"{instance}/services/Soap/m/67.0",
                            headers={'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': 'checkDeployStatus'},
                            data=check_soap.encode('utf-8'),
                            timeout=15
                        )
                        if check_resp.status_code == 200:
                            check_root = ET.fromstring(check_resp.text)
                            done_el = check_root.find('.//{http://soap.sforce.com/2006/04/metadata}done')
                            success_el = check_root.find('.//{http://soap.sforce.com/2006/04/metadata}success')
                            status_el = check_root.find('.//{http://soap.sforce.com/2006/04/metadata}status')
                            if done_el is not None and done_el.text == 'true':
                                if success_el is not None and success_el.text == 'true':
                                    deploy_status = 'Succeeded'
                                else:
                                    deploy_status = 'Failed'
                                    # Extract error message
                                    problem_el = check_root.find('.//{http://soap.sforce.com/2006/04/metadata}problem')
                                    if problem_el is not None and problem_el.text:
                                        deploy_error_msg = problem_el.text[:300]
                                    else:
                                        deploy_error_msg = check_resp.text[:300]
                                break
                    else:
                        deploy_status = 'Timeout'

                    if deploy_status == 'Succeeded':
                        flow_result = {
                            'id': deploy_id,
                            'name': flow_data['flowLabel'],
                            'apiName': flow_api_name,
                            'status': 'Deployed',
                            'waitNote': f'Open in Flow Builder to add {EMAIL_SERIES[series_key]["wait_days"]}-day wait elements between emails'
                        }
                    elif deploy_status == 'Failed':
                        errors.append(f"Flow deploy failed: {deploy_error_msg}")
                        flow_result = {
                            'name': flow_data['flowLabel'],
                            'apiName': flow_api_name,
                            'status': 'Failed',
                            'error': deploy_error_msg
                        }
                    else:
                        flow_result = {
                            'id': deploy_id,
                            'name': flow_data['flowLabel'],
                            'apiName': flow_api_name,
                            'status': 'Deploying (timeout waiting for confirmation)'
                        }
                else:
                    errors.append(f"Flow creation failed: {deploy_resp.status_code} - {deploy_resp.text[:200]}")
                    flow_result = {
                        'xml': flow_xml,
                        'name': flow_data['flowLabel'],
                        'apiName': flow_api_name,
                        'status': 'NotDeployed',
                        'error': f"API returned {deploy_resp.status_code}"
                    }
            except Exception as fe:
                errors.append(f"Flow deploy: {str(fe)[:150]}")
                flow_result = {
                    'xml': flow_data['xml'] if flow_data else '',
                    'name': flow_data['flowLabel'] if flow_data else '',
                    'status': 'NotDeployed',
                    'error': str(fe)[:150]
                }

    # Step 4: Create Campaign + Campaign Brief if requested
    campaign_result = None
    if create_campaign and len(created_emails) > 0:
        series = EMAIL_SERIES[series_key]
        campaign_name = f"{brand_name} {series['name']}"
        today_str = datetime.now().strftime('%Y-%m-%d')

        try:
            bu_id = ''
            try:
                bu_resp = sf_api('GET',
                    '/services/data/v66.0/query/?q=' + quote("SELECT Id FROM BusinessUnit LIMIT 1"),
                    token, instance)
                if bu_resp.ok:
                    bu_records = bu_resp.json().get('records', [])
                    if bu_records:
                        bu_id = bu_records[0]['Id']
            except Exception:
                pass

            camp_body = {
                'Name': campaign_name,
                'Type': 'Email',
                'Status': 'Planned',
                'IsActive': True,
                'Description': f"{brand_name} - {series['description']}"
            }
            if bu_id:
                camp_body['BusinessUnitId'] = bu_id

            camp_resp = sf_api('POST', '/services/data/v66.0/sobjects/Campaign', token, instance, body=camp_body)
            if camp_resp.status_code in (200, 201):
                campaign_id = camp_resp.json().get('id', '')

                campaign_result = {
                    'id': campaign_id,
                    'name': campaign_name,
                    'status': 'Created'
                }

                user_id = ''
                try:
                    user_resp = sf_api('GET', '/services/data/v66.0/chatter/users/me', token, instance)
                    if user_resp.ok:
                        user_id = user_resp.json().get('id', '')
                except Exception:
                    pass

                # Create standard Campaign Brief (Brief object)
                tone = config.get('tone', {})
                tone_label = tone.get('label', 'Professional') if isinstance(tone, dict) else str(tone)
                identity = config.get('identity', '')
                industry = config.get('industry', '')
                brand_content_id = data.get('brandContentId', '')

                subject_lines = []
                cta_texts = []
                for em in emails:
                    c = em.get('copy', {})
                    if c.get('subject'):
                        subject_lines.append(c['subject'])
                    if c.get('cta_text'):
                        cta_texts.append(c['cta_text'])

                if series_key == 'nurture':
                    primary_goal = 'Drive engagement, increase conversions, and build brand awareness'
                else:
                    primary_goal = 'Onboard new customers, drive product adoption, and build relationship'

                brief_fields = {
                    'Name': f"{brand_name} {series['name']} Brief",
                    'Description': identity or f"{brand_name} {series['name']} email campaign",
                    'KeyMessage': '\n'.join(subject_lines) if subject_lines else brand_name,
                    'TargetAudience': industry or 'General audience',
                    'PrimaryGoal': primary_goal,
                    'PrimaryCtas': '\n'.join(cta_texts) if cta_texts else '',
                    'PrimaryKpi': 'Open Rate, Click-Through Rate, Conversion Rate',
                }

                if bu_id:
                    brief_fields['BusinessUnitId'] = bu_id
                if brand_content_id:
                    brief_fields['BrandId'] = brand_content_id

                try:
                    brief_resp = sf_api('POST', '/services/data/v66.0/sobjects/Brief',
                                        token, instance, body=brief_fields)
                    if brief_resp.status_code in (200, 201):
                        brief_id = brief_resp.json().get('id', '')
                        campaign_result['briefId'] = brief_id
                        campaign_result['briefName'] = brief_fields['Name']

                        # Create BriefPlanSteps for each email in the series
                        for step_idx, em in enumerate(emails):
                            c = em.get('copy', {})
                            step_content = c.get('subject', f'Email {step_idx + 1}')
                            wait_days = series.get('wait_days', 1)

                            step_body = {
                                'BriefId': brief_id,
                                'StepNumber': step_idx + 1,
                                'StepType': 'Send',
                                'Channel': 'Email',
                                'Content': step_content,
                            }
                            if step_idx > 0:
                                step_body['WaitNumber'] = wait_days
                                step_body['WaitUnit'] = 'Days'

                            try:
                                sf_api('POST', '/services/data/v66.0/sobjects/BriefPlanStep',
                                       token, instance, body=step_body)
                            except Exception:
                                pass  # Non-critical — brief itself was created
                    else:
                        errors.append(f"Campaign Brief: {brief_resp.status_code} - {brief_resp.text[:200]}")
                except Exception as be:
                    errors.append(f"Campaign Brief: {str(be)[:150]}")
            else:
                errors.append(f"Campaign creation: {camp_resp.status_code} - {camp_resp.text[:200]}")
        except Exception as ce:
            errors.append(f"Campaign: {str(ce)[:150]}")

    return {
        'success': len(created_emails) > 0,
        'emails': created_emails,
        'flow': flow_result,
        'campaign': campaign_result,
        'errors': errors,
        'totalCreated': len(created_emails)
    }


@app.route('/api/sf/deploy-email-series', methods=['POST'])
def deploy_email_series():
    """Deploy email series to Salesforce: upload emails to CMS, publish, create flow."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    if not token or not instance:
        return jsonify({'error': 'Not connected to Salesforce'}), 401

    data = request.json or {}
    if data.get('series', '') not in EMAIL_SERIES:
        return jsonify({'error': f'Invalid series: {data.get("series", "")}'}), 400
    if not data.get('workspaceId'):
        return jsonify({'error': 'No workspace selected'}), 400
    if not data.get('emails'):
        return jsonify({'error': 'No emails provided'}), 400

    result = _deploy_email_series_internal(token, instance, data)
    return jsonify(result)


@app.route('/api/sf/deploy-all', methods=['POST'])
def deploy_all():
    """Unified deploy: brand + images + optional email series, all in one request."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    if not token or not instance:
        return jsonify({'error': 'Not connected to Salesforce'}), 401

    data = request.json or {}
    config = data.get('config', {})
    workspace_id = data.get('workspaceId', '')
    workspace_name = data.get('workspaceName', 'Default_Content_Workspace')
    email_series_list = data.get('emailSeries', [])  # list of series keys: ['nurture', 'welcome']
    email_config = data.get('emailConfig', {})
    email_data = data.get('emailData', {})  # { nurture: [{copy, logoUrl, heroUrl},...], welcome: [...] }

    if not workspace_id:
        return jsonify({'error': 'No workspace selected'}), 400

    results = {
        'brand': None,
        'emailSeries': {},
        'errors': [],
        'success': False
    }

    # Step 1: Deploy brand + images
    brand_result = _deploy_brand_internal(token, instance, config, workspace_id)
    results['brand'] = brand_result
    if brand_result.get('errors'):
        results['errors'].extend(brand_result['errors'])

    # Pre-discover org-specific flow metadata (data graph + DMO) once for all series
    org_data_graph = 'Marketing_Data_Graph'
    org_dmo = 'UnifiedssotIndividualInd1__dlm'
    if email_series_list:
        try:
            headers = {'Authorization': f'Bearer {token}'}
            dg_resp = requests.get(
                f"{instance}/services/data/v67.0/tooling/query/",
                params={'q': "SELECT DeveloperName FROM DataGraphDefinition LIMIT 5"},
                headers=headers, timeout=5
            )
            if dg_resp.status_code == 200:
                for rec in dg_resp.json().get('records', []):
                    name = rec.get('DeveloperName', '')
                    if 'Marketing' in name or 'marketing' in name:
                        org_data_graph = name
                        break
                else:
                    recs = dg_resp.json().get('records', [])
                    if recs:
                        org_data_graph = recs[0]['DeveloperName']

            dmo_resp = requests.get(
                f"{instance}/services/data/v67.0/tooling/query/",
                params={'q': "SELECT QualifiedApiName FROM EntityDefinition WHERE QualifiedApiName LIKE 'Unifiedssot%Individual%dlm' LIMIT 1"},
                headers=headers, timeout=5
            )
            if dmo_resp.status_code == 200:
                dmo_recs = dmo_resp.json().get('records', [])
                if dmo_recs:
                    org_dmo = dmo_recs[0]['QualifiedApiName']
        except Exception:
            pass  # Use defaults

    # Step 2: Deploy each selected email series
    for series_key in email_series_list:
        series_emails = email_data.get(series_key, [])
        if not series_emails:
            continue

        series_data = {
            'series': series_key,
            'emails': series_emails,
            'config': config,
            'workspaceId': workspace_id,
            'workspaceName': workspace_name,
            'segmentId': email_config.get('segmentId', ''),
            'senderId': email_config.get('senderId', ''),
            'subscriptionId': email_config.get('subscriptionId', ''),
            'channelTypeId': email_config.get('channelTypeId', ''),
            'createFlow': email_config.get('createFlow', True),
            'createCampaign': email_config.get('createCampaign', True),
            'headerColor': email_config.get('headerColor', None),
            'brandContentId': brand_result.get('brandId', ''),
            'dataGraph': org_data_graph,
            'dmoObject': org_dmo
        }

        series_result = _deploy_email_series_internal(token, instance, series_data)
        results['emailSeries'][series_key] = series_result
        if series_result.get('errors'):
            results['errors'].extend(series_result['errors'])

    results['success'] = brand_result.get('success', False)
    return jsonify(results)


@app.route('/oauth/logout')
def oauth_logout():
    """Clear Salesforce session."""
    session.pop('sf_access_token', None)
    session.pop('sf_instance_url', None)
    session.pop('sf_refresh_token', None)
    return jsonify({'success': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', sys.argv[1] if len(sys.argv) > 1 else 5111))
    print(f'Starting Brand Builder server on http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
