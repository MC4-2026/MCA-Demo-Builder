#!/usr/bin/env python3
"""
MCA Demo Brand Builder — Backend Server
Fetches websites server-side, extracts brand assets (colors, fonts, tone, images).
"""

import re
import json
import sys
import uuid
import colorsys
import threading
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='.')
CORS(app)

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

        w = int(img.get('width', 0) or 0)
        h = int(img.get('height', 0) or 0)
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


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', sys.argv[1] if len(sys.argv) > 1 else 5111))
    print(f'Starting Brand Builder server on http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
