#!/usr/bin/env python3
"""
MCA Demo Brand Builder — Backend Server
Fetches websites server-side, extracts brand assets (colors, fonts, tone, images).
"""

_ENGINE_REV = 'mc4-lr-bbr-2026'  # build revision tag
_APP_VERSION = '2.5.1'  # 2.5.1 = Proxy all preview images through server (fixes cross-origin load failures)

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

try:
    import cairosvg
    HAS_CAIROSVG = True
except ImportError:
    HAS_CAIROSVG = False

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
    'lending_solar': {
        'label': 'Solar Lending',
        'industry': 'Lending',
        'description': 'Empowering, sustainability-focused language that connects financial savings with environmental impact. Guides homeowners through solar financing with optimism about energy independence and long-term value.',
        'identity_template': '{name} is a clean energy lending platform making solar and sustainable home improvements accessible and affordable. They communicate with an empowering, forward-looking voice that connects financial benefits with positive environmental impact, helping homeowners take control of their energy future.',
        'customer_term': 'homeowners'
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
        'lending_solar': ['solar', 'solar panel', 'solar loan', 'solar financing', 'clean energy',
                          'renewable', 'energy savings', 'sustainable', 'solar installation', 'energy independence',
                          'home improvement loan', 'green energy', 'net metering', 'solar power', 'goodleap'],
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


def _svg_to_png_bytes(svg_url):
    """Fetch an SVG from a URL and convert it to PNG bytes.
    Returns (png_bytes, error_msg). If conversion fails, returns (None, error_msg)."""
    if not HAS_CAIROSVG:
        return None, 'cairosvg not installed'
    try:
        resp = requests.get(svg_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        content_type = resp.headers.get('content-type', '').lower()
        svg_data = resp.content
        # Verify it's actually SVG content
        if b'<svg' not in svg_data[:500].lower():
            return None, 'Not valid SVG content'
        png_bytes = cairosvg.svg2png(bytestring=svg_data, output_width=600, output_height=200)
        return png_bytes, None
    except Exception as e:
        return None, f'SVG conversion failed: {str(e)[:100]}'


def _is_svg_url(url):
    """Check if a URL points to an SVG file."""
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith('.svg') or 'image/svg' in path


def extract_images(soup, base_url):
    """LC/MC brand-analysis pipeline — image extraction stage.

    Strategy: collect logos and hero/product images into separate buckets,
    filter out third-party media logos (Forbes, Bloomberg, etc.), then merge
    with guaranteed slots for hero images.
    """
    logos = []
    heroes = []
    seen = set()
    parsed_base = urlparse(base_url)
    brand_domain = parsed_base.netloc.lower().replace('www.', '')

    # ── Third-party / press logo filter ──
    # Common media, press, award, and partner logo domains & keywords that
    # should NOT consume brand-image slots.
    _THIRD_PARTY_PATTERNS = re.compile(
        r'forbes|bloomberg|cnbc|wsj|wall.?street|fortune|fast.?company|'
        r'business.?insider|techcrunch|reuters|inc\.com|entrepreneur\.com|'
        r'yahoo|cnn|nbc|abc|cbs|fox.?news|the.?verge|wired|mashable|'
        r'new.?york.?times|nytimes|washington.?post|usa.?today|bbc|'
        r'huffpost|huffington|time\.com|guardian|independent|'
        r'trustpilot|bbb|better.?business|google.?play|app.?store|'
        r'apple\.com/app|play\.google|badge|award|certification|seal|'
        r'accredited|rated|verified|partner.?logo|client.?logo|'
        r'as.?seen|featured.?in|press.?logo|media.?logo|news.?logo',
        re.I
    )

    def _is_third_party_logo(url, alt_text):
        """Return True if this image is likely a third-party media/press logo."""
        url_lower = url.lower()
        alt_lower = (alt_text or '').lower()
        combined = url_lower + ' ' + alt_lower
        # Check against known third-party patterns
        if _THIRD_PARTY_PATTERNS.search(combined):
            return True
        # Check if the image is hosted on a completely different domain
        try:
            img_domain = urlparse(url).netloc.lower().replace('www.', '')
            if img_domain and brand_domain and img_domain != brand_domain:
                # Image from another domain — could be CDN (ok) or third-party (bad)
                # CDN subdomains often share the root domain
                brand_root = '.'.join(brand_domain.split('.')[-2:])
                img_root = '.'.join(img_domain.split('.')[-2:])
                if brand_root != img_root:
                    # Different root domain — check if it's a known CDN
                    cdn_patterns = ['cloudfront', 'cloudinary', 'imgix', 'akamai',
                                    'fastly', 'cdn', 'amazonaws', 'azureedge',
                                    'contentful', 'prismic', 'sanity', 'storyblok',
                                    'datocms', 'vercel', 'netlify', 'imgbb']
                    if not any(p in img_domain for p in cdn_patterns):
                        # Not a CDN and not the brand domain — likely third-party
                        return True
        except Exception:
            pass
        return False

    def _make_entry(url, img_type, alt):
        """Build an image entry dict with a readable display label."""
        display_label = alt.strip() if alt else ''
        generic_alts = ('', 'image', 'hero image', 'background image', 'hero', 'logo')
        if not display_label or display_label.lower() in generic_alts:
            try:
                path_part = urlparse(url).path
                fname = path_part.split('/')[-1].rsplit('.', 1)[0] if '/' in path_part else ''
                if fname and len(fname) > 2:
                    cleaned = re.sub(r'[-_]+', ' ', fname).strip().title()
                    cleaned = re.sub(r'\b\d{3,4}x\d{3,4}\b', '', cleaned).strip()
                    if cleaned and len(cleaned) > 2:
                        display_label = cleaned[:50]
            except Exception:
                pass
        if not display_label or display_label.lower() in generic_alts:
            display_label = f'{img_type.title()} {len(logos) + len(heroes) + 1}'
        return {'url': url, 'type': img_type, 'alt': alt or img_type.title(),
                'label': display_label, 'selected': True}

    def _skip_url(url):
        """Return True for tracking pixels, icons, and other junk images."""
        skip_patterns = ['pixel', 'tracking', 'spacer', '1x1', 'blank.gif', 'beacon',
                         'icon-', 'favicon', 'spinner', 'loading.', 'placeholder']
        return any(x in url.lower() for x in skip_patterns)

    def add_logo(src, alt):
        if not src or src.startswith('data:'):
            return
        url = resolve_url(src, base_url)
        if not url or url in seen:
            return
        if _skip_url(url):
            return
        # Filter out third-party press/media logos
        if _is_third_party_logo(url, alt):
            return
        seen.add(url)
        logos.append(_make_entry(url, 'logo', alt))

    def add_hero(src, alt):
        if not src or src.startswith('data:'):
            return
        url = resolve_url(src, base_url)
        if not url or url in seen:
            return
        if _skip_url(url):
            return
        # Skip very small SVGs that are likely icons (but allow SVG logos)
        if url.lower().endswith('.svg'):
            return
        seen.add(url)
        heroes.append(_make_entry(url, 'hero', alt))

    # ── Phase 1: Collect logos ──
    logo_selectors = [
        'header img', 'nav img',
        '[class*="logo"] img', 'img[class*="logo"]',
        'img[alt*="logo"]', 'img[src*="logo"]',
        'a[class*="logo"] img', '[id*="logo"] img',
        'img[id*="logo"]', '.navbar-brand img',
        '[class*="brand"] img',
    ]
    for selector in logo_selectors:
        for el in soup.select(selector):
            src = get_best_src(el)
            alt = el.get('alt', 'Logo')
            add_logo(src, alt)

    # <picture> inside logo areas
    for selector in ['header picture source', '[class*="logo"] picture source']:
        for el in soup.select(selector):
            src = el.get('srcset', '')
            if src:
                src = parse_srcset_best(src) or src.split(',')[0].strip().split()[0]
            add_logo(src, 'Logo')

    # SVG logos — header/nav
    for img in soup.select('header img[src$=".svg"], nav img[src$=".svg"]'):
        src = img.get('src', '')
        if not src or src.startswith('data:'):
            continue
        url = resolve_url(src, base_url)
        if url and url not in seen and not _is_third_party_logo(url, img.get('alt', '')):
            seen.add(url)
            logos.append(_make_entry(url, 'logo', img.get('alt', 'Logo')))

    # ── Phase 2: Collect hero / product images ──

    # <picture> elements
    for picture in soup.find_all('picture'):
        sources = picture.find_all('source')
        img_el = picture.find('img')
        best_src = ''
        alt = ''
        for source in sources:
            srcset = source.get('srcset', '')
            if srcset:
                candidate = parse_srcset_best(srcset)
                if candidate:
                    best_src = candidate
                    break
        if not best_src and img_el:
            best_src = get_best_src(img_el)
            alt = img_el.get('alt', '')
        if best_src:
            add_hero(best_src, alt or 'Image')

    # All <img> tags — detect large / hero images
    for img in soup.find_all('img'):
        src = get_best_src(img)
        alt = img.get('alt', '')
        if not src:
            srcset = img.get('srcset', '')
            if srcset:
                src = parse_srcset_best(srcset)
        if not src:
            for lazy_attr in ('data-src', 'data-lazy-src', 'data-original', 'data-full-src'):
                lazy_val = img.get(lazy_attr, '')
                if lazy_val and not lazy_val.startswith('data:'):
                    src = lazy_val
                    break
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

        img_classes = ' '.join(img.get('class', [])).lower() if img.get('class') else ''
        is_large = is_large or bool(re.search(r'full|large|wide|cover|hero|banner|featured', img_classes))

        # Walk up to 4 ancestor levels for context
        parent_class = ' '.join(img.parent.get('class', [])) if img.parent else ''
        grandparent_class = ' '.join(img.parent.parent.get('class', [])) if img.parent and img.parent.parent else ''
        great_gp_class = ''
        try:
            great_gp_class = ' '.join(img.parent.parent.parent.get('class', [])) if img.parent and img.parent.parent and img.parent.parent.parent else ''
        except (AttributeError, TypeError):
            pass
        great_great_gp_class = ''
        try:
            great_great_gp_class = ' '.join(img.parent.parent.parent.parent.get('class', [])) if img.parent and img.parent.parent and img.parent.parent.parent and img.parent.parent.parent.parent else ''
        except (AttributeError, TypeError):
            pass
        context = (parent_class + ' ' + grandparent_class + ' ' + great_gp_class + ' ' + great_great_gp_class).lower()
        in_hero = bool(re.search(r'hero|banner|jumbotron|splash|featured|carousel|slider|masthead|promo|spotlight|showcase|intro|landing', context))

        parent_tags = []
        p = img.parent
        for _ in range(4):
            if p and p.name:
                parent_tags.append(p.name)
                p = p.parent
            else:
                break
        in_main_section = any(t in ('section', 'main', 'article') for t in parent_tags)

        src_hint = bool(re.search(r'hero|banner|featured|cover|main|splash|carousel|promo|spotlight|header|home|landing', src, re.I))
        # Next.js /_next/image — detect large-quality images
        nextjs_large = bool(re.search(r'_next/image.*[?&]w=(1[2-9]\d{2}|[2-9]\d{3})', src, re.I))
        # Also catch any _next/image with decent quality
        nextjs_any = bool(re.search(r'_next/image', src, re.I))

        if is_large or in_hero or src_hint or nextjs_large or (nextjs_any and (w > 200 or h > 100)) or (in_main_section and (w > 300 or h > 150)):
            add_hero(src, alt or 'Hero Image')

    # Inline style background images
    for el in soup.find_all(style=re.compile(r'background')):
        style = el.get('style', '')
        for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', style):
            add_hero(m.group(1), 'Background Image')

    # CSS background-image in <style> blocks
    for style_tag in soup.find_all('style'):
        css_text = style_tag.get_text()
        for m in re.finditer(r'background(?:-image)?\s*:\s*[^;]*url\(["\']?([^"\')\s]+)["\']?\)', css_text):
            src = m.group(1)
            if re.search(r'\.(jpg|jpeg|png|webp|gif)', src, re.I):
                add_hero(src, 'Background Image')

    # data-background attributes (common in slider/parallax plugins)
    for el in soup.find_all(attrs={'data-background': True}):
        add_hero(el['data-background'], 'Background Image')
    for el in soup.find_all(attrs={'data-bg': True}):
        add_hero(el['data-bg'], 'Background Image')
    for el in soup.find_all(attrs={'data-bg-src': True}):
        add_hero(el['data-bg-src'], 'Background Image')

    # ── Phase 3: Merge with guaranteed slots ──
    # Reserve up to 4 slots for logos, up to 10 for heroes, total max 14
    MAX_LOGOS = 4
    MAX_HEROES = 10
    MAX_TOTAL = 14
    final_logos = logos[:MAX_LOGOS]
    remaining_slots = MAX_TOTAL - len(final_logos)
    final_heroes = heroes[:min(MAX_HEROES, remaining_slots)]

    return final_logos + final_heroes


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


@app.route('/api/version')
def api_version():
    """Return current app version for deploy verification."""
    return jsonify({'version': _APP_VERSION, 'engine': _ENGINE_REV})


@app.route('/api/img-proxy')
def img_proxy():
    """Proxy an external image to avoid CORS issues in browser previews.

    Usage: /api/img-proxy?url=https://example.com/logo.svg
    Streams the image through the server so the browser can render it
    even when the origin doesn't set CORS headers (common with SVGs).
    """
    from flask import Response
    target_url = request.args.get('url', '')
    if not target_url:
        return jsonify({'error': 'Missing url parameter'}), 400
    # Security: only allow image-like URLs
    try:
        parsed = urlparse(target_url)
        if parsed.scheme not in ('http', 'https'):
            return jsonify({'error': 'Invalid URL scheme'}), 400
    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400
    try:
        # Build headers with proper Referer for the target domain (required by
        # Next.js _next/image and other CDNs that check origin)
        fetch_headers = dict(HEADERS)
        fetch_headers['Referer'] = f'{parsed.scheme}://{parsed.netloc}/'
        fetch_headers['Accept'] = 'image/webp,image/avif,image/apng,image/svg+xml,image/*,*/*;q=0.8'
        resp = requests.get(target_url, headers=fetch_headers, timeout=10)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', 'image/png')
        # Only proxy image content types (including SVG)
        if not (content_type.startswith('image/') or 'svg' in content_type):
            return jsonify({'error': 'Not an image'}), 400
        return Response(
            resp.content,
            content_type=content_type,
            headers={
                'Cache-Control': 'public, max-age=3600',
                'Access-Control-Allow-Origin': '*'
            }
        )
    except Exception as e:
        return jsonify({'error': f'Failed to fetch image: {str(e)}'}), 502


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


def sf_api(method, path, access_token, instance_url, body=None, _retried=False, timeout=15):
    """Make an authenticated Salesforce REST API call with auto-refresh on 401."""
    url = instance_url.rstrip('/') + path
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    if method == 'GET':
        resp = requests.get(url, headers=headers, timeout=timeout)
    elif method == 'POST':
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    else:
        resp = requests.request(method, url, headers=headers, json=body, timeout=timeout)

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
    Returns dict with brandId, contentIds, totalCreated, errors, success,
    and imageMap: {original_url: {contentKey, managedContentId, cmsUrl, type}}."""
    content_ids = []
    errors = []
    brand_id = ''
    brand_content_key = ''
    image_map = {}  # original_url -> {contentKey, managedContentId, cmsUrl, type}

    # 1. Upload images as sfdc_cms__image items
    images = config.get('images', [])
    for img in images:
        original_url = img.get('url', '')
        img_title = (config.get('brandName', 'Brand') + '_' + (img.get('alt', 'image'))[:40]).replace(' ', '_')
        try:
            # SVG handling: convert to PNG for email compatibility
            is_svg = _is_svg_url(original_url)
            if is_svg:
                png_bytes, svg_err = _svg_to_png_bytes(original_url)
                if png_bytes:
                    # Upload PNG as file (multipart) instead of URL reference
                    png_b64 = base64.b64encode(png_bytes).decode('utf-8')
                    input_param = json.dumps({
                        'contentSpaceOrFolderId': workspace_id,
                        'contentType': 'sfdc_cms__image',
                        'title': img_title,
                        'contentBody': {
                            'sfdc_cms:media': {
                                'source': {
                                    'type': 'file',
                                    'mimeType': 'image/png',
                                    'fileName': img_title + '.png'
                                }
                            }
                        }
                    })
                    files = {
                        'ManagedContentInputParam': (None, input_param, 'application/json'),
                        'sfdc_cms:media': (img_title + '.png', png_bytes, 'image/png')
                    }
                    img_resp = requests.post(
                        f"{instance}/services/data/v62.0/connect/cms/contents",
                        headers={'Authorization': f'Bearer {token}'},
                        files=files,
                        timeout=15
                    )
                else:
                    # SVG conversion failed — fall back to URL reference (won't work in email but brand still gets it)
                    errors.append(f'SVG→PNG conversion skipped for {img.get("alt", "image")}: {svg_err}')
                    img_resp = sf_api('POST', '/services/data/v62.0/connect/cms/contents', token, instance, {
                        'contentSpaceOrFolderId': workspace_id,
                        'contentType': 'sfdc_cms__image',
                        'title': img_title,
                        'contentBody': {
                            'sfdc_cms:media': {
                                'source': {'type': 'url', 'url': original_url}
                            }
                        }
                    })
            else:
                # Non-SVG: upload as URL reference (existing behavior)
                img_resp = sf_api('POST', '/services/data/v62.0/connect/cms/contents', token, instance, {
                    'contentSpaceOrFolderId': workspace_id,
                    'contentType': 'sfdc_cms__image',
                    'title': img_title,
                    'contentBody': {
                        'sfdc_cms:media': {
                            'source': {'type': 'url', 'url': original_url}
                        }
                    }
                })

            if img_resp.ok if hasattr(img_resp, 'ok') else (img_resp.status_code in (200, 201)):
                resp_data = img_resp.json()
                managed_id = resp_data.get('managedContentId', resp_data.get('id', ''))
                content_key = resp_data.get('contentKey', resp_data.get('contentUrlName', ''))
                content_ids.append(managed_id)
                # Build CMS media URL for this image
                cms_url = ''
                if content_key:
                    cms_url = f'{instance}/cms/media/{content_key}'
                image_map[original_url] = {
                    'contentKey': content_key,
                    'managedContentId': managed_id,
                    'cmsUrl': cms_url,
                    'type': img.get('type', 'image'),
                    'wasSvg': is_svg
                }
            else:
                err_text = img_resp.text[:200] if hasattr(img_resp, 'text') else str(img_resp)[:200]
                errors.append(f'Image upload failed ({img.get("alt", "unknown")}): {err_text}')
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
            brand_data = brand_resp.json()
            brand_id = brand_data['managedContentId']
            brand_content_key = brand_data.get('contentKey', brand_data.get('apiName', ''))
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
        'brandContentKey': brand_content_key,
        'contentIds': content_ids,
        'totalCreated': len(content_ids),
        'errors': errors,
        'imageMap': image_map,
        'version': _APP_VERSION
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

def _nurture_copy_solar(bn, ct):
    """Solar lending nurture copy."""
    return [
        {
            'subject': f'Power Your Home with the Sun — {bn}',
            'preheader': f'See how {ct} are saving with solar.',
            'heading': f'Your Brighter Energy Future Starts Here',
            'body': f'Going solar isn\'t just good for the planet — it\'s great for your wallet. {bn} makes solar financing simple, affordable, and accessible, so {ct} can start saving from day one while making a real environmental impact.',
            'cta_text': 'See Your Savings',
        },
        {
            'subject': f'How {ct.title()} Are Cutting Energy Costs with {bn}',
            'preheader': f'Real results from {ct} who made the switch.',
            'heading': f'{ct.title()} Love the Switch to Solar',
            'body': f'{ct.title()} across the country are reducing their energy bills by 40-70%% with solar. {bn} offers flexible financing options with competitive rates, so you can start benefiting immediately — no huge upfront cost required.',
            'cta_text': 'See Their Stories',
        },
        {
            'subject': f'Ready to Go Solar? {bn} Makes It Easy',
            'preheader': 'Get approved in minutes with flexible terms.',
            'heading': 'Take Control of Your Energy',
            'body': f'Energy costs keep rising, but you don\'t have to keep paying more. With {bn}, getting approved for solar financing is fast and straightforward. Take the first step toward energy independence today.',
            'cta_text': 'Check Your Options',
        },
    ]

def _welcome_copy_solar(bn, ct):
    """Solar lending welcome copy."""
    return [
        {
            'subject': f'Welcome to {bn} — Your Solar Journey Begins!',
            'preheader': f'You\'re on the path to energy independence.',
            'heading': f'Welcome to a Brighter Future!',
            'body': f'Thank you for choosing {bn} to power your solar journey. You\'ve just taken a meaningful step toward lower energy costs, a smaller carbon footprint, and true energy independence. We\'re excited to be part of it.',
            'cta_text': 'View Your Plan',
        },
        {
            'subject': f'Track Your Solar Project with {bn}',
            'preheader': 'Your dashboard is ready — monitor every step.',
            'heading': f'Your Solar Dashboard Is Live',
            'body': f'Log into your {bn} account to track your solar project timeline, view your financing details, and monitor your expected energy savings. Everything you need is in one place.',
            'cta_text': 'Log In Now',
        },
        {
            'subject': f'Maximize Your Solar Investment with {bn}',
            'preheader': 'Tips to get the most from your system.',
            'heading': 'Get the Most from Your Solar',
            'body': f'{bn} is here beyond the installation. Explore resources on tax incentives, energy monitoring, battery storage options, and referral programs that can help {ct} maximize their solar investment.',
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

# Sub-industry overrides: specific tone_key -> copy generators (takes priority over industry group)
SUBINDUSTRY_COPY_MAP = {
    'lending_solar':            (_nurture_copy_solar,     _welcome_copy_solar),
}


def generate_series_copy(series_key, config):
    """Generate all email copy for a series, based on industry tone."""
    brand_name = config.get('brandName', 'Brand')
    tone_key = config.get('tone', {}).get('key', 'general')
    tone = INDUSTRY_TONES.get(tone_key, INDUSTRY_TONES['general'])
    customer_term = tone['customer_term']
    industry_group = tone['industry']

    # Check sub-industry override first, then fall back to industry group
    nurture_fn, welcome_fn = SUBINDUSTRY_COPY_MAP.get(
        tone_key,
        INDUSTRY_COPY_MAP.get(industry_group, (_nurture_copy_general, _welcome_copy_general))
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
    """Generate a segment-triggered Journey flow XML with sendEmailMessage actions and WaitDuration pauses."""
    series = EMAIL_SERIES.get(series_key)
    if not series:
        return None

    brand_name = config.get('brandName', 'Brand')
    series_name = series['name']
    wait_days = series['wait_days']
    flow_label = f"{brand_name} {series_name}"
    flow_label_xml = flow_label.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    flow_api_name = re.sub(r'[^A-Za-z0-9_]', '_', flow_label).replace('__', '_')

    num_emails = min(len(email_content_keys), len(series['emails']))

    # Helper: generate a clean API name for each email action
    def _action_name(idx):
        """Generate consistent action name like 'email_1_nurture' from index."""
        return f"email_{idx + 1}_{series_key}"

    # Build action calls with WaitDuration pauses between them
    action_calls_xml = ''
    waits_xml = ''

    for i in range(num_emails):
        email_key = email_content_keys[i]
        email_def = series['emails'][i]
        action_name = _action_name(i)
        # Use email name for label, escape XML special chars
        raw_label = email_def.get('name', f'Email {i+1}')
        email_label = raw_label.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&apos;')

        # Determine connector: email -> pause (if not last), or no connector (last email)
        if i < num_emails - 1:
            pause_name = f"Pause_{i + 1}"
            connector_xml = f'''
        <connector>
            <targetReference>{pause_name}</targetReference>
        </connector>'''
        else:
            connector_xml = ''  # Last email has no connector (flow ends)

        content_id = f"marketing--{workspace_name}.sfdc_cms__email--{email_key}"

        action_calls_xml += f'''
    <actionCalls>
        <name>{action_name}</name>
        <label>{email_label}</label>
        <locationX>0</locationX>
        <locationY>0</locationY>
        <actionName>sendEmailMessage</actionName>
        <actionType>sendEmailMessage</actionType>{connector_xml}
        <flowTransactionModel>CurrentTransaction</flowTransactionModel>
        <inputParameters>
            <name>clickTracking</name>
            <value>
                <booleanValue>true</booleanValue>
            </value>
        </inputParameters>
        <inputParameters>
            <name>openTracking</name>
            <value>
                <booleanValue>true</booleanValue>
            </value>
        </inputParameters>
        <inputParameters>
            <name>contentId</name>
            <value>
                <stringValue>{content_id}</stringValue>
            </value>
        </inputParameters>
        <nameSegment>sendEmailMessage</nameSegment>
    </actionCalls>'''

        # Add WaitDuration pause after each email except the last
        if i < num_emails - 1:
            pause_name = f"Pause_{i + 1}"
            next_action_name = _action_name(i + 1)
            days = wait_days if isinstance(wait_days, int) else wait_days[i] if isinstance(wait_days, list) and i < len(wait_days) else 3

            waits_xml += f'''
    <waits>
        <name>{pause_name}</name>
        <elementSubtype>WaitDuration</elementSubtype>
        <label>Wait_{days}_Days</label>
        <locationX>0</locationX>
        <locationY>0</locationY>
        <defaultConnectorLabel>Default Path</defaultConnectorLabel>
        <waitEvents>
            <conditionLogic>and</conditionLogic>
            <connector>
                <targetReference>{next_action_name}</targetReference>
            </connector>
            <label>el_{i}</label>
            <offset>{days}</offset>
            <offsetUnit>Days</offsetUnit>
        </waitEvents>
    </waits>'''

    # First email action name for start connector
    first_email_name = _action_name(0)

    flow_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">{action_calls_xml}
    <apiVersion>67.0</apiVersion>
    <areMetricsLoggedToDataCloud>false</areMetricsLoggedToDataCloud>
    <dataSpace>default</dataSpace>
    <environments>Default</environments>
    <interviewLabel>{flow_label_xml} {{!$Flow.CurrentDateTime}}</interviewLabel>
    <label>{flow_label_xml}</label>
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
    <processType>Journey</processType>
    <start>
        <locationX>0</locationX>
        <locationY>0</locationY>
        <connector>
            <targetReference>{first_email_name}</targetReference>
        </connector>
        <triggerType>Segment</triggerType>
    </start>
    <status>InvalidDraft</status>{waits_xml}
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


def build_cms_email_content_json(email_html, subject, preheader, title='', brand_content_key=''):
    """Build the CMS email contentBody structure for sfdc_cms__email.

    Matches the real MCA CMS email schema with top-level subjectLine, preheader,
    sfdc_cms:title, messagePurpose, and the block tree using definition/children.
    If brand_content_key is provided, associates the email with the specified CMS brand.
    """
    block_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    column_id = str(uuid.uuid4())
    html_id = str(uuid.uuid4())

    # Build brandSource — link to specific brand if content key provided
    brand_source = {"defaultBrandOption": "sfdcBrand"}
    if brand_content_key:
        brand_source["sfdcBrandContentKey"] = brand_content_key

    return {
        "subjectLine": subject,
        "preheader": preheader or "",
        "sfdc_cms:title": title or subject,
        "messagePurpose": "promotional",
        "lightning:brandSource": brand_source,
        "backgroundColor": "{!$brand.colorScheme.root}" if brand_content_key else "#ffffff",
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
                        "stackOnMobile": True,
                        "lightning:colorScheme": "{!$brand.colorScheme}" if brand_content_key else {},
                        "lightning:backgroundImage": {
                            "repeat": "no-repeat",
                            "position": "center center",
                            "size": "cover"
                        }
                    },
                    "children": [
                        {
                            "definition": "lightning/column",
                            "id": column_id,
                            "type": "block",
                            "attributes": {
                                "columnWidth": 12.0,
                                "lightning:colorScheme": "{!$brand.colorScheme}" if brand_content_key else {},
                                "lightning:backgroundImage": {
                                    "repeat": "no-repeat",
                                    "position": "center center",
                                    "size": "cover"
                                }
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


def _soap_deploy_flow(flow_xml, flow_api_name, token, instance, poll_timeout=45):
    """Deploy a flow via SOAP Metadata API and poll checkDeployStatus until done.

    Returns dict with:
      success (bool), deployId (str), error (str if failed),
      componentErrors (list of error strings for debugging)
    """
    # 1. Build ZIP
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        package_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types><members>{flow_api_name}</members><name>Flow</name></types>
    <version>67.0</version>
</Package>'''
        zf.writestr('package.xml', package_xml)
        zf.writestr(f'flows/{flow_api_name}.flow-meta.xml', flow_xml)
    zip_b64 = base64.b64encode(zip_buffer.getvalue()).decode('ascii')

    soap_url = instance.rstrip('/') + '/services/Soap/m/67.0'

    # 2. Submit deploy
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

    try:
        deploy_resp = requests.post(
            soap_url,
            headers={'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': 'deploy'},
            data=deploy_soap.encode('utf-8'),
            timeout=15  # 15s to submit — generous enough for SOAP
        )
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'SOAP deploy submit timed out (15s)', 'componentErrors': []}
    except Exception as e:
        return {'success': False, 'error': f'SOAP deploy submit error: {str(e)[:200]}', 'componentErrors': []}

    # 3. Extract deploy ID from SOAP response
    deploy_id = None
    try:
        root = ET.fromstring(deploy_resp.text)
        for el in root.iter():
            if el.tag.endswith('}id') or el.tag == 'id':
                deploy_id = el.text
                break
    except ET.ParseError:
        pass

    if not deploy_id:
        # Try to extract any error message from the SOAP fault
        error_msg = 'No deploy ID returned'
        try:
            root = ET.fromstring(deploy_resp.text)
            for el in root.iter():
                if 'faultstring' in el.tag.lower() or el.tag.endswith('}faultstring'):
                    error_msg = el.text or error_msg
                    break
        except Exception:
            error_msg += f' (HTTP {deploy_resp.status_code}: {deploy_resp.text[:200]})'
        return {'success': False, 'error': error_msg, 'componentErrors': []}

    # 4. Poll checkDeployStatus
    check_soap_template = '''<?xml version="1.0" encoding="utf-8"?>
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

    check_soap = check_soap_template.format(token=token, deploy_id=deploy_id)
    start_time = time.time()
    poll_interval = 3  # seconds between polls

    while (time.time() - start_time) < poll_timeout:
        time.sleep(poll_interval)
        try:
            cr = requests.post(
                soap_url,
                headers={'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': 'checkDeployStatus'},
                data=check_soap.encode('utf-8'),
                timeout=10
            )
            croot = ET.fromstring(cr.text)

            done = False
            success = False
            for el in croot.iter():
                if el.tag.endswith('}done'):
                    done = el.text == 'true'
                if el.tag.endswith('}success'):
                    success = el.text == 'true'

            if done:
                if success:
                    return {'success': True, 'deployId': deploy_id, 'componentErrors': []}
                else:
                    # Extract error details
                    component_errors = []
                    for el in croot.iter():
                        if el.tag.endswith('}problem') or el.tag.endswith('}message'):
                            if el.text:
                                component_errors.append(el.text)
                    error_summary = component_errors[0] if component_errors else 'Deploy failed (unknown reason)'
                    return {
                        'success': False,
                        'deployId': deploy_id,
                        'error': error_summary,
                        'componentErrors': component_errors
                    }
        except Exception:
            continue  # Network blip — retry

    return {
        'success': False,
        'deployId': deploy_id,
        'error': f'Deploy status check timed out after {poll_timeout}s (deploy may still be processing)',
        'componentErrors': []
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
    brand_content_key = data.get('brandContentKey', '')
    # imageMap from brand deploy: {original_url: {contentKey, managedContentId, cmsUrl, type, wasSvg}}
    image_map = data.get('imageMap', {})

    if series_key not in EMAIL_SERIES:
        return {'success': False, 'errors': [f'Invalid series: {series_key}'], 'emails': [], 'flow': None, 'campaign': None, 'totalCreated': 0}
    if not workspace_id:
        return {'success': False, 'errors': ['No workspace selected'], 'emails': [], 'flow': None, 'campaign': None, 'totalCreated': 0}

    # Resolve workspace ApiName (required for flow contentId references)
    # The content ID format is: marketing--{ApiName}.sfdc_cms__email--{contentKey}
    # The frontend sends the display name, but we need the ApiName field
    try:
        ws_resp = sf_api('GET',
            '/services/data/v67.0/query/?q=' + quote(
                f"SELECT ApiName FROM ManagedContentSpace WHERE Id = '{workspace_id}' LIMIT 1"),
            token, instance, timeout=5)
        if ws_resp.ok:
            ws_recs = ws_resp.json().get('records', [])
            if ws_recs and ws_recs[0].get('ApiName') and ws_recs[0]['ApiName'] != 'None':
                workspace_name = ws_recs[0]['ApiName']
    except Exception:
        pass  # Fall back to the name from the frontend
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

        # Resolve image URLs to CMS media URLs when available (Fix: use CMS URLs in email HTML)
        if logo_url and logo_url in image_map:
            cms_logo = image_map[logo_url].get('cmsUrl', '')
            if cms_logo:
                logo_url = cms_logo
            elif image_map[logo_url].get('wasSvg'):
                # SVG logo with no CMS URL — don't embed SVG in email (incompatible)
                logo_url = ''
        elif logo_url and _is_svg_url(logo_url):
            # SVG logo not in image_map — skip it in email (SVGs don't render in email clients)
            logo_url = ''
        if hero_url and hero_url in image_map:
            cms_hero = image_map[hero_url].get('cmsUrl', '')
            if cms_hero:
                hero_url = cms_hero

        email_html = render_email_html(copy_data, config, logo_url, hero_url, header_color=header_color)

        email_name = f"{brand_name}_{series['name'].replace(' ', '_')}_Email_{i + 1}"
        email_title = f"{brand_name} - {series['name']} - Email {i + 1}"

        content_json = build_cms_email_content_json(
            email_html, copy_data.get('subject', ''), copy_data.get('preheader', ''),
            title=email_title, brand_content_key=brand_content_key
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
                f"{instance}/services/data/v67.0/connect/cms/contents",
                headers={'Authorization': f'Bearer {token}'},
                files=files,
                timeout=15
            )

            if resp.status_code == 401 and try_refresh_token():
                token = session.get('sf_access_token')
                resp = requests.post(
                    f"{instance}/services/data/v67.0/connect/cms/contents",
                    headers={'Authorization': f'Bearer {token}'},
                    files=files,
                    timeout=15
                )

            if resp.status_code in (200, 201):
                result = resp.json()
                content_id = result.get('managedContentId', result.get('contentId', result.get('id', '')))
                content_key = result.get('contentKey', result.get('contentUrlName', email_name))

                created_emails.append({
                    'name': email_title,
                    'contentId': content_id,
                    'contentKey': content_key,
                    'order': i + 1
                })
            else:
                errors.append(f"Email {i + 1} upload failed: {resp.status_code} - {resp.text[:200]}")
        except Exception as e:
            errors.append(f"Email {i + 1}: {str(e)[:150]}")

    # Note: Emails are left as drafts (not published).
    # The brand association and email content are set at creation time.
    # Images are published separately in _deploy_brand_internal.

    # Step 3: Create the flow if requested and we have emails
    # Uses _soap_deploy_flow which submits + polls checkDeployStatus until done
    flow_result = None
    if create_flow and len(created_emails) > 0:
        email_content_keys = [e['contentKey'] for e in created_emails]

        discovered_data_graph = data.get('dataGraph', 'Marketing_Data_Graph')
        discovered_dmo = data.get('dmoObject', 'UnifiedssotIndividualInd1__dlm')

        flow_data = generate_flow_xml(
            series_key, email_content_keys, config,
            workspace_name, segment_id, sender_id, subscription_id, channel_type_id,
            data_graph=discovered_data_graph, dmo_object=discovered_dmo
        )

        if flow_data:
            flow_xml = flow_data['xml']
            flow_api_name = flow_data['flowApiName']
            wait_days = EMAIL_SERIES[series_key]['wait_days']

            deploy_result = _soap_deploy_flow(flow_xml, flow_api_name, token, instance, poll_timeout=45)

            if deploy_result['success']:
                flow_result = {
                    'name': flow_data['flowLabel'],
                    'apiName': flow_api_name,
                    'status': 'Deployed',
                    'deployId': deploy_result.get('deployId', ''),
                    'waitNote': f'Flow deployed with {wait_days}-day wait steps between emails. Open in Flow Builder to review and activate.'
                }
            else:
                error_detail = deploy_result.get('error', 'Unknown error')
                component_errors = deploy_result.get('componentErrors', [])
                errors.append(f"Flow deploy failed: {error_detail}")
                if component_errors:
                    for ce in component_errors[:3]:  # include up to 3 component errors
                        errors.append(f"  → {ce}")
                flow_result = {
                    'name': flow_data['flowLabel'],
                    'apiName': flow_api_name,
                    'status': 'Failed',
                    'error': error_detail,
                    'componentErrors': component_errors[:5]
                }

    # Step 4: Create Campaign + Campaign Brief if requested
    campaign_result = None
    if create_campaign and len(created_emails) > 0:
        series = EMAIL_SERIES[series_key]
        campaign_name = f"{brand_name} {series['name']}"

        try:
            # Look up BusinessUnit in the 'default' Data Space (must match flow's <dataSpace>default</dataSpace>)
            bu_id = ''
            try:
                # First find the DataSpace record for 'default'
                ds_resp = sf_api('GET',
                    '/services/data/v67.0/query/?q=' + quote(
                        "SELECT Id FROM DataSpace WHERE DataSpaceApiName = 'default' LIMIT 1"),
                    token, instance, timeout=5)
                ds_id = ''
                if ds_resp.ok:
                    ds_recs = ds_resp.json().get('records', [])
                    if ds_recs:
                        ds_id = ds_recs[0]['Id']

                if ds_id:
                    bu_resp = sf_api('GET',
                        '/services/data/v67.0/query/?q=' + quote(
                            f"SELECT Id FROM BusinessUnit WHERE DataSpaceId = '{ds_id}' LIMIT 1"),
                        token, instance, timeout=5)
                    if bu_resp.ok:
                        bu_recs = bu_resp.json().get('records', [])
                        if bu_recs:
                            bu_id = bu_recs[0]['Id']
                else:
                    bu_resp = sf_api('GET',
                        '/services/data/v67.0/query/?q=' + quote(
                            "SELECT Id FROM BusinessUnit WHERE DeveloperName = 'defaultBusinessUnit' LIMIT 1"),
                        token, instance, timeout=5)
                    if bu_resp.ok:
                        bu_recs = bu_resp.json().get('records', [])
                        if bu_recs:
                            bu_id = bu_recs[0]['Id']
            except Exception as bu_ex:
                # Log BU lookup failure but continue — campaign can be created without BU
                errors.append(f"BusinessUnit lookup failed (non-blocking): {str(bu_ex)[:100]}")

            # Build all Campaign + Brief + BriefPlanSteps in ONE Composite API call
            identity = config.get('identity', '')
            industry = config.get('industry', '')
            brand_content_id = data.get('brandContentId', '')

            subject_lines = [em.get('copy', {}).get('subject', '') for em in emails if em.get('copy', {}).get('subject')]
            cta_texts = [em.get('copy', {}).get('cta_text', '') for em in emails if em.get('copy', {}).get('cta_text')]

            primary_goal = ('Drive engagement, increase conversions, and build brand awareness'
                            if series_key == 'nurture' else
                            'Onboard new customers, drive product adoption, and build relationship')

            brief_name = f"{brand_name} {series['name']} Brief"
            camp_body = {
                'Name': campaign_name, 'Type': 'Email', 'Status': 'Planned',
                'IsActive': True, 'Description': f"{brand_name} - {series['description']}"
            }
            if bu_id:
                camp_body['BusinessUnitId'] = bu_id

            brief_fields = {
                'Name': brief_name,
                'Description': identity or f"{brand_name} {series['name']} email campaign",
                'KeyMessage': '\n'.join(subject_lines) if subject_lines else brand_name,
                'TargetAudience': industry or 'General audience',
                'PrimaryGoal': primary_goal,
                'PrimaryCtas': '\n'.join(cta_texts) if cta_texts else '',
                'PrimaryKpi': 'Open Rate, Click-Through Rate, Conversion Rate',
            }
            if brand_content_id:
                brief_fields['BrandId'] = brand_content_id
            if bu_id:
                brief_fields['BusinessUnitId'] = bu_id

            # Composite: Campaign -> Brief -> link Campaign.BriefId -> BriefPlanSteps
            subrequests = [
                {'method': 'POST', 'url': '/services/data/v67.0/sobjects/Campaign',
                 'referenceId': 'newCampaign', 'body': camp_body},
                {'method': 'POST', 'url': '/services/data/v67.0/sobjects/Brief',
                 'referenceId': 'newBrief', 'body': brief_fields},
                {'method': 'PATCH', 'url': '/services/data/v67.0/sobjects/Campaign/@{newCampaign.id}',
                 'referenceId': 'linkBrief', 'body': {'BriefId': '@{newBrief.id}'}}
            ]
            for step_idx, em in enumerate(emails):
                step_body = {
                    'BriefId': '@{newBrief.id}',
                    'StepNumber': step_idx + 1, 'StepType': 'Send',
                    'Channel': 'Email',
                    'Content': em.get('copy', {}).get('subject', f'Email {step_idx + 1}'),
                }
                if step_idx > 0:
                    step_body['WaitNumber'] = series.get('wait_days', 1)
                    step_body['WaitUnit'] = 'Days'
                subrequests.append({
                    'method': 'POST', 'url': '/services/data/v67.0/sobjects/BriefPlanStep',
                    'referenceId': f'step{step_idx}', 'body': step_body
                })

            # Attempt composite call with retry
            comp_resp = None
            for comp_attempt in range(2):
                if comp_attempt > 0:
                    time.sleep(2)
                comp_resp = sf_api('POST', '/services/data/v67.0/composite',
                                   token, instance, body={'compositeRequest': subrequests})
                if comp_resp.status_code in (200, 201):
                    break
                # On 401, sf_api auto-refreshes, so retry immediately
                if comp_resp.status_code == 401:
                    continue

            if comp_resp and comp_resp.status_code in (200, 201):
                comp_results = comp_resp.json().get('compositeResponse', [])
                camp_sub = next((r for r in comp_results if r.get('referenceId') == 'newCampaign'), None)
                brief_sub = next((r for r in comp_results if r.get('referenceId') == 'newBrief'), None)

                if camp_sub and camp_sub.get('httpStatusCode') in (200, 201):
                    campaign_result = {
                        'id': camp_sub['body'].get('id', ''),
                        'name': campaign_name,
                        'status': 'Created'
                    }
                    if brief_sub and brief_sub.get('httpStatusCode') in (200, 201):
                        campaign_result['briefId'] = brief_sub['body'].get('id', '')
                        campaign_result['briefName'] = brief_name
                    else:
                        brief_err = brief_sub.get('body', 'Unknown error') if brief_sub else 'No response'
                        errors.append(f"Brief creation failed (campaign still created): {str(brief_err)[:200]}")
                else:
                    # Composite campaign sub-request failed — try direct Campaign POST as fallback
                    camp_err = camp_sub.get('body', 'Unknown') if camp_sub else 'No response'
                    errors.append(f"Campaign composite failed: {str(camp_err)[:200]} — trying direct create")
                    try:
                        direct_resp = sf_api('POST', '/services/data/v67.0/sobjects/Campaign',
                                             token, instance, body=camp_body)
                        if direct_resp.ok:
                            direct_data = direct_resp.json()
                            campaign_result = {
                                'id': direct_data.get('id', ''),
                                'name': campaign_name,
                                'status': 'Created (without brief)'
                            }
                        else:
                            errors.append(f"Campaign direct create also failed: {direct_resp.status_code} - {direct_resp.text[:200]}")
                    except Exception as de:
                        errors.append(f"Campaign direct fallback error: {str(de)[:150]}")
            else:
                # Composite API call failed entirely — try direct Campaign POST as fallback
                comp_err = f"{comp_resp.status_code} - {comp_resp.text[:200]}" if comp_resp else 'No response'
                errors.append(f"Campaign+Brief composite failed: {comp_err} — trying direct create")
                try:
                    direct_resp = sf_api('POST', '/services/data/v67.0/sobjects/Campaign',
                                         token, instance, body=camp_body)
                    if direct_resp.ok:
                        direct_data = direct_resp.json()
                        campaign_result = {
                            'id': direct_data.get('id', ''),
                            'name': campaign_name,
                            'status': 'Created (without brief)'
                        }
                    else:
                        errors.append(f"Campaign direct create also failed: {direct_resp.status_code} - {direct_resp.text[:200]}")
                except Exception as de:
                    errors.append(f"Campaign direct fallback error: {str(de)[:150]}")
        except Exception as ce:
            errors.append(f"Campaign: {str(ce)[:150]}")

    # Step 5: Link flow to campaign via FlowRecord.AssociatedRecordId
    # Flow deploys async via SOAP, so FlowRecord may not exist yet — retry a few times
    if flow_result and campaign_result and campaign_result.get('id') and flow_result.get('apiName'):
        flow_api = flow_result['apiName']
        camp_id = campaign_result['id']
        linked = False
        for attempt in range(4):
            if attempt > 0:
                import time
                time.sleep(3)
            try:
                fr_resp = sf_api('GET',
                    '/services/data/v67.0/query/?q=' + quote(
                        f"SELECT Id FROM FlowRecord WHERE ApiName = '{flow_api}' ORDER BY CreatedDate DESC LIMIT 1"
                    ), token, instance, timeout=8)
                if fr_resp.ok:
                    fr_records = fr_resp.json().get('records', [])
                    if fr_records:
                        flow_record_id = fr_records[0]['Id']
                        link_resp = sf_api('PATCH',
                            f'/services/data/v67.0/sobjects/FlowRecord/{flow_record_id}',
                            token, instance, body={'AssociatedRecordId': camp_id})
                        if link_resp.ok or link_resp.status_code == 204:
                            flow_result['linkedToCampaign'] = True
                            linked = True
                            break
                        else:
                            try:
                                link_err_body = link_resp.json()
                                link_err_msg = str(link_err_body)[:300]
                            except Exception:
                                link_err_msg = link_resp.text[:300]
                            errors.append(f"Flow-campaign link: {link_resp.status_code} - {link_err_msg}")
                            break
            except Exception:
                pass
        if not linked and 'linkedToCampaign' not in (flow_result or {}):
            errors.append("Flow-campaign link: FlowRecord not found after deploy (may still be processing)")

    return {
        'success': len(created_emails) > 0,
        'emails': created_emails,
        'flow': flow_result,
        'campaign': campaign_result,
        'errors': errors,
        'totalCreated': len(created_emails),
        'version': _APP_VERSION
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


@app.route('/api/sf/deploy-flow', methods=['POST'])
def deploy_flow():
    """Deploy a single marketing flow via SOAP Metadata API. Called separately per series."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    if not token or not instance:
        return jsonify({'error': 'Not connected to Salesforce'}), 401

    data = request.json or {}
    series_key = data.get('series', '')
    email_content_keys = data.get('emailContentKeys', [])
    config = data.get('config', {})
    workspace_name = data.get('workspaceName', 'Default_Content_Workspace')
    workspace_id = data.get('workspaceId', '')
    segment_id = data.get('segmentId', '')
    sender_id = data.get('senderId', '')
    subscription_id = data.get('subscriptionId', '')
    channel_type_id = data.get('channelTypeId', '')

    # Resolve workspace ApiName from ID (same fix as _deploy_email_series_internal)
    if workspace_id:
        try:
            ws_resp = sf_api('GET',
                '/services/data/v67.0/query/?q=' + quote(
                    f"SELECT ApiName FROM ManagedContentSpace WHERE Id = '{workspace_id}' LIMIT 1"),
                token, instance, timeout=5)
            if ws_resp.ok:
                ws_recs = ws_resp.json().get('records', [])
                if ws_recs and ws_recs[0].get('ApiName') and ws_recs[0]['ApiName'] != 'None':
                    workspace_name = ws_recs[0]['ApiName']
        except Exception:
            pass

    if series_key not in EMAIL_SERIES:
        return jsonify({'error': f'Invalid series: {series_key}'}), 400
    if not email_content_keys:
        return jsonify({'error': 'No email content keys provided'}), 400

    flow_data = generate_flow_xml(
        series_key, email_content_keys, config,
        workspace_name, segment_id, sender_id, subscription_id, channel_type_id,
        data_graph='Marketing_Data_Graph', dmo_object='UnifiedssotIndividualInd1__dlm'
    )

    if not flow_data:
        return jsonify({'error': 'Failed to generate flow XML'}), 500

    flow_xml = flow_data['xml']
    flow_api_name = flow_data['flowApiName']
    wait_days = EMAIL_SERIES[series_key]['wait_days']

    # Deploy via shared helper (submits + polls checkDeployStatus)
    deploy_result = _soap_deploy_flow(flow_data['xml'], flow_api_name, token, instance, poll_timeout=45)

    if deploy_result['success']:
        return jsonify({
            'success': True,
            'name': flow_data['flowLabel'],
            'apiName': flow_api_name,
            'status': 'Deployed',
            'deployId': deploy_result.get('deployId', ''),
            'waitNote': f'Flow deployed with {wait_days}-day wait steps between emails. Open in Flow Builder to review and activate.'
        })
    else:
        return jsonify({
            'success': False,
            'name': flow_data['flowLabel'],
            'apiName': flow_api_name,
            'status': 'Failed',
            'error': deploy_result.get('error', 'Unknown error'),
            'componentErrors': deploy_result.get('componentErrors', [])
        }), 500


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

    # Resolve workspace ApiName from ID
    try:
        ws_resp = sf_api('GET',
            '/services/data/v67.0/query/?q=' + quote(
                f"SELECT ApiName FROM ManagedContentSpace WHERE Id = '{workspace_id}' LIMIT 1"),
            token, instance, timeout=5)
        if ws_resp.ok:
            ws_recs = ws_resp.json().get('records', [])
            if ws_recs and ws_recs[0].get('ApiName') and ws_recs[0]['ApiName'] != 'None':
                workspace_name = ws_recs[0]['ApiName']
    except Exception:
        pass

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

    # Org-specific flow metadata — use known defaults (no discovery calls to save time)
    org_data_graph = 'Marketing_Data_Graph'
    org_dmo = 'UnifiedssotIndividualInd1__dlm'

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
            'createFlow': False,  # Flows deploy separately to avoid 30s timeout
            'createCampaign': email_config.get('createCampaign', True),
            'headerColor': email_config.get('headerColor', None),
            'brandContentId': brand_result.get('brandId', ''),
            'brandContentKey': brand_result.get('brandContentKey', ''),
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
