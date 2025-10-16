# cogs/counting.py
# Production-Ready Counting Cog — All Issues Fixed
# - discord.py v2 (app_commands)
# - motor (async MongoDB) with optimized connection pooling
# - Rate limit handling with exponential backoff
# - Memory leak prevention with automatic cleanup
# - Comprehensive error handling and logging
# - Queue system for deletions to prevent rate limit hits
# - Emoji validation and fallback mechanisms
# - Channel existence validation
# - Local backup cleanup


import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import re
import json
import asyncio
import time
import logging
from datetime import datetime, timezone
from collections import deque
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument


load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL not found in .env")


# --- Configurable defaults ---
DEFAULT_EMOJI_CUSTOM = "<:ogs_tick:1427918161327558736>"  # Success tick emoji
CROSS_EMOJI_CUSTOM = "<:ogs_cross:1427918018196930642>"  # Error cross emoji
DEFAULT_EMOJI = "✅"  # Unicode fallback
CROSS_EMOJI = "❌"  # Unicode fallback
BACKUP_INTERVAL_HOURS = 24
BACKUP_FOLDER = os.path.join("database", "backups")
BACKUP_RETENTION_DAYS = 30
EMBED_COLOR = discord.Color(int("2f3136".lstrip("#"), 16))
DM_COOLDOWN_SECONDS = 3600  # 1 hour between DMs to banned users
CACHE_CLEANUP_HOURS = 6  # Clean up memory caches every 6 hours
DELETION_QUEUE_DELAY = 0.25  # 250ms between deletions (4 per second, safe margin)
MAX_CONCURRENT_DB_OPS = 20  # Limit concurrent database operations
os.makedirs(BACKUP_FOLDER, exist_ok=True)


# Regexes
PURE_INT_RE = re.compile(r"^\d+$")
NUMBER_ANY_RE = re.compile(r"\d+")
CUSTOM_EMOJI_RE = re.compile(r"<(a)?:(\w+):(\d+)>")


class CountingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Reuse existing MongoDB connection or create new one
        if hasattr(bot, 'mongo_client') and bot.mongo_client:
            self.mongo = bot.mongo_client
            self._owns_connection = False
            logging.info("Counting: Reusing existing MongoDB connection")
        else:
            # Optimized MongoDB connection with production settings
            self.mongo = AsyncIOMotorClient(
                MONGO_URL,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=45000,
                maxPoolSize=50,
                minPoolSize=5,  # Reduced to prevent connection overload
                maxIdleTimeMS=45000,
                retryWrites=True,
                retryReads=True
            )
            self._owns_connection = True
            # Store on bot for other cogs to reuse
            bot.mongo_client = self.mongo
            logging.info("Counting: Created new MongoDB connection")
        
        self.db = self.mongo["counting_bot"]
        self.coll = self.db["counting_data"]
        self.backups = self.db["backups"]
        
        # Caches and locks
        self.banned_cache = {}     # guild_id -> set(user_ids)
        self.locks = {}            # guild_id -> asyncio.Lock
        self._dm_sent = {}         # (guild_id, user_id) -> timestamp
        self._last_lock_cleanup = time.time()
        
        # Rate limit management
        self.deletion_queue = deque()  # Queue for message deletions
        self.db_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DB_OPS)
        
        # Start tasks
        self.bot.loop.create_task(self._ensure_indexes())
        self.backup_loop.start()
        self.cache_cleanup_loop.start()
        self.deletion_worker.start()


    async def _ensure_indexes(self):
        """Create database indexes for performance"""
        try:
            await self.coll.create_index("guild_id", unique=True)
            await self.coll.create_index([("guild_id", 1), ("banned", 1)])
            
            # TTL index: automatically delete backups older than retention period
            try:
                await self.backups.create_index(
                    "ts", 
                    expireAfterSeconds=BACKUP_RETENTION_DAYS * 86400
                )
            except Exception as idx_error:
                # Index exists with different options, drop and recreate
                if "IndexOptionsConflict" in str(idx_error) or "already exists" in str(idx_error):
                    await self.backups.drop_index("ts_1")
                    await self.backups.create_index(
                        "ts", 
                        expireAfterSeconds=BACKUP_RETENTION_DAYS * 86400
                    )
                else:
                    raise
            
            logging.info("Database indexes created successfully")
        except Exception as e:
            logging.warning(f"Index creation error: {e}")


    # ---------- Background Tasks ----------
    @tasks.loop(hours=BACKUP_INTERVAL_HOURS)
    async def backup_loop(self):
        """Periodic backup with local file cleanup"""
        try:
            await self.create_backup()
            await self.cleanup_old_local_backups()
        except Exception as e:
            logging.error(f"Backup error: {e}")


    @backup_loop.before_loop
    async def before_backup_loop(self):
        await self.bot.wait_until_ready()


    @tasks.loop(hours=CACHE_CLEANUP_HOURS)
    async def cache_cleanup_loop(self):
        """Clean up memory caches to prevent leaks"""
        try:
            current_time = time.time()
            
            # Clean up DM sent cache
            self._dm_sent = {
                k: v for k, v in self._dm_sent.items() 
                if current_time - v < DM_COOLDOWN_SECONDS * 2
            }
            
            # Clean up unused locks (guilds bot is no longer in)
            if current_time - self._last_lock_cleanup > 3600:
                guild_ids = {g.id for g in self.bot.guilds}
                self.locks = {
                    gid: lock for gid, lock in self.locks.items() 
                    if gid in guild_ids
                }
                self._last_lock_cleanup = current_time
            
            logging.info(f"Cache cleanup: {len(self._dm_sent)} DM records, {len(self.locks)} locks")
        except Exception as e:
            logging.error(f"Cache cleanup error: {e}")


    @cache_cleanup_loop.before_loop
    async def before_cache_cleanup(self):
        await self.bot.wait_until_ready()


    @tasks.loop(seconds=DELETION_QUEUE_DELAY)
    async def deletion_worker(self):
        """Process message deletions with rate limit protection"""
        if self.deletion_queue:
            message, reason = self.deletion_queue.popleft()
            try:
                await message.delete()
            except discord.NotFound:
                pass  # Message already deleted
            except discord.Forbidden:
                logging.warning(f"Missing permissions to delete message in {message.guild.name}")
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    logging.warning(f"Rate limited on deletion, requeueing")
                    self.deletion_queue.appendleft((message, reason))
                    await asyncio.sleep(1)
                else:
                    logging.error(f"Deletion error: {e}")


    @deletion_worker.before_loop
    async def before_deletion_worker(self):
        await self.bot.wait_until_ready()


    async def cleanup_old_local_backups(self):
        """Remove local backup files older than retention period"""
        try:
            cutoff_time = time.time() - (BACKUP_RETENTION_DAYS * 86400)
            removed_count = 0
            
            for filename in os.listdir(BACKUP_FOLDER):
                if not filename.startswith("backup_"):
                    continue
                    
                filepath = os.path.join(BACKUP_FOLDER, filename)
                if os.path.getmtime(filepath) < cutoff_time:
                    os.remove(filepath)
                    removed_count += 1
            
            if removed_count > 0:
                logging.info(f"Cleaned up {removed_count} old local backups")
        except Exception as e:
            logging.error(f"Local backup cleanup error: {e}")


    async def create_backup(self):
        """Create backup in MongoDB and local filesystem"""
        docs = []
        try:
            cursor = self.coll.find({})
            async for d in cursor:
                dd = dict(d)
                dd["_backup_ts"] = datetime.now(timezone.utc).isoformat()
                docs.append(dd)
            
            if docs:
                # MongoDB backup
                await self.backups.insert_one({
                    "ts": datetime.now(timezone.utc), 
                    "data": docs,
                    "guild_count": len(docs)
                })
                
                # Local backup with microseconds to prevent collisions
                timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
                path = os.path.join(BACKUP_FOLDER, f"backup_{timestamp}.json")
                
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(docs, f, default=str, indent=2)
                
                logging.info(f"Backup completed: {len(docs)} guilds backed up")
        except Exception as e:
            logging.error(f"Backup creation failed: {e}")


    # ---------- Helper methods ----------
    async def _delete_after(self, message: discord.Message, delay: float):
        """Delete a message after a delay"""
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except Exception:
            pass

    # ---------- DB helpers with connection pooling ----------
    async def _get_or_create(self, guild_id: int) -> dict:
        """Get or create guild document with semaphore protection"""
        async with self.db_semaphore:
            doc = await self.coll.find_one({"guild_id": guild_id})
            if doc:
                self.banned_cache[guild_id] = set(doc.get("banned", []))
                return doc
            
            default = {
                "guild_id": guild_id,
                "channel_id": None,
                "log_channel_id": None,
                "current": 0,
                "last_user": None,
                "record": 0,
                "counts": {},
                "banned": [],
                "emoji": DEFAULT_EMOJI_CUSTOM,
                "created_at": datetime.now(timezone.utc)
            }
            await self.coll.insert_one(default)
            self.banned_cache[guild_id] = set()
            return default


    async def upsert_fields(self, guild_id: int, patch: dict):
        """Update guild fields with semaphore protection"""
        async with self.db_semaphore:
            await self.coll.update_one(
                {"guild_id": guild_id}, 
                {"$set": patch}, 
                upsert=True
            )
            if "banned" in patch:
                self.banned_cache[guild_id] = set(patch["banned"])


    def queue_deletion(self, message: discord.Message, reason: str = ""):
        """Add message to deletion queue for rate-limited processing"""
        self.deletion_queue.append((message, reason))


    async def safe_add_reaction(self, message: discord.Message, emoji_str: str, use_custom: bool = True):
        """Safely add reaction with fallback and error handling"""
        try:
            # Check if it's a custom emoji format
            emoji_match = CUSTOM_EMOJI_RE.match(emoji_str)
            if emoji_match:
                # Parse custom emoji
                is_animated = emoji_match.group(1) == 'a'
                emoji_name = emoji_match.group(2)
                emoji_id = int(emoji_match.group(3))
                em = discord.PartialEmoji(name=emoji_name, id=emoji_id, animated=is_animated)
                await message.add_reaction(em)
                return
            
            # It's a unicode emoji, add directly
            await message.add_reaction(emoji_str)
        except (discord.NotFound, discord.HTTPException) as e:
            # Emoji not found or invalid, use default fallback
            logging.warning(f"Failed to add reaction '{emoji_str}': {e}. Using fallback.")
            try:
                # Try default custom emoji
                default_match = CUSTOM_EMOJI_RE.match(DEFAULT_EMOJI_CUSTOM)
                if default_match:
                    is_animated = default_match.group(1) == 'a'
                    emoji_name = default_match.group(2)
                    emoji_id = int(default_match.group(3))
                    em = discord.PartialEmoji(name=emoji_name, id=emoji_id, animated=is_animated)
                    await message.add_reaction(em)
                else:
                    await message.add_reaction("✅")
            except Exception:
                pass  # Silently fail if even default fails


    async def validate_emoji(self, emoji: str, guild: discord.Guild) -> bool:
        """Validate that emoji exists and is accessible"""
        emoji_match = CUSTOM_EMOJI_RE.match(emoji)
        if emoji_match:
            emoji_id = int(emoji_match.group(3))
            # Check if bot can access this emoji
            emoji_obj = self.bot.get_emoji(emoji_id)
            return emoji_obj is not None
        return True  # Unicode emojis are always valid


    # ---------- Counting command group ----------
    count_group = app_commands.Group(name="count", description="Counting game commands")
    
    @count_group.command(name="info", description="Show counting info")
    async def info(self, interaction: discord.Interaction):
        """View counting status"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        doc = await self._get_or_create(interaction.guild.id)
        ch = interaction.guild.get_channel(doc.get("channel_id")) if doc.get("channel_id") else None
        last_user = interaction.guild.get_member(doc["last_user"]) if doc.get("last_user") else None
        top = sorted(doc.get("counts", {}).items(), key=lambda x: x[1], reverse=True)[:10]
        lb = "\n".join([f"<@{uid}> — {cnt}" for uid, cnt in top]) or "No counts yet."
        
        embed = discord.Embed(title="Counting Status", color=EMBED_COLOR)
        embed.add_field(name="Channel", value=(ch.mention if ch else "Not set"), inline=True)
        embed.add_field(name="Last Count", value=str(doc.get("current", 0)), inline=True)
        embed.add_field(name="Next Number", value=str(doc.get("current", 0) + 1), inline=True)
        embed.add_field(name="Record", value=str(doc.get("record", 0)), inline=True)
        embed.add_field(name="Last user", value=(last_user.mention if last_user else "None"), inline=True)
        embed.add_field(name="Top Contributors (Top 10)", value=lb, inline=False)
        embed.set_footer(text=f"Deletion queue: {len(self.deletion_queue)} messages")
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @count_group.command(name="top", description="Show top counters")
    async def top(self, interaction: discord.Interaction):
        """View counting leaderboard"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        await interaction.response.defer()
        doc = await self._get_or_create(interaction.guild.id)
        top = sorted(doc.get("counts", {}).items(), key=lambda x: x[1], reverse=True)[:10]
        embed = discord.Embed(title="🏆 Counting Leaderboard", color=EMBED_COLOR)
        if not top:
            embed.description = "No counts recorded yet."
        else:
            for i, (uid, cnt) in enumerate(top, start=1):
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"#{i}"
                embed.add_field(name=f"{medal} <@{uid}>", value=f"{cnt} counts", inline=False)
        await interaction.followup.send(embed=embed)
    
    @count_group.command(name="setup", description="Set counting channel")
    @app_commands.describe(channel="Pick a channel for counting")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Set counting channel"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        # Validate bot has permissions in channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.read_messages and perms.send_messages and perms.manage_messages):
            await interaction.response.send_message(
                f"⚠ Missing permissions in {channel.mention}. I need: Read Messages, Send Messages, Manage Messages.",
                ephemeral=True
            )
            return
        
        await self._get_or_create(interaction.guild.id)
        await self.upsert_fields(interaction.guild.id, {"channel_id": channel.id})
        await interaction.response.send_message(embed=discord.Embed(
            title="✓ Counting channel set",
            description=f"{channel.mention} is now the counting channel.",
            color=EMBED_COLOR
        ), ephemeral=True)
        await self._log(interaction.guild.id, f"Counting channel set to {channel.mention} by {interaction.user}.")
    
    @count_group.command(name="logs", description="Set log channel")
    @app_commands.describe(channel="Where to send logs")
    @app_commands.checks.has_permissions(administrator=True)
    async def logs(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Set log channel"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.read_messages and perms.send_messages):
            await interaction.response.send_message(
                f"⚠ Missing permissions in {channel.mention}. I need: Read Messages, Send Messages.",
                ephemeral=True
            )
            return
        
        await self._get_or_create(interaction.guild.id)
        await self.upsert_fields(interaction.guild.id, {"log_channel_id": channel.id})
        await interaction.response.send_message(embed=discord.Embed(
            title="✓ Log channel set",
            description=f"{channel.mention} will receive counting logs.",
            color=EMBED_COLOR
        ), ephemeral=True)
        await self._log(interaction.guild.id, f"Log channel set to {channel.mention} by {interaction.user}.")
    
    @count_group.command(name="reset", description="Start over from 1")
    @app_commands.checks.has_permissions(administrator=True)
    async def reset(self, interaction: discord.Interaction):
        """Reset count"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        await self._get_or_create(interaction.guild.id)
        await self.upsert_fields(interaction.guild.id, {"current": 0, "last_user": None})
        await interaction.response.send_message(
            "✓ Count reset! Next number is 1.", 
            ephemeral=True
        )
        await self._log(interaction.guild.id, f"Count reset by {interaction.user} ({interaction.user.id}).")
    
    @count_group.command(name="set", description="Jump to a number")
    @app_commands.describe(number="What number to continue from")
    @app_commands.checks.has_permissions(administrator=True)
    async def set(self, interaction: discord.Interaction, number: int):
        """Set count number"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        if number <= 0:
            await interaction.response.send_message("Number must be positive (1 or greater).", ephemeral=True)
            return
        
        await self._get_or_create(interaction.guild.id)
        await self.upsert_fields(interaction.guild.id, {"current": number - 1, "last_user": None})
        await interaction.response.send_message(
            f"✓ Count set! Next number should be **{number}**.", 
            ephemeral=True
        )
        await self._log(interaction.guild.id, f"Count set to {number} by {interaction.user}.")
    
    @count_group.command(name="ban", description="Ban/unban someone")
    @app_commands.describe(user="User to ban or unban")
    @app_commands.checks.has_permissions(administrator=True)
    async def ban(self, interaction: discord.Interaction, user: discord.Member):
        """Ban/unban user from counting"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        doc = await self._get_or_create(interaction.guild.id)
        banned = set(doc.get("banned", []))
        uid = user.id
        
        if uid in banned:
            banned.remove(uid)
            await self.upsert_fields(interaction.guild.id, {"banned": list(banned)})
            await interaction.response.send_message(
                f"✓ Unbanned {user.mention} from counting.", 
                ephemeral=True
            )
            await self._log(interaction.guild.id, f"{user} ({uid}) unbanned by {interaction.user}.")
        else:
            banned.add(uid)
            await self.upsert_fields(interaction.guild.id, {"banned": list(banned)})
            await interaction.response.send_message(
                f"✓ Banned {user.mention} from counting. Their messages will be deleted.", 
                ephemeral=True
            )
            await self._log(interaction.guild.id, f"{user} ({uid}) banned from counting by {interaction.user}.")
    
    @count_group.command(name="emoji", description="Change reaction emoji")
    @app_commands.describe(emoji="Emoji to use (e.g., ✅ or custom emoji)")
    @app_commands.checks.has_permissions(administrator=True)
    async def emoji(self, interaction: discord.Interaction, emoji: str):
        """Set custom emoji for counting"""
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        
        # Validate emoji
        if not await self.validate_emoji(emoji.strip(), interaction.guild):
            await interaction.response.send_message(
                "⚠️ Invalid emoji or I don't have access to it. Use a standard emoji or a custom emoji from this server.",
                ephemeral=True
            )
            return
        
        await self._get_or_create(interaction.guild.id)
        await self.upsert_fields(interaction.guild.id, {"emoji": emoji.strip()})
        await interaction.response.send_message(
            f"✓ Reaction emoji set to {emoji.strip()}", 
            ephemeral=True
        )
        await self._log(interaction.guild.id, f"Count emoji changed to {emoji.strip()} by {interaction.user}.")


    # ---------- Logging helper ----------
    async def _log(self, guild_id: int, message: str):
        """Send log message to configured log channel"""
        try:
            doc = await self.coll.find_one({"guild_id": guild_id}, {"log_channel_id": 1})
            if doc and doc.get("log_channel_id"):
                ch = self.bot.get_channel(doc["log_channel_id"])
                if ch:
                    embed = discord.Embed(
                        description=message, 
                        color=EMBED_COLOR, 
                        timestamp=datetime.now(timezone.utc)
                    )
                    await ch.send(embed=embed)
        except Exception as e:
            logging.error(f"Logging error: {e}")


    # ---------- Message handlers ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle counting messages with full validation and rate limiting"""
        # Ignore DMs, bots, webhooks
        if not message.guild or message.author.bot or message.webhook_id:
            return

        guild_id = message.guild.id
        
        # Get config (lightweight query)
        doc = await self.coll.find_one(
            {"guild_id": guild_id}, 
            {"channel_id": 1, "banned": 1, "current": 1, "last_user": 1, "emoji": 1, "record": 1}
        )
        
        if not doc or not doc.get("channel_id"):
            return

        # Only process configured counting channel
        if message.channel.id != doc["channel_id"]:
            return
        
        # Validate channel still exists and bot has permissions
        channel = message.guild.get_channel(doc["channel_id"])
        if not channel:
            return
        
        perms = channel.permissions_for(message.guild.me)
        if not (perms.manage_messages and perms.add_reactions):
            return

        # Ensure lock exists
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()

        # Check banned users
        banned = set(doc.get("banned", []))
        if message.author.id in banned:
            self.queue_deletion(message, "banned_user")
            
            # Send DM with cooldown
            dm_key = (guild_id, message.author.id)
            current_time = time.time()
            if dm_key not in self._dm_sent or current_time - self._dm_sent[dm_key] > DM_COOLDOWN_SECONDS:
                try:
                    embed = discord.Embed(
                        description=f"<:alert:1426440385269338164> You are banned from counting in **{message.guild.name}**, Your messages will be deleted.",
                        color=EMBED_COLOR
                    )
                    await message.author.send(embed=embed)
                    self._dm_sent[dm_key] = current_time
                except Exception:
                    pass
            return

        # Multi-number detection
        numbers_found = NUMBER_ANY_RE.findall(message.content)
        if len(numbers_found) > 1:
            self.queue_deletion(message, "multi_number")
            await self._log(guild_id, f"<:alert:1426440385269338164> Multi-number message from {message.author} deleted.")
            return

        # Pure integer only
        if not PURE_INT_RE.match(message.content.strip()):
            self.queue_deletion(message, "invalid_format")
            return

        # Parse number
        try:
            num = int(message.content.strip())
        except ValueError:
            self.queue_deletion(message, "parse_error")
            return

        # Acquire per-guild lock
        async with self.locks[guild_id]:
            # Use cached values from initial query
            current = doc.get("current", 0)
            expected = current + 1
            last_user = doc.get("last_user")
            emoji = doc.get("emoji", DEFAULT_EMOJI)

            # Check same user twice in a row
            if last_user and last_user == message.author.id:
                try:
                    await self.safe_add_reaction(message, CROSS_EMOJI_CUSTOM)
                    await asyncio.sleep(0.3)
                    embed = discord.Embed(
                        description=f"<:ogs_bell:1427918360401940552> {message.author.mention}, you can't count twice in a row!",
                        color=EMBED_COLOR
                    )
                    warning_msg = await message.channel.send(embed=embed)
                    # Delete warning message after 3 seconds to prevent stacking
                    asyncio.create_task(self._delete_after(warning_msg, 3))
                    self.queue_deletion(message, "same_user_twice")
                except Exception as e:
                    logging.error(f"Same user error handling: {e}")
                await self._log(guild_id, f"<:ogs_info:1427918257226121288> {message.author} tried counting twice ({num}).")
                return

            # Check wrong number
            if num != expected:
                try:
                    await self.safe_add_reaction(message, CROSS_EMOJI_CUSTOM)
                    await asyncio.sleep(0.3)
                    embed = discord.Embed(
                        description=f"<a:reddot:1427539828521697282> Expected **{expected}** but got **{num}**, Starting over from **1**!",
                        color=EMBED_COLOR
                    )
                    embed.set_footer(text=f"Broken by {message.author.display_name}")
                    warning_msg = await message.channel.send(embed=embed)
                    # This message persists (no auto-delete) as requested
                except Exception:
                    pass
                
                # Reset count
                async with self.db_semaphore:
                    await self.coll.update_one(
                        {"guild_id": guild_id}, 
                        {"$set": {"current": 0, "last_user": None}}
                    )
                
                await self._log(
                    guild_id, 
                    f"<a:reddot:1427539828521697282> {message.author} broke the count! Expected {expected}, got {num}. Reset to 0."
                )
                return

            # Atomic update for correct number
            uid_str = str(message.author.id)
            filter_doc = {
                "guild_id": guild_id,
                "current": current,
                "$or": [
                    {"last_user": {"$exists": False}}, 
                    {"last_user": {"$ne": message.author.id}}
                ]
            }
            update_doc = {
                "$set": {"current": num, "last_user": message.author.id},
                "$inc": {f"counts.{uid_str}": 1},
                "$max": {"record": num}
            }

            try:
                async with self.db_semaphore:
                    res = await self.coll.find_one_and_update(
                        filter_doc,
                        update_doc,
                        return_document=ReturnDocument.AFTER
                    )
            except Exception as e:
                logging.error(f"Atomic update error (guild {guild_id}): {e}")
                self.queue_deletion(message, "db_error")
                await self._log(guild_id, f"<:ogs_info:1427918257226121288> Database error during count update: {e}")
                return

            if res is None:
                # Race condition: another update happened first
                self.queue_deletion(message, "race_condition")
                
                # Notify user via DM about race condition
                try:
                    await message.author.send(
                        f"<:ogs_info:1427918257226121288> Your count in **{message.guild.name}** was posted at the same time as someone else. "
                        f"Please check the channel and continue counting!"
                    )
                except Exception:
                    pass
                
                await self._log(
                    guild_id, 
                    f"<a:reddot:1427539828521697282> Race condition: {message.author}'s message ({num}) deleted (concurrent update)."
                )
                return

            # Success! Add reaction with custom emoji from database
            success_emoji = res.get("emoji", DEFAULT_EMOJI_CUSTOM)
            await self.safe_add_reaction(message, success_emoji)

            # Announce new record (multiples of 100)
            old_record = doc.get("record", 0)
            new_record = res.get("record", 0)
            if new_record > old_record and new_record % 100 == 0:
                try:
                    await message.channel.send(
                        f" **New Record!** The count has reached **{new_record}**! "
                    )
                except Exception:
                    pass


    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Prevent edit cheating in counting channel"""
        if before.content == after.content:
            return
        
        if not after.guild or after.author.bot:
            return
        
        # Check if in counting channel
        doc = await self.coll.find_one(
            {"guild_id": after.guild.id}, 
            {"channel_id": 1}
        )
        
        if doc and doc.get("channel_id") == after.channel.id:
            self.queue_deletion(after, "edited_message")
            await self._log(
                after.guild.id, 
                f"<:ogs_bell:1427918360401940552> Deleted edited message from {after.author} (anti-cheat)."
            )


    async def cog_unload(self):
        """Clean shutdown of all tasks and connections"""
        logging.info("Unloading counting cog...")
        
        # Cancel all tasks
        self.backup_loop.cancel()
        self.cache_cleanup_loop.cancel()
        self.deletion_worker.cancel()
        
        # Process remaining deletions
        logging.info(f"Processing {len(self.deletion_queue)} remaining deletions...")
        while self.deletion_queue and len(self.deletion_queue) < 50:
            message, reason = self.deletion_queue.popleft()
            try:
                await message.delete()
            except Exception:
                pass
        
        # Close MongoDB connection only if we own it
        if self._owns_connection:
            try:
                self.mongo.close()
                logging.info("MongoDB connection closed")
            except Exception as e:
                logging.error(f"MongoDB close error: {e}")
        else:
            logging.info("MongoDB connection shared, not closing")


# ---------- Setup ----------
async def setup(bot: commands.Bot):
    await bot.add_cog(CountingCog(bot))
