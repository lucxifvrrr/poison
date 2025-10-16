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

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

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
        
        # Global cooldown mapping
        self._cd_mapping = commands.CooldownMapping.from_cooldown(1, 0.2, commands.BucketType.user)
        
        # Response tracking to prevent duplicates
        self._response_tracker: Set[str] = set()
        self._tracker_cleanup_time = time.time()

        # Prefix commands
        @self.command()
        async def ping(ctx):
            await ctx.send(f'<a:sukoon_greendot:1322894177775783997> Latency: {self.latency*1000:.2f}ms')
        
        @self.command()
        @commands.is_owner()
        async def sync(ctx):
            """Manually sync slash commands (owner only)"""
            try:
                msg = await ctx.send("⏳ Syncing commands...")
                synced = await self.tree.sync()
                current_hash = self._get_command_hash()
                self._save_sync_cache(current_hash, time.time())
                await msg.edit(content=f"✅ Successfully synced {len(synced)} commands!")
                logging.info(f"Manual sync triggered by {ctx.author}")
            except HTTPException as e:
                if e.status == 429:
                    await msg.edit(content="❌ Rate limited! Please wait before syncing again.")
                else:
                    await msg.edit(content=f"❌ Sync failed: {e}")
            except Exception as e:
                await msg.edit(content=f"❌ Error: {e}")
        
        @self.command()
        @commands.is_owner()
        async def reload(ctx, cog: str = None):
            """Reload a cog or all cogs (owner only)"""
            msg = await ctx.send("⏳ Reloading...")
            
            if cog:
                # Reload specific cog
                try:
                    await self.reload_extension(cog)
                    await msg.edit(content=f"✅ Successfully reloaded `{cog}`\n💡 Config changes applied!")
                    logging.info(f"Cog reloaded by {ctx.author}: {cog}")
                except Exception as e:
                    await msg.edit(content=f"❌ Failed to reload `{cog}`: {e}")
                    logging.error(f"Failed to reload cog {cog}: {e}")
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
                
                result = f"✅ Reloaded {len(success)} cog(s)\n💡 Config changes applied!"
                if failed:
                    result += f"\n❌ Failed: {len(failed)}\n" + "\n".join(f"  • {f}" for f in failed[:5])
                
                await msg.edit(content=result)
                logging.info(f"All cogs reloaded by {ctx.author}: {len(success)} success, {len(failed)} failed")

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
    
    def _save_sync_cache(self, command_hash: str, sync_time: float, rate_limited: bool = False):
        """Save the sync cache."""
        try:
            cache_data = {
                'command_hash': command_hash,
                'last_sync': sync_time,
                'rate_limited': rate_limited
            }
            with open(COMMAND_CACHE_FILE, 'w') as f:
                json.dump(cache_data, f)
        except Exception as e:
            logging.warning(f"Failed to save sync cache: {e}")

    async def setup_hook(self) -> None:
        logging.info("Bot setup starting...")
        self.session = aiohttp.ClientSession()
        await self.load_cogs()
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        await asyncio.sleep(1)

        # Smart command sync with caching
        current_hash = self._get_command_hash()
        cache = self._load_sync_cache()
        last_hash = cache.get('command_hash')
        last_sync = cache.get('last_sync', 0)
        was_rate_limited = cache.get('rate_limited', False)
        current_time = time.time()
        
        # Minimum intervals to avoid rate limits
        time_since_last_sync = current_time - last_sync
        min_sync_interval = 300  # 5 minutes
        rate_limit_backoff = 900  # 15 minutes if previously rate limited
        
        # If we were rate limited last time, wait longer
        if was_rate_limited and time_since_last_sync < rate_limit_backoff:
            logging.info(f"Skipping sync: Previously rate limited. Wait {int(rate_limit_backoff - time_since_last_sync)}s more")
            self._synced_commands = self.tree.get_commands()
            return
        
        # Only sync if:
        # 1. Commands changed AND at least 5 minutes passed
        # 2. OR it's been more than 1 hour (for periodic refresh)
        should_sync = (
            (current_hash != last_hash and time_since_last_sync > min_sync_interval) or 
            time_since_last_sync > 3600
        )
        
        if should_sync:
            try:
                logging.info("Command changes detected, syncing...")
                self._synced_commands = await self.tree.sync()
                logging.info(f"Successfully synced {len(self._synced_commands)} slash commands")
                self._save_sync_cache(current_hash, current_time, rate_limited=False)
            except HTTPException as e:
                if e.status == 429:
                    logging.warning(f"Rate limited! Commands will not sync for 15 minutes.")
                    # Save that we were rate limited to prevent retries
                    self._save_sync_cache(current_hash, current_time, rate_limited=True)
                    self._synced_commands = self.tree.get_commands()
                else:
                    logging.error(f"Failed to sync slash commands: {e}")
            except Exception as e:
                logging.error(f"Unexpected error during command sync: {e}")
        else:
            if time_since_last_sync < min_sync_interval:
                logging.info(f"Skipping sync: Only {int(time_since_last_sync)}s since last sync (minimum: {min_sync_interval}s)")
            else:
                logging.info("Commands unchanged, skipping sync to avoid rate limits")
            self._synced_commands = self.tree.get_commands()

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
        
        logging.info(f"Bot ready: {self.user.name} ({self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guilds")
        logging.info(f"Total commands available: {len(self._synced_commands)}")
        
        # Display slash command names
        if self._synced_commands:
            print("\033[36m" + "=" * 50 + "\033[0m")
            print("\033[36mAvailable Slash Commands:\033[0m")
            for cmd in self._synced_commands:
                print(f"  \033[32m/{cmd.name}\033[0m - {cmd.description}")
            print("\033[36m" + "=" * 50 + "\033[0m\n")
        else:
            print()

    async def close(self) -> None:
        if self.is_closed():
            return
            
        print("\n\033[33m" + "=" * 50 + "\033[0m")
        print("\033[31mBot is shutting down...\033[0m")
        print("\033[33m" + "=" * 50 + "\033[0m\n")

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self.session and not self.session.closed:
            await self.session.close()

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