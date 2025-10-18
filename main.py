import discord
from discord.ext import commands
import logging
import os
from dotenv import load_dotenv
import asyncio
import aiohttp
import sys
import signal
from typing import Optional, List, Set
from logging.handlers import TimedRotatingFileHandler
from pyfiglet import Figlet
from discord import HTTPException
import time
from logging import StreamHandler
import json
import hashlib
from motor.motor_asyncio import AsyncIOMotorClient

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MONGO_URL = os.getenv("MONGO_URL")
AUTO_SYNC_COMMANDS = os.getenv("AUTO_SYNC_COMMANDS", "true").lower() == "true"

# Directory constants
LOGS_DIR = "logs"
DATABASE_DIR = "database"
COGS_DIR = "cogs"
COMMAND_CACHE_FILE = "database/command_sync_cache.json"

def setup_directories() -> None:
    for directory in (LOGS_DIR, DATABASE_DIR, COGS_DIR):
        os.makedirs(directory, exist_ok=True)

def setup_logging() -> None:
    """Configure logging with file rotation."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # File handler with rotation
    file_handler = TimedRotatingFileHandler(
        os.path.join(LOGS_DIR, "bot.log"),
        when="midnight",
        backupCount=7,
        encoding='utf-8',
        utc=True
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    # Console handler for errors only
    console_handler = StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

def validate_environment() -> None:
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not WEBHOOK_URL:
        missing.append("WEBHOOK_URL")
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

def print_banner(bot_name: str = "Discord Bot") -> None:
    f = Figlet(font='slant')
    banner = f.renderText(bot_name)
    print("\033[36m" + banner + "\033[0m")
    print("\033[33m" + "=" * 50 + "\033[0m")
    print("\033[32m" + "Bot is starting up..." + "\033[0m")
    print("\033[33m" + "=" * 50 + "\033[0m\n")

class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True

        super().__init__(command_prefix=".", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self._ready_once = False
        self._synced_commands: List[discord.app_commands.Command] = []
        self._shutdown_requested = False
        self._cleanup_task: Optional[asyncio.Task] = None
        
        # Add logger for cogs to use
        self.logger = logging.getLogger('discord.bot')
        
        # Shared MongoDB connection for all cogs
        self.mongo_client: Optional[AsyncIOMotorClient] = None
        if MONGO_URL:
            try:
                self.mongo_client = AsyncIOMotorClient(
                    MONGO_URL,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=10000,
                    socketTimeoutMS=45000,
                    maxPoolSize=50,
                    minPoolSize=5,
                    maxIdleTimeMS=45000,
                    retryWrites=True,
                    retryReads=True
                )
                logging.info("Shared MongoDB connection initialized")
            except Exception as e:
                logging.warning(f"Failed to initialize shared MongoDB connection: {e}")
                self.mongo_client = None
        
        # Global cooldown mapping
        self._cd_mapping = commands.CooldownMapping.from_cooldown(1, 0.2, commands.BucketType.user)
        
        # Response tracking to prevent duplicates
        self._response_tracker: Set[str] = set()
        self._tracker_cleanup_time = time.time()
        self._sync_lock = asyncio.Lock()
        self._rate_limit_count = 0
        self._auto_sync_task: Optional[asyncio.Task] = None
        self._last_sync_attempt = 0

        # Prefix commands
        @self.command()
        async def ping(ctx):
            await ctx.send(f'<a:sukoon_greendot:1322894177775783997> Latency: {self.latency*1000:.2f}ms')
        
        @self.command()
        @commands.is_owner()
        async def sync(ctx):
            """Manually sync slash commands (owner only)"""
            msg = await ctx.send("<a:heartspark_ogs:1427918324066422834> Syncing commands...")
            try:
                async with self._sync_lock:
                    synced = await self.tree.sync()
                    current_hash = self._get_command_hash()
                    # Reset rate limit counter on successful manual sync
                    self._rate_limit_count = 0
                    self._save_sync_cache(current_hash, time.time(), rate_limited=False, retry_after=None)
                    await msg.edit(content=f"<a:white_tick:1426439810733572136> Successfully synced {len(synced)} commands!")
                    logging.info(f"Manual sync triggered by {ctx.author}")
            except HTTPException as e:
                if e.status == 429:
                    self._rate_limit_count += 1
                    retry_after = e.response.headers.get('Retry-After', 'unknown')
                    backoff_hours = min(2 ** self._rate_limit_count, 24)
                    self._save_sync_cache(self._get_command_hash(), time.time(), rate_limited=True, retry_after=retry_after)
                    await msg.edit(
                        content=f"❌ Rate limited! Discord says wait {retry_after}s. "
                        f"Bot will wait {backoff_hours}h before auto-sync. "
                        f"**Stop restarting the bot!**"
                    )
                    logging.error(f"Manual sync rate limited by {ctx.author}")
                else:
                    await msg.edit(content=f"❌ Sync failed: {e}")
            except Exception as e:
                await msg.edit(content=f"❌ Error: {e}")
        
        @self.command()
        @commands.is_owner()
        async def cogs(ctx):
            """List all loaded cogs (owner only)"""
            if not self.extensions:
                await ctx.send("No cogs loaded.")
                return
            
            cog_list = "\n".join(f"  • `{ext}`" for ext in sorted(self.extensions.keys()))
            await ctx.send(f"**Loaded Cogs ({len(self.extensions)}):**\n{cog_list}")
        
        @self.command()
        @commands.is_owner()
        async def reload(ctx, *, cog: str = None):
            """Reload a cog or all cogs (owner only)"""
            msg = await ctx.send("<a:reddot:1427539828521697282> Reloading...")
            
            if cog:
                # Reload specific cog - support both folder names and full module paths
                extensions_to_reload = []
                
                # Check if it's a full module path (e.g., cogs.counting.counting)
                if cog in self.extensions:
                    extensions_to_reload.append(cog)
                else:
                    # Try to find extensions matching the folder/cog name
                    # Support formats: "counting", "cogs.counting", etc.
                    search_term = cog.replace("cogs.", "").strip().lower()
                    
                    for ext in self.extensions.keys():
                        # Match if the search term is in the extension path
                        # Examples: "counting" matches "cogs.counting.counting"
                        #           "giveaways" matches "cogs.giveaways.giveaway_admin"
                        ext_lower = ext.lower()
                        ext_parts = ext_lower.split('.')
                        
                        # Check if search term matches any part of the module path
                        if search_term in ext_parts:
                            extensions_to_reload.append(ext)
                        # Also check if it's a substring match (for partial names)
                        elif any(search_term in part for part in ext_parts):
                            extensions_to_reload.append(ext)
                
                if not extensions_to_reload:
                    # Show available cogs to help user
                    available = "\n".join(f"  • `{ext}`" for ext in sorted(self.extensions.keys())[:10])
                    try:
                        await msg.edit(content=f"<:ogs_bell:1427918360401940552> No cog found matching `{cog}`\n\n**Available cogs:**\n{available}")
                    except discord.NotFound:
                        await ctx.send(f"<:ogs_bell:1427918360401940552> No cog found matching `{cog}`\n\n**Available cogs:**\n{available}")
                    return
                
                # Reload all matching extensions
                success = []
                failed = []
                
                for ext in extensions_to_reload:
                    try:
                        await self.reload_extension(ext)
                        success.append(ext)
                        logging.info(f"Cog reloaded by {ctx.author}: {ext}")
                    except Exception as e:
                        failed.append(f"{ext}: {str(e)[:50]}")
                        logging.error(f"Failed to reload cog {ext}: {e}")
                
                # Build response message
                if success and not failed:
                    result = f"<a:white_tick:1426439810733572136> Successfully reloaded:\n" + "\n".join(f"  • `{s}`" for s in success) + ""
                elif success and failed:
                    result = f"<a:white_tick:1426439810733572136> Reloaded {len(success)}:\n" + "\n".join(f"  • `{s}`" for s in success)
                    result += f"\n<:ogs_bell:1427918360401940552> Failed {len(failed)}:\n" + "\n".join(f"  • {f}" for f in failed[:3])
                else:
                    result = f"<:ogs_bell:1427918360401940552> Failed to reload:\n" + "\n".join(f"  • {f}" for f in failed[:3])
                
                try:
                    await msg.edit(content=result)
                except discord.NotFound:
                    await ctx.send(content=result)
            else:
                # Reload all cogs
                success = []
                failed = []
                
                for extension in list(self.extensions.keys()):
                    try:
                        await self.reload_extension(extension)
                        success.append(extension)
                    except Exception as e:
                        failed.append(f"{extension}: {str(e)[:50]}")
                        logging.error(f"Failed to reload {extension}: {e}")
                
                result = f"<a:white_tick:1426439810733572136> Reloaded {len(success)} cog(s)"
                if failed:
                    result += f"\n<:ogs_bell:1427918360401940552> Failed: {len(failed)}\n" + "\n".join(f"  • {f}" for f in failed[:5])
                
                try:
                    await msg.edit(content=result)
                except discord.NotFound:
                    await ctx.send(content=result)
                logging.info(f"All cogs reloaded by {ctx.author}: {len(success)} success, {len(failed)} failed")
        
        @self.command()
        @commands.is_owner()
        async def syncstatus(ctx):
            """Check command sync status (owner only)"""
            cache = self._load_sync_cache()
            last_sync = cache.get('last_sync', 0)
            was_rate_limited = cache.get('rate_limited', False)
            rate_limit_count = cache.get('rate_limit_count', 0)
            retry_after = cache.get('retry_after', 'N/A')
            
            current_time = time.time()
            time_since_sync = current_time - last_sync
            
            # Format time
            if last_sync > 0:
                hours_ago = int(time_since_sync) // 3600
                minutes_ago = (int(time_since_sync) % 3600) // 60
                last_sync_str = f"{hours_ago}h {minutes_ago}m ago"
            else:
                last_sync_str = "Never"
            
            # Calculate next sync time
            if was_rate_limited:
                rate_limit_backoff = min(3600 * (2 ** rate_limit_count), 86400)
                time_remaining = rate_limit_backoff - time_since_sync
                if time_remaining > 0:
                    hours = int(time_remaining) // 3600
                    minutes = (int(time_remaining) % 3600) // 60
                    next_sync = f"{hours}h {minutes}m (auto-retry)"
                    status_emoji = "<:ogs_info:1427918257226121288>"
                    status_text = "Rate Limited"
                else:
                    next_sync = "Ready to sync"
                    status_emoji = "✅"
                    status_text = "Recovered"
            else:
                # Check if we're in the minimum interval
                min_sync_interval = 3600  # 1 hour
                if time_since_sync < min_sync_interval:
                    time_remaining = min_sync_interval - time_since_sync
                    minutes = int(time_remaining) // 60
                    next_sync = f"In {minutes}m (cooldown)"
                    status_emoji = "⏳"
                    status_text = "Cooldown"
                else:
                    status_emoji = "✅"
                    status_text = "Normal"
                    next_sync = "On command change or 24h"
            
            embed = discord.Embed(
                title=f"{status_emoji} Command Sync Status",
                color=discord.Color.orange() if was_rate_limited else (discord.Color.blue() if status_text == "Cooldown" else discord.Color.green()),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Status", value=status_text, inline=True)
            embed.add_field(name="Total Commands", value=len(self._synced_commands), inline=True)
            embed.add_field(name="Rate Limit Count", value=rate_limit_count, inline=True)
            embed.add_field(name="Last Sync", value=last_sync_str, inline=True)
            embed.add_field(name="Next Sync", value=next_sync, inline=True)
            embed.add_field(name="Discord Retry-After", value=f"{retry_after}s" if retry_after != 'N/A' else 'N/A', inline=True)
            
            # Add helpful tips
            tips = []
            if was_rate_limited:
                tips.append("⚠️ **Rate Limited**: Wait for auto-retry, don't restart!")
            elif status_text == "Cooldown":
                tips.append("⏳ **Cooldown Active**: Bot prevents syncing too frequently")
            tips.append("💡 Use `.reload` to reload cogs without restarting")
            
            embed.add_field(name="Tips", value="\n".join(tips), inline=False)
            embed.set_footer(text="Auto-sync scheduler is running in background")
            
            await ctx.send(embed=embed)

    async def _should_respond(self, ctx) -> bool:
        """Check if bot should respond to prevent duplicates"""
        response_id = f"{ctx.channel.id}:{ctx.message.id}:{ctx.command.name}"
        
        # Clean up old entries every 5 minutes
        current_time = time.time()
        if current_time - self._tracker_cleanup_time > 300:
            self._response_tracker.clear()
            self._tracker_cleanup_time = current_time
        
        if response_id in self._response_tracker:
            return False
            
        self._response_tracker.add(response_id)
        return True

    async def invoke(self, ctx):
        """Override invoke to add duplicate prevention for all commands"""
        if await self._should_respond(ctx):
            await super().invoke(ctx)

    async def on_command_error(self, ctx, error):
        """Handle command errors"""
        # Ignore errors that are handled by command-specific error handlers
        if isinstance(error, (commands.CommandOnCooldown, commands.CheckFailure)):
            return
        
        # Check if the command has its own error handler
        if hasattr(ctx.command, 'on_error'):
            # Command has its own error handler, let it handle the error
            return
        
        # Ignore common user errors that should be handled by command error handlers
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, 
                            commands.MemberNotFound, commands.UserNotFound)):
            # These should be handled by command-specific error handlers
            # If we reach here, the command doesn't have an error handler
            return
        
        # Only log unexpected/unhandled errors
        if isinstance(error, commands.CommandInvokeError):
            # Unwrap the original error
            original = error.original
            logging.error(f"Unexpected error in {ctx.command}: {type(original).__name__}: {original}")
        else:
            logging.error(f"Command error in {ctx.command}: {type(error).__name__}: {error}")

    async def process_commands(self, message):
        """Process commands with global cooldown"""
        if message.author.bot:
            return
            
        ctx = await self.get_context(message)
        if ctx.command is None:
            return
            
        # Check global cooldown
        bucket = self._cd_mapping.get_bucket(message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            return
            
        await self.invoke(ctx)

    def _get_command_hash(self) -> str:
        """Generate a hash of current command structure to detect changes."""
        commands_data = []
        for cmd in self.tree.get_commands():
            cmd_dict = {
                'name': cmd.name,
                'description': cmd.description,
                'options': str(cmd.parameters) if hasattr(cmd, 'parameters') else ''
            }
            commands_data.append(cmd_dict)
        
        # Sort for consistent hashing
        commands_data.sort(key=lambda x: x['name'])
        commands_str = json.dumps(commands_data, sort_keys=True)
        return hashlib.md5(commands_str.encode()).hexdigest()
    
    def _load_sync_cache(self) -> dict:
        """Load the last sync cache."""
        try:
            if os.path.exists(COMMAND_CACHE_FILE):
                with open(COMMAND_CACHE_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to load sync cache: {e}")
        return {}
    
    def _save_sync_cache(self, command_hash: str, sync_time: float, rate_limited: bool = False, retry_after: int = None):
        """Save the sync cache."""
        try:
            cache_data = {
                'command_hash': command_hash,
                'last_sync': sync_time,
                'rate_limited': rate_limited,
                'rate_limit_count': self._rate_limit_count,
                'retry_after': retry_after
            }
            with open(COMMAND_CACHE_FILE, 'w') as f:
                json.dump(cache_data, f, indent=2)
        except Exception as e:
            logging.warning(f"Failed to save sync cache: {e}")

    async def setup_hook(self) -> None:
        logging.info("Bot setup starting...")
        self.session = aiohttp.ClientSession()
        await self.load_cogs()
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._auto_sync_task = asyncio.create_task(self._auto_sync_scheduler())
        
        # Add startup delay to avoid immediate sync on rapid restarts
        await asyncio.sleep(5)

        # Smart command sync with caching
        async with self._sync_lock:
            # Check if auto-sync is disabled
            if not AUTO_SYNC_COMMANDS:
                logging.info("Auto-sync disabled via environment variable. Use .sync command to sync manually.")
                self._synced_commands = self.tree.get_commands()
                return
            
            current_hash = self._get_command_hash()
            cache = self._load_sync_cache()
            last_hash = cache.get('command_hash')
            last_sync = cache.get('last_sync', 0)
            was_rate_limited = cache.get('rate_limited', False)
            self._rate_limit_count = cache.get('rate_limit_count', 0)
            retry_after = cache.get('retry_after')
            current_time = time.time()
            
            # Minimum intervals to avoid rate limits
            time_since_last_sync = current_time - last_sync
            min_sync_interval = 3600  # 1 hour (conservative to avoid rate limits)
            
            # Exponential backoff: 1hr, 2hr, 4hr, 8hr, max 24hr
            rate_limit_backoff = min(3600 * (2 ** self._rate_limit_count), 86400)
            
            # If we were rate limited last time, check if backoff period has passed
            if was_rate_limited and time_since_last_sync < rate_limit_backoff:
                wait_time = int(rate_limit_backoff - time_since_last_sync)
                hours = wait_time // 3600
                minutes = (wait_time % 3600) // 60
                logging.warning(
                    f"⚠️ SYNC BLOCKED: Rate limited {self._rate_limit_count} time(s). "
                    f"Wait {hours}h {minutes}m more. Auto-sync will retry automatically."
                )
                self._synced_commands = self.tree.get_commands()
                return
            elif was_rate_limited and time_since_last_sync >= rate_limit_backoff:
                # Backoff period has passed, automatically reset rate limit status
                logging.info(f"✅ Rate limit backoff period expired. Resetting rate limit status.")
                self._rate_limit_count = max(0, self._rate_limit_count - 1)  # Gradually reduce count
                was_rate_limited = False
            
            # Only sync if:
            # 1. Commands changed AND at least 1 hour passed
            # 2. OR it's been more than 24 hours (for periodic refresh)
            should_sync = (
                (current_hash != last_hash and time_since_last_sync > min_sync_interval) or 
                time_since_last_sync > 86400  # 24 hours
            )
            
            if should_sync:
                try:
                    logging.info(f"Command changes detected, syncing... (Last sync: {int(time_since_last_sync/60)}m ago)")
                    self._synced_commands = await self.tree.sync()
                    logging.info(f"✅ Successfully synced {len(self._synced_commands)} slash commands")
                    # Reset rate limit counter on success
                    self._rate_limit_count = 0
                    self._last_sync_attempt = current_time
                    self._save_sync_cache(current_hash, current_time, rate_limited=False, retry_after=None)
                except HTTPException as e:
                    if e.status == 429:
                        # Increment rate limit counter for exponential backoff
                        self._rate_limit_count += 1
                        retry_after_seconds = None
                        try:
                            retry_after_seconds = int(e.response.headers.get('Retry-After', 0))
                        except (ValueError, TypeError):
                            pass
                        
                        backoff_time = min(3600 * (2 ** self._rate_limit_count), 86400)
                        hours = backoff_time // 3600
                        
                        logging.error(
                            f"❌ RATE LIMITED by Discord (attempt #{self._rate_limit_count})! "
                            f"Discord says retry after: {retry_after_seconds}s. "
                            f"Bot will automatically retry in {hours}h (exponential backoff)."
                        )
                        logging.info(
                            f"💡 TIP: The bot will handle this automatically. "
                            f"Use '.reload' for code changes instead of restarting."
                        )
                        self._last_sync_attempt = current_time
                        
                        # Save that we were rate limited to prevent retries
                        self._save_sync_cache(current_hash, current_time, rate_limited=True, retry_after=retry_after_seconds)
                        self._synced_commands = self.tree.get_commands()
                    else:
                        logging.error(f"Failed to sync slash commands: {e}")
                except Exception as e:
                    logging.error(f"Unexpected error during command sync: {e}")
            else:
                if time_since_last_sync < min_sync_interval:
                    minutes = int(time_since_last_sync) // 60
                    minutes_remaining = int((min_sync_interval - time_since_last_sync) / 60)
                    logging.info(f"⏭️ Skipping sync: Only {minutes}m since last sync (next allowed in {minutes_remaining}m)")
                else:
                    logging.info("⏭️ Commands unchanged, skipping sync to avoid rate limits")
                self._synced_commands = self.tree.get_commands()

    async def _auto_sync_scheduler(self) -> None:
        """Automatically retry syncing after rate limit backoff periods"""
        await asyncio.sleep(60)  # Wait 1 minute after startup
        
        while not self.is_closed():
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                
                # Load cache to check status
                cache = self._load_sync_cache()
                was_rate_limited = cache.get('rate_limited', False)
                last_sync = cache.get('last_sync', 0)
                rate_limit_count = cache.get('rate_limit_count', 0)
                current_time = time.time()
                time_since_last_sync = current_time - last_sync
                
                if not was_rate_limited:
                    continue  # No action needed
                
                # Calculate if backoff period has expired
                rate_limit_backoff = min(3600 * (2 ** rate_limit_count), 86400)
                
                if time_since_last_sync >= rate_limit_backoff:
                    logging.info("🔄 Auto-sync: Rate limit backoff expired, attempting sync...")
                    
                    async with self._sync_lock:
                        try:
                            current_hash = self._get_command_hash()
                            self._synced_commands = await self.tree.sync()
                            logging.info(f"✅ Auto-sync successful! Synced {len(self._synced_commands)} commands")
                            
                            # Reset rate limit on success
                            self._rate_limit_count = 0
                            self._save_sync_cache(current_hash, current_time, rate_limited=False, retry_after=None)
                        except HTTPException as e:
                            if e.status == 429:
                                # Still rate limited, increase backoff
                                self._rate_limit_count = rate_limit_count + 1
                                retry_after_seconds = None
                                try:
                                    retry_after_seconds = int(e.response.headers.get('Retry-After', 0))
                                except (ValueError, TypeError):
                                    pass
                                
                                new_backoff = min(3600 * (2 ** self._rate_limit_count), 86400)
                                hours = new_backoff // 3600
                                logging.warning(
                                    f"⚠️ Auto-sync still rate limited. "
                                    f"Will retry in {hours}h (attempt #{self._rate_limit_count})"
                                )
                                self._save_sync_cache(current_hash, current_time, rate_limited=True, retry_after=retry_after_seconds)
                            else:
                                logging.error(f"Auto-sync failed: {e}")
                        except Exception as e:
                            logging.error(f"Auto-sync error: {e}")
                else:
                    # Still in backoff period
                    wait_time = int(rate_limit_backoff - time_since_last_sync)
                    hours = wait_time // 3600
                    minutes = (wait_time % 3600) // 60
                    if minutes > 0 or hours > 0:  # Only log if significant time remaining
                        logging.debug(f"⏳ Auto-sync waiting: {hours}h {minutes}m until next retry")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in auto-sync scheduler: {e}")
    
    async def _periodic_cleanup(self) -> None:
        """Clean up resources periodically"""
        while not self.is_closed():
            try:
                await asyncio.sleep(300)
                import gc
                gc.collect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in periodic cleanup: {e}")

    async def on_ready(self):
        if self._ready_once:
            return
        self._ready_once = True

        print("\033[2J\033[H")
        print_banner(self.user.name)
        print(f"\033[32mLogged in as {self.user.name} ({self.user.id})\033[0m")
        print(f"\033[33mLoaded {len(self._synced_commands)} slash commands\033[0m")
        
        # Display auto-sync status
        cache = self._load_sync_cache()
        if cache.get('rate_limited', False):
            rate_limit_count = cache.get('rate_limit_count', 0)
            print(f"\033[33m⚠️  Auto-Sync: Rate limited ({rate_limit_count}x) - Will retry automatically\033[0m")
        else:
            print(f"\033[32m✅ Auto-Sync: Active and monitoring\033[0m")
        
        logging.info(f"Bot ready: {self.user.name} ({self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guilds")
        logging.info(f"Total commands available: {len(self._synced_commands)}")
        
        # Display slash command names
        if self._synced_commands:
            print("\033[36m" + "=" * 50 + "\033[0m")
            print("\033[36mAvailable Slash Commands:\033[0m")
            for cmd in self._synced_commands:
                print(f"  \033[32m/{cmd.name}\033[0m - {cmd.description}")
            print("\033[36m" + "=" * 50 + "\033[0m")
            print(f"\033[35m💡 TIP: Use .reload to reload cogs without restarting!\033[0m")
            print(f"\033[35m📊 TIP: Use .syncstatus to check sync status anytime!\033[0m\n")
        else:
            print()

    async def close(self) -> None:
        if self.is_closed():
            return
            
        print("\n\033[33m" + "=" * 50 + "\033[0m")
        print("\033[31mBot is shutting down...\033[0m")
        print("\033[33m" + "=" * 50 + "\033[0m\n")

        # Cancel background tasks
        for task in [self._cleanup_task, self._auto_sync_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self.session and not self.session.closed:
            await self.session.close()
        
        # Close shared MongoDB connection
        if self.mongo_client:
            try:
                self.mongo_client.close()
                logging.info("Shared MongoDB connection closed")
            except Exception as e:
                logging.error(f"Error closing MongoDB connection: {e}")

        await super().close()

    async def load_cogs(self) -> None:
        """Load all cogs from the cogs directory and subdirectories."""
        if not os.path.isdir(COGS_DIR):
            return

        # Files to skip (not cogs)
        skip_files = {'config.py', '__init__.py'}
        
        # Walk through cogs directory and subdirectories
        for root, dirs, files in os.walk(COGS_DIR):
            # Get relative path from cogs directory
            rel_path = os.path.relpath(root, COGS_DIR)
            
            for filename in files:
                # Skip non-Python files and special files
                if not filename.endswith('.py') or filename in skip_files:
                    continue
                
                # Build module path
                if rel_path == '.':
                    # File is directly in cogs folder
                    module = f"{COGS_DIR}.{filename[:-3]}"
                else:
                    # File is in a subfolder
                    rel_module = rel_path.replace(os.sep, '.')
                    module = f"{COGS_DIR}.{rel_module}.{filename[:-3]}"
                
                try:
                    await self.load_extension(module)
                    logging.info(f"Loaded cog: {module}")
                except Exception as e:
                    logging.error(f"Failed to load cog {module}: {e}")

    async def send_error_report(self, error_message: str) -> None:
        if not self.session or self.session.closed or self.is_closed():
            return
        try:
            async with self.session.post(WEBHOOK_URL, json={"content": error_message}) as resp:
                resp.raise_for_status()
        except Exception as e:
            logging.error(f"Failed to send error report: {e}")

def setup_signal_handlers(bot: DiscordBot) -> None:
    def shutdown_handler(signum=None, frame=None):
        bot._shutdown_requested = True
        if not bot.is_closed():
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(bot.close())
            except RuntimeError:
                pass
    
    if sys.platform != "win32":
        try:
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(signal.SIGTERM, shutdown_handler)
            loop.add_signal_handler(signal.SIGINT, shutdown_handler)
        except NotImplementedError:
            signal.signal(signal.SIGTERM, shutdown_handler)
            signal.signal(signal.SIGINT, shutdown_handler)
    else:
        signal.signal(signal.SIGINT, shutdown_handler)

async def main():
    bot = None
    try:
        setup_directories()
        setup_logging()
        validate_environment()
    except ValueError as e:
        print(f"\033[31mStartup error: {e}\033[0m")
        sys.exit(1)

    try:
        bot = DiscordBot()
        setup_signal_handlers(bot)

        async with bot:
            async def shutdown_checker():
                while not bot.is_closed():
                    if bot._shutdown_requested:
                        await bot.close()
                        break
                    await asyncio.sleep(1)
            
            await asyncio.gather(
                bot.start(DISCORD_TOKEN),
                shutdown_checker(),
                return_exceptions=True
            )
            
    except KeyboardInterrupt:
        if bot and not bot.is_closed():
            await bot.close()
    except Exception as e:
        logging.error(f"Fatal error in main: {e}")
        if bot and bot.session and not bot.session.closed and not bot.is_closed():
            try:
                await bot.send_error_report(f"Fatal error: {e}")
            except:
                pass
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\033[31mBot shutdown by keyboard interrupt\033[0m")
    except Exception as e:
        print(f"\n\033[31mFatal error during startup: {e}\033[0m")
        logging.error(f"Fatal error during startup: {e}")
        sys.exit(1)