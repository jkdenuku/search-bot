"""
サイト内検索の実行モジュール。

処理の流れ:
1. url_template の {query} をキーワードで置換し、検索結果ページを取得する
2. 通常のリンク結果を抽出する
   - selector が指定されていれば、そのCSSセレクタで結果要素を抽出
   - 指定されていなければ、ページ内の全<a>タグから機械的に抽出(簡易版)
3. ページ内の画像(<img>タグ)を抽出する
4. ページ内のリンクの中に viewkey= を含むものがあれば、それらを別枠として検出し、
   それぞれのリンクを個別に開いて、ページ内の
   'var flashvars_XXXXX = {...};' からタイトル(video_title)と
   画像URL(.jpg等で終わる値)を取得する
   → viewkey= を含むサイトを使っていない場合、この処理は何もしない(空リストを返す)
"""

import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------
# 設定値
# ---------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
}

REQUEST_TIMEOUT = 10          # 検索結果ページ取得のタイムアウト(秒)
DETAIL_TIMEOUT = 8            # viewkey詳細ページ取得のタイムアウト(秒)
DETAIL_CONCURRENCY = 8        # viewkey詳細ページに同時アクセスする数
DETAIL_FETCH_LIMIT = 10       # viewkey詳細ページを個別取得する最大件数(重くなりすぎ防止)

MAX_LINKS = 30                 # 通常のリンク結果の取得上限(表示は5件のみ使用)
MAX_IMAGES = 10                 # 一覧ページから拾う画像の取得上限(表示は5件のみ使用)
MAX_VIEWKEY_LINKS = 10           # viewkeyリンクの取得上限(表示は5件のみ使用)
RESULTS_PER_PAGE = 5           # (未使用: 互換性のため残置)

MIN_LINK_TEXT_LEN = 4          # 簡易抽出時、これより短いテキストのリンクは除外
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
VIEWKEY_MARKER = "viewkey="

# flashvars_XXXXX = { ... }; ブロックを検出する正規表現
FLASHVARS_BLOCK_RE = re.compile(r"var\s+flashvars_\w+\s*=\s*(\{.*?\});", re.DOTALL)
TITLE_KEYS = ("video_title", "title", "video_name")


# ---------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------

@dataclass
class LinkResult:
    """通常の検索結果リンク"""
    title: str
    url: str


@dataclass
class VideoResult:
    """viewkey= を含むリンクから取得した、動画のタイトル・URL・サムネイル"""
    title: str
    url: str
    thumbnail: Optional[str] = None


@dataclass
class SearchResponse:
    links: List[LinkResult]        # 通常のリンク結果
    images: List[str]              # 一覧ページ内の画像URL
    videos: List[VideoResult]      # viewkey= を含むリンクの詳細(タイトル・サムネイル付き)


# ---------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------

def build_search_url(url_template: str, query: str) -> str:
    encoded_query = urllib.parse.quote_plus(query)
    return url_template.replace("{query}", encoded_query)


def _resolve_url(base_url: str, href: str) -> str:
    return urllib.parse.urljoin(base_url, href)


def _is_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(IMAGE_EXTENSIONS)


async def _fetch_html(session: aiohttp.ClientSession, url: str, timeout: int) -> str:
    """指定URLのHTMLを取得する。ステータスエラー時は例外を投げる。"""
    async with session.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=aiohttp.ClientTimeout(total=timeout),
        allow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        return await resp.text(errors="ignore")


# ---------------------------------------------------------------------
# 1. 通常のリンク結果の抽出
# ---------------------------------------------------------------------

def _extract_links_with_selector(soup: BeautifulSoup, selector: str, base_url: str) -> List[LinkResult]:
    results: List[LinkResult] = []

    for el in soup.select(selector):
        a_tag = el if el.name == "a" else el.find("a")
        if a_tag is None or not a_tag.get("href"):
            continue

        title = a_tag.get_text(strip=True) or el.get_text(strip=True)
        if not title:
            continue

        results.append(LinkResult(title=title, url=_resolve_url(base_url, a_tag["href"])))
        if len(results) >= MAX_LINKS:
            break

    return results


def _extract_links_generic(soup: BeautifulSoup, base_url: str) -> List[LinkResult]:
    """selector未指定時の簡易抽出: ページ内のaタグを機械的に拾う"""
    results: List[LinkResult] = []
    seen = set()

    for a_tag in soup.find_all("a", href=True):
        title = a_tag.get_text(strip=True)
        href = a_tag["href"]

        if len(title) < MIN_LINK_TEXT_LEN:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue

        full_url = _resolve_url(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)

        results.append(LinkResult(title=title, url=full_url))
        if len(results) >= MAX_LINKS:
            break

    return results


def extract_links(soup: BeautifulSoup, base_url: str, selector: Optional[str]) -> List[LinkResult]:
    if selector:
        results = _extract_links_with_selector(soup, selector, base_url)
        if results:
            return results
        # セレクタで何も取れなければ簡易抽出にフォールバック
    return _extract_links_generic(soup, base_url)


# ---------------------------------------------------------------------
# 2. 一覧ページ内の画像抽出
# ---------------------------------------------------------------------

def extract_page_images(soup: BeautifulSoup, base_url: str) -> List[str]:
    """ページ内の<img>タグからサムネイルらしき画像URLを最大MAX_IMAGES件抽出する。"""
    images: List[str] = []
    seen = set()

    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src")
        if not src:
            continue

        full_url = _resolve_url(base_url, src)

        # 小さすぎるアイコン・ロゴらしきものは除外(幅/高さ指定がある場合のみチェック)
        width, height = img_tag.get("width"), img_tag.get("height")
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
            break

    return images


# ---------------------------------------------------------------------
# 3. viewkey= を含むリンクの検出と、その詳細(タイトル・サムネイル)取得
# ---------------------------------------------------------------------

def find_viewkey_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    """
    検索結果ページ内から viewkey= を含むリンクのURLを最大MAX_VIEWKEY_LINKS件集める。
    見つからなければ空リストを返す(= viewkey機能を使わないサイトではここで終わる)。
    """
    urls: List[str] = []
    seen = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if VIEWKEY_MARKER not in href:
            continue

        full_url = _resolve_url(base_url, href)
        if VIEWKEY_MARKER not in full_url or full_url in seen:
            continue
        seen.add(full_url)

        urls.append(full_url)
        if len(urls) >= MAX_VIEWKEY_LINKS:
            break

    return urls


def _parse_flashvars(html_text: str) -> dict:
    """HTML中の 'var flashvars_XXXXX = {...};' を dict として取り出す。"""
    match = FLASHVARS_BLOCK_RE.search(html_text)
    if not match:
        return {}

    result = {}
    for m in re.finditer(r'["\']([\w]+)["\']\s*:\s*["\']([^"\']*)["\']', match.group(1)):
        result[m.group(1)] = m.group(2)
    return result


def _title_from_flashvars(flashvars: dict) -> Optional[str]:
    for key in TITLE_KEYS:
        if flashvars.get(key):
            return flashvars[key]
    return None


def _image_from_flashvars(flashvars: dict) -> Optional[str]:
    """flashvarsの値の中から、.jpg等で終わる画像URLをキー名に関わらず探す。"""
    for value in flashvars.values():
        if isinstance(value, str) and _is_image_url(value):
            return value
    return None


async def _fetch_video_detail(
    session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore
) -> VideoResult:
    """
    1件のviewkeyリンクを開き、flashvarsからタイトルと画像URLを取得する。
    取得に失敗した場合でも、タイトルはURLをそのまま使ってVideoResultを返す
    (呼び出し側で「取得できなかった」ことが分かるよう例外にはしない)。
    """
    async with semaphore:
        try:
            html_text = await _fetch_html(session, url, DETAIL_TIMEOUT)
        except Exception:
            return VideoResult(title=url, url=url, thumbnail=None)

    flashvars = _parse_flashvars(html_text)
    title = _title_from_flashvars(flashvars) or url
    thumbnail = _image_from_flashvars(flashvars)
    if thumbnail:
        thumbnail = _resolve_url(url, thumbnail)

    return VideoResult(title=title, url=url, thumbnail=thumbnail)


async def fetch_video_details(viewkey_urls: List[str]) -> List[VideoResult]:
    """
    viewkeyリンクのURLリストを受け取り、それぞれ個別にページを開いて
    タイトル・サムネイルを取得する。DETAIL_FETCH_LIMIT件を超える分は
    詳細取得を行わず、URLをタイトルとして返す(重くなりすぎるのを防ぐため)。
    """
    if not viewkey_urls:
        return []

    targets = viewkey_urls[:DETAIL_FETCH_LIMIT]
    remaining = viewkey_urls[DETAIL_FETCH_LIMIT:]

    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_video_detail(session, url, semaphore) for url in targets]
        results = await asyncio.gather(*tasks)

    results = list(results)
    for url in remaining:
        results.append(VideoResult(title=url, url=url, thumbnail=None))

    return results


# ---------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------

async def search(url_template: str, query: str, selector: Optional[str] = None) -> SearchResponse:
    """
    サイト内検索を実行する。

    1. 検索結果ページを取得
    2. 通常のリンク結果・画像を抽出
    3. viewkey= を含むリンクがあれば、それぞれ個別に開いてタイトル・サムネイルを取得

    ネットワークエラー(接続失敗・404・410など)は例外として呼び出し側に伝える。
    """
    search_url = build_search_url(url_template, query)

    async with aiohttp.ClientSession() as session:
        html_text = await _fetch_html(session, search_url, REQUEST_TIMEOUT)

    soup = BeautifulSoup(html_text, "html.parser")

    links = extract_links(soup, search_url, selector)
    images = extract_page_images(soup, search_url)

    viewkey_urls = find_viewkey_links(soup, search_url)
    videos = await fetch_video_details(viewkey_urls) if viewkey_urls else []

    return SearchResponse(links=links, images=images, videos=videos)
