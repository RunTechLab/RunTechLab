#!/usr/bin/env python3
"""
RunTech Lab publisher.

Local use:
  python publish_article.py --prepare-upload

GitHub Action use:
  python publish_article.py --ci

The script validates public articles, updates index.html and sitemap.xml,
and optionally prepares output/github-ready with only the latest article
and its image folder for manual upload.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent
BASE_URL = "https://runtechlab.pages.dev"
ARTICLES_DIR = ROOT / "articoli"
IMAGES_DIR = ROOT / "immagini"
INDEX_PATH = ROOT / "index.html"
SITEMAP_PATH = ROOT / "sitemap.xml"
OUTPUT_DIR = ROOT / "output"
GITHUB_READY_DIR = OUTPUT_DIR / "github-ready"
REPORT_DIR = OUTPUT_DIR / "report"

ARTICLE_START = "<!-- RUNTECH:ARTICLES:START -->"
ARTICLE_END = "<!-- RUNTECH:ARTICLES:END -->"

CLOUDFLARE_ANALYTICS_SNIPPET = """<!-- Cloudflare Web Analytics --><script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{"token": "94eea5e84787487c99bf1e73c114700e"}'></script><!-- End Cloudflare Web Analytics -->"""


@dataclass
class Article:
    path: Path
    slug: str
    title: str
    description: str
    category: str
    date_iso: str
    date_label: str
    hero_src: str | None


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8", newline="\n")


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script\b.*?</script>", " ", value)
    value = re.sub(r"(?is)<style\b.*?</style>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def first_tag_text(markup: str, tag: str) -> str:
    match = re.search(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", markup)
    return strip_tags(match.group(1)) if match else ""


def meta_content(markup: str, name: str) -> str:
    patterns = [
        rf'(?is)<meta\s+name=["\']{re.escape(name)}["\']\s+content=["\']([^"\']+)["\']',
        rf'(?is)<meta\s+content=["\']([^"\']+)["\']\s+name=["\']{re.escape(name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, markup)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def property_content(markup: str, prop: str) -> str:
    patterns = [
        rf'(?is)<meta\s+property=["\']{re.escape(prop)}["\']\s+content=["\']([^"\']+)["\']',
        rf'(?is)<meta\s+content=["\']([^"\']+)["\']\s+property=["\']{re.escape(prop)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, markup)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def title_tag(markup: str) -> str:
    return first_tag_text(markup, "title")


def h2_sections(markup: str) -> list[tuple[str, str]]:
    return [
        (strip_tags(match.group(1)), match.group(2))
        for match in re.finditer(r"(?is)<h2\b[^>]*>(.*?)</h2>(.*?)(?=<h2\b|</div>\s*</article>|$)", markup)
    ]


def is_weak_section(content: str) -> bool:
    plain = strip_tags(content)
    if re.search(r"(?i)\b(da completare|placeholder|lorem ipsum|todo|sezione vuota|prompt immagine)\b", plain):
        return True
    has_real_markup = re.search(r"(?is)<(p|ul|ol|li|table|blockquote)\b", content)
    if not has_real_markup:
        return True
    if len(plain) < 45 and not re.search(r"(?is)<(ul|ol|table)\b", content):
        return True
    return False


def image_path_from_src(src: str) -> Path | None:
    if not src or src.startswith(("http://", "https://", "data:")):
        return None
    clean = src.split("#", 1)[0].split("?", 1)[0]
    return (ARTICLES_DIR / clean).resolve()


def validate_article(path: Path) -> tuple[Article | None, list[str]]:
    markup = read_text(path)
    errors: list[str] = []
    slug = path.stem

    title = first_tag_text(markup, "h1")
    if not title:
        errors.append("manca H1")
    if len(re.findall(r"(?is)<h1\b", markup)) != 1:
        errors.append("H1 non unico")

    page_title = title_tag(markup)
    if not page_title:
        errors.append("manca title")
    elif len(page_title) > 70:
        errors.append(f"title troppo lungo ({len(page_title)})")

    description = meta_content(markup, "description")
    if not description:
        errors.append("manca meta description")
    elif len(description) < 145 or len(description) > 160:
        errors.append(f"meta description fuori range ({len(description)})")
    if description.endswith(("...", "…")):
        errors.append("meta description troncata")

    required_snippets = [
        'name="robots" content="index,follow"',
        'rel="canonical"',
        'property="og:title"',
        'property="og:description"',
        'property="og:image"',
        'property="og:url"',
        '../stile.css',
    ]
    lowered = markup.lower()
    for snippet in required_snippets:
        if snippet.lower() not in lowered:
            errors.append(f"manca {snippet}")

    canonical = re.search(r'(?is)<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']+)["\']', markup)
    expected_url = f"{BASE_URL}/articoli/{slug}.html"
    if canonical and canonical.group(1).strip() != expected_url:
        errors.append("canonical non coerente con slug")

    for heading, content in h2_sections(markup):
        if is_weak_section(content):
            errors.append(f"H2 vuoto o debole: {heading}")

    verdict_match = re.search(r"(?is)<h2\b[^>]*>\s*Verdetto RunTech Lab\s*</h2>(.*?)(?=<h2\b|$)", markup)
    verdict = strip_tags(verdict_match.group(0)) if verdict_match else ""
    for required in ["Sintesi finale", "Per chi ha senso", "Per chi non ha senso", "Valutazione pratica"]:
        if required.lower() not in verdict.lower():
            errors.append(f"verdetto incompleto: manca {required}")

    if re.search(r"(?i)\b(da completare|placeholder|lorem ipsum|todo|prompt immagine|immagine consigliata)\b", strip_tags(markup)):
        errors.append("contiene placeholder o istruzioni interne")

    hero_src = None
    img_match = re.search(r'(?is)<img\b[^>]*\bsrc=["\']([^"\']+)["\']', markup)
    if img_match:
        hero_src = img_match.group(1).strip()
        local_image = image_path_from_src(hero_src)
        if local_image and not local_image.exists():
            errors.append(f"immagine non trovata: {hero_src}")
    else:
        errors.append("manca immagine hero")

    category = "Running"
    meta_match = re.search(r'(?is)<p\s+class=["\']article-meta["\'][^>]*>(.*?)</p>', markup)
    if meta_match:
        parts = [part.strip() for part in strip_tags(meta_match.group(1)).split("·")]
        if len(parts) >= 2 and parts[1]:
            category = parts[1]

    date_iso = path.stat().st_mtime_ns
    published_iso = date.today().isoformat()
    date_label = published_iso
    time_match = re.search(r'(?is)<time\b[^>]*datetime=["\']([^"\']+)["\'][^>]*>(.*?)</time>', markup)
    if time_match:
        published_iso = time_match.group(1).strip()
        date_label = re.sub(r"(?i)^Pubblicato il\s+", "", strip_tags(time_match.group(2)))
    else:
        date_label = published_iso

    article = Article(
        path=path,
        slug=slug,
        title=title or slug,
        description=description,
        category=category,
        date_iso=published_iso,
        date_label=date_label,
        hero_src=hero_src,
    )
    return (None if errors else article), errors


def valid_articles() -> tuple[list[Article], dict[str, list[str]]]:
    articles: list[Article] = []
    rejected: dict[str, list[str]] = {}
    for path in sorted(ARTICLES_DIR.glob("*.html")):
        if path.name.startswith("_") or path.stem in {"articolo-template", "esempio-articolo-completo"}:
            continue
        article, errors = validate_article(path)
        if article:
            articles.append(article)
        else:
            rejected[path.name] = errors
    articles.sort(key=lambda item: (item.date_iso, item.path.stat().st_mtime), reverse=True)
    return articles, rejected




def inject_cloudflare_analytics(markup: str) -> str:
    """Insert Cloudflare Web Analytics before </head>, avoiding duplicates."""
    if "static.cloudflareinsights.com/beacon.min.js" in markup:
        return markup
    if "</head>" not in markup:
        return markup
    return markup.replace("</head>", f"  {CLOUDFLARE_ANALYTICS_SNIPPET}\n</head>", 1)


def ensure_analytics_on_site() -> None:
    """Apply Cloudflare Analytics to homepage and all public article pages."""
    html_files = [INDEX_PATH]
    if ARTICLES_DIR.exists():
        html_files.extend(
            path for path in sorted(ARTICLES_DIR.glob("*.html"))
            if not path.name.startswith("_")
        )

    for path in html_files:
        if not path.exists():
            continue
        current = read_text(path)
        updated = inject_cloudflare_analytics(current)
        if updated != current:
            write_text(path, updated)

def update_index(articles: list[Article]) -> None:
    markup = read_text(INDEX_PATH)
    if ARTICLE_START not in markup or ARTICLE_END not in markup:
        raise SystemExit("Marker RUNTECH:ARTICLES non trovati in index.html")

    cards = []
    for article in articles:
        cards.append(
            f'''          <article class="article-card">
            <a href="articoli/{html.escape(article.path.name)}">
              <span class="card-label">{html.escape(article.category)}</span>
              <h3>{html.escape(article.title)}</h3>
              <p>{html.escape(article.description)}</p>
              <time datetime="{html.escape(article.date_iso)}">{html.escape(article.date_label)}</time>
            </a>
          </article>'''
        )

    replacement = ARTICLE_START + "\n" + "\n".join(cards) + "\n          " + ARTICLE_END
    pattern = re.compile(re.escape(ARTICLE_START) + r".*?" + re.escape(ARTICLE_END), re.S)
    write_text(INDEX_PATH, pattern.sub(replacement, markup))


def update_sitemap(articles: list[Article]) -> None:
    today = date.today().isoformat()
    urls = [
        f"""  <url>
    <loc>{BASE_URL}/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>"""
    ]
    for article in articles:
        urls.append(
            f"""  <url>
    <loc>{escape(BASE_URL + '/articoli/' + article.path.name)}</loc>
    <lastmod>{html.escape(article.date_iso)}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>"""
        )
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n'
    sitemap += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sitemap += "\n".join(urls)
    sitemap += "\n</urlset>\n"
    write_text(SITEMAP_PATH, sitemap)


def clear_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_article_assets(article: Article, target_root: Path) -> list[Path]:
    copied: list[Path] = []
    target_article = target_root / "articoli" / article.path.name
    target_article.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(article.path, target_article)
    copied.append(target_article)

    image_dir = IMAGES_DIR / article.slug
    if image_dir.exists():
        target_image_dir = target_root / "immagini" / article.slug
        shutil.copytree(image_dir, target_image_dir, dirs_exist_ok=True)
        copied.extend(path for path in target_image_dir.rglob("*") if path.is_file())
    elif article.hero_src:
        local_image = image_path_from_src(article.hero_src)
        if local_image and local_image.exists() and local_image.is_relative_to(IMAGES_DIR):
            rel = local_image.relative_to(ROOT)
            target_image = target_root / rel
            target_image.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_image, target_image)
            copied.append(target_image)
    return copied


def prepare_upload(articles: list[Article]) -> None:
    clear_dir(GITHUB_READY_DIR)
    if not articles:
        raise SystemExit("Nessun articolo valido da preparare.")
    latest = articles[0]
    copied = copy_article_assets(latest, GITHUB_READY_DIR)
    instructions = [
        "RunTech Lab - file da caricare su GitHub",
        "",
        "Carica SOLO questi file/cartelle nel repository:",
        "",
    ]
    for path in copied:
        instructions.append(str(path.relative_to(GITHUB_READY_DIR)).replace("\\", "/"))
    instructions.extend(
        [
            "",
            "Non devi caricare index.html o sitemap.xml ogni volta se la GitHub Action e presente nel repository.",
            "La GitHub Action esegue publish_article.py e aggiorna homepage e sitemap automaticamente.",
        ]
    )
    write_text(GITHUB_READY_DIR / "LEGGIMI_CARICA_SU_GITHUB.txt", "\n".join(instructions) + "\n")


def write_report(articles: list[Article], rejected: dict[str, list[str]]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["RunTech Lab - report pubblicazione", ""]
    lines.append("Articoli validi:")
    if articles:
        for article in articles:
            lines.append(f"- {article.path.name}")
    else:
        lines.append("- nessuno")
    lines.append("")
    lines.append("Articoli esclusi:")
    if rejected:
        for name, errors in rejected.items():
            lines.append(f"- {name}")
            for error in errors:
                lines.append(f"  - {error}")
    else:
        lines.append("- nessuno")
    write_text(REPORT_DIR / "pubblicazione.txt", "\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and publish RunTech Lab articles.")
    parser.add_argument("--ci", action="store_true", help="Fail when no valid articles are available.")
    parser.add_argument("--prepare-upload", action="store_true", help="Prepare output/github-ready with latest article assets.")
    args = parser.parse_args()

    articles, rejected = valid_articles()
    if args.ci and not articles:
        raise SystemExit("Nessun articolo valido trovato.")

    update_index(articles)
    update_sitemap(articles)
    ensure_analytics_on_site()
    write_report(articles, rejected)
    if args.prepare_upload:
        prepare_upload(articles)

    print(f"Articoli validi: {len(articles)}")
    print(f"Articoli esclusi: {len(rejected)}")
    print("Homepage e sitemap aggiornate.")
    if args.prepare_upload:
        print(f"File da caricare preparati in: {GITHUB_READY_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
