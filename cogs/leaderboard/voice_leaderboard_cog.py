"""
Voice Leaderboard Cog - Tracks voice time with live leaderboards matching JSON template exactly
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
import os
from typing import Optional, List, Dict
import pytz
from dotenv import load_dotenv
import logging
import asyncio
from .leaderboard_config import (
    EMBED_COLOR, Emojis, Images, VoiceTemplates, 
    PeriodConfig, ButtonConfig, LeaderboardSettings
)

load_dotenv()


class VoiceLeaderboardPaginator(discord.ui.View):
    def __init__(self, cog, guild_id: int, period: str, page: int = 0, vibe_channel_id: int = None):
        self.cog = cog
        self.guild_id = guild_id
        self.period = period
        self.page = page
        self.vibe_channel_id = vibe_channel_id
        self.max_pages_cache = None  # Cache max pages
        
        # Initialize the view with timeout for memory management
        super().__init__(timeout=300)  # 5 minutes timeout
        
        # Clear default buttons (they're added by decorators)
        self.clear_items()
        
        # Only add pagination buttons for daily period
        if period == 'daily':
            left_emoji = discord.PartialEmoji(name=Emojis.LEFT_BUTTON_NAME, id=Emojis.LEFT_BUTTON_ID)
            right_emoji = discord.PartialEmoji(name=Emojis.RIGHT_BUTTON_NAME, id=Emojis.RIGHT_BUTTON_ID)
            
            # Create pagination buttons
            left_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=left_emoji,
                custom_id=f"{ButtonConfig.VOICE_LEFT_PREFIX}_{period}_{guild_id}"
            )
            left_button.callback = self.previous_page
            
            right_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=right_emoji,
                custom_id=f"{ButtonConfig.VOICE_RIGHT_PREFIX}_{period}_{guild_id}"
            )
            right_button.callback = self.next_page
            
            self.add_item(left_button)
            self.add_item(right_button)
        
        # Add Join the Vibe button for monthly and weekly only
        if period in ['monthly', 'weekly'] and vibe_channel_id:
            vibe_emoji = discord.PartialEmoji(name='original_Peek', id=1429151221939441776, animated=True)
            vibe_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Join the Vibe",
                emoji=vibe_emoji,
                url=f"https://discord.com/channels/{guild_id}/{vibe_channel_id}"
            )
            self.add_item(vibe_button)
    
    async def previous_page(self, interaction: discord.Interaction):
        try:
            # Check if interaction is still valid
            if interaction.response.is_done():
                self.cog.logger.warning("Interaction already responded to in previous_page")
                return
            
            if self.page > 0:
                self.page -= 1
                # Update the embed for this period
                new_embed = await self.cog._build_period_embed(self.guild_id, self.period, page=self.page)
                await interaction.response.edit_message(embed=new_embed, view=self)
            else:
                await interaction.response.send_message("You're on the first page!", ephemeral=True)
        except discord.NotFound:
            self.cog.logger.warning("Interaction expired in previous_page")
        except discord.InteractionResponded:
            self.cog.logger.warning("Interaction already responded in previous_page")
        except Exception as e:
            self.cog.logger.error(f"Error in previous_page (voice): {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ An error occurred while updating the leaderboard.", ephemeral=True)
            except Exception as inner_e:
                self.cog.logger.error(f"Failed to send error message: {inner_e}")
    
    async def next_page(self, interaction: discord.Interaction):
        try:
            # Check if interaction is still valid
            if interaction.response.is_done():
                self.cog.logger.warning("Interaction already responded to in next_page")
                return
            
            # Use cached max_pages if available
            if self.max_pages_cache is None:
                stats = await self.cog._get_top_users(self.guild_id, self.period, limit=LeaderboardSettings.MAX_MEMBERS_FETCH)
                if not stats:
                    await interaction.response.send_message("No data available!", ephemeral=True)
                    return
                self.max_pages_cache = max(0, (len(stats) - 1) // LeaderboardSettings.MEMBERS_PER_PAGE)
            
            if self.page < self.max_pages_cache:
                self.page += 1
                # Update the embed for this period
                new_embed = await self.cog._build_period_embed(self.guild_id, self.period, page=self.page)
                await interaction.response.edit_message(embed=new_embed, view=self)
            else:
                await interaction.response.send_message("You're on the last page!", ephemeral=True)
        except discord.NotFound:
            self.cog.logger.warning("Interaction expired in next_page")
        except discord.InteractionResponded:
            self.cog.logger.warning("Interaction already responded in next_page")
        except Exception as e:
            self.cog.logger.error(f"Error in next_page (voice): {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ An error occurred while updating the leaderboard.", ephemeral=True)
            except Exception as inner_e:
                self.cog.logger.error(f"Failed to send error message: {inner_e}")


class VoiceLeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None
        self.voice_sessions = {}
        self.voice_sessions_lock = asyncio.Lock()  # Prevent race conditions
        self.session_save_queue = {}  # Buffer for pending saves
        self.max_session_duration = 10080  # Max 7 days in minutes
        self.last_daily_reset = {}  # {guild_id: datetime}
        self.last_weekly_reset = {}  # {guild_id: datetime}
        self.last_monthly_reset = {}  # {guild_id: datetime}
        self.logger = logging.getLogger('discord.bot.voice_leaderboard')
    
    async def cog_load(self):
        # Reuse bot's shared MongoDB connection
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.mongo_client = self.bot.mongo_client
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Voice Leaderboard Cog: Reusing shared MongoDB connection")
        else:
            # Fallback: create new connection if shared one doesn't exist
            mongo_url = os.getenv('MONGO_URL')
            if not mongo_url:
                raise ValueError("MONGO_URL not found in environment variables")
            self.mongo_client = AsyncIOMotorClient(mongo_url)
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Voice Leaderboard Cog: MongoDB connected")
        
        # Create indexes for optimal performance
        await self._create_indexes()
        
        # Tasks will auto-start when bot is ready (via before_loop hooks)
        # Don't manually start them here to avoid deadlock during cog loading
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Start tasks when bot is ready to avoid deadlock during cog loading"""
        if not self.update_leaderboards.is_running():
            self.update_leaderboards.start()
            self.daily_reset.start()
            self.weekly_reset.start()
            self.monthly_reset.start()
            self.save_voice_sessions_periodically.start()  # Periodic session saves
            self.periodic_session_cleanup.start()  # Hourly cleanup
            self.logger.info("Voice leaderboard tasks started")
    
    async def cog_unload(self):
        await self._save_all_voice_sessions()
        self.update_leaderboards.cancel()
        self.daily_reset.cancel()
        self.weekly_reset.cancel()
        self.monthly_reset.cancel()
        self.save_voice_sessions_periodically.cancel()
        self.periodic_session_cleanup.cancel()
        # Don't close shared MongoDB connection - it's managed by the bot
        # Only close if we created our own connection
        if self.mongo_client and not hasattr(self.bot, 'mongo_client'):
            self.mongo_client.close()
    
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Clean up data when bot is removed from a guild"""
        try:
            self.logger.info(f"Bot removed from guild {guild.name} ({guild.id}), cleaning up voice leaderboard data")
            
            # Save any active voice sessions for this guild before cleanup
            async with self.voice_sessions_lock:
                sessions_to_remove = [(gid, uid) for (gid, uid) in self.voice_sessions.keys() if gid == guild.id]
                for session_key in sessions_to_remove:
                    del self.voice_sessions[session_key]
            
            # Delete leaderboard messages
            await self.db.leaderboard_messages.delete_one({'guild_id': guild.id, 'type': 'voice'})
            
            # Delete user stats (only voice fields - chat cog will handle chat)
            result = await self.db.user_stats.update_many(
                {'guild_id': guild.id},
                {'$set': {'voice_daily': 0, 'voice_weekly': 0, 'voice_monthly': 0, 'voice_alltime': 0}}
            )
            
            # Update guild config to disable voice
            await self.db.guild_configs.update_one(
                {'guild_id': guild.id},
                {'$set': {'voice_enabled': False}}
            )
            
            self.logger.info(f"Voice leaderboard cleanup complete for guild {guild.id}")
        except Exception as e:
            self.logger.error(f"Error cleaning up voice data for guild {guild.id}: {e}", exc_info=True)
    
    async def _initialize_voice_sessions(self):
        # Bot is already ready when this is called from before_loop
        for guild in self.bot.guilds:
            config = await self._get_guild_config(guild.id)
            if not config or not config.get('voice_enabled'):
                continue
            for channel in guild.voice_channels:
                if guild.afk_channel and channel.id == guild.afk_channel.id:
                    continue
                for member in channel.members:
                    if not member.bot:
                        self.voice_sessions[(guild.id, member.id)] = datetime.utcnow()
        self.logger.info(f"Initialized {len(self.voice_sessions)} active voice sessions")
    
    async def _save_all_voice_sessions(self):
        """Save all active voice sessions to database before shutdown with memory cleanup"""
        if not self.voice_sessions:
            return
        
        async with self.voice_sessions_lock:
            saved_count = 0
            error_count = 0
            sessions_to_save = list(self.voice_sessions.items())
            
            # Process in batches to avoid overwhelming the database
            batch_size = 50
            for i in range(0, len(sessions_to_save), batch_size):
                batch = sessions_to_save[i:i + batch_size]
                for (guild_id, user_id), joined_at in batch:
                    try:
                        # Validate session data
                        if not isinstance(joined_at, datetime):
                            self.logger.warning(f"Invalid session data for user {user_id}: {joined_at}")
                            continue
                            
                        minutes = (datetime.utcnow() - joined_at).total_seconds() / 60
                        if minutes > 0:
                            await self._increment_voice_time(guild_id, user_id, minutes)
                            saved_count += 1
                    except Exception as e:
                        error_count += 1
                        self.logger.error(f"Error saving voice session for user {user_id} in guild {guild_id}: {e}")
                
                # Small delay between batches
                if i + batch_size < len(sessions_to_save):
                    await asyncio.sleep(0.1)
            
            # Clear all sessions and save queue
            self.voice_sessions.clear()
            self.session_save_queue.clear()
            
            if saved_count > 0 or error_count > 0:
                self.logger.info(f"Session save complete: {saved_count} saved, {error_count} errors")
    
    async def _create_indexes(self):
        """Create database indexes for voice leaderboard collections"""
        try:
            # Guild configs indexes
            await self.db.guild_configs.create_index('guild_id', unique=True)
            await self.db.guild_configs.create_index([('voice_enabled', 1)])
            
            # User stats indexes for voice queries
            await self.db.user_stats.create_index([('guild_id', 1), ('user_id', 1)], unique=True)
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_daily', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_weekly', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_monthly', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_alltime', -1)])
            
            # Leaderboard messages indexes
            await self.db.leaderboard_messages.create_index([('guild_id', 1), ('type', 1)], unique=True)
            await self.db.leaderboard_messages.create_index([('channel_id', 1)])
            
            # Weekly history indexes for archives
            await self.db.weekly_history.create_index([('guild_id', 1), ('type', 1), ('period', 1), ('reset_date', -1)])
            
            # TTL index to auto-delete archives older than 1 year (31536000 seconds)
            # Drop any conflicting old index first
            try:
                await self.db.weekly_history.drop_index('reset_date_1')
            except:
                pass  # Index doesn't exist, that's fine
            
            await self.db.weekly_history.create_index(
                [('reset_date', 1)],
                expireAfterSeconds=31536000,
                name='archive_ttl_1year'
            )
            
            self.logger.info("Voice Leaderboard: Database indexes created/verified")
        except Exception as e:
            self.logger.warning(f"Error creating indexes (may already exist): {e}")
    
    async def _get_guild_config(self, guild_id: int) -> Optional[Dict]:
        return await self.db.guild_configs.find_one({'guild_id': guild_id})
    
    async def _ensure_guild_config(self, guild_id: int) -> Dict:
        config = await self._get_guild_config(guild_id)
        if not config:
            config = {'guild_id': guild_id, 'voice_enabled': False, 'voice_channel_id': None, 'timezone': 'UTC', 'leaderboard_limit': 10, 'created_at': datetime.utcnow()}
            await self.db.guild_configs.insert_one(config)
        return config
    
    async def _increment_voice_time(self, guild_id: int, user_id: int, minutes: float, max_retries: int = 3):
        """Increment voice time with validation, error handling, and retry logic"""
        # Validate input
        if minutes <= 0:
            self.logger.debug(f"Skipping voice increment for user {user_id}: minutes={minutes}")
            return
        
        # Round to avoid float precision issues
        minutes = round(minutes, 2)
        
        # Validate minutes to prevent data corruption
        if minutes > self.max_session_duration:
            self.logger.warning(f"Suspicious voice time detected: {minutes} minutes for user {user_id} in guild {guild_id}. Capping at {self.max_session_duration}.")
            minutes = self.max_session_duration
        elif minutes > 1440:  # 1-7 days (log but allow - some users stay in VC long-term)
            self.logger.info(f"Long voice session detected: {minutes:.1f} minutes ({minutes/60:.1f} hours) for user {user_id} in guild {guild_id}")
        
        # Retry logic for database operations
        for attempt in range(max_retries):
            try:
                result = await self.db.user_stats.update_one(
                    {'guild_id': guild_id, 'user_id': user_id},
                    {
                        '$inc': {
                            'voice_daily': minutes, 
                            'voice_weekly': minutes, 
                            'voice_monthly': minutes, 
                            'voice_alltime': minutes
                        }, 
                        '$set': {'last_update': datetime.utcnow()}
                    },
                    upsert=True
                )
                if result.acknowledged:
                    return
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
                    self.logger.warning(f"Retry {attempt + 1}/{max_retries} for voice increment: {e}")
                else:
                    self.logger.error(f"Failed to increment voice time after {max_retries} attempts for user {user_id} in guild {guild_id}: {e}")
    
    async def _get_top_users(self, guild_id: int, period: str, limit: int = 100) -> List[Dict]:
        """Get top users with error handling and validation"""
        try:
            field_map = {'daily': 'voice_daily', 'weekly': 'voice_weekly', 'monthly': 'voice_monthly', 'alltime': 'voice_alltime'}
            field = field_map.get(period, 'voice_weekly')
            
            # Validate limit to prevent excessive queries
            limit = min(limit, LeaderboardSettings.MAX_MEMBERS_FETCH)
            
            cursor = self.db.user_stats.find({'guild_id': guild_id, field: {'$gt': 0}}).sort(field, -1).limit(limit)
            return await cursor.to_list(length=limit)
        except Exception as e:
            self.logger.error(f"Error fetching top users for guild {guild_id}, period {period}: {e}")
            return []
    
    async def _get_last_month_winner(self, guild_id: int) -> Optional[Dict]:
        """Get last month's top active member from archive"""
        try:
            # Find the most recent monthly archive for this guild
            cursor = self.db.weekly_history.find({
                'guild_id': guild_id,
                'type': 'voice',
                'period': 'monthly'
            }).sort('reset_date', -1).limit(1)
            
            archives = await cursor.to_list(length=1)
            if not archives:
                return None
            
            archive = archives[0]
            stats = archive.get('stats', [])
            
            if not stats:
                return None
            
            # Find the user with highest voice_monthly
            top_user = max(stats, key=lambda x: x.get('voice_monthly', 0))
            
            if top_user.get('voice_monthly', 0) > 0:
                return {
                    'user_id': top_user['user_id'],
                    'minutes': top_user['voice_monthly'],
                    'month': archive['reset_date']
                }
            
            return None
        except Exception as e:
            self.logger.error(f"Error fetching last month winner for guild {guild_id}: {e}")
            return None
    
    async def _get_leaderboard_message(self, guild_id: int) -> Optional[Dict]:
        return await self.db.leaderboard_messages.find_one({'guild_id': guild_id, 'type': 'voice'})
    
    async def _save_leaderboard_messages(self, guild_id: int, channel_id: int, daily_id: int, weekly_id: int, monthly_id: int):
        """Save all three leaderboard message IDs"""
        await self.db.leaderboard_messages.update_one(
            {'guild_id': guild_id, 'type': 'voice'},
            {'$set': {
                'channel_id': channel_id,
                'daily_message_id': daily_id,
                'weekly_message_id': weekly_id,
                'monthly_message_id': monthly_id,
                'last_update': datetime.utcnow()
            }},
            upsert=True
        )
    
    async def _save_leaderboard_message(self, guild_id: int, channel_id: int, message_id: int):
        """Legacy method - kept for compatibility"""
        await self.db.leaderboard_messages.update_one(
            {'guild_id': guild_id, 'type': 'voice'},
            {'$set': {'channel_id': channel_id, 'daily_message_id': message_id, 'last_update': datetime.utcnow()}},
            upsert=True
        )
    
    def _format_time(self, minutes: float) -> str:
        """Format minutes into human-readable time string with validation"""
        # Validate and sanitize input
        if not isinstance(minutes, (int, float)):
            self.logger.error(f"Invalid type for minutes: {type(minutes)}")
            return "0m"
        
        if minutes < 0:
            self.logger.warning(f"Negative time value received: {minutes}")
            minutes = 0
        elif minutes > self.max_session_duration * 30:  # Sanity check for display
            self.logger.warning(f"Extremely large time value: {minutes}")
            minutes = self.max_session_duration * 30
        
        # Round to avoid float display issues
        minutes = round(minutes)
        
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        mins = minutes % 60
        if hours >= 24:
            days = hours // 24
            hours = hours % 24
            if hours == 0:
                return f"{days}d"
            return f"{days}d {hours}h"
        if mins == 0:
            return f"{hours} hours"
        return f"{hours}h {mins}m"
    
    async def _build_all_embeds(self, guild_id: int, period: str, page: int = 0) -> List[discord.Embed]:
        """Build ALL embeds: header image + monthly + weekly + daily"""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return [discord.Embed(title="Error", description="Guild not found")]
        
        embeds = []
        
        # Embed 0: Header image only
        header_embed = discord.Embed(color=EMBED_COLOR)
        header_embed.set_image(url=Images.VOICE_HEADER)
        embeds.append(header_embed)
        
        # Embeds 1-3: Monthly, Weekly, Daily
        periods = ['monthly', 'weekly', 'daily']
        for p in periods:
            embed = await self._build_period_embed(guild_id, p, page)
            embeds.append(embed)
        
        return embeds
    
    async def _build_period_embed(self, guild_id: int, period: str, page: int) -> discord.Embed:
        """Build a single period embed with dynamic data"""
        guild = self.bot.get_guild(guild_id)
        stats = await self._get_top_users(guild_id, period, limit=LeaderboardSettings.MAX_MEMBERS_FETCH)
        
        start_idx = page * LeaderboardSettings.MEMBERS_PER_PAGE
        end_idx = start_idx + LeaderboardSettings.MEMBERS_PER_PAGE
        page_stats = stats[start_idx:end_idx]
        total_minutes = sum(s.get(f'voice_{period}', 0) for s in stats)
        total_hours = int(total_minutes // 60)
        
        # Build leaderboard lines
        leaderboard_lines = []
        for idx, user_stat in enumerate(page_stats, start=start_idx + 1):
            user = guild.get_member(user_stat['user_id'])
            if user:
                username = user.display_name
            else:
                # For users who left the server, use consistent hash format
                user_id_str = str(user_stat['user_id'])
                hash_char = chr(65 + (user_stat['user_id'] % 26))  # A-Z based on ID
                username = f"User{hash_char}-{user_id_str[-6:]}"  # e.g., UserB-123456
            minutes = user_stat.get(f'voice_{period}', 0)
            time_str = self._format_time(minutes)
            # Truncate long usernames
            if len(username) > 20:
                username = username[:17] + "..."
            leaderboard_lines.append(f"- `{idx:02d}` | {Emojis.USER} `{username}` {Emojis.ARROW} `{time_str}`")
        
        leaderboard_text = "\n".join(leaderboard_lines) if leaderboard_lines else "No data yet"
        
        # Top user
        top_user_name = "No one yet"
        top_user_time = "0m"
        if stats:
            top_user = guild.get_member(stats[0]['user_id'])
            if top_user:
                top_user_name = top_user.display_name
            else:
                # For users who left the server, use consistent hash format
                user_id_str = str(stats[0]['user_id'])
                hash_char = chr(65 + (stats[0]['user_id'] % 26))
                top_user_name = f"User{hash_char}-{user_id_str[-6:]}"
            # Truncate long names
            if len(top_user_name) > 20:
                top_user_name = top_user_name[:17] + "..."
            top_user_minutes = stats[0].get(f'voice_{period}', 0)
            top_user_time = self._format_time(top_user_minutes)
        
        # Period display
        if period == 'monthly':
            period_display = datetime.utcnow().strftime('%B %Y')
        else:
            period_display = PeriodConfig.PERIOD_DISPLAY_NAMES.get(period, 'Unknown')
        
        period_title = PeriodConfig.VOICE_TITLES.get(period, 'Rankings')
        subtitle = PeriodConfig.VOICE_SUBTITLES.get(period, 'Most active voice members are here!')
        
        # Next reset time
        config = await self._get_guild_config(guild_id)
        tz = pytz.timezone(config.get('timezone', LeaderboardSettings.DEFAULT_TIMEZONE) if config else LeaderboardSettings.DEFAULT_TIMEZONE)
        now = datetime.now(tz)
        
        if period == 'daily':
            next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        elif period == 'weekly':
            days_until_sunday = (LeaderboardSettings.WEEKLY_RESET_DAY - now.weekday()) % 7
            if days_until_sunday == 0 and now.hour >= LeaderboardSettings.WEEKLY_RESET_HOUR:
                days_until_sunday = 7
            next_reset = now.replace(hour=LeaderboardSettings.WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
        else:  # monthly
            # Calculate next month's first day in the same timezone
            if now.month == 12:
                next_reset = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                next_reset = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        
        reset_timestamp = int(next_reset.timestamp())
        
        # Get last month's winner for monthly embeds
        last_month_winner_text = None
        if period == 'monthly':
            last_month_data = await self._get_last_month_winner(guild_id)
            if last_month_data:
                winner_member = guild.get_member(last_month_data['user_id'])
                if winner_member:
                    winner_name = winner_member.display_name
                else:
                    # User left, use hash format
                    user_id_str = str(last_month_data['user_id'])
                    hash_char = chr(65 + (last_month_data['user_id'] % 26))
                    winner_name = f"User{hash_char}-{user_id_str[-6:]}"
                
                # Format time
                minutes = last_month_data['minutes']
                if minutes >= 60:
                    hours = int(minutes // 60)
                    mins = int(minutes % 60)
                    time_str = f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
                else:
                    time_str = f"{int(minutes)}m"
                
                # Format the month name
                month_name = last_month_data['month'].strftime('%B %Y')
                last_month_winner_text = f"`{winner_name}` with `{time_str}` in {month_name}"
        
        # Build description using config template
        description = VoiceTemplates.build_description(
            period_title=period_title,
            total_hours=total_hours,
            period_display=period_display,
            top_user_name=top_user_name,
            top_user_time=top_user_time,
            leaderboard_text=leaderboard_text,
            reset_timestamp=reset_timestamp,
            subtitle=subtitle,
            period=period,
            last_month_winner=last_month_winner_text
        )
        
        # Create embed
        embed = discord.Embed(description=description, color=EMBED_COLOR, timestamp=datetime.utcnow())
        
        # Footer
        footer_text = VoiceTemplates.FOOTER_TEXT
        if len(stats) > LeaderboardSettings.MEMBERS_PER_PAGE:
            total_pages = (len(stats) - 1) // LeaderboardSettings.MEMBERS_PER_PAGE + 1
            footer_text = f"Page {page + 1}/{total_pages} • {footer_text}"
        embed.set_footer(text=footer_text, icon_url=Images.FOOTER_ICON)
        
        # Divider image
        embed.set_image(url=Images.DIVIDER)
        
        return embed
    
    async def _create_full_leaderboard_message(self, channel: discord.TextChannel, guild_id: int, vibe_channel_id: int = None):
        """Create separate leaderboard messages for each period with individual buttons"""
        try:
            # Send header image
            header_embed = discord.Embed(color=EMBED_COLOR)
            header_embed.set_image(url=Images.VOICE_HEADER)
            await channel.send(embed=header_embed)
            
            # Send monthly embed with Join the Vibe button
            monthly_embed = await self._build_period_embed(guild_id, 'monthly', page=0)
            monthly_view = VoiceLeaderboardPaginator(self, guild_id, 'monthly', page=0, vibe_channel_id=vibe_channel_id)
            monthly_message = await channel.send(embed=monthly_embed, view=monthly_view)
            
            # Send weekly embed with Join the Vibe button
            weekly_embed = await self._build_period_embed(guild_id, 'weekly', page=0)
            weekly_view = VoiceLeaderboardPaginator(self, guild_id, 'weekly', page=0, vibe_channel_id=vibe_channel_id)
            weekly_message = await channel.send(embed=weekly_embed, view=weekly_view)
            
            # Send daily embed with pagination buttons
            daily_embed = await self._build_period_embed(guild_id, 'daily', page=0)
            daily_view = VoiceLeaderboardPaginator(self, guild_id, 'daily', page=0, vibe_channel_id=vibe_channel_id)
            daily_message = await channel.send(embed=daily_embed, view=daily_view)
            
            # Save ALL message IDs for updates
            await self._save_leaderboard_messages(guild_id, channel.id, daily_message.id, weekly_message.id, monthly_message.id)
            self.logger.info(f"Created voice leaderboard messages for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Failed to create leaderboard for guild {guild_id}: {e}", exc_info=True)
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Track voice activity with improved race condition handling and memory management"""
        # Filter bots immediately
        if member.bot:
            return
        
        try:
            config = await self._get_guild_config(member.guild.id)
            if not config or not config.get('voice_enabled'):
                return
            
            guild_id = member.guild.id
            user_id = member.id
            session_key = (guild_id, user_id)
            current_time = datetime.utcnow()
            
            # Clean up old sessions periodically (memory leak prevention)
            if len(self.voice_sessions) > 1000:
                await self._cleanup_stale_sessions()
            
            # Determine if channels are AFK
            afk_channel_id = member.guild.afk_channel.id if member.guild.afk_channel else None
            before_is_afk = before.channel and afk_channel_id and before.channel.id == afk_channel_id
            after_is_afk = after.channel and afk_channel_id and after.channel.id == afk_channel_id
            
            async with self.voice_sessions_lock:
                # User joined a voice channel (not AFK)
                if before.channel is None and after.channel is not None and not after_is_afk:
                    self.voice_sessions[session_key] = current_time
                    self.logger.debug(f"Voice session started: {member.display_name} in guild {guild_id}")
                
                # User left a voice channel (not from AFK)
                elif before.channel is not None and after.channel is None and not before_is_afk:
                    if session_key in self.voice_sessions:
                        joined_at = self.voice_sessions.pop(session_key, None)
                        if joined_at and isinstance(joined_at, datetime):
                            minutes = (current_time - joined_at).total_seconds() / 60
                            if 0 < minutes < self.max_session_duration:  # Validate reasonable time
                                # Queue the save instead of immediate write
                                self.session_save_queue[session_key] = minutes
                                # Process queue if it gets too large
                                if len(self.session_save_queue) >= 10:
                                    await self._process_save_queue()
                            elif minutes >= self.max_session_duration:
                                self.logger.warning(f"Session exceeded max duration for {member.display_name}: {minutes:.1f} minutes")
                                await self._increment_voice_time(guild_id, user_id, self.max_session_duration)
                
                # User moved between channels
                elif before.channel != after.channel and before.channel is not None and after.channel is not None:
                    # Moving to AFK from active channel
                    if not before_is_afk and after_is_afk:
                        if session_key in self.voice_sessions:
                            joined_at = self.voice_sessions.pop(session_key, None)
                            if joined_at and isinstance(joined_at, datetime):
                                minutes = (current_time - joined_at).total_seconds() / 60
                                if 0 < minutes < self.max_session_duration:
                                    self.session_save_queue[session_key] = minutes
                                    if len(self.session_save_queue) >= 10:
                                        await self._process_save_queue()
                    
                    # Moving from AFK to active channel
                    elif before_is_afk and not after_is_afk:
                        self.voice_sessions[session_key] = current_time
                        self.logger.debug(f"Moved from AFK: {member.display_name}")
                    
                    # Moving between active channels (keep session alive)
                    elif not before_is_afk and not after_is_afk:
                        # Validate existing session
                        if session_key not in self.voice_sessions:
                            self.voice_sessions[session_key] = current_time
                            self.logger.debug(f"Session missing, started new: {member.display_name}")
        except Exception as e:
            self.logger.error(f"Error in voice state update for {member.id}: {e}", exc_info=True)
    
    @tasks.loop(minutes=5)
    async def update_leaderboards(self):
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue
                try:
                    msg_data = await self._get_leaderboard_message(guild_id)
                    vibe_channel_id = config.get('vibe_channel_id')
                    
                    # If no message data exists, try to create messages if channel is configured
                    if not msg_data:
                        voice_channel_id = config.get('voice_channel_id')
                        if voice_channel_id:
                            channel = guild.get_channel(voice_channel_id)
                            if channel:
                                self.logger.info(f"No leaderboard messages found for guild {guild_id}, creating...")
                                await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                        continue
                    
                    channel = guild.get_channel(msg_data['channel_id'])
                    if not channel:
                        # Channel was deleted, clean up database reference
                        self.logger.warning(f"Voice leaderboard channel {msg_data['channel_id']} not found for guild {guild_id}, cleaning up")
                        await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'voice'})
                        continue
                    
                    # Get message IDs (support both old and new format)
                    daily_id = msg_data.get('daily_message_id') or msg_data.get('message_id')
                    weekly_id = msg_data.get('weekly_message_id')
                    monthly_id = msg_data.get('monthly_message_id')
                    
                    messages_missing = False
                    
                    # Update all three messages
                    try:
                        # Update daily message
                        if daily_id:
                            try:
                                daily_message = await channel.fetch_message(daily_id)
                                if daily_message.author.id == self.bot.user.id:
                                    daily_embed = await self._build_period_embed(guild_id, 'daily', page=0)
                                    daily_view = VoiceLeaderboardPaginator(self, guild_id, 'daily', page=0, vibe_channel_id=vibe_channel_id)
                                    await daily_message.edit(embed=daily_embed, view=daily_view)
                                else:
                                    messages_missing = True
                            except discord.NotFound:
                                messages_missing = True
                        
                        # Update weekly message
                        if weekly_id:
                            try:
                                weekly_message = await channel.fetch_message(weekly_id)
                                if weekly_message.author.id == self.bot.user.id:
                                    weekly_embed = await self._build_period_embed(guild_id, 'weekly', page=0)
                                    weekly_view = VoiceLeaderboardPaginator(self, guild_id, 'weekly', page=0, vibe_channel_id=vibe_channel_id)
                                    await weekly_message.edit(embed=weekly_embed, view=weekly_view)
                                else:
                                    messages_missing = True
                            except discord.NotFound:
                                messages_missing = True
                        
                        # Update monthly message
                        if monthly_id:
                            try:
                                monthly_message = await channel.fetch_message(monthly_id)
                                if monthly_message.author.id == self.bot.user.id:
                                    monthly_embed = await self._build_period_embed(guild_id, 'monthly', page=0)
                                    monthly_view = VoiceLeaderboardPaginator(self, guild_id, 'monthly', page=0, vibe_channel_id=vibe_channel_id)
                                    await monthly_message.edit(embed=monthly_embed, view=monthly_view)
                                else:
                                    messages_missing = True
                            except discord.NotFound:
                                messages_missing = True
                        
                        # If any messages are missing, recreate all
                        if messages_missing or not all([daily_id, weekly_id, monthly_id]):
                            self.logger.info(f"Voice leaderboard messages missing or invalid for guild {guild_id}, recreating")
                            await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'voice'})
                            await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                            
                    except discord.Forbidden as e:
                        self.logger.error(
                            f"Permission denied editing message for guild {guild_id}: {e}. "
                            f"Removing invalid reference and recreating."
                        )
                        await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'voice'})
                        await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                    except discord.HTTPException as e:
                        self.logger.error(f"HTTP error updating voice leaderboard for guild {guild_id}: {e}")
                except Exception as e:
                    self.logger.error(f"Error updating voice leaderboard for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in update_leaderboards task: {e}", exc_info=True)
    
    @update_leaderboards.before_loop
    async def before_update_leaderboards(self):
        await self.bot.wait_until_ready()
        # Initialize voice sessions after bot is ready
        await self._initialize_voice_sessions()
    
    @tasks.loop(minutes=5)  # Check every 5 minutes for zero chance of missing
    async def weekly_reset(self):
        """Check for weekly reset with missed reset detection
        
        NOTE: If Star of the Week system is configured, it handles weekly resets.
        This task only runs for guilds without Star system or as a backup.
        """
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                
                # Check if Star of the Week system is managing resets for this guild
                star_config = await self.db.star_configs.find_one({'guild_id': guild_id})
                if star_config:
                    # Star system is configured - it will handle weekly resets
                    self.logger.debug(f"Star system manages weekly resets for guild {guild_id}, skipping")
                    continue
                
                tz_name = config.get('timezone', 'UTC')
                try:
                    # Validate timezone
                    if tz_name not in pytz.all_timezones:
                        self.logger.warning(f"Invalid timezone '{tz_name}' for guild {guild_id}, using UTC")
                        tz_name = 'UTC'
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    
                    # Multiple checks to NEVER miss weekly reset
                    last_reset_time = config.get('last_voice_weekly_reset')
                    should_reset = False
                    
                    if last_reset_time:
                        hours_since = (datetime.utcnow() - last_reset_time).total_seconds() / 3600
                        days_since = hours_since / 24
                        
                        # Check 1: Has it been at least 6.5 days?
                        if days_since >= 6.5:
                            if now.weekday() == 6 and now.hour >= 12:  # Sunday noon or later
                                should_reset = True
                                self.logger.info(f"Voice weekly reset for guild {guild_id}: {days_since:.1f} days since last")
                            elif now.weekday() == 0:  # Monday (missed Sunday)
                                should_reset = True
                                self.logger.warning(f"Missed Sunday voice reset for guild {guild_id}, doing it now")
                            elif days_since >= 7.0:  # Full week passed
                                should_reset = True
                                self.logger.warning(f"Full week passed for voice guild {guild_id}: {days_since:.1f} days")
                    else:
                        # Never reset before - do it now if enabled
                        if config.get('voice_enabled'):
                            should_reset = True
                            self.logger.info(f"First voice weekly reset for guild {guild_id}")
                    
                    if should_reset:
                        await self._reset_weekly_stats(guild_id)
                        # Persist reset time to database
                        await self.db.guild_configs.update_one(
                            {'guild_id': guild_id},
                            {'$set': {'last_voice_weekly_reset': datetime.utcnow()}}
                        )
                        self.last_weekly_reset[guild_id] = now
                
                except pytz.exceptions.UnknownTimeZoneError:
                    self.logger.error(f"Invalid timezone for guild {guild_id}: {tz_name}")
                except Exception as e:
                    self.logger.error(f"Error checking weekly reset for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in weekly_reset task: {e}", exc_info=True)
    
    @weekly_reset.before_loop
    async def before_weekly_reset(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)  # Check every 5 minutes
    async def daily_reset(self):
        """Check for daily reset (midnight guild time)"""
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    # Validate timezone
                    if tz_name not in pytz.all_timezones:
                        self.logger.warning(f"Invalid timezone '{tz_name}' for guild {guild_id}, using UTC")
                        tz_name = 'UTC'
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    # Check for reset window (midnight to 1 AM)
                    if 0 <= now.hour < 1:
                        # Check if already reset today
                        last_reset = self.last_daily_reset.get(guild_id)
                        if last_reset and last_reset.date() == now.date():
                            continue  # Already reset today
                        
                        await self._reset_daily_stats(guild_id)
                        self.last_daily_reset[guild_id] = now
                except pytz.exceptions.UnknownTimeZoneError:
                    self.logger.error(f"Invalid timezone for guild {guild_id}: {tz_name}")
                except Exception as e:
                    self.logger.error(f"Error checking daily reset for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in daily_reset task: {e}", exc_info=True)
    
    @daily_reset.before_loop
    async def before_daily_reset(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)  # Check every 5 minutes
    async def monthly_reset(self):
        """Check for monthly reset (1st of month midnight guild time)"""
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    # Validate timezone
                    if tz_name not in pytz.all_timezones:
                        self.logger.warning(f"Invalid timezone '{tz_name}' for guild {guild_id}, using UTC")
                        tz_name = 'UTC'
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    # Check for monthly reset window (1st of month, midnight to 1 AM)
                    if now.day == 1 and 0 <= now.hour < 1:
                        # Check if already reset this month (use database as source of truth)
                        last_db_reset = config.get('last_voice_monthly_reset')
                        if last_db_reset:
                            last_reset_tz = last_db_reset.replace(tzinfo=pytz.UTC).astimezone(tz)
                            if last_reset_tz.month == now.month and last_reset_tz.year == now.year:
                                continue  # Already reset this month
                        
                        await self._reset_monthly_stats(guild_id)
                        self.last_monthly_reset[guild_id] = now
                        # Persist to database for crash recovery
                        await self.db.guild_configs.update_one(
                            {'guild_id': guild_id},
                            {'$set': {'last_voice_monthly_reset': datetime.utcnow()}}
                        )
                except Exception as e:
                    self.logger.error(f"Error checking monthly reset for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in monthly_reset task: {e}", exc_info=True)
    
    @monthly_reset.before_loop
    async def before_monthly_reset(self):
        await self.bot.wait_until_ready()
    
    async def _cleanup_stale_sessions(self):
        """Remove stale sessions to prevent memory leaks"""
        try:
            current_time = datetime.utcnow()
            stale_sessions = []
            
            for session_key, joined_at in self.voice_sessions.items():
                if isinstance(joined_at, datetime):
                    duration = (current_time - joined_at).total_seconds() / 60
                    # Remove sessions older than max duration
                    if duration > self.max_session_duration:
                        stale_sessions.append(session_key)
            
            for session_key in stale_sessions:
                guild_id, user_id = session_key
                joined_at = self.voice_sessions.pop(session_key, None)
                if joined_at:
                    # Save the max duration
                    await self._increment_voice_time(guild_id, user_id, self.max_session_duration)
                    self.logger.warning(f"Cleaned up stale session for user {user_id} in guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error cleaning up stale sessions: {e}")
    
    async def _process_save_queue(self):
        """Process queued voice time saves"""
        if not self.session_save_queue:
            return
        
        try:
            queue_copy = self.session_save_queue.copy()
            self.session_save_queue.clear()
            
            for (guild_id, user_id), minutes in queue_copy.items():
                await self._increment_voice_time(guild_id, user_id, minutes)
                self.logger.debug(f"Processed queued save: {minutes:.2f} minutes for user {user_id}")
        except Exception as e:
            self.logger.error(f"Error processing save queue: {e}")
    
    @tasks.loop(minutes=10)
    async def save_voice_sessions_periodically(self):
        """
        Periodically save active voice sessions to prevent data loss on crashes.
        Runs every 10 minutes to update ongoing sessions.
        """
        try:
            # Process any pending saves first
            await self._process_save_queue()
            
            if not self.voice_sessions:
                return
            
            async with self.voice_sessions_lock:
                saved_count = 0
                error_count = 0
                current_time = datetime.utcnow()
                sessions_to_update = []
                
                for (guild_id, user_id), joined_at in list(self.voice_sessions.items()):
                    try:
                        if not isinstance(joined_at, datetime):
                            self.logger.warning(f"Invalid session data for user {user_id}: {joined_at}")
                            continue
                        
                        # Calculate time since last save (or join)
                        minutes = (current_time - joined_at).total_seconds() / 60
                        
                        if minutes > 0 and minutes < self.max_session_duration:
                            sessions_to_update.append(((guild_id, user_id), minutes))
                        elif minutes >= self.max_session_duration:
                            # Cap at max duration and remove session
                            sessions_to_update.append(((guild_id, user_id), self.max_session_duration))
                            del self.voice_sessions[(guild_id, user_id)]
                            self.logger.warning(f"Capped long session at {self.max_session_duration} minutes for user {user_id}")
                    except Exception as e:
                        error_count += 1
                        self.logger.error(f"Error processing session for user {user_id}: {e}")
                
                # Save in batches
                for (guild_id, user_id), minutes in sessions_to_update:
                    await self._increment_voice_time(guild_id, user_id, minutes)
                    # Reset session start time to now (so we don't double-count)
                    if (guild_id, user_id) in self.voice_sessions:
                        self.voice_sessions[(guild_id, user_id)] = current_time
                    saved_count += 1
                
                if saved_count > 0 or error_count > 0:
                    self.logger.debug(f"Periodic save: {saved_count} saved, {error_count} errors, {len(self.voice_sessions)} active")
        
        except Exception as e:
            self.logger.error(f"Error in save_voice_sessions_periodically task: {e}", exc_info=True)
    
    @save_voice_sessions_periodically.before_loop
    async def before_save_voice_sessions_periodically(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=1)
    async def periodic_session_cleanup(self):
        """
        Periodic cleanup of stale voice sessions.
        Runs every hour to prevent memory leaks even in low-activity servers.
        """
        try:
            if len(self.voice_sessions) > 0:
                self.logger.debug(f"Running periodic session cleanup ({len(self.voice_sessions)} active sessions)")
                await self._cleanup_stale_sessions()
        except Exception as e:
            self.logger.error(f"Error in periodic session cleanup: {e}", exc_info=True)
    
    @periodic_session_cleanup.before_loop
    async def before_periodic_session_cleanup(self):
        await self.bot.wait_until_ready()
    
    async def _reset_daily_stats(self, guild_id: int):
        """Reset daily voice stats"""
        try:
            await self.db.user_stats.update_many(
                {'guild_id': guild_id},
                {'$set': {'voice_daily': 0}}
            )
            self.logger.info(f"Reset daily voice stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting daily stats for guild {guild_id}: {e}", exc_info=True)
    
    async def _reset_monthly_stats(self, guild_id: int):
        """Reset monthly voice stats and archive data"""
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'voice_monthly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'voice', 'period': 'monthly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'voice_monthly': 0}})
            self.logger.info(f"Archived and reset monthly voice stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting monthly stats for guild {guild_id}: {e}", exc_info=True)
    
    async def _reset_weekly_stats(self, guild_id: int):
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'voice_weekly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'voice', 'period': 'weekly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'voice_weekly': 0}})
            self.logger.info(f"Archived and reset weekly voice stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting weekly stats for guild {guild_id}: {e}", exc_info=True)
    
    @app_commands.command(name="live-leaderboard-voice", description="Setup or toggle live voice leaderboard")
    @app_commands.describe(
        action="Enable, disable, or setup the leaderboard",
        voice_channel="Channel for voice leaderboard (required for setup)",
        timezone="Timezone in IANA format (e.g., America/New_York)",
        vibe_channel="Channel users will be redirected to when clicking 'Join the Vibe' button"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
        app_commands.Choice(name="Setup", value="setup")
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_voice_leaderboard(
        self, 
        interaction: discord.Interaction, 
        action: app_commands.Choice[str],
        voice_channel: Optional[discord.TextChannel] = None,
        timezone: Optional[str] = "UTC",
        vibe_channel: Optional[discord.TextChannel] = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            config = await self._get_guild_config(interaction.guild.id)
            
            # Handle disable action
            if action.value == "disable":
                if not config or not config.get('voice_enabled'):
                    await interaction.followup.send("⚠️ Voice leaderboard is already disabled!", ephemeral=True)
                    return
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': {'voice_enabled': False}}
                )
                await interaction.followup.send(
                    "✅ **Voice leaderboard disabled!**\n"
                    "📊 Voice time tracking has been paused.\n"
                    "💡 Use `/live-leaderboard-voice action:Enable` to re-enable.",
                    ephemeral=True
                )
                return
            
            # Handle enable action
            if action.value == "enable":
                if not config:
                    await interaction.followup.send(
                        "⚠️ Voice leaderboard not configured yet!\n"
                        "💡 Use `/live-leaderboard-voice action:Setup` first.",
                        ephemeral=True
                    )
                    return
                
                if config.get('voice_enabled'):
                    await interaction.followup.send("⚠️ Voice leaderboard is already enabled!", ephemeral=True)
                    return
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': {'voice_enabled': True}}
                )
                
                channel_id = config.get('voice_channel_id')
                channel_mention = f"<#{channel_id}>" if channel_id else "Not set"
                
                await interaction.followup.send(
                    f"✅ **Voice leaderboard enabled!**\n"
                    f"📊 Channel: {channel_mention}\n"
                    f"🌍 Timezone: `{config.get('timezone', 'UTC')}`\n"
                    f"💡 Voice time tracking has resumed.",
                    ephemeral=True
                )
                return
            
            # Handle setup action
            if action.value == "setup":
                if not voice_channel:
                    await interaction.followup.send(
                        "❌ **Channel required for setup!**\n"
                        "Please provide a channel using the `voice_channel` parameter.",
                        ephemeral=True
                    )
                    return
                
                # Validate timezone
                try:
                    pytz.timezone(timezone)
                except pytz.exceptions.UnknownTimeZoneError:
                    await interaction.followup.send(
                        f"❌ Invalid timezone: `{timezone}`\n"
                        f"💡 Use IANA format (e.g., `America/New_York`, `Europe/London`, `Asia/Tokyo`)",
                        ephemeral=True
                    )
                    return
                
                await self._ensure_guild_config(interaction.guild.id)
                
                # Prepare update data
                update_data = {
                    'voice_enabled': True, 
                    'voice_channel_id': voice_channel.id, 
                    'timezone': timezone
                }
                
                # Add vibe_channel if provided
                if vibe_channel:
                    update_data['vibe_channel_id'] = vibe_channel.id
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': update_data}
                )
                
                await self._create_full_leaderboard_message(voice_channel, interaction.guild.id, vibe_channel.id if vibe_channel else None)
                
                vibe_info = f"\n🎵 Vibe Channel: {vibe_channel.mention}" if vibe_channel else ""
                await interaction.followup.send(
                    f"✅ **Voice leaderboard setup complete!**\n"
                    f"📊 Channel: {voice_channel.mention}\n"
                    f"🌍 Timezone: `{timezone}`{vibe_info}\n"
                    f"🔄 Updates every 5 minutes\n"
                    f"💡 Use `/live-leaderboard-voice action:Disable` to pause tracking.",
                    ephemeral=True
                )
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceLeaderboardCog(bot))
