#!/usr/bin/env python3
"""
MCA Demo Brand Builder — Backend Server
Fetches websites server-side, extracts brand assets (colors, fonts, tone, images).
"""

import os
import re
import json
import sys
import uuid
import colorsys
import threading
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


# ─── Tone Analysis ───

TONE_LEXICON = {
    'professional': ['solutions', 'enterprise', 'industry', 'leading', 'trusted', 'expertise',
                     'compliance', 'professional', 'excellence', 'strategic', 'performance',
                     'optimize', 'deliver', 'partners', 'global', 'reliable', 'proven',
                     'commitment', 'quality', 'efficient'],
    'friendly': ['welcome', 'together', 'community', 'join', 'share', 'help', 'easy',
                 'simple', 'friendly', 'everyone', 'connect', 'team', 'support', 'care',
                 'great', 'wonderful', 'happy', 'smile', 'neighbor', 'folks'],
    'bold': ['disrupt', 'revolution', 'transform', 'bold', 'future', 'next', 'power',
             'unleash', 'breakthrough', 'fearless', 'unstoppable', 'change', 'innovate',
             'impact', 'redefine', 'reimagine', 'challenge', 'dare', 'launch'],
    'warm': ['heart', 'family', 'love', 'personal', 'journey', 'story', 'inspire',
             'dream', 'believe', 'nurture', 'grow', 'caring', 'compassion', 'human',
             'authentic', 'home', 'yours', 'together', 'life', 'moment'],
    'technical': ['api', 'platform', 'deploy', 'infrastructure', 'data', 'scale',
                  'architecture', 'integrate', 'system', 'protocol', 'algorithm',
                  'stack', 'code', 'developer', 'framework', 'automate', 'analytics'],
    'playful': ['fun', 'awesome', 'wow', 'amazing', 'cool', 'play', 'enjoy',
                'adventure', 'discover', 'magic', 'surprise', 'delight', 'exciting',
                'vibe', 'epic', 'whoa', 'sweet', 'boom'],
    'luxurious': ['premium', 'exclusive', 'luxury', 'elegant', 'refined', 'curated',
                  'bespoke', 'crafted', 'timeless', 'heritage', 'prestige', 'exquisite',
                  'sophisticat', 'artisan', 'distinguished', 'finest'],
    'casual': ['hey', 'stuff', 'check out', 'pretty', 'thing', 'got', 'yeah',
               'super', 'chill', 'real', 'honest', 'straightforward', 'no-nonsense',
               'actually', 'basically', 'just']
}

TONE_LABELS = {
    'professional': 'Professional and authoritative',
    'friendly': 'Friendly and approachable',
    'bold': 'Bold and innovative',
    'warm': 'Warm and empathetic',
    'technical': 'Technical and precise',
    'playful': 'Playful and energetic',
    'luxurious': 'Luxurious and refined',
    'casual': 'Casual and conversational'
}

TONE_DESCRIPTIONS = {
    'professional': 'Communicates with confidence and credibility. Uses formal language that establishes authority and trust while maintaining clarity.',
    'friendly': 'Speaks in a warm, inclusive voice that makes everyone feel welcome. Uses simple language and a helpful, approachable manner.',
    'bold': 'Challenges the status quo with energetic, forward-thinking language. Speaks to innovation, transformation, and creating impact.',
    'warm': 'Connects on a human level with empathy and authenticity. Uses personal, heartfelt language that inspires and nurtures.',
    'technical': 'Precise and detailed communication focused on capabilities and specifications. Values accuracy and depth of information.',
    'playful': 'Light-hearted and fun communication that sparks joy. Uses creative language, humor, and enthusiasm to engage audiences.',
    'luxurious': 'Refined, elegant communication that conveys exclusivity and premium quality. Uses sophisticated language and measured pacing.',
    'casual': 'Straightforward, down-to-earth communication. Speaks like a trusted friend — honest, relatable, and no-nonsense.'
}

def analyze_tone(soup):
    """Analyze tone from page text content."""
    text_parts = []

    # Meta description
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        text_parts.append(meta['content'])

    # OG description
    og = soup.find('meta', attrs={'property': 'og:description'})
    if og and og.get('content'):
        text_parts.append(og['content'])

    # Headings
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4']):
        text_parts.append(tag.get_text(strip=True))

    # Paragraphs
    for p in soup.find_all('p'):
        text_parts.append(p.get_text(strip=True))

    # Hero / tagline
    for cls in ['hero', 'tagline', 'subtitle', 'banner', 'headline', 'slogan']:
        for el in soup.find_all(class_=re.compile(cls, re.I)):
            text_parts.append(el.get_text(strip=True))

    all_text = ' '.join(text_parts).lower()[:8000]

    scores = {}
    for tone, words in TONE_LEXICON.items():
        score = 0
        for w in words:
            score += len(re.findall(r'\b' + re.escape(w), all_text, re.I))
        scores[tone] = score

    winner = max(scores, key=scores.get) if any(scores.values()) else 'professional'

    return {
        'label': TONE_LABELS[winner],
        'description': TONE_DESCRIPTIONS[winner]
    }


# ─── Identity Generation ───

def generate_identity(name, soup, tone):
    meta = soup.find('meta', attrs={'name': 'description'})
    meta_desc = meta['content'] if meta and meta.get('content') else ''
    h1 = soup.find('h1')
    h1_text = h1.get_text(strip=True) if h1 else ''
    tagline = h1_text or meta_desc

    identity = f"{name} is a brand that {tone['description'].lower().rstrip('.')}."
    if tagline:
        identity += f' Their core message: "{tagline[:150]}."'
    identity += ' They value consistency, clarity, and connecting with their audience through every touchpoint.'
    return identity


# ─── Image Extraction ───

def extract_images(soup, base_url):
    images = []
    seen = set()
    parsed_base = urlparse(base_url)

    def add_image(src, img_type, alt):
        url = resolve_url(src, base_url)
        if not url or url in seen:
            return
        # Skip tiny tracking pixels, svgs with data URIs, etc.
        if any(x in url.lower() for x in ['pixel', 'tracking', 'spacer', '1x1', 'blank.gif', 'beacon']):
            return
        seen.add(url)
        images.append({'url': url, 'type': img_type, 'alt': alt or img_type.title(), 'selected': True})

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
    ]
    for selector, img_type in logo_selectors:
        for img in soup.select(selector):
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src', '')
            alt = img.get('alt', 'Logo')
            add_image(src, img_type, alt)

    # SVG logos in header
    for svg in soup.select('header svg, nav svg, [class*="logo"] svg'):
        # Can't extract SVG easily as image URL, skip
        pass

    # Hero / large images
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src', '')
        alt = img.get('alt', '')
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

        parent_class = ' '.join(img.parent.get('class', [])) if img.parent else ''
        grandparent_class = ' '.join(img.parent.parent.get('class', [])) if img.parent and img.parent.parent else ''
        context = (parent_class + ' ' + grandparent_class).lower()
        in_hero = bool(re.search(r'hero|banner|jumbotron|splash|featured|carousel|slider|masthead', context))

        src_hint = bool(re.search(r'hero|banner|featured|cover|main|splash|carousel', src, re.I))

        if is_large or in_hero or src_hint:
            url = resolve_url(src, base_url)
            if url and url not in seen:
                add_image(src, 'hero', alt or 'Hero Image')

    # Background images
    for el in soup.find_all(style=re.compile(r'background')):
        style = el.get('style', '')
        m = re.search(r'url\(["\']?([^"\')\s]+)["\']?\)', style)
        if m:
            add_image(m.group(1), 'hero', 'Background Image')

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
        tone = analyze_tone(soup)
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
            'toneLabel': tone['label'],
            'toneDescription': tone['description'],
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
    return jsonify(job)


@app.route('/')
def index():
    return send_from_directory('.', 'brand-builder.html')


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


def sf_api(method, path, access_token, instance_url, body=None):
    """Make an authenticated Salesforce REST API call."""
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


@app.route('/api/sf/workspaces')
def get_workspaces():
    """List CMS workspaces in the connected Salesforce org."""
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
    return jsonify({'workspaces': [{'id': r['Id'], 'name': r['Name']} for r in records]})


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
    return jsonify({
        'success': True,
        'workspace': {
            'id': result.get('id', ''),
            'name': result.get('name', api_name)
        }
    })


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

    content_ids = []
    errors = []

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
            return jsonify({'error': f'Brand creation failed: {err_detail}', 'imageErrors': errors}), 500
    except Exception as e:
        return jsonify({'error': f'Brand creation error: {str(e)[:200]}', 'imageErrors': errors}), 500

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

    return jsonify({
        'success': True,
        'brandId': brand_id,
        'contentIds': content_ids,
        'totalCreated': len(content_ids),
        'errors': errors
    })


@app.route('/api/sf/status')
def sf_status():
    """Check if user is connected to Salesforce."""
    token = session.get('sf_access_token')
    instance = session.get('sf_instance_url')
    return jsonify({
        'connected': bool(token and instance),
        'instanceUrl': instance or ''
    })


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
