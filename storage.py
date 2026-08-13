"""
サイト設定の保存・読込モジュール。
guild_id ごとに登録サイトを sites.json に保存する。

データ構造:
{
  "<guild_id>": {
    "<site_name>": {
      "url_template": "https://example.com/search?q={query}",
      "selector": "a.result-link"   # 省略時は None
    },
    ...
  },
  ...
}
"""

import json
import os
from typing import Optional

DATA_FILE = os.path.join(os.path.dirname(__file__), "sites.json")


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_site(guild_id: int, name: str, url_template: str, selector: Optional[str] = None) -> None:
    data = _load()
    gid = str(guild_id)
    data.setdefault(gid, {})
    data[gid][name] = {"url_template": url_template, "selector": selector}
    _save(data)


def remove_site(guild_id: int, name: str) -> bool:
    data = _load()
    gid = str(guild_id)
    if gid in data and name in data[gid]:
        del data[gid][name]
        _save(data)
        return True
    return False


def list_sites(guild_id: int) -> dict:
    data = _load()
    return data.get(str(guild_id), {})


def get_site(guild_id: int, name: str) -> Optional[dict]:
    return list_sites(guild_id).get(name)
