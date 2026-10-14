# Lumida website

The product's landing page: plain HTML, CSS and JavaScript, no build step, no dependencies (fonts load from Google Fonts).

| File | What it is |
|---|---|
| `index.html` | the page |
| `styles.css` | colours, type and layout (design tokens at the top) |
| `main.js` | small interactions: the hero scan, the findings inbox, the question card, the terminal, scroll reveal, the sign-up form |
| `assets/mark.svg` | the logo mark: a 3×3 grid of table cells, one lit (the problem Lumida found) |
| `assets/logo.svg` | mark + wordmark (the wordmark uses Space Grotesk; convert the text to outlines before using the file outside the site) |
| `assets/favicon.svg` | the mark simplified for 16 px |

## View it locally
```bash
python -m http.server 8765 --directory website
```
Then open http://localhost:8765.

## Before going live
- **Sign-up form:** set `EARLY_ACCESS_ENDPOINT` in `main.js` to a form service (e.g. Formspree) or your own API. Until then the form says, honestly, that sign-ups aren't open yet.
- **Claims:** every number on the page comes from the test lab (15/15 relationships, 0 false alarms on quiet nights). Keep it that way. Don't add compliance claims (SOC 2, HIPAA, …) before they're true.
- **Hosting:** any static host works. The simplest are Netlify or Cloudflare Pages: connect the repo and set the publish directory to `website`. GitHub Pages only serves `/` or `/docs` from a branch, so it would need a small deploy workflow.
- **Domain:** check availability of `lumida.dev` / `lumida.io` / `getlumida.com` before printing it anywhere.

## Brand
- Colours: midnight `#0B1020`, surfaces `#111833` / `#161F40`, cells `#2A3354` / `#3B4670`, text `#E6E9F2`, muted `#8A94B8`, **lumen amber `#FFB547`** (the only warm colour, used for "found it" moments), teal `#2DD4BF` (healthy), coral `#FF6B6B` (problems).
- Type: Space Grotesk (headings), Inter (body), JetBrains Mono (numbers, code, terminal).
- Tone: calm and precise. Real numbers, no hype, no robots or brains.
