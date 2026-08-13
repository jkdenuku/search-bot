"""
サイト内検索Discordボット。

コマンド:
  /setting add name:<表示名> url_template:<検索URL、{query}を含む> selector:<省略可>
  /setting list
  /setting remove name:<表示名>
  /search site:<登録済みサイト名> query:<検索語>

必要な環境変数:
  DISCORD_BOT_TOKEN : Discord Developer Portalで発行したBotトークン
  PORT              : (Render Web Service用) ヘルスチェック用HTTPサーバーの待受ポート。
                       Renderが自動的に設定するので通常は手動設定不要。

起動方法:
  pip install -r requirements.txt
  export DISCORD_BOT_TOKEN="your-token-here"
  python bot.py

補足:
  Render の Web Service で動かす場合、HTTPポートを開いてヘルスチェックに
  応答する必要があるため、discordのBotとは別にダミーのHTTPサーバーを
  同じプロセス内で並行起動しています(下部の run_web_server 参照)。
"""

import os
import asyncio

from aiohttp import web

import discord
from discord import app_commands
from discord.ext import commands

import storage
import scraper

INTENTS = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=INTENTS)


# ---------------------------------------------------------------------
# Render の Web Service 用ヘルスチェックサーバー
# (Discord Botとは無関係。Renderがポートを検知できるようにするためだけの処理)
# ---------------------------------------------------------------------

async def handle_health_check(request):
    return web.Response(text="Bot is running")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    print(f"ヘルスチェック用サーバーをポート {port} で起動しました")


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"スラッシュコマンドを {len(synced)} 件同期しました")
    except Exception as e:
        print(f"コマンド同期に失敗しました: {e}")
    print(f"ログイン完了: {bot.user}")


# ---------------------------------------------------------------------
# /setting グループ
# ---------------------------------------------------------------------

setting_group = app_commands.Group(name="setting", description="検索対象サイトの登録・管理")


@setting_group.command(name="add", description="検索対象サイトを登録します")
@app_commands.describe(
    name="サイトの表示名(検索コマンドで選ぶときの名前)",
    url_template="検索用URL。キーワード部分を {query} と書いてください (例: https://example.com/search?q={query})",
    selector="任意。検索結果のリンクを示すCSSセレクタ。わからなければ空欄でOKです",
)
async def setting_add(
    interaction: discord.Interaction,
    name: str,
    url_template: str,
    selector: str = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("このコマンドはサーバー内でのみ使えます。", ephemeral=True)
        return

    if "{query}" not in url_template:
        await interaction.response.send_message(
            "url_template には検索語を埋め込む位置として `{query}` を含めてください。\n"
            "例: `https://example.com/search?q={query}`",
            ephemeral=True,
        )
        return

    storage.add_site(interaction.guild.id, name, url_template, selector)

    mode = "CSSセレクタ指定(高精度)" if selector else "簡易抽出(セレクタ未指定)"
    await interaction.response.send_message(
        f"サイト「{name}」を登録しました。({mode})\n"
        f"検索URL: `{url_template}`" + (f"\nセレクタ: `{selector}`" if selector else ""),
        ephemeral=True,
    )


@setting_group.command(name="list", description="登録済みの検索対象サイト一覧を表示します")
async def setting_list(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("このコマンドはサーバー内でのみ使えます。", ephemeral=True)
        return

    sites = storage.list_sites(interaction.guild.id)
    if not sites:
        await interaction.response.send_message("まだサイトが登録されていません。`/setting add` で登録してください。", ephemeral=True)
        return

    lines = []
    for name, conf in sites.items():
        mode = "CSSセレクタ指定" if conf.get("selector") else "簡易抽出"
        lines.append(f"・**{name}** ({mode}) — `{conf['url_template']}`")

    await interaction.response.send_message("**登録済みサイト一覧**\n" + "\n".join(lines), ephemeral=True)


@setting_group.command(name="remove", description="登録済みサイトを削除します")
@app_commands.describe(name="削除するサイトの表示名")
async def setting_remove(interaction: discord.Interaction, name: str):
    if interaction.guild is None:
        await interaction.response.send_message("このコマンドはサーバー内でのみ使えます。", ephemeral=True)
        return

    removed = storage.remove_site(interaction.guild.id, name)
    if removed:
        await interaction.response.send_message(f"サイト「{name}」を削除しました。", ephemeral=True)
    else:
        await interaction.response.send_message(f"サイト「{name}」は登録されていません。", ephemeral=True)


@setting_remove.autocomplete("name")
async def setting_remove_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild is None:
        return []
    sites = storage.list_sites(interaction.guild.id)
    return [
        app_commands.Choice(name=n, value=n)
        for n in sites.keys()
        if current.lower() in n.lower()
    ][:25]


bot.tree.add_command(setting_group)


# ---------------------------------------------------------------------
# /search コマンド用: ページネーション表示
#
# 表示は2種類のページ群に分かれる:
#   1. 通常のリンク結果ページ(links) — 見つかった場合のみ
#   2. viewkey動画の結果ページ(videos、サムネイル付き) — 見つかった場合のみ
# どちらも1ページ5件、ボタンで全ページを行き来できる。
# ---------------------------------------------------------------------

RESULTS_PER_PAGE = scraper.RESULTS_PER_PAGE


def _chunk(items: list, size: int) -> list:
    """リストをsize件ずつのページに分割する。0件なら空リストを返す。"""
    if not items:
        return []
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_embed(site: str, query: str, link_pages: list, video_pages: list, page_index: int) -> discord.Embed:
    """
    全ページ(通常リンクページ → 動画ページ の順に連結したもの)のうち、
    page_index 番目のページを表示するembedを組み立てる。
    """
    total_pages = len(link_pages) + len(video_pages)

    if page_index < len(link_pages):
        # 通常リンクのページ
        page_items = link_pages[page_index]
        embed = discord.Embed(
            title=f"🔗 「{query}」の検索結果 — {site}",
            color=discord.Color.blue(),
        )
        for r in page_items:
            title = r.title if len(r.title) <= 100 else r.title[:97] + "..."
            embed.add_field(name=title, value=r.url, inline=False)
    else:
        # 動画(viewkey)のページ
        video_page_index = page_index - len(link_pages)
        page_items = video_pages[video_page_index]
        embed = discord.Embed(
            title=f"🎬 「{query}」の動画結果 — {site}",
            color=discord.Color.orange(),
        )
        for r in page_items:
            title = r.title if len(r.title) <= 100 else r.title[:97] + "..."
            embed.add_field(name=title, value=r.url, inline=False)
        # このページの動画のうち、サムネイルがあるものを1枚embed画像として表示
        first_thumb = next((r.thumbnail for r in page_items if r.thumbnail), None)
        if first_thumb:
            embed.set_image(url=first_thumb)

    embed.set_footer(text=f"ページ {page_index + 1} / {total_pages}")
    return embed


class SearchResultsView(discord.ui.View):
    """検索結果のページ送りボタンを提供するView。実行者本人のみ操作可能。"""

    def __init__(self, author_id: int, site: str, query: str, link_pages: list, video_pages: list):
        super().__init__(timeout=300)  # 5分操作が無ければボタンを無効化
        self.author_id = author_id
        self.site = site
        self.query = query
        self.link_pages = link_pages
        self.video_pages = video_pages
        self.total_pages = len(link_pages) + len(video_pages)
        self.page = 0
        self._update_button_state()

    def _update_button_state(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("この検索結果はコマンドを実行した本人のみ操作できます。", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def _current_embed(self) -> discord.Embed:
        return build_embed(self.site, self.query, self.link_pages, self.video_pages, self.page)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_button_state()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._update_button_state()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)


@bot.tree.command(name="search", description="登録済みサイト内をキーワード検索します")
@app_commands.describe(site="検索対象のサイト(登録済みのものから選択)", query="検索キーワード")
async def search_command(interaction: discord.Interaction, site: str, query: str):
    if interaction.guild is None:
        await interaction.response.send_message("このコマンドはサーバー内でのみ使えます。", ephemeral=True)
        return

    site_conf = storage.get_site(interaction.guild.id, site)
    if site_conf is None:
        await interaction.response.send_message(
            f"サイト「{site}」は登録されていません。`/setting list` で確認してください。",
            ephemeral=True,
        )
        return

    # 本人にしか見えない応答にする
    await interaction.response.defer(ephemeral=True)

    try:
        response = await scraper.search(
            url_template=site_conf["url_template"],
            query=query,
            selector=site_conf.get("selector"),
        )
    except Exception as e:
        await interaction.followup.send(f"検索中にエラーが発生しました: `{e}`", ephemeral=True)
        return

    if not response.links and not response.videos and not response.images:
        await interaction.followup.send(f"「{query}」の検索結果が見つかりませんでした。(サイト: {site})", ephemeral=True)
        return

    link_pages = _chunk(response.links, RESULTS_PER_PAGE)
    video_pages = _chunk(response.videos, RESULTS_PER_PAGE)

    view = SearchResultsView(
        author_id=interaction.user.id,
        site=site,
        query=query,
        link_pages=link_pages,
        video_pages=video_pages,
    )
    embed = build_embed(site, query, link_pages, video_pages, page_index=0)

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # --- 一覧ページ内で見つかった画像(最大150件)を本人にのみ見える形で連続送信 ---
    for i, image_url in enumerate(response.images, start=1):
        image_embed = discord.Embed(
            title=f"関連画像 {i}/{len(response.images)}",
            color=discord.Color.green(),
        )
        image_embed.set_image(url=image_url)
        await interaction.followup.send(embed=image_embed, ephemeral=True)


@search_command.autocomplete("site")
async def search_site_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild is None:
        return []
    sites = storage.list_sites(interaction.guild.id)
    return [
        app_commands.Choice(name=n, value=n)
        for n in sites.keys()
        if current.lower() in n.lower()
    ][:25]


async def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "環境変数 DISCORD_BOT_TOKEN が設定されていません。\n"
            "export DISCORD_BOT_TOKEN=\"your-token-here\" を実行してから再度起動してください。"
        )

    # ヘルスチェック用サーバーとDiscord Botを同時に起動する
    await run_web_server()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
