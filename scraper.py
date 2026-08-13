"""
サイト内検索の実行モジュール。

- url_template 内の {query} をキーワードで置換してリクエスト
- selector が指定されていれば、そのCSSセレクタで結果要素を抽出し、
  各要素の中の最初の <a> のテキストとhrefをタイトル・リンクとして扱う
  (要素自体がaタグの場合はそのまま使う)
- selector が指定されていなければ、ページ内の全 <a> タグを機械的に抽出し、
  テキストがある程度の長さを持つものを結果候補として返す(簡易版)
"""

import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 10
DETAIL_PAGE_TIMEOUT = 8
DETAIL_FETCH_CONCURRENCY = 8  # 詳細ページに同時アクセスする最大数
DETAIL_FETCH_LIMIT = 30  # 詳細ページへの個別アクセスを行う最大件数(重くなりすぎないための上限)
MAX_RESULTS = 150
MAX_IMAGES = 150
MAX_VIEWKEY_LINKS = 150
RESULTS_PER_PAGE = 5
MIN_LINK_TEXT_LEN = 4  # 簡易抽出時、これより短いテキストのリンクは除外

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
VIEWKEY_MARKER = "viewkey="

# flashvars_XXXXX = { ... }; ブロックを検出する正規表現
FLASHVARS_BLOCK_RE = re.compile(r"var\s+flashvars_\w+\s*=\s*(\{.*?\});", re.DOTALL)

# タイトルとして使う候補キー(見つかった順に採用)
TITLE_KEYS = ("video_title", "title", "video_name")


@dataclass
class SearchResult:
    title: str
    url: str
    thumbnail: Optional[str] = None


@dataclass
class SearchResponse:
    links: List[SearchResult]
    images: List[str]
    viewkey_links: List[SearchResult]


def build_search_url(url_template: str, query: str) -> str:
    encoded_query = urllib.parse.quote_plus(query)
    return url_template.replace("{query}", encoded_query)


def _resolve_url(base_url: str, href: str) -> str:
    return urllib.parse.urljoin(base_url, href)


def _extract_with_selector(soup: BeautifulSoup, selector: str, base_url: str) -> List[SearchResult]:
    results: List[SearchResult] = []
    elements = soup.select(selector)

    for el in elements:
        # 要素自体がaタグならそれを使う。そうでなければ中の最初のaタグを探す
        a_tag = el if el.name == "a" else el.find("a")
        if a_tag is None or not a_tag.get("href"):
            continue

        title = a_tag.get_text(strip=True) or el.get_text(strip=True)
        if not title:
            continue

        href = _resolve_url(base_url, a_tag["href"])
        results.append(SearchResult(title=title, url=href))

        if len(results) >= MAX_RESULTS:
            break

    return results


def _extract_generic(soup: BeautifulSoup, base_url: str) -> List[SearchResult]:
    """selector未指定時の簡易抽出: ページ内のaタグを機械的に拾う"""
    results: List[SearchResult] = []
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        title = a_tag.get_text(strip=True)
        href = a_tag["href"]

        if len(title) < MIN_LINK_TEXT_LEN:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue

        full_url = _resolve_url(base_url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        results.append(SearchResult(title=title, url=full_url))

        if len(results) >= MAX_RESULTS:
            break

    return results


def _is_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(IMAGE_EXTENSIONS)


def _extract_images(soup: BeautifulSoup, base_url: str) -> List[str]:
    """
    ページ内の画像URLを最大MAX_IMAGES件抽出する。
    - <img src="..."> タグ
    - 拡張子が画像である <a href="..."> タグ(画像へ直接リンクしている場合)
    の両方を対象にする。
    """
    images: List[str] = []
    seen = set()

    # <img> タグから収集
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src")
        if not src:
            continue

        full_url = _resolve_url(base_url, src)

        # 小さすぎるアイコン・ロゴらしきものは除外(幅/高さ指定がある場合のみチェック)
        width = img_tag.get("width")
        height = img_tag.get("height")
        try:
            if width and int(width) < 40:
                continue
            if height and int(height) < 40:
                continue
        except ValueError:
            pass

        if full_url in seen:
            continue
        seen.add(full_url)
        images.append(full_url)

        if len(images) >= MAX_IMAGES:
            return images

    # 画像への直リンク(<a href="....jpg">など)も対象に含める
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not _is_image_url(href):
            continue

        full_url = _resolve_url(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        images.append(full_url)

        if len(images) >= MAX_IMAGES:
            return images

    return images


def _find_image_url_in_flashvars(flashvars: dict) -> Optional[str]:
    """
    flashvars の中身から、値が画像URL(.jpg/.jpeg/.png/.webp等で終わる)になっている
    ものをキー名に関わらず探す。
    """
    for value in flashvars.values():
        if isinstance(value, str) and _is_image_url(value):
            return value
    return None


def _parse_flashvars(html_text: str) -> dict:
    """
    HTMLソース中の 'var flashvars_XXXXX = { ... };' ブロックを探し、
    中身をざっくり dict として取り出す(厳密なJSではないため正規表現で個別キーを拾う)。
    """
    match = FLASHVARS_BLOCK_RE.search(html_text)
    if not match:
        return {}

    block = match.group(1)
    result = {}
    for m in re.finditer(r'["\']([\w]+)["\']\s*:\s*["\']([^"\']*)["\']', block):
        key, value = m.group(1), m.group(2)
        result[key] = value
    return result


def _collect_viewkey_candidates(soup: BeautifulSoup, base_url: str) -> List[tuple]:
    """検索結果ページ内から、viewkey= を含むリンクのURLとフォールバック用テキストを集める。"""
    candidates: List[tuple] = []  # (url, fallback_title)
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]

        if VIEWKEY_MARKER not in href:
            continue

        full_url = _resolve_url(base_url, href)
        if VIEWKEY_MARKER not in full_url:
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        fallback_title = a_tag.get_text(strip=True) or None
        candidates.append((full_url, fallback_title))

        if len(candidates) >= MAX_VIEWKEY_LINKS:
            break

    return candidates


async def _fetch_one_detail(session: "aiohttp.ClientSession", url: str, semaphore: "asyncio.Semaphore") -> tuple:
    """
    viewkey= を含む1ページに個別アクセスし、flashvars からタイトルとサムネイルを取得する。
    (title, thumbnail) を返す。取れなければ (None, None)。
    """
    async with semaphore:
        try:
            async with session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=DETAIL_PAGE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None, None
                html_text = await resp.text(errors="ignore")
        except Exception:
            return None, None

    flashvars = _parse_flashvars(html_text)
    if not flashvars:
        return None, None

    title = None
    for key in TITLE_KEYS:
        if flashvars.get(key):
            title = flashvars[key]
            break

    thumbnail = _find_image_url_in_flashvars(flashvars)
    if thumbnail:
        thumbnail = _resolve_url(url, thumbnail)

    return title, thumbnail


async def _fetch_viewkey_details(candidates: List[tuple]) -> List[SearchResult]:
    """
    viewkeyリンク候補それぞれに個別アクセスし、そのリンク自身のタイトル・サムネイルを取得する。
    リンクと画像・タイトルが必ず1対1で対応するようにする。
    """
    semaphore = asyncio.Semaphore(DETAIL_FETCH_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_one_detail(session, url, semaphore) for url, _ in candidates]
        fetched = await asyncio.gather(*tasks)

    results: List[SearchResult] = []
    for (url, fallback_title), (title, thumbnail) in zip(candidates, fetched):
        results.append(SearchResult(
            title=title or fallback_title or url,
            url=url,
            thumbnail=thumbnail,
        ))
    return results


async def _extract_viewkey_links(soup: BeautifulSoup, base_url: str) -> List[SearchResult]:
    """
    ページ内の全リンクから、URLに 'viewkey=' を含むものだけを最大MAX_VIEWKEY_LINKS件抽出し、
    そのうち先頭DETAIL_FETCH_LIMIT件について、それぞれのページに個別・並行アクセスして
    そのリンク自身のタイトル・サムネイルを取得する。
    (リンクと画像・タイトルが必ず1対1で対応するようにするため、使い回しはしない)
    残りの件数は、検索結果ページ上のリンクテキストのみをタイトルとして使う(画像なし)。
    """
    candidates = _collect_viewkey_candidates(soup, base_url)
    if not candidates:
        return []

    detail_targets = candidates[:DETAIL_FETCH_LIMIT]
    remaining = candidates[DETAIL_FETCH_LIMIT:]

    results = await _fetch_viewkey_details(detail_targets)

    for url, fallback_title in remaining:
        results.append(SearchResult(title=fallback_title or url, url=url, thumbnail=None))

    return results


async def search(url_template: str, query: str, selector: Optional[str] = None) -> SearchResponse:
    """
    サイト内検索を実行し、検索結果ページ1枚分から、リンク結果・画像URLを取得する。
    viewkey= を含むリンクについては、各ページに個別・並行アクセスして
    'var flashvars_XXXXX = {...};' からそのリンク自身のタイトルとサムネイルを取得する
    (リンクごとに個別取得するため、画像やタイトルが別のリンクのものと混ざることはない)。
    ネットワークエラーやパース失敗時は例外を投げる(呼び出し側でハンドリング)。

    discord.py のイベントループ内から呼ばれるため、この関数自体は async def にしている。
    (内部で asyncio.run() は使わない — 実行中のループの中では呼べないため)
    """
    search_url = build_search_url(url_template, query)

    async with aiohttp.ClientSession() as session:
        async with session.get(
            search_url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            html_text = await resp.text(errors="ignore")

    soup = BeautifulSoup(html_text, "html.parser")

    if selector:
        links = _extract_with_selector(soup, selector, search_url)
        # セレクタで何も取れなければ簡易抽出にフォールバック
        if not links:
            links = _extract_generic(soup, search_url)
    else:
        links = _extract_generic(soup, search_url)

    # 一覧ページ自体の<img>タグからも画像を拾う(取れれば)
    page_images = _extract_images(soup, search_url)

    viewkey_links = await _extract_viewkey_links(soup, search_url)

    # viewkeyリンクの詳細ページから取得したサムネイル画像もimagesに合流させる
    # (一覧ページに画像が無いサイトでもここで画像が取れる)
    viewkey_thumbnails = [r.thumbnail for r in viewkey_links if r.thumbnail]

    images = page_images.copy()
    for thumb in viewkey_thumbnails:
        if thumb not in images and len(images) < MAX_IMAGES:
            images.append(thumb)

    return SearchResponse(links=links, images=images, viewkey_links=viewkey_links)
