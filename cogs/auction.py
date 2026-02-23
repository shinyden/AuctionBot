"""
cogs/auction.py – Auction search and info using hybrid_group (prefix + slash).

Field mapping (DB short name → meaning):
  mid  = message_id          aid  = auction_id
  ts   = unix_timestamp      pn   = pokemon_name
  lv   = level               sh   = shiny
  gx   = gmax                nat  = nature
  gen  = gender              hi   = held_item
  xp   = xp                  iv   = total_iv_percent
  hp   = iv_hp               atk  = iv_attack
  def  = iv_defense          spa  = iv_sp_atk
  spd  = iv_sp_def           spe  = iv_speed
  mv   = moves               bid  = winning_bid
  bdr  = bidder_id           sn   = seller_name
  sid  = seller_id
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

import config
from config import get_gender_emoji, REPLY
from filters import all_flags_help
from utils import (
    build_query, format_date, iv_line,
    format_winning_bid, format_winning_bid_long,
    shiny_prefix, get_pokemon_image_url,
)

# ─── DB connection ─────────────────────────────────────────────────────────────
_mongo = MongoClient(config.MONGO_URI)
_db    = _mongo[config.MONGO_DB_NAME]
_col   = _db[config.MONGO_COLLECTION]

# ─── Message URL template ──────────────────────────────────────────────────────
# Built from the stored message ID (mid); no need to store the full URL anymore.
_MSG_URL_TEMPLATE = "https://discord.com/channels/716390832034414685/766198531626106941/{mid}"


def _build_message_url(record: dict) -> str | None:
    mid = record.get("mid")
    if not mid:
        return None
    return _MSG_URL_TEMPLATE.format(mid=mid)


# ─────────────────────────────────────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH – single result line
# ─────────────────────────────────────────────────────────────────────────────

def _result_line(r: dict) -> str:
    auction_id = r.get("aid", "?")
    name       = r.get("pn") or "Unknown"
    level      = r.get("lv")
    level_s    = f"L{level}" if level is not None else "L???"
    shiny      = shiny_prefix(r)
    gender     = get_gender_emoji(r.get("gen"))
    iv         = r.get("iv")
    iv_s       = f"{iv:.2f}%" if iv is not None else "???%"
    bid_s      = format_winning_bid(r)
    date_s     = format_date(r)

    return (
        f"`#{auction_id}` {shiny}**{level_s} {name}** {gender}"
        f"　•　{iv_s}　•　`{bid_s}`　•　{date_s}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH VIEW  (factory, paginated)
# ─────────────────────────────────────────────────────────────────────────────

def create_search_view(
    user_id: int,
    query: dict,
    sort: list,
    total: int,
    query_str: str,
    current_page: int = 0,
) -> discord.ui.LayoutView:

    max_page     = max(0, (total - 1) // config.RESULTS_PER_PAGE)
    skip         = current_page * config.RESULTS_PER_PAGE
    results      = list(_col.find(query).sort(sort).skip(skip).limit(config.RESULTS_PER_PAGE))
    start        = skip + 1
    end          = skip + len(results)
    lines        = [_result_line(r) for r in results]
    results_text = "\n".join(lines) if lines else "_No results._"
    header_text  = f"**🔍 Auction Search** — _{query_str}_"
    footer_text  = (
        f"Showing {start}–{end} of {total:,}  •  "
        f"Page {current_page + 1}/{max_page + 1}"
    )

    class PrevBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="◀ Prev",
                custom_id="s_prev",
                disabled=(current_page == 0),
            )
        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != user_id:
                await interaction.response.send_message(
                    view=_error_view("❌ Not your search!"), ephemeral=True)
                return
            await interaction.response.edit_message(
                view=create_search_view(user_id, query, sort, total, query_str, current_page - 1))

    class NextBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="Next ▶",
                custom_id="s_next",
                disabled=(current_page >= max_page),
            )
        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != user_id:
                await interaction.response.send_message(
                    view=_error_view("❌ Not your search!"), ephemeral=True)
                return
            await interaction.response.edit_message(
                view=create_search_view(user_id, query, sort, total, query_str, current_page + 1))

    # Shiny check uses "sh" key
    accent    = config.SHINY_EMBED_COLOR if query.get("sh") else config.EMBED_COLOR
    has_pages = total > config.RESULTS_PER_PAGE

    inner: list = [
        discord.ui.TextDisplay(content=header_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=results_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=f"_{footer_text}_"),
    ]
    if has_pages:
        inner += [
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(PrevBtn(), NextBtn()),
        ]

    class SearchView(discord.ui.LayoutView):
        container = discord.ui.Container(*inner, accent_colour=accent)
        def __init__(self):
            super().__init__(timeout=180)

    return SearchView()


# ─────────────────────────────────────────────────────────────────────────────
# INFO VIEW
# ─────────────────────────────────────────────────────────────────────────────

def create_info_view(record: dict) -> discord.ui.LayoutView:
    name       = record.get("pn") or "Unknown"
    shiny      = record.get("sh", False)
    level      = record.get("lv")
    gender     = get_gender_emoji(record.get("gen"))
    nature     = record.get("nat") or "???"
    xp         = record.get("xp", "???")
    held       = record.get("hi") or "None"
    auction_id = record.get("aid", "?")
    bid_s      = format_winning_bid_long(record)
    bidder_id  = record.get("bdr")
    seller     = record.get("sn") or "Unknown"
    seller_id  = record.get("sid")
    date_s     = format_date(record)
    iv_tot     = record.get("iv")
    iv_tot_s   = f"{iv_tot:.2f}%" if iv_tot is not None else "???%"
    moves      = record.get("mv") or []
    msg_url    = _build_message_url(record)
    level_s    = str(level) if level is not None else "???"
    shiny_s    = shiny_prefix(record)
    img_url    = get_pokemon_image_url(name, shiny)
    accent     = config.SHINY_EMBED_COLOR if shiny else config.EMBED_COLOR
    bidder_s   = f"<@{bidder_id}>" if bidder_id else "Unknown"

    # Seller display: mention if we have their ID, otherwise just show name
    seller_s   = f"`{seller}`" if not seller_id else f"<@{seller_id}> (`{seller}`)"

    # ── Basic Info block ──────────────────────────────────────────────────────
    basic_text = (
        f"**📋 Basic Info**\n"
        f"{REPLY} **Name:** {shiny_s}{name}\n"
        f"{REPLY} **Level:** `{level_s}`\n"
        f"{REPLY} **Gender:** {gender}\n"
        f"{REPLY} **XP:** `{xp}`\n"
        f"{REPLY} **Held Item:** `{held}`\n"
        f"{REPLY} **Nature:** `{nature}`"
    )

    # ── Auction Info block ────────────────────────────────────────────────────
    auction_text = (
        f"**💰 Auction Info**\n"
        f"{REPLY} **Winning Bid:** `{bid_s}`\n"
        f"{REPLY} **Bidder:** {bidder_s}\n"
        f"{REPLY} **Seller:** {seller_s}\n"
        f"{REPLY} **Date:** `{date_s}`"
    )

    # ── IVs block ─────────────────────────────────────────────────────────────
    iv_text = (
        f"**📊 IVs** — `{iv_tot_s}` total\n"
        + iv_line("HP",  record.get("hp"))   + "\n"
        + iv_line("ATK", record.get("atk"))  + "\n"
        + iv_line("DEF", record.get("def"))  + "\n"
        + iv_line("SpA", record.get("spa"))  + "\n"
        + iv_line("SpD", record.get("spd"))  + "\n"
        + iv_line("Spe", record.get("spe"))
    )

    # ── Moves block ───────────────────────────────────────────────────────────
    moves_text = (
        "**⚔️ Moves**\n"
        + ("\n".join(f"{REPLY} {m}" for m in moves) if moves else "_None_")
    )

    # ── Build component list ──────────────────────────────────────────────────
    comps: list = [
        discord.ui.TextDisplay(content=f"## {shiny_s}Auction #{auction_id}"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
    ]

    if img_url:
        comps.append(discord.ui.Section(
            discord.ui.TextDisplay(content=basic_text),
            accessory=discord.ui.Thumbnail(media=img_url),
        ))
    else:
        comps.append(discord.ui.TextDisplay(content=basic_text))

    comps += [
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=auction_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=iv_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=moves_text),
    ]
    if msg_url:
        comps += [
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label="🔗 View Auction Log",
                    url=msg_url,
                )
            ),
        ]

    class InfoView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=accent)
        def __init__(self):
            super().__init__(timeout=300)

    return InfoView()


# ─────────────────────────────────────────────────────────────────────────────
# HELP VIEW
# ─────────────────────────────────────────────────────────────────────────────

def create_help_view() -> discord.ui.LayoutView:
    from categories import list_categories

    flag_lines = []
    for f in all_flags_help():
        arg_s   = " <value>" if f["takes_arg"] else ""
        aliases = ", ".join(f["aliases"][:3]) if f["aliases"] else ""
        flag_lines.append(f"{REPLY} `{f['flag']}{arg_s}` — {f['help']}")
        if aliases:
            flag_lines.append(f"　_aliases: {aliases}_")

    cat_lines = []
    for c in list_categories():
        aliases_s = ", ".join(c["aliases"][:4])
        cat_lines.append(f"{REPLY} `{c['key']}` **{c['name']}** — _{aliases_s}_")

    examples = (
        f"{REPLY} `j!a s --name Alcremie --gmax`\n"
        f"{REPLY} `j!a s --name pikachu --shiny --iv >90`\n"
        f"{REPLY} `j!a s --atkiv 31 --spdiv 31 --sort price`\n"
        f"{REPLY} `j!a s --evo bulbasaur`\n"
        f"{REPLY} `j!a s --category starters --iv >=85`\n"
        f"{REPLY} `j!a s --move fake out --level >50`\n"
        f"{REPLY} `j!a s --seller @user`\n"
        f"{REPLY} `j!a i 1544762`"
    )

    class HelpView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.TextDisplay(content="## 📖 Auction Bot — Help"),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content=(
                "**Commands:**\n"
                f"{REPLY} `j!a s [flags]` or `/auction search` — search auctions\n"
                f"{REPLY} `j!a i <id>` or `/auction info` — full auction info"
            )),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content="**🔍 Filters:**\n" + "\n".join(flag_lines)),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content="**📦 Categories (`--category`):**\n" + "\n".join(cat_lines)),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content="**💡 Examples:**\n" + examples),
            accent_colour=config.EMBED_COLOR,
        )
        def __init__(self):
            super().__init__(timeout=180)

    return HelpView()


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class Auction(commands.Cog):
    """Pokémon auction search and info – Components V2"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /auction  OR  j!auction / j!a ────────────────────────────────────────
    @commands.hybrid_group(name="auction", aliases=["a"], invoke_without_command=True)
    async def auction_group(self, ctx: commands.Context):
        """Pokémon auction commands"""
        await ctx.send(view=create_help_view())

    # ── /auction search  OR  j!a s ────────────────────────────────────────────
    @auction_group.command(name="search", aliases=["s"])
    @app_commands.describe(filters="Filters e.g: --name pikachu --shiny --iv >90 --sort price")
    async def auction_search(self, ctx: commands.Context, *, filters: str = ""):
        """Search past auctions with filters"""
        raw         = filters.split() if filters else []
        query, sort = build_query(raw)
        total       = _col.count_documents(query)

        if total == 0:
            await ctx.send(view=_error_view("❌ No auctions found matching your filters."))
            return

        query_str = filters.strip() or "All auctions"
        await ctx.send(view=create_search_view(ctx.author.id, query, sort, total, query_str, 0))

    # ── /auction info  OR  j!a i ──────────────────────────────────────────────
    @auction_group.command(name="info", aliases=["i"])
    @app_commands.describe(auction_id="The auction ID number")
    async def auction_info(self, ctx: commands.Context, auction_id: str = ""):
        """View full details of a specific auction"""
        if not auction_id:
            await ctx.send(
                view=_error_view("❌ Usage: `j!a i <auction_id>`"),
                reference=ctx.message,
                mention_author=False,
            )
            return
        try:
            aid = int(auction_id)
        except ValueError:
            await ctx.send(
                view=_error_view("❌ Invalid auction ID — must be a number."),
                reference=ctx.message,
                mention_author=False,
            )
            return

        record = _col.find_one({"aid": aid})
        if not record:
            await ctx.send(
                view=_error_view(f"❌ Auction `#{aid}` not found."),
                reference=ctx.message,
                mention_author=False,
            )
            return

        await ctx.send(
            view=create_info_view(record),
            reference=ctx.message,
            mention_author=False,
        )

    # ── /auction help  OR  j!a h ──────────────────────────────────────────────
    @auction_group.command(name="help", aliases=["h"])
    async def auction_help(self, ctx: commands.Context):
        """Show all available filters and examples"""
        await ctx.send(view=create_help_view())


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Auction(bot))
