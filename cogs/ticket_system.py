import os
import json
import asyncio
import datetime
import discord
from discord.ext import commands

DATA_FILE = "tickets.json"
BOT_OWNER_ID = 757990668357599302

def load_tickets():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_tickets(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

class TicketSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.category_id = int(os.getenv("CATEGORY_ID", 0))
        self.staff_role_name = os.getenv("STAFF_ROLE_NAME", "Staff")
        self.max_tickets = int(os.getenv("MAX_TICKETS", 12))

    def is_staff_or_owner(self, member: discord.Member, permission_attr: str = "manage_channels") -> bool:
        if member.id == BOT_OWNER_ID:
            return True
        if hasattr(member, "guild_permissions"):
            return getattr(member.guild_permissions, permission_attr, False)
        return False

    async def get_origin_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    async def convert_attachments(self, attachments):
        files = []
        for attachment in attachments:
            try:
                file = await attachment.to_file()
                files.append(file)
            except discord.HTTPException:
                pass
        return files

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if message.content.startswith("!"):
            return

        tickets = load_tickets()
        user_id = str(message.author.id)

        # 1. Staff replying from inside a staff ticket channel
        for uid, data in tickets.items():
            if data["staff_channel_id"] == message.channel.id:
                target_user = message.guild.get_member(int(uid))
                origin_channel = await self.get_origin_channel(data["origin_channel_id"])

                if origin_channel and target_user:
                    try:
                        staff_files = await self.convert_attachments(message.attachments)
                        content = f"{target_user.mention} {message.content}" if message.content else target_user.mention

                        await origin_channel.send(content=content, files=staff_files)
                        await message.add_reaction("✅")
                    except discord.Forbidden:
                        await message.add_reaction("❌")
                        await message.channel.send(
                            f"⚠️ **Warning:** Missing permission to send message to <#{data['origin_channel_id']}>."
                        )
                    except discord.HTTPException as e:
                        await message.add_reaction("❌")
                        await message.channel.send(f"⚠️ Failed to send message: {e}")
                else:
                    await message.channel.send("⚠️ Could not find original channel or target user.")
                return

        # 2. User mentioning Yangyang in public / voice chat
        if self.bot.user in message.mentions:
            clean_content = message.content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
            user_files = await self.convert_attachments(message.attachments)

            if user_id in tickets:
                # Active ticket exists -> Route message & files to staff channel
                staff_channel = self.bot.get_channel(tickets[user_id]["staff_channel_id"])
                if staff_channel:
                    tickets[user_id]["origin_channel_id"] = message.channel.id
                    save_tickets(tickets)

                    embed = discord.Embed(
                        description=clean_content or "*(Attachment attached)*",
                        color=discord.Color.blue()
                    )
                    embed.set_author(name=f"Update from {message.author}", icon_url=message.author.display_avatar.url)
                    
                    await staff_channel.send(embed=embed, files=user_files)
                    await message.add_reaction("📥")
                else:
                    # Clean up stale record if channel was manually deleted
                    del tickets[user_id]
                    save_tickets(tickets)
            
            # If user has no active ticket
            if user_id not in tickets:
                # --- CONCURRENCY GUARD: Check if ticket capacity is reached ---
                if len(tickets) >= self.max_tickets:
                    await message.channel.send(
                        f"⚠️ Sorry {message.author.mention}, I'm currently busy with **{len(tickets)}/{self.max_tickets}** active inquiries! "
                        "Please try again after some time."
                    )
                    await message.add_reaction("⏳")
                    return

                category = message.guild.get_channel(self.category_id)
                if not category or not isinstance(category, discord.CategoryChannel):
                    await message.channel.send("⚠️ Error: Ticket category not found or invalid CATEGORY_ID.")
                    return

                staff_role = discord.utils.get(message.guild.roles, name=self.staff_role_name)

                overwrites = {
                    message.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    message.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
                if staff_role:
                    overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                clean_username = message.author.name.lower().replace(" ", "-")
                channel_name = f"ticket-{clean_username}-{user_id[-4:]}"

                try:
                    staff_channel = await message.guild.create_text_channel(
                        name=channel_name,
                        category=category,
                        overwrites=overwrites
                    )

                    tickets[user_id] = {
                        "staff_channel_id": staff_channel.id,
                        "origin_channel_id": message.channel.id
                    }
                    save_tickets(tickets)

                    embed = discord.Embed(
                        title="New Inquiry",
                        description=clean_content or "*(Attachment attached)*",
                        color=discord.Color.green()
                    )
                    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
                    embed.set_footer(text="Type a message here to reply. Use !close to end ticket.")
                    
                    await staff_channel.send(
                        content=f"@here New ticket from {message.author.mention}", 
                        embed=embed, 
                        files=user_files
                    )
                    await message.add_reaction("🎫")

                except discord.HTTPException as e:
                    await message.channel.send(f"⚠️ Failed to create ticket channel: `{e}`")

    @commands.command()
    async def close(self, ctx):
        tickets = load_tickets()
        target_user_id = None
        origin_channel_id = None

        for uid, data in list(tickets.items()):
            if data["staff_channel_id"] == ctx.channel.id:
                target_user_id = uid
                origin_channel_id = data["origin_channel_id"]
                break

        if not target_user_id:
            await ctx.send("This command can only be used inside an active ticket channel.")
            return

        origin_channel = await self.get_origin_channel(origin_channel_id)

        if origin_channel:
            try:
                await origin_channel.send(f"<@{target_user_id}> Nice chatting with you!")
            except discord.Forbidden:
                await ctx.send("⚠️ Warning: Missing permission to send closing message in voice/text channel.")

        del tickets[target_user_id]
        save_tickets(tickets)

        await ctx.send("Closing ticket and deleting channel in 3 seconds...")
        await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(seconds=3))
        await ctx.channel.delete()

    @commands.command(name="closeall")
    async def close_all_tickets(self, ctx: commands.Context):
        if not self.is_staff_or_owner(ctx.author, "manage_channels"):
            await ctx.send("❌ You don't have permission to close all tickets.")
            return

        category = ctx.guild.get_channel(self.category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await ctx.send("❌ Ticket category not found or invalid CATEGORY_ID.")
            return

        tickets = load_tickets()
        ticket_channels = [ch for ch in category.text_channels if ch.name.startswith("ticket-")]

        if not ticket_channels and not tickets:
            await ctx.send("⚠️ No active ticket channels or records found to close.")
            return

        status_msg = await ctx.send(f"🔒 Closing **{len(ticket_channels)}** active ticket channel(s)... Please wait.")

        for uid, data in list(tickets.items()):
            origin_channel = await self.get_origin_channel(data.get("origin_channel_id", 0))
            if origin_channel:
                try:
                    await origin_channel.send(f"<@{uid}> Your inquiry ticket has been closed by staff.")
                except discord.Forbidden:
                    pass

        for channel in ticket_channels:
            try:
                await channel.delete()
                await asyncio.sleep(0.5)
            except discord.HTTPException:
                pass

        save_tickets({})

        try:
            await status_msg.edit(content="✅ Successfully closed and cleared all active tickets.")
        except discord.NotFound:
            pass

async def setup(bot):
    await bot.add_cog(TicketSystem(bot))