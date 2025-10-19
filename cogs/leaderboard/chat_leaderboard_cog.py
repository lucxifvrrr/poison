"""
Chat Leaderboard Cog - Tracks chat messages with live leaderboards matching JSON template exactly
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
    EMBED_COLOR, Emojis, Images, ChatTemplates, 
    PeriodConfig, ButtonConfig, LeaderboardSettings
)

load_dotenv()


class LeaderboardPaginator(discord.ui.View):
    def __init__(self, cog, guild_id: int, period: str, page: int = 0, vibe_channel_id: int = None):
        # Don't call super().__init__() yet - we need to conditionally add buttons first
        self.cog = cog
        self.guild_id = guild_id
        self.period = period
        self.page = page
        self.vibe_channel_id = vibe_channel_id
        self.max_pages_cache = None  # Cache max pages to avoid repeated queries
        
        # Initialize the view with no timeout for persistent buttons
        super().__init__(timeout=None)  # Persistent buttons that don't expire
        
        # Clear default buttons (they're added by decorators)
        self.clear_items()
        
        # Only add pagination buttons for daily period
        if period == 'daily':
            # Use custom emoji IDs from config
            left_emoji = discord.PartialEmoji(name=Emojis.LEFT_BUTTON_NAME, id=Emojis.LEFT_BUTTON_ID)
            right_emoji = discord.PartialEmoji(name=Emojis.RIGHT_BUTTON_NAME, id=Emojis.RIGHT_BUTTON_ID)
            
            # Create pagination buttons
            left_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=left_emoji,
                custom_id=f"{ButtonConfig.CHAT_LEFT_PREFIX}_{period}_{guild_id}"
            )
            left_button.callback = self.previous_page
            
            right_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=right_emoji,
                custom_id=f"{ButtonConfig.CHAT_RIGHT_PREFIX}_{period}_{guild_id}"
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
            self.cog.logger.error(f"Error in previous_page (chat): {e}", exc_info=True)
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
            self.cog.logger.error(f"Error in next_page (chat): {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ An error occurred while updating the leaderboard.", ephemeral=True)
            except Exception as inner_e:
                self.cog.logger.error(f"Failed to send error message: {inner_e}")


class ChatLeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None
        self.last_daily_reset = {}  # {guild_id: datetime}
        self.last_weekly_reset = {}  # {guild_id: datetime}
        self.last_monthly_reset = {}  # {guild_id: datetime}
        self.last_update_time = {}  # {guild_id: datetime} - track when leaderboard was last updated
        self.view_cache = {}  # {(guild_id, period): view_instance} - cache views to preserve state
        self.logger = logging.getLogger('discord.bot.chat_leaderboard')
    
    async def cog_load(self):
        # Reuse bot's shared MongoDB connection
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.mongo_client = self.bot.mongo_client
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Chat Leaderboard Cog: Reusing shared MongoDB connection")
        else:
            # Fallback: create new connection if shared one doesn't exist
            mongo_url = os.getenv('MONGO_URL')
            if not mongo_url:
                raise ValueError("MONGO_URL not found in environment variables")
            self.mongo_client = AsyncIOMotorClient(mongo_url)
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Chat Leaderboard Cog: MongoDB connected")
        
        # Create indexes for optimal performance
        await self._create_indexes()
        
        # Tasks will start when bot is ready (via on_ready event)
        # Don't start them here to avoid deadlock during cog loading
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Start tasks when bot is ready to avoid deadlock during cog loading"""
        if not self.update_leaderboards.is_running():
            self.update_leaderboards.start()
            self.daily_reset.start()
            self.weekly_reset.start()
            self.monthly_reset.start()
            self.logger.info("Chat leaderboard tasks started")
    
    async def cog_unload(self):
        self.update_leaderboards.cancel()
        self.daily_reset.cancel()
        self.weekly_reset.cancel()
        self.monthly_reset.cancel()
        # Don't close shared MongoDB connection - it's managed by the bot
        # Only close if we created our own connection
        if self.mongo_client and not hasattr(self.bot, 'mongo_client'):
            self.mongo_client.close()
    
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Clean up data when bot is removed from a guild"""
        try:
            self.logger.info(f"Bot removed from guild {guild.name} ({guild.id}), cleaning up chat leaderboard data")
            
            # Delete leaderboard messages
            await self.db.leaderboard_messages.delete_one({'guild_id': guild.id, 'type': 'chat'})
            
            # Delete user stats (only chat fields - voice cog will handle voice)
            # Note: We keep user_stats document but could optionally clean if no voice data
            result = await self.db.user_stats.update_many(
                {'guild_id': guild.id},
                {'$set': {'chat_daily': 0, 'chat_weekly': 0, 'chat_monthly': 0, 'chat_alltime': 0}}
            )
            
            # Update guild config to disable chat
            await self.db.guild_configs.update_one(
                {'guild_id': guild.id},
                {'$set': {'chat_enabled': False}}
            )
            
            self.logger.info(f"Chat leaderboard cleanup complete for guild {guild.id}")
        except Exception as e:
            self.logger.error(f"Error cleaning up chat data for guild {guild.id}: {e}", exc_info=True)
    
    async def _get_guild_config(self, guild_id: int) -> Optional[Dict]:
        return await self.db.guild_configs.find_one({'guild_id': guild_id})
    
    async def _create_indexes(self):
        """Create database indexes for chat leaderboard collections"""
        try:
            # Guild configs indexes
            await self.db.guild_configs.create_index('guild_id', unique=True)
            await self.db.guild_configs.create_index([('chat_enabled', 1)])
            
            # User stats indexes for chat queries
            await self.db.user_stats.create_index([('guild_id', 1), ('user_id', 1)], unique=True)
            await self.db.user_stats.create_index([('guild_id', 1), ('chat_daily', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('chat_weekly', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('chat_monthly', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('chat_alltime', -1)])
            
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
            
            self.logger.info("Chat Leaderboard: Database indexes created/verified")
        except Exception as e:
            self.logger.warning(f"Error creating indexes (may already exist): {e}")
    
    async def _ensure_guild_config(self, guild_id: int) -> Dict:
        config = await self._get_guild_config(guild_id)
        if not config:
            config = {'guild_id': guild_id, 'chat_enabled': False, 'chat_channel_id': None, 'timezone': 'UTC', 'leaderboard_limit': 10, 'created_at': datetime.utcnow()}
            await self.db.guild_configs.insert_one(config)
        return config
    
    async def _increment_chat_count(self, guild_id: int, user_id: int, max_retries: int = 3):
        """Increment chat count with retry logic and error handling"""
        for attempt in range(max_retries):
            try:
                result = await self.db.user_stats.update_one(
                    {'guild_id': guild_id, 'user_id': user_id},
                    {
                        '$inc': {
                            'chat_daily': 1, 
                            'chat_weekly': 1, 
                            'chat_monthly': 1, 
                            'chat_alltime': 1
                        }, 
                        '$set': {'last_update': datetime.utcnow()}
                    },
                    upsert=True
                )
                if result.acknowledged:
                    return
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))  # Exponential backoff
                    self.logger.warning(f"Retry {attempt + 1}/{max_retries} for chat increment: {e}")
                else:
                    self.logger.error(f"Failed to increment chat count after {max_retries} attempts for user {user_id} in guild {guild_id}: {e}")
    
    async def _get_top_users(self, guild_id: int, period: str, limit: int = 100) -> List[Dict]:
        """Get top users with error handling and validation"""
        try:
            field_map = {'daily': 'chat_daily', 'weekly': 'chat_weekly', 'monthly': 'chat_monthly', 'alltime': 'chat_alltime'}
            field = field_map.get(period, 'chat_weekly')
            
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
                'type': 'chat',
                'period': 'monthly'
            }).sort('reset_date', -1).limit(1)
            
            archives = await cursor.to_list(length=1)
            if not archives:
                return None
            
            archive = archives[0]
            stats = archive.get('stats', [])
            
            if not stats:
                return None
            
            # Find the user with highest chat_monthly
            top_user = max(stats, key=lambda x: x.get('chat_monthly', 0))
            
            if top_user.get('chat_monthly', 0) > 0:
                return {
                    'user_id': top_user['user_id'],
                    'count': top_user['chat_monthly'],
                    'month': archive['reset_date']
                }
            
            return None
        except Exception as e:
            self.logger.error(f"Error fetching last month winner for guild {guild_id}: {e}")
            return None
    
    async def _get_leaderboard_message(self, guild_id: int) -> Optional[Dict]:
        return await self.db.leaderboard_messages.find_one({'guild_id': guild_id, 'type': 'chat'})
    
    async def _save_leaderboard_messages(self, guild_id: int, channel_id: int, daily_id: int, weekly_id: int, monthly_id: int):
        """Save all three leaderboard message IDs"""
        await self.db.leaderboard_messages.update_one(
            {'guild_id': guild_id, 'type': 'chat'},
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
            {'guild_id': guild_id, 'type': 'chat'},
            {'$set': {'channel_id': channel_id, 'daily_message_id': message_id, 'last_update': datetime.utcnow()}},
            upsert=True
        )
    
    async def _build_all_embeds(self, guild_id: int, period: str, page: int = 0) -> List[discord.Embed]:
        """Build ALL embeds: header image + monthly + weekly + daily"""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return [discord.Embed(title="Error", description="Guild not found")]
        
        embeds = []
        
        # Embed 0: Header image only
        header_embed = discord.Embed(color=EMBED_COLOR)
        header_embed.set_image(url=Images.CHAT_HEADER)
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
        total_messages = sum(s.get(f'chat_{period}', 0) for s in stats)
        
        # Build leaderboard lines
        leaderboard_lines = []
        for idx, user_stat in enumerate(page_stats, start=start_idx + 1):
            user = guild.get_member(user_stat['user_id'])
            if user:
                username = user.display_name
            else:
                # For users who left the server, use a hash of their ID to avoid collisions
                user_id_str = str(user_stat['user_id'])
                # Use last 6 digits plus a hash character for uniqueness
                hash_char = chr(65 + (user_stat['user_id'] % 26))  # A-Z based on ID
                username = f"User{hash_char}-{user_id_str[-6:]}"  # e.g., UserB-123456
            count = user_stat.get(f'chat_{period}', 0)
            # Truncate long usernames to prevent display issues
            if len(username) > 20:
                username = username[:17] + "..."
            leaderboard_lines.append(f"- `{idx:02d}` | `{username}` {Emojis.ARROW} `{count:,} messages`")
        
        leaderboard_text = "\n".join(leaderboard_lines) if leaderboard_lines else "No data yet"
        
        # Top user
        top_user_name = "No one yet"
        top_user_count = 0
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
            top_user_count = stats[0].get(f'chat_{period}', 0)
        
        # Period display
        if period == 'monthly':
            period_display = datetime.utcnow().strftime('%B %Y')
        else:
            period_display = PeriodConfig.PERIOD_DISPLAY_NAMES.get(period, 'Unknown')
        
        period_title = PeriodConfig.CHAT_TITLES.get(period, 'Rankings')
        subtitle = PeriodConfig.CHAT_SUBTITLES.get(period, 'Most active members are here!')
        
        # Next reset time
        config = await self._get_guild_config(guild_id)
        tz = pytz.timezone(config.get('timezone', LeaderboardSettings.DEFAULT_TIMEZONE) if config else LeaderboardSettings.DEFAULT_TIMEZONE)
        now = datetime.now(tz)
        
        if period == 'daily':
            next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        elif period == 'weekly':
            days_until_sunday = (LeaderboardSettings.WEEKLY_RESET_DAY - now.weekday()) % 7
            # Calculate the reset time for this week
            this_week_reset = now.replace(hour=LeaderboardSettings.WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0)
            # If it's Sunday (days_until_sunday == 0) and we're past the reset time, go to next week
            if days_until_sunday == 0 and now >= this_week_reset:
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
                
                # Format the month name
                month_name = last_month_data['month'].strftime('%B %Y')
                last_month_winner_text = f"`{winner_name}` with `{last_month_data['count']:,} messages` in {month_name}"
        
        # Build description using config template
        description = ChatTemplates.build_description(
            period_title=period_title,
            total_messages=total_messages,
            period_display=period_display,
            top_user_name=top_user_name,
            top_user_count=top_user_count,
            leaderboard_text=leaderboard_text,
            reset_timestamp=reset_timestamp,
            subtitle=subtitle,
            last_month_winner=last_month_winner_text
        )
        
        # Create embed
        embed = discord.Embed(description=description, color=EMBED_COLOR)
        
        # Footer
        footer_text = ChatTemplates.FOOTER_TEXT
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
            # Clear old cached views since we're creating new messages
            for period in ['daily', 'weekly', 'monthly']:
                cache_key = (guild_id, period)
                if cache_key in self.view_cache:
                    del self.view_cache[cache_key]
            
            # Send header image
            header_embed = discord.Embed(color=EMBED_COLOR)
            header_embed.set_image(url=Images.CHAT_HEADER)
            await channel.send(embed=header_embed)
            
            # Send monthly embed with Join the Vibe button
            monthly_embed = await self._build_period_embed(guild_id, 'monthly', page=0)
            monthly_view = LeaderboardPaginator(self, guild_id, 'monthly', page=0, vibe_channel_id=vibe_channel_id)
            monthly_message = await channel.send(embed=monthly_embed, view=monthly_view)
            self.view_cache[(guild_id, 'monthly')] = monthly_view
            
            # Send weekly embed with Join the Vibe button
            weekly_embed = await self._build_period_embed(guild_id, 'weekly', page=0)
            weekly_view = LeaderboardPaginator(self, guild_id, 'weekly', page=0, vibe_channel_id=vibe_channel_id)
            weekly_message = await channel.send(embed=weekly_embed, view=weekly_view)
            self.view_cache[(guild_id, 'weekly')] = weekly_view
            
            # Send daily embed with pagination buttons
            daily_embed = await self._build_period_embed(guild_id, 'daily', page=0)
            daily_view = LeaderboardPaginator(self, guild_id, 'daily', page=0, vibe_channel_id=vibe_channel_id)
            daily_message = await channel.send(embed=daily_embed, view=daily_view)
            self.view_cache[(guild_id, 'daily')] = daily_view
            
            # Save ALL message IDs for updates
            await self._save_leaderboard_messages(guild_id, channel.id, daily_message.id, weekly_message.id, monthly_message.id)
            self.logger.info(f"Created chat leaderboard messages for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Failed to create leaderboard for guild {guild_id}: {e}", exc_info=True)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Track chat messages with bot filtering and error handling"""
        # Filter bots and DMs
        if message.author.bot or not message.guild:
            return
        
        try:
            config = await self._get_guild_config(message.guild.id)
            if not config or not config.get('chat_enabled'):
                return
            
            # Don't count messages in ignored channels if configured
            ignored_channels = config.get('ignored_channels', [])
            if message.channel.id in ignored_channels:
                return
            
            await self._increment_chat_count(message.guild.id, message.author.id)
        except Exception as e:
            self.logger.error(f"Error processing message from {message.author.id} in guild {message.guild.id}: {e}")
    
    @tasks.loop(minutes=5)
    async def update_leaderboards(self):
        try:
            self.logger.info("Chat leaderboard update task running...")
            cursor = self.db.guild_configs.find({'chat_enabled': True})
            configs = await cursor.to_list(length=1000)
            self.logger.info(f"Found {len(configs)} guilds with chat leaderboard enabled")
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
                        chat_channel_id = config.get('chat_channel_id')
                        if chat_channel_id:
                            channel = guild.get_channel(chat_channel_id)
                            if channel:
                                self.logger.info(f"No leaderboard messages found for guild {guild_id}, creating...")
                                await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                        else:
                            self.logger.debug(f"No chat_channel_id configured for guild {guild_id}")
                        continue
                    
                    channel = guild.get_channel(msg_data['channel_id'])
                    if not channel:
                        # Channel was deleted, clean up database reference
                        self.logger.warning(f"Chat leaderboard channel {msg_data['channel_id']} not found for guild {guild_id}, cleaning up")
                        await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'chat'})
                        continue
                    
                    # Get message IDs (support both old and new format)
                    daily_id = msg_data.get('daily_message_id') or msg_data.get('message_id')
                    weekly_id = msg_data.get('weekly_message_id')
                    monthly_id = msg_data.get('monthly_message_id')
                    
                    self.logger.debug(f"Guild {guild_id} - Message IDs: daily={daily_id}, weekly={weekly_id}, monthly={monthly_id}")
                    
                    messages_missing = False
                    
                    # Update all three messages
                    try:
                        # Update daily message
                        if daily_id:
                            try:
                                self.logger.debug(f"Fetching daily message {daily_id} for guild {guild_id}")
                                daily_message = await channel.fetch_message(daily_id)
                                self.logger.debug(f"Successfully fetched daily message {daily_id}")
                                if daily_message.author.id == self.bot.user.id:
                                    # Get or create cached view to preserve page state
                                    cache_key = (guild_id, 'daily')
                                    if cache_key not in self.view_cache:
                                        self.view_cache[cache_key] = LeaderboardPaginator(self, guild_id, 'daily', page=0, vibe_channel_id=vibe_channel_id)
                                    daily_view = self.view_cache[cache_key]
                                    # Build embed with current page from cached view
                                    daily_embed = await self._build_period_embed(guild_id, 'daily', page=daily_view.page)
                                    await daily_message.edit(embed=daily_embed, view=daily_view)
                                    self.logger.debug(f"Updated daily leaderboard for guild {guild_id}")
                                else:
                                    self.logger.warning(f"Daily message {daily_id} not owned by bot for guild {guild_id}")
                                    messages_missing = True
                            except discord.NotFound:
                                self.logger.warning(f"Daily message {daily_id} not found for guild {guild_id}")
                                messages_missing = True
                            except Exception as e:
                                self.logger.error(f"Error updating daily message for guild {guild_id}: {e}")
                                messages_missing = True
                        else:
                            self.logger.warning(f"No daily_id found for guild {guild_id}")
                            messages_missing = True
                        
                        # Update weekly message
                        if weekly_id:
                            try:
                                self.logger.debug(f"Fetching weekly message {weekly_id} for guild {guild_id}")
                                weekly_message = await channel.fetch_message(weekly_id)
                                self.logger.debug(f"Successfully fetched weekly message {weekly_id}")
                                if weekly_message.author.id == self.bot.user.id:
                                    # Get or create cached view
                                    cache_key = (guild_id, 'weekly')
                                    if cache_key not in self.view_cache:
                                        self.view_cache[cache_key] = LeaderboardPaginator(self, guild_id, 'weekly', page=0, vibe_channel_id=vibe_channel_id)
                                    weekly_view = self.view_cache[cache_key]
                                    weekly_embed = await self._build_period_embed(guild_id, 'weekly', page=0)
                                    await weekly_message.edit(embed=weekly_embed, view=weekly_view)
                                    self.logger.debug(f"Updated weekly leaderboard for guild {guild_id}")
                                else:
                                    self.logger.warning(f"Weekly message {weekly_id} not owned by bot for guild {guild_id}")
                                    messages_missing = True
                            except discord.NotFound:
                                self.logger.warning(f"Weekly message {weekly_id} not found for guild {guild_id}")
                                messages_missing = True
                            except Exception as e:
                                self.logger.error(f"Error updating weekly message for guild {guild_id}: {e}")
                                messages_missing = True
                        else:
                            self.logger.warning(f"No weekly_id found for guild {guild_id}")
                            messages_missing = True
                        
                        # Update monthly message
                        if monthly_id:
                            try:
                                self.logger.debug(f"Fetching monthly message {monthly_id} for guild {guild_id}")
                                monthly_message = await channel.fetch_message(monthly_id)
                                self.logger.debug(f"Successfully fetched monthly message {monthly_id}")
                                if monthly_message.author.id == self.bot.user.id:
                                    # Get or create cached view
                                    cache_key = (guild_id, 'monthly')
                                    if cache_key not in self.view_cache:
                                        self.view_cache[cache_key] = LeaderboardPaginator(self, guild_id, 'monthly', page=0, vibe_channel_id=vibe_channel_id)
                                    monthly_view = self.view_cache[cache_key]
                                    monthly_embed = await self._build_period_embed(guild_id, 'monthly', page=0)
                                    await monthly_message.edit(embed=monthly_embed, view=monthly_view)
                                    self.logger.debug(f"Updated monthly leaderboard for guild {guild_id}")
                                else:
                                    self.logger.warning(f"Monthly message {monthly_id} not owned by bot for guild {guild_id}")
                                    messages_missing = True
                            except discord.NotFound:
                                self.logger.warning(f"Monthly message {monthly_id} not found for guild {guild_id}")
                                messages_missing = True
                            except Exception as e:
                                self.logger.error(f"Error updating monthly message for guild {guild_id}: {e}")
                                messages_missing = True
                        else:
                            self.logger.warning(f"No monthly_id found for guild {guild_id}")
                            messages_missing = True
                        
                        # If any messages are missing, recreate all
                        if messages_missing:
                            self.logger.info(f"Chat leaderboard messages missing or invalid for guild {guild_id}, recreating all embeds")
                            await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'chat'})
                            await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                        else:
                            # Track successful update
                            self.last_update_time[guild_id] = datetime.utcnow()
                            self.logger.info(f"Successfully updated all chat leaderboards for guild {guild_id}")
                            
                    except discord.Forbidden as e:
                        self.logger.error(
                            f"Permission denied editing message for guild {guild_id}: {e}. "
                            f"Removing invalid reference and recreating."
                        )
                        await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'chat'})
                        await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                    except discord.HTTPException as e:
                        self.logger.error(f"HTTP error updating chat leaderboard for guild {guild_id}: {e}")
                except Exception as e:
                    self.logger.error(f"Error updating chat leaderboard for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in update_leaderboards task: {e}", exc_info=True)
    
    @update_leaderboards.before_loop
    async def before_update_leaderboards(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)  # Check every 5 minutes for maximum reliability
    async def weekly_reset(self):
        """Check for weekly reset with missed reset detection
        
        NOTE: If Star of the Week system is configured, it handles weekly resets.
        This task only runs for guilds without Star system or as a backup.
        """
        try:
            cursor = self.db.guild_configs.find({'chat_enabled': True})
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
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    
                    # Multiple checks to NEVER miss weekly reset
                    last_reset_time = config.get('last_chat_weekly_reset')
                    should_reset = False
                    
                    if last_reset_time:
                        hours_since = (datetime.utcnow() - last_reset_time).total_seconds() / 3600
                        days_since = hours_since / 24
                        
                        # Check 1: Has it been at least 6.5 days?
                        if days_since >= 6.5:
                            if now.weekday() == 6 and now.hour >= 12:  # Sunday noon or later
                                should_reset = True
                                self.logger.info(f"Weekly reset for guild {guild_id}: {days_since:.1f} days since last")
                            elif now.weekday() == 0:  # Monday (missed Sunday)
                                should_reset = True
                                self.logger.warning(f"Missed Sunday reset for guild {guild_id}, doing it now")
                            elif days_since >= 7.0:  # Full week
                                should_reset = True
                                self.logger.warning(f"Full week passed for guild {guild_id}: {days_since:.1f} days")
                    else:
                        # Never reset before - do it now
                        should_reset = True
                        self.logger.info(f"First weekly reset for guild {guild_id}")
                    
                    if should_reset:
                        await self._reset_weekly_stats(guild_id)
                        # Persist reset time to database
                        await self.db.guild_configs.update_one(
                            {'guild_id': guild_id},
                            {'$set': {'last_chat_weekly_reset': datetime.utcnow()}}
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
            cursor = self.db.guild_configs.find({'chat_enabled': True})
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
                    # Check for daily reset - be very careful not to miss
                    last_reset = self.last_daily_reset.get(guild_id)
                    last_db_reset = config.get('last_chat_daily_reset')
                    should_reset = False
                    
                    # Use database time as primary source
                    if last_db_reset:
                        hours_since = (datetime.utcnow() - last_db_reset).total_seconds() / 3600
                        if hours_since >= 23:  # At least 23 hours passed
                            if now.hour >= 0:  # Midnight or later
                                should_reset = True
                    elif last_reset:
                        if last_reset.date() != now.date():
                            should_reset = True
                    else:
                        should_reset = True  # Never reset before
                    
                    if should_reset:
                        
                        await self._reset_daily_stats(guild_id)
                        self.last_daily_reset[guild_id] = now
                        # Also save to database for persistence
                        await self.db.guild_configs.update_one(
                            {'guild_id': guild_id},
                            {'$set': {'last_chat_daily_reset': datetime.utcnow()}}
                        )
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
            cursor = self.db.guild_configs.find({'chat_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    # Check for monthly reset window (1st of month, midnight to 1 AM)
                    if now.day == 1 and 0 <= now.hour < 1:
                        # Check if already reset this month (use database as source of truth)
                        last_db_reset = config.get('last_chat_monthly_reset')
                        if last_db_reset:
                            last_reset_tz = last_db_reset.replace(tzinfo=pytz.UTC).astimezone(tz)
                            if last_reset_tz.month == now.month and last_reset_tz.year == now.year:
                                continue  # Already reset this month
                        
                        await self._reset_monthly_stats(guild_id)
                        self.last_monthly_reset[guild_id] = now
                        # Persist to database for crash recovery
                        await self.db.guild_configs.update_one(
                            {'guild_id': guild_id},
                            {'$set': {'last_chat_monthly_reset': datetime.utcnow()}}
                        )
                except Exception as e:
                    self.logger.error(f"Error checking monthly reset for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in monthly_reset task: {e}", exc_info=True)
    
    @monthly_reset.before_loop
    async def before_monthly_reset(self):
        await self.bot.wait_until_ready()
    
    async def _reset_daily_stats(self, guild_id: int):
        """Reset daily chat stats"""
        try:
            await self.db.user_stats.update_many(
                {'guild_id': guild_id},
                {'$set': {'chat_daily': 0}}
            )
            self.logger.info(f"Reset daily chat stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting daily stats for guild {guild_id}: {e}", exc_info=True)
    
    async def _reset_monthly_stats(self, guild_id: int):
        """Reset monthly chat stats and archive data"""
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'chat_monthly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'chat', 'period': 'monthly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'chat_monthly': 0}})
            self.logger.info(f"Archived and reset monthly chat stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting monthly stats for guild {guild_id}: {e}", exc_info=True)
    
    async def _reset_weekly_stats(self, guild_id: int):
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'chat_weekly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'chat', 'period': 'weekly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'chat_weekly': 0}})
            self.logger.info(f"Archived and reset weekly chat stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting weekly stats for guild {guild_id}: {e}", exc_info=True)
    
    @app_commands.command(name="leaderboard-refresh", description="[ADMIN] Force refresh chat leaderboard now")
    @app_commands.checks.has_permissions(administrator=True)
    async def refresh_leaderboard(self, interaction: discord.Interaction):
        """Force refresh the chat leaderboard immediately"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            guild_id = interaction.guild.id
            
            # Check configuration
            config = await self._get_guild_config(guild_id)
            if not config or not config.get('chat_enabled'):
                await interaction.followup.send("❌ Chat leaderboard not enabled! Use `/live-leaderboard action:Enable` first.", ephemeral=True)
                return
            
            # Get message data
            msg_data = await self._get_leaderboard_message(guild_id)
            if not msg_data:
                await interaction.followup.send("❌ No leaderboard messages found! Use `/live-leaderboard action:Setup` first.", ephemeral=True)
                return
            
            channel = interaction.guild.get_channel(msg_data['channel_id'])
            if not channel:
                await interaction.followup.send(f"❌ Leaderboard channel not found!", ephemeral=True)
                return
            
            vibe_channel_id = config.get('vibe_channel_id')
            
            # Get message IDs
            daily_id = msg_data.get('daily_message_id')
            weekly_id = msg_data.get('weekly_message_id')
            monthly_id = msg_data.get('monthly_message_id')
            
            updated = []
            errors = []
            
            # Update daily
            if daily_id:
                try:
                    daily_message = await channel.fetch_message(daily_id)
                    daily_embed = await self._build_period_embed(guild_id, 'daily', page=0)
                    daily_view = LeaderboardPaginator(self, guild_id, 'daily', page=0, vibe_channel_id=vibe_channel_id)
                    await daily_message.edit(embed=daily_embed, view=daily_view)
                    updated.append("Daily")
                except Exception as e:
                    errors.append(f"Daily: {e}")
            
            # Update weekly
            if weekly_id:
                try:
                    weekly_message = await channel.fetch_message(weekly_id)
                    weekly_embed = await self._build_period_embed(guild_id, 'weekly', page=0)
                    weekly_view = LeaderboardPaginator(self, guild_id, 'weekly', page=0, vibe_channel_id=vibe_channel_id)
                    await weekly_message.edit(embed=weekly_embed, view=weekly_view)
                    updated.append("Weekly")
                except Exception as e:
                    errors.append(f"Weekly: {e}")
            
            # Update monthly
            if monthly_id:
                try:
                    monthly_message = await channel.fetch_message(monthly_id)
                    monthly_embed = await self._build_period_embed(guild_id, 'monthly', page=0)
                    monthly_view = LeaderboardPaginator(self, guild_id, 'monthly', page=0, vibe_channel_id=vibe_channel_id)
                    await monthly_message.edit(embed=monthly_embed, view=monthly_view)
                    updated.append("Monthly")
                except Exception as e:
                    errors.append(f"Monthly: {e}")
            
            # Build response
            response = "# 🔄 Leaderboard Refresh Complete\n\n"
            if updated:
                response += f"✅ **Updated:** {', '.join(updated)}\n"
            if errors:
                response += f"\n❌ **Errors:**\n" + "\n".join(f"• {err}" for err in errors)
            
            # Show current stats
            active_daily = await self.db.user_stats.count_documents({'guild_id': guild_id, 'chat_daily': {'$gt': 0}})
            top_users = await self._get_top_users(guild_id, 'daily', limit=1)
            
            response += f"\n\n📊 **Current Stats:**\n"
            response += f"• Active users today: {active_daily}\n"
            if top_users:
                top_user = top_users[0]
                member = interaction.guild.get_member(top_user['user_id'])
                username = member.display_name if member else f"User {top_user['user_id']}"
                response += f"• Top user: {username} with {top_user.get('chat_daily', 0)} messages\n"
            
            await interaction.followup.send(response, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error during refresh: {e}", ephemeral=True)
            self.logger.error(f"Refresh command error: {e}", exc_info=True)
    
    @app_commands.command(name="leaderboard-debug", description="[ADMIN] Debug chat leaderboard system")
    @app_commands.checks.has_permissions(administrator=True)
    async def debug_leaderboard(self, interaction: discord.Interaction):
        """Diagnose chat leaderboard issues"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            guild_id = interaction.guild.id
            
            # Check configuration
            config = await self._get_guild_config(guild_id)
            if not config:
                await interaction.followup.send("❌ No configuration found! Use `/live-leaderboard action:Setup` first.", ephemeral=True)
                return
            
            debug_info = "# 🔍 Chat Leaderboard Debug Info\n\n"
            
            # Configuration status
            debug_info += "## ⚙️ Configuration\n"
            debug_info += f"✅ Config exists\n"
            debug_info += f"**Enabled:** {'✅ Yes' if config.get('chat_enabled') else '❌ No'}\n"
            debug_info += f"**Timezone:** `{config.get('timezone', 'UTC')}`\n"
            
            chat_channel_id = config.get('chat_channel_id')
            if chat_channel_id:
                channel = interaction.guild.get_channel(chat_channel_id)
                if channel:
                    debug_info += f"**Channel:** {channel.mention} (ID: {chat_channel_id})\n"
                else:
                    debug_info += f"**Channel:** ❌ Not found (ID: {chat_channel_id})\n"
            else:
                debug_info += f"**Channel:** ❌ Not configured\n"
            
            # Task status
            debug_info += f"\n## 🔄 Background Tasks\n"
            debug_info += f"**Update Task:** {'✅ Running' if self.update_leaderboards.is_running() else '❌ NOT RUNNING'}\n"
            if self.update_leaderboards.is_running():
                debug_info += f"**Task Loop Count:** {self.update_leaderboards.current_loop}\n"
                last_update = self.last_update_time.get(guild_id)
                if last_update:
                    time_since = datetime.utcnow() - last_update
                    minutes_ago = int(time_since.total_seconds() / 60)
                    debug_info += f"**Last Update:** {minutes_ago} minute(s) ago\n"
                else:
                    debug_info += f"**Last Update:** Never (or bot just restarted)\n"
            debug_info += f"**Daily Reset:** {'✅ Running' if self.daily_reset.is_running() else '❌ NOT RUNNING'}\n"
            debug_info += f"**Weekly Reset:** {'✅ Running' if self.weekly_reset.is_running() else '❌ NOT RUNNING'}\n"
            debug_info += f"**Monthly Reset:** {'✅ Running' if self.monthly_reset.is_running() else '❌ NOT RUNNING'}\n"
            
            # Check message data
            msg_data = await self._get_leaderboard_message(guild_id)
            debug_info += f"\n## 📨 Leaderboard Messages\n"
            if msg_data:
                debug_info += f"**Channel ID:** {msg_data.get('channel_id')}\n"
                debug_info += f"**Daily Message ID:** {msg_data.get('daily_message_id', 'Not set')}\n"
                debug_info += f"**Weekly Message ID:** {msg_data.get('weekly_message_id', 'Not set')}\n"
                debug_info += f"**Monthly Message ID:** {msg_data.get('monthly_message_id', 'Not set')}\n"
            else:
                debug_info += f"❌ No message data found in database\n"
            
            # Check user stats
            stats_count = await self.db.user_stats.count_documents({'guild_id': guild_id})
            active_daily = await self.db.user_stats.count_documents({'guild_id': guild_id, 'chat_daily': {'$gt': 0}})
            active_weekly = await self.db.user_stats.count_documents({'guild_id': guild_id, 'chat_weekly': {'$gt': 0}})
            active_monthly = await self.db.user_stats.count_documents({'guild_id': guild_id, 'chat_monthly': {'$gt': 0}})
            
            debug_info += f"\n## 📊 User Statistics\n"
            debug_info += f"**Total Users Tracked:** {stats_count}\n"
            debug_info += f"**Active Today:** {active_daily}\n"
            debug_info += f"**Active This Week:** {active_weekly}\n"
            debug_info += f"**Active This Month:** {active_monthly}\n"
            
            # Get top user for verification
            top_users = await self._get_top_users(guild_id, 'daily', limit=1)
            if top_users:
                top_user = top_users[0]
                member = interaction.guild.get_member(top_user['user_id'])
                username = member.display_name if member else f"User {top_user['user_id']}"
                debug_info += f"\n**Top User Today:** {username} with {top_user.get('chat_daily', 0)} messages\n"
            
            # Check on_message listener
            debug_info += f"\n## 👂 Event Listeners\n"
            debug_info += f"**on_message:** ✅ Registered\n"
            debug_info += f"**Bot User ID:** {self.bot.user.id}\n"
            
            # Database connection
            debug_info += f"\n## 💾 Database\n"
            try:
                await self.db.command('ping')
                debug_info += f"**Connection:** ✅ Active\n"
            except Exception as e:
                debug_info += f"**Connection:** ❌ Error: {e}\n"
            
            # Recommendations
            debug_info += f"\n## 💡 Recommendations\n"
            if not config.get('chat_enabled'):
                debug_info += f"⚠️ Enable the leaderboard with `/live-leaderboard action:Enable`\n"
            if not self.update_leaderboards.is_running():
                debug_info += f"⚠️ Update task not running - restart the bot\n"
            if not msg_data:
                debug_info += f"⚠️ No leaderboard messages - run `/live-leaderboard action:Setup`\n"
            if active_daily == 0:
                debug_info += f"⚠️ No activity tracked today - send some messages to test\n"
            
            await interaction.followup.send(debug_info, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error during debug: {e}", ephemeral=True)
            self.logger.error(f"Debug command error: {e}", exc_info=True)
    
    @app_commands.command(name="live-leaderboard", description="Setup or toggle live chat leaderboard")
    @app_commands.describe(
        action="Enable, disable, or setup the leaderboard",
        chat_channel="Channel for chat leaderboard (required for setup)",
        timezone="Timezone in IANA format (e.g., America/New_York)",
        vibe_channel="Channel users will be redirected to when clicking 'Join the Vibe' button"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
        app_commands.Choice(name="Setup", value="setup")
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_leaderboard(
        self, 
        interaction: discord.Interaction, 
        action: app_commands.Choice[str],
        chat_channel: Optional[discord.TextChannel] = None,
        timezone: Optional[str] = "UTC",
        vibe_channel: Optional[discord.TextChannel] = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            config = await self._get_guild_config(interaction.guild.id)
            
            # Handle disable action
            if action.value == "disable":
                if not config or not config.get('chat_enabled'):
                    await interaction.followup.send("⚠️ Chat leaderboard is already disabled!", ephemeral=True)
                    return
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': {'chat_enabled': False}}
                )
                await interaction.followup.send(
                    "✅ **Chat leaderboard disabled!**\n"
                    "📊 Stats tracking has been paused.\n"
                    "💡 Use `/live-leaderboard action:Enable` to re-enable.",
                    ephemeral=True
                )
                return
            
            # Handle enable action
            if action.value == "enable":
                if not config:
                    await interaction.followup.send(
                        "⚠️ Chat leaderboard not configured yet!\n"
                        "💡 Use `/live-leaderboard action:Setup` first.",
                        ephemeral=True
                    )
                    return
                
                if config.get('chat_enabled'):
                    await interaction.followup.send("⚠️ Chat leaderboard is already enabled!", ephemeral=True)
                    return
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': {'chat_enabled': True}}
                )
                
                channel_id = config.get('chat_channel_id')
                channel_mention = f"<#{channel_id}>" if channel_id else "Not set"
                
                await interaction.followup.send(
                    f"✅ **Chat leaderboard enabled!**\n"
                    f"📊 Channel: {channel_mention}\n"
                    f"🌍 Timezone: `{config.get('timezone', 'UTC')}`\n"
                    f"💡 Stats tracking has resumed.",
                    ephemeral=True
                )
                return
            
            # Handle setup action
            if action.value == "setup":
                if not chat_channel:
                    await interaction.followup.send(
                        "❌ **Channel required for setup!**\n"
                        "Please provide a channel using the `chat_channel` parameter.",
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
                    'chat_enabled': True, 
                    'chat_channel_id': chat_channel.id, 
                    'timezone': timezone
                }
                
                # Add vibe_channel if provided
                if vibe_channel:
                    update_data['vibe_channel_id'] = vibe_channel.id
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': update_data}
                )
                
                await self._create_full_leaderboard_message(chat_channel, interaction.guild.id, vibe_channel.id if vibe_channel else None)
                
                vibe_info = f"\n🎵 Vibe Channel: {vibe_channel.mention}" if vibe_channel else ""
                await interaction.followup.send(
                    f"✅ **Chat leaderboard setup complete!**\n"
                    f"📊 Channel: {chat_channel.mention}\n"
                    f"🌍 Timezone: `{timezone}`{vibe_info}\n"
                    f"💡 Leaderboard will update every 5 minutes.",
                    ephemeral=True
                )
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatLeaderboardCog(bot))
