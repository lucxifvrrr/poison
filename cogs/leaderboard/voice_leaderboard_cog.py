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
from .leaderboard_config import (
    EMBED_COLOR, Emojis, Images, VoiceTemplates, 
    PeriodConfig, ButtonConfig, LeaderboardSettings
)

load_dotenv()


class VoiceLeaderboardPaginator(discord.ui.View):
    def __init__(self, cog, guild_id: int, period: str, page: int = 0):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.period = period
        self.page = page
        
        left_emoji = discord.PartialEmoji(name=Emojis.LEFT_BUTTON_NAME, id=Emojis.LEFT_BUTTON_ID)
        right_emoji = discord.PartialEmoji(name=Emojis.RIGHT_BUTTON_NAME, id=Emojis.RIGHT_BUTTON_ID)
        
        self.children[0].emoji = left_emoji
        self.children[1].emoji = right_emoji
        self.children[0].custom_id = f"{ButtonConfig.VOICE_LEFT_PREFIX}_{period}_{guild_id}"
        self.children[1].custom_id = f"{ButtonConfig.VOICE_RIGHT_PREFIX}_{period}_{guild_id}"
    
    @discord.ui.button(style=discord.ButtonStyle.secondary, custom_id="voice_left")
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if self.page > 0:
                self.page -= 1
                embeds = await self.cog._build_all_embeds(self.guild_id, self.period, page=self.page)
                await interaction.response.edit_message(embeds=embeds, view=self)
            else:
                await interaction.response.send_message("You're on the first page!", ephemeral=True)
        except Exception as e:
            print(f"❌ Error in previous_page: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred while updating the leaderboard.", ephemeral=True)
            except:
                pass
    
    @discord.ui.button(style=discord.ButtonStyle.secondary, custom_id="voice_right")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            stats = await self.cog._get_top_users(self.guild_id, self.period, limit=LeaderboardSettings.MAX_MEMBERS_FETCH)
            if not stats:
                await interaction.response.send_message("No data available!", ephemeral=True)
                return
            max_pages = (len(stats) - 1) // LeaderboardSettings.MEMBERS_PER_PAGE
            if self.page < max_pages:
                self.page += 1
                embeds = await self.cog._build_all_embeds(self.guild_id, self.period, page=self.page)
                await interaction.response.edit_message(embeds=embeds, view=self)
            else:
                await interaction.response.send_message("You're on the last page!", ephemeral=True)
        except Exception as e:
            print(f"❌ Error in next_page: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred while updating the leaderboard.", ephemeral=True)
            except:
                pass


class VoiceLeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None
        self.voice_sessions = {}
        self.last_weekly_reset = {}  # {guild_id: datetime}
        self.last_monthly_reset = {}  # {guild_id: datetime}
    
    async def cog_load(self):
        # Reuse bot's shared MongoDB connection
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.mongo_client = self.bot.mongo_client
            self.db = self.mongo_client['discord_bot']
            print("✅ Voice Leaderboard Cog: Reusing shared MongoDB connection")
        else:
            # Fallback: create new connection if shared one doesn't exist
            mongo_url = os.getenv('MONGO_URL')
            if not mongo_url:
                raise ValueError("MONGO_URL not found in environment variables")
            self.mongo_client = AsyncIOMotorClient(mongo_url)
            self.db = self.mongo_client['discord_bot']
            print("✅ Voice Leaderboard Cog: MongoDB connected")
        
        # Start tasks AFTER database connection is established
        # Note: _initialize_voice_sessions will be called by before_update_leaderboards
        self.update_leaderboards.start()
        self.daily_reset.start()
        self.weekly_reset.start()
        self.monthly_reset.start()
    
    async def cog_unload(self):
        await self._save_all_voice_sessions()
        self.update_leaderboards.cancel()
        self.daily_reset.cancel()
        self.weekly_reset.cancel()
        self.monthly_reset.cancel()
        # Don't close shared MongoDB connection - it's managed by the bot
        # Only close if we created our own connection
        if self.mongo_client and not hasattr(self.bot, 'mongo_client'):
            self.mongo_client.close()
    
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
        print(f"✅ Initialized {len(self.voice_sessions)} active voice sessions")
    
    async def _save_all_voice_sessions(self):
        for (guild_id, user_id), joined_at in self.voice_sessions.items():
            minutes = (datetime.utcnow() - joined_at).total_seconds() / 60
            await self._increment_voice_time(guild_id, user_id, minutes)
        self.voice_sessions.clear()
    
    async def _get_guild_config(self, guild_id: int) -> Optional[Dict]:
        return await self.db.guild_configs.find_one({'guild_id': guild_id})
    
    async def _ensure_guild_config(self, guild_id: int) -> Dict:
        config = await self._get_guild_config(guild_id)
        if not config:
            config = {'guild_id': guild_id, 'voice_enabled': False, 'voice_channel_id': None, 'timezone': 'UTC', 'leaderboard_limit': 10, 'created_at': datetime.utcnow()}
            await self.db.guild_configs.insert_one(config)
        return config
    
    async def _increment_voice_time(self, guild_id: int, user_id: int, minutes: float):
        if minutes <= 0:
            return
        await self.db.user_stats.update_one(
            {'guild_id': guild_id, 'user_id': user_id},
            {'$inc': {'voice_daily': minutes, 'voice_weekly': minutes, 'voice_monthly': minutes, 'voice_alltime': minutes}, '$set': {'last_update': datetime.utcnow()}},
            upsert=True
        )
    
    async def _get_top_users(self, guild_id: int, period: str, limit: int = 100) -> List[Dict]:
        field_map = {'daily': 'voice_daily', 'weekly': 'voice_weekly', 'monthly': 'voice_monthly', 'alltime': 'voice_alltime'}
        field = field_map.get(period, 'voice_weekly')
        cursor = self.db.user_stats.find({'guild_id': guild_id, field: {'$gt': 0}}).sort(field, -1).limit(limit)
        return await cursor.to_list(length=limit)
    
    async def _get_leaderboard_message(self, guild_id: int) -> Optional[Dict]:
        return await self.db.leaderboard_messages.find_one({'guild_id': guild_id, 'type': 'voice'})
    
    async def _save_leaderboard_message(self, guild_id: int, channel_id: int, message_id: int):
        await self.db.leaderboard_messages.update_one(
            {'guild_id': guild_id, 'type': 'voice'},
            {'$set': {'channel_id': channel_id, 'message_id': message_id, 'last_update': datetime.utcnow()}},
            upsert=True
        )
    
    def _format_time(self, minutes: float) -> str:
        if minutes < 60:
            return f"{int(minutes)}m"
        hours = int(minutes // 60)
        mins = int(minutes % 60)
        if hours >= 24:
            days = hours // 24
            hours = hours % 24
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
            username = user.display_name if user else f"User#{user_stat['user_id']}"
            minutes = user_stat.get(f'voice_{period}', 0)
            time_str = self._format_time(minutes)
            leaderboard_lines.append(f"- `{idx:02d}` | `{username}` {Emojis.ARROW} `{time_str}`")
        
        leaderboard_text = "\n".join(leaderboard_lines) if leaderboard_lines else "No data yet"
        
        # Top user
        top_user_name = "No one yet"
        top_user_time = "0m"
        if stats:
            top_user = guild.get_member(stats[0]['user_id'])
            top_user_name = top_user.display_name if top_user else f"User#{stats[0]['user_id']}"
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
        else:
            if now.month == 12:
                next_reset = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                next_reset = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        
        reset_timestamp = int(next_reset.timestamp())
        
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
            period=period
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
    
    async def _create_full_leaderboard_message(self, channel: discord.TextChannel, guild_id: int):
        """Create complete leaderboard message with ALL 4 embeds (header + monthly + weekly + daily)"""
        try:
            embeds = await self._build_all_embeds(guild_id, 'monthly', page=0)
            view = VoiceLeaderboardPaginator(self, guild_id, 'monthly', page=0)
            message = await channel.send(embeds=embeds, view=view)
            await self._save_leaderboard_message(guild_id, channel.id, message.id)
            print(f"✅ Created voice leaderboard for guild {guild_id}")
        except Exception as e:
            print(f"❌ Failed to create leaderboard: {e}")
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        config = await self._get_guild_config(member.guild.id)
        if not config or not config.get('voice_enabled'):
            return
        
        guild_id = member.guild.id
        user_id = member.id
        session_key = (guild_id, user_id)
        
        if before.channel is None and after.channel is not None:
            if member.guild.afk_channel and after.channel.id == member.guild.afk_channel.id:
                return
            self.voice_sessions[session_key] = datetime.utcnow()
        elif before.channel is not None and after.channel is None:
            if member.guild.afk_channel and before.channel.id == member.guild.afk_channel.id:
                return
            if session_key in self.voice_sessions:
                joined_at = self.voice_sessions.pop(session_key)
                minutes = (datetime.utcnow() - joined_at).total_seconds() / 60
                await self._increment_voice_time(guild_id, user_id, minutes)
        elif before.channel != after.channel:
            was_in_afk = member.guild.afk_channel and before.channel and before.channel.id == member.guild.afk_channel.id
            is_in_afk = member.guild.afk_channel and after.channel and after.channel.id == member.guild.afk_channel.id
            if not was_in_afk and is_in_afk:
                if session_key in self.voice_sessions:
                    joined_at = self.voice_sessions.pop(session_key)
                    minutes = (datetime.utcnow() - joined_at).total_seconds() / 60
                    await self._increment_voice_time(guild_id, user_id, minutes)
            elif was_in_afk and not is_in_afk:
                self.voice_sessions[session_key] = datetime.utcnow()
    
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
                    if not msg_data:
                        continue
                    channel = guild.get_channel(msg_data['channel_id'])
                    if not channel:
                        continue
                    try:
                        message = await channel.fetch_message(msg_data['message_id'])
                        embeds = await self._build_all_embeds(guild_id, 'monthly', page=0)
                        view = VoiceLeaderboardPaginator(self, guild_id, 'monthly', page=0)
                        await message.edit(embeds=embeds, view=view)
                    except discord.NotFound:
                        embeds = await self._build_all_embeds(guild_id, 'monthly', page=0)
                        view = VoiceLeaderboardPaginator(self, guild_id, 'monthly', page=0)
                        new_message = await channel.send(embeds=embeds, view=view)
                        await self._save_leaderboard_message(guild_id, channel.id, new_message.id)
                except Exception as e:
                    print(f"❌ Error updating voice leaderboard: {e}")
        except Exception as e:
            print(f"❌ Error in update_leaderboards task: {e}")
    
    @update_leaderboards.before_loop
    async def before_update_leaderboards(self):
        await self.bot.wait_until_ready()
        # Initialize voice sessions after bot is ready
        await self._initialize_voice_sessions()
    
    @tasks.loop(hours=1)
    async def weekly_reset(self):
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    if now.weekday() == 6 and now.hour == 12:
                        # Check if already reset this week
                        last_reset = self.last_weekly_reset.get(guild_id)
                        if last_reset and (now - last_reset).days < 6:
                            continue  # Already reset this week
                        
                        await self._reset_weekly_stats(guild_id)
                        self.last_weekly_reset[guild_id] = now
                except Exception as e:
                    print(f"❌ Error checking reset: {e}")
        except Exception as e:
            print(f"❌ Error in weekly_reset task: {e}")
    
    @weekly_reset.before_loop
    async def before_weekly_reset(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=1)
    async def daily_reset(self):
        """Check for daily reset (midnight guild time)"""
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    if now.hour == 0:  # Midnight
                        await self._reset_daily_stats(guild_id)
                except Exception as e:
                    print(f"❌ Error checking daily reset: {e}")
        except Exception as e:
            print(f"❌ Error in daily_reset task: {e}")
    
    @daily_reset.before_loop
    async def before_daily_reset(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=1)
    async def monthly_reset(self):
        """Check for monthly reset (1st of month midnight guild time)"""
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    if now.day == 1 and now.hour == 0:  # 1st of month, midnight
                        # Check if already reset this month
                        last_reset = self.last_monthly_reset.get(guild_id)
                        if last_reset and last_reset.month == now.month and last_reset.year == now.year:
                            continue  # Already reset this month
                        
                        await self._reset_monthly_stats(guild_id)
                        self.last_monthly_reset[guild_id] = now
                except Exception as e:
                    print(f"❌ Error checking monthly reset: {e}")
        except Exception as e:
            print(f"❌ Error in monthly_reset task: {e}")
    
    @monthly_reset.before_loop
    async def before_monthly_reset(self):
        await self.bot.wait_until_ready()
    
    async def _reset_daily_stats(self, guild_id: int):
        """Reset daily voice stats"""
        try:
            await self.db.user_stats.update_many(
                {'guild_id': guild_id},
                {'$set': {'voice_daily': 0}}
            )
            print(f"✅ Reset daily voice stats for guild {guild_id}")
        except Exception as e:
            print(f"❌ Error resetting daily stats: {e}")
    
    async def _reset_monthly_stats(self, guild_id: int):
        """Reset monthly voice stats and archive data"""
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'voice_monthly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'voice', 'period': 'monthly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'voice_monthly': 0}})
            print(f"✅ Archived and reset monthly voice stats for guild {guild_id}")
        except Exception as e:
            print(f"❌ Error resetting monthly stats: {e}")
    
    async def _reset_weekly_stats(self, guild_id: int):
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'voice_weekly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'voice', 'period': 'weekly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'voice_weekly': 0}})
            print(f"✅ Archived and reset weekly voice stats for guild {guild_id}")
        except Exception as e:
            print(f"❌ Error resetting weekly stats: {e}")
    
    @app_commands.command(name="live-leaderboard-voice", description="Setup or toggle live voice leaderboard")
    @app_commands.describe(
        action="Enable, disable, or setup the leaderboard",
        voice_channel="Channel for voice leaderboard (required for setup)",
        timezone="Timezone in IANA format (e.g., America/New_York)"
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
        timezone: Optional[str] = "UTC"
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
                except:
                    await interaction.followup.send(
                        f"❌ Invalid timezone: `{timezone}`\n"
                        f"💡 Use IANA format (e.g., `America/New_York`, `Europe/London`, `Asia/Tokyo`)",
                        ephemeral=True
                    )
                    return
                
                await self._ensure_guild_config(interaction.guild.id)
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': {
                        'voice_enabled': True, 
                        'voice_channel_id': voice_channel.id, 
                        'timezone': timezone
                    }}
                )
                
                await self._create_full_leaderboard_message(voice_channel, interaction.guild.id)
                
                await interaction.followup.send(
                    f"✅ **Voice leaderboard setup complete!**\n"
                    f"📊 Channel: {voice_channel.mention}\n"
                    f"🌍 Timezone: `{timezone}`\n"
                    f"🔄 Updates every 5 minutes\n"
                    f"💡 Use `/live-leaderboard-voice action:Disable` to pause tracking.",
                    ephemeral=True
                )
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceLeaderboardCog(bot))
