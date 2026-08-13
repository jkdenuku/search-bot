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
# /search コマンド
#
# 表示は3種類:
#   1. 通常のリンク結果(最大150件)を「表」形式のテキストにまとめて表示
#      (Discordの1embedの文字数制限があるため、必要に応じて複数embedに分割)
#   2. viewkey= を含む動画があれば、1件ずつタイトル・サムネイル付きのembedで表示(最大150件)
#   3. 一覧ページ内で見つかった画像があれば、1枚ずつembedで表示(最大150件)
# ---------------------------------------------------------------------

DISPLAY_LIMIT = 150          # 各カテゴリの表示件数上限
LINKS_PER_EMBED = 15         # リンク一覧embed 1通あたりに詰め込む件数(文字数制限対策)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _build_links_table_embeds(query: str, site: str, links: list) -> list:
    """
    通常のリンク結果を「番号. タイトル - URL」の表形式テキストにまとめ、
    LINKS_PER_EMBED件ごとに分割して複数のembedを作る。
    """
    embeds = []
    total = len(links)
    total_chunks = (total + LINKS_PER_EMBED - 1) // LINKS_PER_EMBED

    for chunk_index in range(total_chunks):
        start = chunk_index * LINKS_PER_EMBED
        chunk = links[start:start + LINKS_PER_EMBED]

        lines = []
        for i, r in enumerate(chunk, start=start + 1):
            title = _truncate(r.title, 60)
            lines.append(f"**{i}.** {title}\n{r.url}")

        embed = discord.Embed(
            title=f"🔗 「{query}」の検索結果 — {site}",
            description="\n\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"リンク {start + 1}〜{start + len(chunk)} / {total}件")
        embeds.append(embed)

    return embeds


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
        response = scraper.search(
            url_template=site_conf["url_template"],
            query=query,
            selector=site_conf.get("selector"),
        )
    except Exception as e:
        await interaction.followup.send(f"検索中にエラーが発生しました: `{e}`", ephemeral=True)
        return

    links = response.links[:DISPLAY_LIMIT]
    videos = response.videos[:DISPLAY_LIMIT]
    images = response.images[:DISPLAY_LIMIT]

    if not links and not videos and not images:
        await interaction.followup.send(f"「{query}」の検索結果が見つかりませんでした。(サイト: {site})", ephemeral=True)
        return

    # --- 1. 通常のリンク結果(最大150件)を表形式でまとめて表示 ---
    for embed in _build_links_table_embeds(query, site, links):
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- 2. viewkey動画(最大150件)を1件ずつ、タイトル・サムネイル付きで表示 ---
    for i, v in enumerate(videos, start=1):
        title = v.title if len(v.title) <= 256 else v.title[:253] + "..."
        video_embed = discord.Embed(
            title=title,
            url=v.url,
            description=v.url,
            color=discord.Color.orange(),
        )
        if v.thumbnail:
            video_embed.set_thumbnail(url=v.thumbnail)
        video_embed.set_footer(text=f"{i}/{len(videos)} — {site}")
        await interaction.followup.send(embed=video_embed, ephemeral=True)

    # --- 3. 画像(最大150件)を1枚ずつ表示 ---
    for i, image_url in enumerate(images, start=1):
        image_embed = discord.Embed(
            title=f"関連画像 {i}/{len(images)}",
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
