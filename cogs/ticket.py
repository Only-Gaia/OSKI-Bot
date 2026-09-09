import asyncio
import discord
from discord import app_commands
from discord.ext import commands

import config
import data


# ================= SETTINGS HELPERS =================

MAX_STAFF_ROLES = 15


def get_guild_settings(guild_id: int) -> dict:
    settings = data.load("settings")
    guild_data = settings.setdefault(str(guild_id), {})
    guild_data.setdefault("staff_roles", [])
    guild_data.setdefault("ticket_counter", 0)
    return guild_data


def save_guild_settings(guild_id: int, guild_data: dict):
    settings = data.load("settings")
    settings[str(guild_id)] = guild_data
    data.save("settings", settings)


def get_next_ticket_number(guild_id: int) -> int:
    guild_data = get_guild_settings(guild_id)
    guild_data["ticket_counter"] += 1
    save_guild_settings(guild_id, guild_data)
    return guild_data["ticket_counter"]


def get_staff_roles(guild: discord.Guild) -> list[discord.Role]:
    guild_data = get_guild_settings(guild.id)
    roles = []
    for role_id in guild_data.get("staff_roles", []):
        role = guild.get_role(role_id)
        if role:
            roles.append(role)
    return roles


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    guild_data = get_guild_settings(member.guild.id)
    staff_role_ids = set(guild_data.get("staff_roles", []))
    return any(role.id in staff_role_ids for role in member.roles)


# ================= TOPIC HELPERS (stato ticket) =================
# Il canale del ticket tiene lo stato nel topic, così sopravvive ai riavvii
# del bot senza bisogno di un database: "ticket|opener:<id>|type:<label>|claimed:<id o 0>"

def build_topic(opener_id: int, ticket_type: str, claimed_id: int = 0) -> str:
    return f"ticket|opener:{opener_id}|type:{ticket_type}|claimed:{claimed_id}"


def parse_topic(topic: str) -> dict | None:
    if not topic or not topic.startswith("ticket|"):
        return None
    parts = topic.split("|")
    result = {}
    for part in parts[1:]:
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        result[key] = value
    try:
        result["opener"] = int(result.get("opener", 0))
        result["claimed"] = int(result.get("claimed", 0))
    except ValueError:
        result["opener"] = 0
        result["claimed"] = 0
    result["type"] = result.get("type", "Ticket")
    return result


# ================= CREAZIONE TICKET =================

async def create_ticket_channel(
    interaction: discord.Interaction,
    ticket_type_label: str,
    emoji: str,
    name_prefix: str,
):
    guild = interaction.guild
    opener = interaction.user

    staff_roles = get_staff_roles(guild)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
        ),
    }
    for role in staff_roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
        )

    number = get_next_ticket_number(guild.id)
    channel_name = f"{name_prefix}-{number:04d}"

    category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=build_topic(opener.id, ticket_type_label),
            reason=f"Ticket aperto da {opener} ({opener.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Non ho i permessi per creare il canale del ticket. Controlla i miei permessi.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"{emoji} {ticket_type_label}",
        description=(
            f"Ciao {opener.mention}, grazie per averci contattato!\n"
            "Un membro dello staff ti risponderà il prima possibile.\n\n"
            "Usa i pulsanti qui sotto per gestire questo ticket."
        ),
        color=config.EMBED_COLOR,
    )
    embed.set_footer(text=f"Ticket #{number:04d} • Aperto da {opener}")

    ping_content = opener.mention
    if staff_roles:
        ping_content += " " + " ".join(role.mention for role in staff_roles)

    await ticket_channel.send(
        content=ping_content,
        embed=embed,
        view=TicketControlView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )

    await interaction.response.send_message(
        f"✅ Il tuo ticket è stato creato: {ticket_channel.mention}", ephemeral=True
    )


# ================= MODAL: ADD USER =================

class AddUserModal(discord.ui.Modal, title="➕ Aggiungi utente al ticket"):
    user_input = discord.ui.TextInput(
        label="ID o menzione dell'utente",
        placeholder="Es. 123456789012345678 oppure @utente",
        required=True,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_input.value.strip().strip("<@!>")
        try:
            user_id = int(raw)
        except ValueError:
            await interaction.response.send_message("❌ ID utente non valido.", ephemeral=True)
            return

        member = interaction.guild.get_member(user_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(user_id)
            except discord.NotFound:
                await interaction.response.send_message("❌ Utente non trovato in questo server.", ephemeral=True)
                return

        channel = interaction.channel
        await channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True
        )
        await interaction.response.send_message(f"✅ {member.mention} è stato aggiunto al ticket.")


# ================= VIEW: CONTROLLI TICKET =================

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="✅", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Solo lo staff può claimare i ticket.", ephemeral=True)
            return

        info = parse_topic(interaction.channel.topic)
        if info is None:
            await interaction.response.send_message("❌ Questo canale non è un ticket valido.", ephemeral=True)
            return

        if info["claimed"]:
            claimer = interaction.guild.get_member(info["claimed"])
            claimer_text = claimer.mention if claimer else f"<@{info['claimed']}>"
            await interaction.response.send_message(f"⚠️ Questo ticket è già stato claimato da {claimer_text}.", ephemeral=True)
            return

        await interaction.channel.edit(topic=build_topic(info["opener"], info["type"], interaction.user.id))
        await interaction.response.send_message(f"✅ Ticket claimato da {interaction.user.mention}.")

    @discord.ui.button(label="Unclaim", emoji="❌", style=discord.ButtonStyle.danger, custom_id="ticket_unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Solo lo staff può gestire i ticket.", ephemeral=True)
            return

        info = parse_topic(interaction.channel.topic)
        if info is None:
            await interaction.response.send_message("❌ Questo canale non è un ticket valido.", ephemeral=True)
            return

        if not info["claimed"]:
            await interaction.response.send_message("⚠️ Questo ticket non è claimato da nessuno.", ephemeral=True)
            return

        await interaction.channel.edit(topic=build_topic(info["opener"], info["type"], 0))
        await interaction.response.send_message(f"❌ Ticket unclaimato da {interaction.user.mention}.")

    @discord.ui.button(label="Add user", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="ticket_adduser")
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Solo lo staff può aggiungere utenti al ticket.", ephemeral=True)
            return
        await interaction.response.send_modal(AddUserModal())

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = parse_topic(interaction.channel.topic)
        is_opener = info is not None and interaction.user.id == info["opener"]

        if not is_staff(interaction.user) and not is_opener:
            await interaction.response.send_message("❌ Non hai i permessi per chiudere questo ticket.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Chiusura del ticket in corso...")
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Ticket chiuso da {interaction.user}")
        except discord.NotFound:
            pass


# ================= SELECT: SUPPORT PANEL =================

SUPPORT_OPTIONS = [
    ("assistenza_generale", "❓", "Assistenza Generale", "assistenza"),
    ("riscatta_giveaway", "🎁", "Riscatta Giveaway", "giveaway"),
    ("consiglio_server", "💡", "Consiglio Server", "consiglio"),
    ("candidatura_staff", "📋", "Candidatura Staff", "candidatura"),
    ("segnala_utente", "🚨", "Segnala Utente", "segnalazione"),
]

BASE_OPTIONS = [
    ("base_candy", "🍬", "Base Candy", "base-candy"),
    ("base_lava", "🌋", "Base Lava", "base-lava"),
    ("base_nucleare", "☣️", "Base Nucleare", "base-nucleare"),
    ("base_divina", "🪽", "Base Divina", "base-divina"),
    ("base_cursed", "👹", "Base cursed", "base-cursed"),
]


class TicketSelect(discord.ui.Select):
    def __init__(self, options_data, placeholder):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji)
            for key, emoji, label, _ in options_data
        ]
        self.options_data = {key: (emoji, label, prefix) for key, emoji, label, prefix in options_data}
        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"ticket_select_{placeholder}",
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        emoji, label, prefix = self.options_data[key]
        await create_ticket_channel(interaction, label, emoji, prefix)
        # reset la select per il prossimo utente
        self.values.clear()


class SupportPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(SUPPORT_OPTIONS, "📂 Scegli il motivo del ticket..."))


class BasePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(BASE_OPTIONS, "🎨 Scegli il tipo di base..."))


# ================= BUTTON: PARTNER / MM PANEL =================

class PartnerPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Richiedi Partnership", emoji="🤝", style=discord.ButtonStyle.primary, custom_id="panel_partner_open"
    )
    async def open_partner(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket_channel(interaction, "Richiesta Partnership", "🤝", "partnership")


class MMPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Richiedi MM", emoji="👮‍♂️", style=discord.ButtonStyle.primary, custom_id="panel_mm_open"
    )
    async def open_mm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket_channel(interaction, "Richiesta MM", "👮‍♂️", "mm")


# ================= COG =================

class Ticket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- PANNELLI ----------

    @commands.hybrid_command(name="supportpanel", description="Crea il pannello ticket di assistenza")
    @commands.has_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def supportpanel(self, ctx: commands.Context):
        embed = discord.Embed(
            title="‼️ TICKET ASSISTENZA ‼️",
            description=(
                "Qui puoi richiedere:\n\n"
                "❓ | Assistenza Generale\n"
                "🎁 | Riscatta Giveaway\n"
                "💡 | Consiglio Server\n"
                "📋 | Candidatura Staff\n"
                "🚨 | Segnala Utente\n\n"
                "⚠️ATTENZIONE⚠️ Teniamo il diritto di chiudere i ticket aperti inutilmente "
                "o lasciati in sospeso da troppo tempo"
            ),
            color=config.EMBED_COLOR,
        )
        await ctx.send(embed=embed, view=SupportPanelView())
        if ctx.interaction:
            await ctx.interaction.followup.send("✅ Pannello creato.", ephemeral=True)

    @commands.hybrid_command(name="partnerpanel", description="Crea il pannello ticket per le partnership")
    @commands.has_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def partnerpanel(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🤝 PANNELLO PARTNERSHIP 🤝",
            description=(
                "Fai in modo che la tua partnership rispetti i nostri requisiti e se li rispetta "
                "puoi aprire ticket tranquillamente\n\n"
                "⚠️ATTENZIONE⚠️ Teniamo il diritto di chiudere i ticket aperti inutilmente "
                "o lasciati in sospeso da troppo tempo"
            ),
            color=config.EMBED_COLOR,
        )
        await ctx.send(embed=embed, view=PartnerPanelView())
        if ctx.interaction:
            await ctx.interaction.followup.send("✅ Pannello creato.", ephemeral=True)

    @commands.hybrid_command(name="basepanel", description="Crea il pannello ticket per le basi")
    @commands.has_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def basepanel(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎨 COLORA LA TUA BASE 🎨",
            description=(
                "Noi facciamo queste basi:\n\n"
                "🍬 | Base Candy\n"
                "🌋 | Base Lava\n"
                "☣️ | Base Nucleare\n"
                "🪽 | Base Divina\n"
                "👹 | Base cursed\n\n"
                "⚠️ATTENZIONE⚠️ Teniamo il diritto di chiudere i ticket aperti inutilmente "
                "o lasciati in sospeso da troppo tempo"
            ),
            color=config.EMBED_COLOR,
        )
        await ctx.send(embed=embed, view=BasePanelView())
        if ctx.interaction:
            await ctx.interaction.followup.send("✅ Pannello creato.", ephemeral=True)

    @commands.hybrid_command(name="mmpanel", description="Crea il pannello ticket per le richieste MM")
    @commands.has_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def mmpanel(self, ctx: commands.Context):
        embed = discord.Embed(
            title="👮‍♂️ RICHIEDI MM 👮‍♂️",
            description=(
                "Se apri un ticket per richiedere MM, assicurati di specificare il tipo di rarità "
                "del brainrot che devi scambiare\n\n"
                "⚠️ATTENZIONE⚠️ Teniamo il diritto di chiudere i ticket aperti inutilmente "
                "o lasciati in sospeso da troppo tempo"
            ),
            color=config.EMBED_COLOR,
        )
        await ctx.send(embed=embed, view=MMPanelView())
        if ctx.interaction:
            await ctx.interaction.followup.send("✅ Pannello creato.", ephemeral=True)

    # ---------- CONFIG RUOLI STAFF ----------

    @commands.hybrid_command(name="rolestaff", description="Aggiunge un ruolo staff (pingato nei ticket, max 15)")
    @commands.has_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(role="Il ruolo da aggiungere come ruolo staff")
    async def rolestaff(self, ctx: commands.Context, role: discord.Role):
        guild_data = get_guild_settings(ctx.guild.id)
        staff_roles = guild_data["staff_roles"]

        if role.id in staff_roles:
            await ctx.send(f"⚠️ Il ruolo {role.mention} è già configurato come ruolo staff.")
            return

        if len(staff_roles) >= MAX_STAFF_ROLES:
            await ctx.send(f"❌ Hai già raggiunto il limite massimo di {MAX_STAFF_ROLES} ruoli staff.")
            return

        staff_roles.append(role.id)
        save_guild_settings(ctx.guild.id, guild_data)
        await ctx.send(f"✅ Il ruolo {role.mention} è stato aggiunto ai ruoli staff ({len(staff_roles)}/{MAX_STAFF_ROLES}).")

    @commands.hybrid_command(name="roleremove", description="Rimuove un ruolo staff configurato")
    @commands.has_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(role="Il ruolo da rimuovere dai ruoli staff")
    async def roleremove(self, ctx: commands.Context, role: discord.Role):
        guild_data = get_guild_settings(ctx.guild.id)
        staff_roles = guild_data["staff_roles"]

        if role.id not in staff_roles:
            await ctx.send(f"⚠️ Il ruolo {role.mention} non è configurato come ruolo staff.")
            return

        staff_roles.remove(role.id)
        save_guild_settings(ctx.guild.id, guild_data)
        await ctx.send(f"✅ Il ruolo {role.mention} è stato rimosso dai ruoli staff ({len(staff_roles)}/{MAX_STAFF_ROLES}).")


async def setup(bot: commands.Bot):
    # Registra le view persistenti così i pannelli e i controlli ticket
    # continuano a funzionare anche dopo un riavvio del bot.
    bot.add_view(SupportPanelView())
    bot.add_view(BasePanelView())
    bot.add_view(PartnerPanelView())
    bot.add_view(MMPanelView())
    bot.add_view(TicketControlView())
    await bot.add_cog(Ticket(bot))
