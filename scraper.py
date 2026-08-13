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
MAX_RESULTS = 8
MIN_LINK_TEXT_LEN = 4  # 簡易抽出時、これより短いテキストのリンクは除外


@dataclass
class SearchResult:
    title: str
    url: str


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


def search(url_template: str, query: str, selector: Optional[str] = None) -> List[SearchResult]:
    """
    サイト内検索を実行し、結果のリストを返す。
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
        results = _extract_with_selector(soup, selector, search_url)
        # セレクタで何も取れなければ簡易抽出にフォールバック
        if not results:
            results = _extract_generic(soup, search_url)
    else:
        results = _extract_generic(soup, search_url)

    return results
