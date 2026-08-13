"""
サイト内検索の実行モジュール。

- url_template 内の {query} をキーワードで置換してリクエスト
- selector が指定されていれば、そのCSSセレクタで結果要素を抽出し、
  各要素の中の最初の <a> のテキストとhrefをタイトル・リンクとして扱う
  (要素自体がaタグの場合はそのまま使う)
- selector が指定されていなければ、ページ内の全 <a> タグを機械的に抽出し、
  テキストがある程度の長さを持つものを結果候補として返す(簡易版)
"""

import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 10
MAX_RESULTS = 5
MAX_IMAGES = 5
MAX_VIEWKEY_LINKS = 5
MIN_LINK_TEXT_LEN = 4  # 簡易抽出時、これより短いテキストのリンクは除外

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
VIEWKEY_MARKER = "viewkey="


@dataclass
class SearchResult:
    title: str
    url: str


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


def _extract_viewkey_links(soup: BeautifulSoup, base_url: str) -> List[SearchResult]:
    """
    ページ内の全リンクから、URLに 'viewkey=' を含むものだけを最大MAX_VIEWKEY_LINKS件抽出する。
    (例: 動画ページなど特定パラメータを含むURLだけを拾いたい場合に使用)
    """
    results: List[SearchResult] = []
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

        title = a_tag.get_text(strip=True) or full_url
        results.append(SearchResult(title=title, url=full_url))

        if len(results) >= MAX_VIEWKEY_LINKS:
            break

    return results


def search(url_template: str, query: str, selector: Optional[str] = None) -> SearchResponse:
    """
    サイト内検索を実行し、リンク結果(最大MAX_RESULTS件)、
    画像URL(最大MAX_IMAGES件)、viewkey=を含むリンク(最大MAX_VIEWKEY_LINKS件)を
    まとめて返す。
    ネットワークエラーやパース失敗時は例外を投げる(呼び出し側でハンドリング)。
    """
    search_url = build_search_url(url_template, query)

    response = requests.get(
        search_url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    if selector:
        links = _extract_with_selector(soup, selector, search_url)
        # セレクタで何も取れなければ簡易抽出にフォールバック
        if not links:
            links = _extract_generic(soup, search_url)
    else:
        links = _extract_generic(soup, search_url)

    images = _extract_images(soup, search_url)
    viewkey_links = _extract_viewkey_links(soup, search_url)

    return SearchResponse(links=links, images=images, viewkey_links=viewkey_links)
