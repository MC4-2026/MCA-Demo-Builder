# MCA Demo Brand Builder

A standalone web app that analyzes any website to extract brand identity elements (colors, fonts, tone of voice, images) and creates a brand configuration for Salesforce Marketing Cloud Advanced CMS.

## Features

- **Website Analysis** — Enter any URL and the app extracts:
  - Color palette (primary, secondary, accent, etc.)
  - Typography (heading and body fonts with CSS variable resolution)
  - Tone of voice (8 categories: professional, friendly, bold, warm, etc.)
  - Logo and hero images from homepage + sub-pages
  - Button styles (color, border-radius)
  - Brand identity statement

- **Interactive Preview** — Edit all extracted elements before creating the brand:
  - Color pickers with hex input
  - Font name editing with live preview
  - Tone selector dropdown
  - Image gallery with include/exclude checkboxes

- **Export** — Export brand config as JSON for MCA CMS import

## Deploy to Heroku

1. Create a new Heroku app
2. Connect this GitHub repo
3. Deploy the `main` branch

Or via CLI:

```bash
heroku create mca-brand-builder
git push heroku main
heroku open
```

## Local Development

```bash
pip install -r requirements.txt
python server.py
# Open http://localhost:5111
```

## Tech Stack

- **Backend**: Python / Flask
- **Frontend**: Vanilla JS + Tailwind CSS
- **Scraping**: Requests + BeautifulSoup4
- **Production**: Gunicorn
