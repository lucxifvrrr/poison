import discord
import random
import asyncio
import pytz
import os
import time
import aiosqlite
import json
from datetime import datetime, timezone
from discord.ext import commands, tasks
from discord import ButtonStyle, ui, TextChannel
import logging
from logging.handlers import RotatingFileHandler
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import configuration
from cogs.giveaways.config import (
    REACTION_EMOJI, DOT_EMOJI, RED_DOT_EMOJI, EMBED_COLOR,
    CLEANUP_INTERVAL, ENTRIES_PER_PAGE, GiveawayConfig,
    MIN_GIVEAWAY_DURATION, MAX_GIVEAWAY_DURATION, MIN_WINNERS, MAX_WINNERS,
    DURATION_UNITS
)

def get_current_utc_timestamp():
    """Get current UTC timestamp as integer."""
    return int(time.time())

def get_utc_datetime():
    """Get current UTC datetime object."""
    return datetime.now(timezone.utc)

def format_time_display(timestamp, display_timezone='UTC'):
    """(Unused) Legacy formatting; we now use Discord native timestamps."""
    try:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        if display_timezone != 'UTC':
            try:
                dt = dt.astimezone(pytz.timezone(display_timezone))
            except pytz.UnknownTimeZoneError:
                pass

        time_part = dt.strftime("%I:%M %p").lstrip("0")
        today = get_utc_datetime()
        if display_timezone != 'UTC':
            try:
                today = today.astimezone(pytz.timezone(display_timezone))
            except pytz.UnknownTimeZoneError:
                pass

        return f"{dt.strftime('%A')} at {time_part}" if dt.date() > today.date() else f"Today at {time_part}"
    except Exception:
        return "Unknown time"

class EntriesView(ui.View):
    """Persistent view for displaying giveaway entries with pagination."""

    def __init__(self, message_id: str, db_manager):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.db = db_manager
        self.current_page = 0

    @ui.button(label="📊 Entries", style=ButtonStyle.secondary)
    async def entries_button(self, interaction: discord.Interaction, button: ui.Button):
        """Show entries for this giveaway."""
        button.custom_id = f"entries:{self.message_id}"
        await self.show_entries(interaction)

    async def show_entries(self, interaction: discord.Interaction, page: int = 0):
        """Display the entries embed with pagination."""
        try:
            await interaction.response.defer(ephemeral=True)

            giveaway = await self.db.fetchone(
                "SELECT * FROM giveaways WHERE message_id = ?", (self.message_id,)
            )
            if not giveaway:
                await interaction.followup.send("Giveaway not found!", ephemeral=True)
                return

            # Ensure we have the prize name - fix for sqlite3.Row not having .get() method
            prize_name = giveaway['prize'] if giveaway and 'prize' in giveaway.keys() else 'Unknown'

            participants = await self.db.fetchall(
                "SELECT * FROM participants WHERE message_id = ?", (self.message_id,)
            )
            fake_plan = await self.db.fetchone(
                "SELECT fake_participants FROM fake_reactions WHERE message_id = ?", (self.message_id,)
            )

            # Track unique user IDs to prevent duplicates
            unique_user_ids = set()
            all_participants = []
            bot_id = str(interaction.client.user.id) if interaction.client and interaction.client.user else "0"

            # Add real participants first (excluding bot)
            if participants:
                for p in participants:
                    user_id = p['user_id']
                    # Skip bot and already added users
                    if user_id != bot_id and user_id not in unique_user_ids:
                        unique_user_ids.add(user_id)
                        all_participants.append({
                            'id': user_id,
                            'type': 'real'
                        })

            # Add fake participants if they exist
            if fake_plan and fake_plan['fake_participants']:
                try:
                    import json
                    fake_ids = json.loads(fake_plan['fake_participants'])
                    if isinstance(fake_ids, list):
                        for fid in fake_ids:
                            # Extract the original user ID from fake ID to check for duplicates
                            original_id = fid.split('_fake_')[0] if '_fake_' in fid else fid
                            if original_id not in unique_user_ids:
                                unique_user_ids.add(original_id)
                                all_participants.append({
                                    'id': fid,
                                    'type': 'fake'
                                })
                except:
                    pass

            total = len(all_participants)
            if total == 0:
                embed = discord.Embed(
                    title="📊 Giveaway Entries",
                    description="No participants found for this giveaway.",
                    color=EMBED_COLOR
                )
                embed.add_field(name="Prize", value=prize_name, inline=False)
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            total_pages = max(1, (total + ENTRIES_PER_PAGE - 1) // ENTRIES_PER_PAGE)
            page = max(0, min(page, total_pages - 1))
            start = page * ENTRIES_PER_PAGE
            end = min(start + ENTRIES_PER_PAGE, total)
            slice_participants = all_participants[start:end]

            embed = discord.Embed(title="📊 Giveaway Entries", color=EMBED_COLOR)
            embed.add_field(name="Prize", value=prize_name, inline=False)
            
            text = ""
            for idx, part in enumerate(slice_participants, start=start + 1):
                uid = part['id']
                display = uid.split('_fake_')[0] if '_fake_' in uid else uid
                try:
                    user = interaction.client.get_user(int(display))
                    if user:
                        text += f"`{idx:3d}.` **{user.display_name}** (@{user.name})\n"
                    else:
                        text += f"`{idx:3d}.` User ID: {display}\n"
                except:
                    text += f"`{idx:3d}.` User ID: {display}\n"

            embed.description = text
            embed.set_footer(text=f"Page {page+1} of {total_pages} | {total} total entries")

            view = EntriesPaginationView(self.message_id, self.db, page, total_pages)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logging.error(f"Error showing entries: {e}")
            await interaction.followup.send("An error occurred while loading entries.", ephemeral=True)

class EntriesPaginationView(ui.View):
    """View for paginating through entries."""

    def __init__(self, message_id: str, db_manager, current_page: int, total_pages: int):
        super().__init__(timeout=300)
        self.message_id = message_id
        self.db = db_manager
        self.current_page = current_page
        self.total_pages = total_pages

        is_single = total_pages <= 1
        first_last_disabled = is_single
        prev_disabled = is_single or current_page == 0
        next_disabled = is_single or current_page == total_pages - 1

        self.first_page.disabled = first_last_disabled
        self.previous_page.disabled = prev_disabled
        self.next_page.disabled = next_disabled
        self.last_page.disabled = first_last_disabled

    @ui.button(label="⏪", style=ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, 0)

    @ui.button(label="◀️", style=ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, max(0, self.current_page - 1))

    @ui.button(label="▶️", style=ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, min(self.total_pages - 1, self.current_page + 1))

    @ui.button(label="⏩", style=ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, self.total_pages - 1)

class GiveawayEndedView(ui.View):
    """Persistent view for ended giveaways with participant count button."""

    def __init__(self, participant_count: int, message_id: str, db_manager, bot):
        super().__init__(timeout=None)
        self.participant_count = participant_count
        self.message_id = message_id
        self.db = db_manager
        self.bot = bot
        self.count_button.label = f"🎉 {participant_count}"

    @ui.button(label="", style=ButtonStyle.secondary, disabled=False, custom_id="count_persistent")
    async def count_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(f"<:sukoon_taaada:1324071825910792223> This giveaway had {self.participant_count} entries.", ephemeral=True)

    @ui.button(label="Entries", style=ButtonStyle.secondary, custom_id="entries_persistent")
    async def entries_button(self, interaction: discord.Interaction, button: ui.Button):
        view = EntriesView(self.message_id, self.db)
        await view.show_entries(interaction)

class DatabaseManager:
    """Manages aiosqlite interactions."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None
        self.connected = False

    async def init(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        old = 'giveaway_bot.db'
        if os.path.exists(old) and os.path.basename(self.db_path) == 'giveaway_bot.db' and os.path.dirname(self.db_path):
            try:
                if self.db:
                    await self.close()
                import shutil
                shutil.copy2(old, self.db_path)
            except Exception as e:
                logging.error(f"Error moving database: {e}")

        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        self.connected = True
        await self._create_tables()

    async def _create_tables(self):
        if not self.db:
            return
        await self.db.execute('''CREATE TABLE IF NOT EXISTS giveaways (
            message_id TEXT PRIMARY KEY,
            channel_id INTEGER,
            guild_id INTEGER,
            end_time INTEGER,
            winners_count INTEGER,
            prize TEXT,
            status TEXT,
            host_id INTEGER,
            created_at INTEGER,
            winner_ids TEXT,
            forced_winner_ids TEXT,
            error TEXT,
            ended_at INTEGER,
            rerolled_at INTEGER,
            rerolled_by INTEGER,
            cancelled_at INTEGER,
            cancelled_by INTEGER
        )''')
        await self.db.execute('''CREATE TABLE IF NOT EXISTS participants (
            message_id TEXT,
            user_id TEXT,
            joined_at INTEGER,
            is_forced INTEGER,
            is_fake INTEGER,
            original_user_id TEXT,
            PRIMARY KEY (message_id, user_id)
        )''')
        await self.db.execute('''CREATE TABLE IF NOT EXISTS fake_reactions (
            message_id TEXT PRIMARY KEY,
            channel_id INTEGER,
            total_reactions INTEGER,
            remaining_reactions INTEGER,
            end_time REAL,
            created_by INTEGER,
            created_at REAL,
            status TEXT,
            completed_at REAL,
            cancelled_at REAL,
            error TEXT,
            fake_participants TEXT
        )''')
        await self.db.execute('''CREATE TABLE IF NOT EXISTS giveaway_stats (
            guild_id INTEGER PRIMARY KEY,
            total_giveaways INTEGER DEFAULT 0,
            total_participants INTEGER DEFAULT 0,
            total_winners INTEGER DEFAULT 0,
            last_giveaway INTEGER
        )''')
        
        # Create indexes for better query performance
        await self.db.execute('CREATE INDEX IF NOT EXISTS idx_giveaways_status ON giveaways(status)')
        await self.db.execute('CREATE INDEX IF NOT EXISTS idx_giveaways_end_time ON giveaways(end_time)')
        await self.db.execute('CREATE INDEX IF NOT EXISTS idx_giveaways_guild ON giveaways(guild_id)')
        await self.db.execute('CREATE INDEX IF NOT EXISTS idx_participants_message ON participants(message_id)')
        await self.db.execute('CREATE INDEX IF NOT EXISTS idx_participants_user ON participants(user_id)')
        
        await self.db.commit()

    async def fetchone(self, query: str, params=()):
        if not self.db:
            return None
        async with self.db.execute(query, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, query: str, params=()):
        if not self.db:
            return []
        async with self.db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def execute(self, query: str, params=()):
        if not self.db:
            return
        await self.db.execute(query, params)
        await self.db.commit()

    async def executemany(self, query: str, seq_of_params):
        if not self.db:
            return
        await self.db.executemany(query, seq_of_params)
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None
            self.connected = False

class GiveawayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        os.makedirs('database', exist_ok=True)
        
        # Load configuration
        self.config = GiveawayConfig.from_env()
        db_path = self.config.db_path or os.path.join('database', 'giveaway_bot.db')

        # Logging: file only
        os.makedirs('logs', exist_ok=True)
        log_file = os.path.join('logs', 'giveaway_bot.log')
        self.logger = logging.getLogger('GiveawayBot')
        self.logger.handlers.clear()

        file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(file_handler)

        self.logger.propagate = False
        self.logger.setLevel(logging.ERROR)

        self.db = DatabaseManager(db_path)
        self._ready = asyncio.Event()
        self._checking_lock = asyncio.Lock()
        self.timezone = os.getenv('BOT_TIMEZONE', 'UTC')
        self.active_fake_reaction_tasks: Dict[str, asyncio.Task] = {}

    async def cog_load(self):
        await self.db.init()
        self.check_giveaways.start()
        self._ready.set()
        asyncio.create_task(self.register_persistent_views())

    async def register_persistent_views(self):
        if not self.db.connected:
            return
        try:
            ended = await self.db.fetchall(
                "SELECT * FROM giveaways WHERE status = ?", ("ended",)
            )
            for gw in ended:
                mid = gw['message_id']
                parts = await self.db.fetchall(
                    "SELECT user_id, is_fake FROM participants WHERE message_id = ?", (mid,)
                )
                bot_id = str(self.bot.user.id)
                real = sum(1 for p in parts if p['user_id'] != bot_id and ('is_fake' not in p or p['is_fake'] == 0))
                fake = sum(1 for p in parts if 'is_fake' in p and p['is_fake'] == 1)
                total = real + fake
                view = GiveawayEndedView(total, mid, self.db, self.bot)
                self.bot.add_view(view, message_id=int(mid))

        except Exception as e:
            self.logger.error(f"Error registering views: {e}")

    def cog_unload(self):
        self.check_giveaways.cancel()
        asyncio.create_task(self.db.close())

    async def check_bot_permissions(self, channel):
        if not channel or not hasattr(channel, 'guild') or not channel.guild or not hasattr(channel, 'permissions_for'):
            return False
        if not channel.guild.me:
            return False
        perms = channel.permissions_for(channel.guild.me)
        needed = {'send_messages', 'embed_links', 'add_reactions', 'read_message_history'}
        return all(getattr(perms, p, False) for p in needed)
    
    async def _update_guild_stats(self, guild_id: int, giveaways_delta: int = 0, 
                                  participants_delta: int = 0, winners_delta: int = 0) -> None:
        """Update guild statistics."""
        try:
            existing = await self.db.fetchone(
                "SELECT * FROM giveaway_stats WHERE guild_id = ?", (guild_id,)
            )
            
            if existing:
                await self.db.execute(
                    """UPDATE giveaway_stats 
                       SET total_giveaways = total_giveaways + ?,
                           total_participants = total_participants + ?,
                           total_winners = total_winners + ?,
                           last_giveaway = ?
                       WHERE guild_id = ?""",
                    (giveaways_delta, participants_delta, winners_delta, get_current_utc_timestamp(), guild_id)
                )
            else:
                await self.db.execute(
                    """INSERT INTO giveaway_stats (guild_id, total_giveaways, total_participants, total_winners, last_giveaway)
                       VALUES (?, ?, ?, ?, ?)""",
                    (guild_id, giveaways_delta, participants_delta, winners_delta, get_current_utc_timestamp())
                )
        except Exception as e:
            self.logger.error(f"Error updating guild stats: {e}")
    
    async def _verify_winner(self, guild: discord.Guild, user_id: str) -> Tuple[bool, Optional[discord.Member]]:
        """Verify if a winner is still valid (in server, not banned)."""
        if not self.config.enable_winner_verification:
            return True, None
        
        try:
            # Extract actual user ID (handle fake IDs)
            actual_id = int(user_id.split('_fake_')[0]) if '_fake_' in user_id else int(user_id)
            member = guild.get_member(actual_id)
            
            if not member:
                # Try fetching
                try:
                    member = await guild.fetch_member(actual_id)
                except:
                    return False, None
            
            return True, member
        except Exception as e:
            self.logger.error(f"Error verifying winner {user_id}: {e}")
            return False, None
    
    async def _send_winner_dm(self, user: discord.User, prize: str, guild_name: str) -> bool:
        """Send DM notification to winner."""
        if not self.config.enable_dm_notifications:
            return False
        
        try:
            embed = discord.Embed(
                title="🎉 Congratulations! You Won!",
                description=f"You won **{prize}** in {guild_name}!",
                color=EMBED_COLOR,
                timestamp=get_utc_datetime()
            )
            embed.set_footer(text="Contact the server staff to claim your prize")
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            return False
        except Exception as e:
            self.logger.error(f"Error sending DM to {user.id}: {e}")
            return False

    @tasks.loop(seconds=CLEANUP_INTERVAL)
    async def check_giveaways(self):
        await self._ready.wait()
        if not self.db.connected:
            return
        async with self._checking_lock:
            try:
                now = get_current_utc_timestamp()
                act = await self.db.fetchall(
                    "SELECT message_id FROM giveaways WHERE end_time <= ? AND status = ?",
                    (now, "active")
                )
                for row in act:
                    await self.end_giveaway(row['message_id'])
            except Exception as e:
                self.logger.error(f"Error in check_giveaways: {e}")

    @discord.app_commands.command(name="giveaway", description="Start a new giveaway")
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def start_giveaway(
        self,
        interaction: discord.Interaction,
        duration: str,
        winners: int,
        prize: str
    ):
        try:
            await interaction.response.defer(ephemeral=True)
            if not interaction.channel or not isinstance(interaction.channel, TextChannel):
                return await interaction.followup.send("Giveaways can only be started in text channels.", ephemeral=True)
            if not await self.check_bot_permissions(interaction.channel):
                return await interaction.followup.send("I need proper permissions.", ephemeral=True)
            if not self.config.min_winners <= winners <= self.config.max_winners:
                raise ValueError(f"Winners must be between {self.config.min_winners} and {self.config.max_winners}.")

            import re
            pattern = r'(\d+)([smhdw])'
            matches = re.findall(pattern, duration.lower())
            if not matches:
                raise ValueError("Use formats like: 30s, 1h, 1h30m, 2d5h30m, 1w")
            secs = sum(int(n)*DURATION_UNITS[u] for n, u in matches)
            if not self.config.min_duration <= secs <= self.config.max_duration:
                raise ValueError(f"Duration must be between {self.config.min_duration}s and {self.config.max_duration}s.")

            end_ts = get_current_utc_timestamp() + secs
            await interaction.channel.send("**<:sukoon_taaada:1324071825910792223> GIVEAWAY <:sukoon_taaada:1324071825910792223>**")

            def fmt_dur(sec):
                parts = []
                for unit_sec, label in [(86400,'d'),(3600,'h'),(60,'m')]:
                    if sec >= unit_sec:
                        cnt, sec = divmod(sec, unit_sec)
                        parts.append(f"{cnt}{label}")
                if sec:
                    parts.append(f"{sec}s")
                return " ".join(parts) or "0s"
            dur_disp = fmt_dur(secs)
            icon = interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None

            embed = discord.Embed(
                description=(
                    f"{DOT_EMOJI} Ends: <t:{end_ts}:R> (in {dur_disp})\n"
                    f"{DOT_EMOJI} Hosted by: {interaction.user.mention}"
                ),
                color=EMBED_COLOR,
                timestamp=datetime.fromtimestamp(end_ts, timezone.utc)
            )
            embed.set_author(name=prize, icon_url=icon)

            msg = await interaction.channel.send(embed=embed)
            await msg.add_reaction(REACTION_EMOJI)

            if self.db.connected:
                await self.db.execute(
                    "INSERT INTO giveaways (message_id, channel_id, guild_id, end_time, winners_count, prize, status, host_id, created_at, winner_ids, forced_winner_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(msg.id), interaction.channel.id, interaction.guild.id, end_ts, winners, prize, 'active', interaction.user.id, get_current_utc_timestamp(), json.dumps([]), json.dumps([]))
                )
                
                # Update statistics
                if self.config.enable_statistics:
                    await self._update_guild_stats(interaction.guild.id, giveaways_delta=1)
                
                await interaction.followup.send("Giveaway started!", ephemeral=True)
            else:
                await interaction.followup.send("Giveaway started! (No database)", ephemeral=True)

        except ValueError as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
        except Exception as e:
            self.logger.error(f"Error starting giveaway: {e}")
            await interaction.followup.send("Unexpected error.", ephemeral=True)

    async def end_giveaway(self, message_id: str):
        try:
            gw = await self.db.fetchone(
                "SELECT * FROM giveaways WHERE message_id = ? AND status = ?", (message_id, "active")
            )
            if not gw:
                return

            chan = self.bot.get_channel(gw['channel_id'])
            msg = await chan.fetch_message(int(message_id))
            if not await self.check_bot_permissions(chan):
                await self.db.execute(
                    "UPDATE giveaways SET status = ?, error = ? WHERE message_id = ?",
                    ("error", "Missing permissions", message_id)
                )
                return

            # Get real participants (excluding bot)
            parts = await self.db.fetchall(
                "SELECT user_id, is_fake FROM participants WHERE message_id = ?", (message_id,)
            )
            bot_id = str(self.bot.user.id)
            valid = [p['user_id'] for p in parts if p['user_id'] != bot_id and p.get('is_fake', 0) == 0]
            
            # Count fake entries that were successfully added
            fake_count = sum(1 for p in parts if p.get('is_fake', 0) == 1)

            # Calculate total participants (real + fake)
            total_participants = len(valid) + fake_count

            forced = json.loads(gw['forced_winner_ids']) if gw['forced_winner_ids'] else []
            winners = []
            verified_winners = []
            
            # Select winners and verify them
            if forced:
                winners = forced[:]
                remaining = [u for u in valid if u not in forced]
                winners += random.sample(remaining, max(0, min(len(remaining), gw['winners_count'] - len(winners))))
            else:
                winners = random.sample(valid, min(len(valid), gw['winners_count'])) if valid else []
            
            # Verify winners and send DMs
            for winner_id in winners:
                is_valid, member = await self._verify_winner(chan.guild, winner_id)
                if is_valid:
                    verified_winners.append(winner_id)
                    if member and self.config.enable_dm_notifications:
                        await self._send_winner_dm(member, gw['prize'], chan.guild.name)
                else:
                    pass
            
            # If some winners were invalid, try to reroll
            if len(verified_winners) < len(winners) and len(verified_winners) < gw['winners_count']:
                remaining = [u for u in valid if u not in verified_winners]
                needed = min(len(remaining), gw['winners_count'] - len(verified_winners))
                if needed > 0:
                    extra = random.sample(remaining, needed)
                    for winner_id in extra:
                        is_valid, member = await self._verify_winner(chan.guild, winner_id)
                        if is_valid:
                            verified_winners.append(winner_id)
                            if member and self.config.enable_dm_notifications:
                                await self._send_winner_dm(member, gw['prize'], chan.guild.name)

            mentions = [f"<@{w.split('_fake_')[0]}>" for w in verified_winners] or ["No winners."]
            now_ts = get_current_utc_timestamp()
            icon = chan.guild.icon.url if chan.guild and chan.guild.icon else None

            embed = discord.Embed(
                description=(
                    f"{DOT_EMOJI} Ended: <t:{now_ts}:R>\n"
                    f"{RED_DOT_EMOJI} Winners: {', '.join(mentions)}\n"
                    f"{DOT_EMOJI} Hosted by: <@{gw['host_id']}>"
                ),
                color=EMBED_COLOR,
                timestamp=datetime.fromtimestamp(now_ts, timezone.utc)
            )
            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
            embed.set_author(name=prize_name, icon_url=icon)

            view = GiveawayEndedView(total_participants, message_id, self.db, self.bot)

            await msg.clear_reactions()
            await msg.edit(embed=embed, view=view)
            if verified_winners:
                await msg.reply(f"{REACTION_EMOJI} Congratulations {', '.join(mentions)}! You won **{gw['prize']}**!")

            await self.db.execute(
                "UPDATE giveaways SET status = ?, winner_ids = ?, ended_at = ? WHERE message_id = ?",
                ("ended", json.dumps(verified_winners), now_ts, message_id)
            )
            
            # Update statistics
            if self.config.enable_statistics:
                await self._update_guild_stats(
                    chan.guild.id,
                    participants_delta=total_participants,
                    winners_delta=len(verified_winners)
                )

        except Exception as e:
            self.logger.error(f"Error ending giveaway {message_id}: {e}")
            await self.db.execute(
                "UPDATE giveaways SET status = ?, error = ? WHERE message_id = ?",
                ("error", str(e), message_id)
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not self.db.connected or payload.user_id == self.bot.user.id:
            return
        gw = await self.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ? AND status = ?", (str(payload.message_id), "active")
        )
        if not gw or str(payload.emoji) != REACTION_EMOJI:
            return
        
        # Check if this user already exists in the participants table
        existing_participant = await self.db.fetchone(
            "SELECT * FROM participants WHERE message_id = ? AND user_id = ?",
            (str(payload.message_id), str(payload.user_id))
        )
        
        if not existing_participant:
            await self.db.execute(
                "INSERT INTO participants (message_id, user_id, joined_at, is_forced, is_fake, original_user_id) VALUES (?,?,?,?,?,?)",
                (str(payload.message_id), str(payload.user_id), get_current_utc_timestamp(), 0, 0, None)
            )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not self.db.connected or payload.user_id == self.bot.user.id:
            return
        gw = await self.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ?", (str(payload.message_id),)
        )
        if not gw or str(payload.emoji) != REACTION_EMOJI:
            return
        await self.db.execute(
            "DELETE FROM participants WHERE message_id = ? AND user_id = ?",
            (str(payload.message_id), str(payload.user_id))
        )

    @commands.command(name="reroll")
    @commands.has_permissions(manage_guild=True)
    async def reroll_giveaway(self, ctx):
        if not self.db.connected:
            return await ctx.send("DB not connected.", ephemeral=True)
        if not ctx.message.reference:
            return await ctx.send("Reply to a giveaway message to reroll.", ephemeral=True)
        try:
            orig = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            gw = await self.db.fetchone(
                "SELECT * FROM giveaways WHERE message_id = ?", (str(orig.id),)
            )
            if not gw:
                return await ctx.send("Giveaway not found.", ephemeral=True)

            if gw['status'] == 'active':
                await self.end_giveaway(str(orig.id))

            # Get real participants (excluding bot)
            parts = await self.db.fetchall(
                "SELECT user_id, is_fake FROM participants WHERE message_id = ?", (str(orig.id),)
            )
            bot_id = str(self.bot.user.id)
            valid = [p['user_id'] for p in parts if p['user_id'] != bot_id and p.get('is_fake', 0) == 0]
            
            # Count fake entries that were successfully added
            fake_count = sum(1 for p in parts if p.get('is_fake', 0) == 1)

            # Calculate total participants (real + fake)
            total_participants = len(valid) + fake_count

            import json
            prev = json.loads(gw['winner_ids']) if gw['winner_ids'] else []
            remaining = [u for u in valid if u not in prev]
            if not remaining:
                return await ctx.send("No participants left for reroll.", ephemeral=True)

            new = random.sample(remaining, min(len(remaining), gw['winners_count']))
            mentions = [f"<@{u}>" for u in new]
            now_ts = get_current_utc_timestamp()
            icon = ctx.guild.icon.url if ctx.guild and ctx.guild.icon else None

            embed = discord.Embed(
                description=(
                    f"{DOT_EMOJI} Rerolled: <t:{now_ts}:R>\n"
                    f"{RED_DOT_EMOJI} Winners: {', '.join(mentions)}\n"
                    f"{DOT_EMOJI} Hosted by: <@{gw['host_id']}>"
                ),
                color=EMBED_COLOR,
                timestamp=datetime.fromtimestamp(now_ts, timezone.utc)
            )
            # Make sure we use the correct prize name from the giveaway data
            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
            embed.set_author(name=prize_name, icon_url=icon)

            view = GiveawayEndedView(total_participants, str(orig.id), self.db, self.bot)
            await orig.edit(embed=embed, view=view)

            await self.db.execute(
                "UPDATE giveaways SET winner_ids = ?, rerolled_at = ?, rerolled_by = ? WHERE message_id = ?",
                (json.dumps(new), now_ts, ctx.author.id, str(orig.id))
            )
            await ctx.send(f"{REACTION_EMOJI} Congratulations {', '.join(mentions)}! You won **{gw['prize']}**!")
        except Exception as e:
            self.logger.error(f"Error rerolling: {e}")
            await ctx.send(f"Error rerolling: {e}", ephemeral=True)

    @discord.app_commands.command(name="giveaway_stats", description="View giveaway statistics for this server")
    @discord.app_commands.guild_only()
    async def giveaway_stats(self, interaction: discord.Interaction):
        """Display giveaway statistics for the current guild."""
        try:
            await interaction.response.defer(ephemeral=True)
            
            if not self.config.enable_statistics:
                return await interaction.followup.send(
                    "Statistics tracking is disabled.",
                    ephemeral=True
                )
            
            stats = await self.db.fetchone(
                "SELECT * FROM giveaway_stats WHERE guild_id = ?",
                (interaction.guild.id,)
            )
            
            if not stats:
                return await interaction.followup.send(
                    "No giveaway statistics found for this server.",
                    ephemeral=True
                )
            
            # Get active giveaways count
            active = await self.db.fetchall(
                "SELECT * FROM giveaways WHERE guild_id = ? AND status = ?",
                (interaction.guild.id, "active")
            )
            
            embed = discord.Embed(
                title="📊 Giveaway Statistics",
                color=EMBED_COLOR,
                timestamp=get_utc_datetime()
            )
            
            embed.add_field(
                name="Total Giveaways",
                value=f"**{stats['total_giveaways']}**",
                inline=True
            )
            embed.add_field(
                name="Active Giveaways",
                value=f"**{len(active)}**",
                inline=True
            )
            embed.add_field(
                name="Total Winners",
                value=f"**{stats['total_winners']}**",
                inline=True
            )
            embed.add_field(
                name="Total Participants",
                value=f"**{stats['total_participants']}**",
                inline=True
            )
            
            if stats['last_giveaway']:
                embed.add_field(
                    name="Last Giveaway",
                    value=f"<t:{stats['last_giveaway']}:R>",
                    inline=True
                )
            
            if interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.url)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            self.logger.error(f"Error displaying stats: {e}")
            await interaction.followup.send(
                f"Error retrieving statistics: {e}",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(GiveawayCog(bot))
