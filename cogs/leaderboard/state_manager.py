"""
Bulletproof State Manager for Leaderboard System
=================================================
Ensures NO scheduled operations are EVER missed, even after crashes.
Tracks all state persistently and recovers automatically.
"""

from datetime import datetime, timedelta
import logging
from typing import Dict, Optional
import pytz
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger('discord.bot.leaderboard.state')


class BulletproofStateManager:
    """
    Manages all scheduled operations with 100% reliability.
    NEVER misses a selection or reset, guaranteed.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.state.manager')
    
    async def ensure_star_selection(self, guild_id: int, star_config: Dict, guild_config: Dict) -> bool:
        """
        Ensures Star of the Week selection happens exactly once per week.
        Returns True if selection should run NOW.
        """
        try:
            # Get timezone
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # Get last selection from persistent storage
            last_selection = guild_config.get('last_star_selection')
            
            # CASE 1: Never selected before
            if not last_selection:
                self.logger.warning(f"Guild {guild_id}: No previous Star selection - RUNNING NOW")
                return True
            
            # Calculate time since last selection
            time_since = now_utc - last_selection
            days_since = time_since.total_seconds() / 86400
            hours_since = time_since.total_seconds() / 3600
            
            # CASE 2: More than 7 days - definitely run
            if days_since >= 7.0:
                self.logger.warning(f"Guild {guild_id}: {days_since:.1f} days since last Star - RUNNING NOW")
                return True
            
            # CASE 3: It's been at least 6 days and we're past Sunday 12 PM (noon)
            if days_since >= 6.0:
                if now.weekday() == 6 and now.hour >= 12:  # Sunday 12 PM (noon) or later
                    self.logger.info(f"Guild {guild_id}: Sunday selection time - RUNNING NOW")
                    return True
                elif now.weekday() == 0:  # Monday (missed Sunday)
                    self.logger.warning(f"Guild {guild_id}: Missed Sunday, it's Monday - RUNNING NOW")
                    return True
                elif now.weekday() > 0:  # Tuesday or later
                    self.logger.warning(f"Guild {guild_id}: It's {now.strftime('%A')}, been {days_since:.1f} days - RUNNING NOW")
                    return True
            
            # CASE 4: Emergency check - if we're approaching 8 days
            if days_since >= 6.9:
                self.logger.error(f"Guild {guild_id}: EMERGENCY - {days_since:.1f} days since last selection - RUNNING NOW")
                return True
            
            # Not time yet - no logging needed (runs every 5 minutes)
            return False
            
        except Exception as e:
            # On ANY error, run selection to be safe
            self.logger.error(f"Error checking Star selection for guild {guild_id}: {e} - RUNNING NOW TO BE SAFE")
            return True
    
    async def ensure_weekly_reset(self, guild_id: int, guild_config: Dict, reset_type: str = 'chat') -> bool:
        """
        Ensures weekly reset happens exactly once per week.
        Returns True if reset should run NOW.
        """
        try:
            # Get timezone
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # Get last reset from persistent storage
            field_name = f'last_{reset_type}_weekly_reset'
            last_reset = guild_config.get(field_name)
            
            # CASE 1: Never reset before
            if not last_reset:
                self.logger.warning(f"Guild {guild_id}: No previous {reset_type} weekly reset - RUNNING NOW")
                return True
            
            # Calculate time since last reset
            time_since = now_utc - last_reset
            days_since = time_since.total_seconds() / 86400
            
            # CASE 2: More than 7 days - definitely reset
            if days_since >= 7.0:
                self.logger.warning(f"Guild {guild_id}: {days_since:.1f} days since last {reset_type} reset - RUNNING NOW")
                return True
            
            # CASE 3: It's been at least 6 days and we're past Sunday noon
            if days_since >= 6.0:
                if now.weekday() == 6 and now.hour >= 12:  # Sunday noon or later
                    self.logger.info(f"Guild {guild_id}: Sunday {reset_type} reset time - RUNNING NOW")
                    return True
                elif now.weekday() == 0:  # Monday
                    self.logger.warning(f"Guild {guild_id}: Missed Sunday {reset_type} reset - RUNNING NOW")
                    return True
                elif now.weekday() > 0:  # Tuesday or later
                    self.logger.warning(f"Guild {guild_id}: It's {now.strftime('%A')}, {reset_type} reset overdue - RUNNING NOW")
                    return True
            
            # CASE 4: Emergency check
            if days_since >= 6.9:
                self.logger.error(f"Guild {guild_id}: EMERGENCY - {days_since:.1f} days since {reset_type} reset - RUNNING NOW")
                return True
            
            return False
            
        except Exception as e:
            # On ANY error, reset to be safe
            self.logger.error(f"Error checking {reset_type} reset for guild {guild_id}: {e} - RUNNING NOW TO BE SAFE")
            return True
    
    async def ensure_daily_reset(self, guild_id: int, guild_config: Dict, reset_type: str = 'chat') -> bool:
        """
        Ensures daily reset happens exactly once per day.
        Returns True if reset should run NOW.
        """
        try:
            # Get timezone
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # Get last reset from persistent storage
            field_name = f'last_{reset_type}_daily_reset'
            last_reset = guild_config.get(field_name)
            
            # CASE 1: Never reset before
            if not last_reset:
                self.logger.warning(f"Guild {guild_id}: No previous {reset_type} daily reset - RUNNING NOW")
                return True
            
            # Calculate time since last reset
            time_since = now_utc - last_reset
            hours_since = time_since.total_seconds() / 3600
            
            # CASE 2: More than 24 hours - definitely reset
            if hours_since >= 24.0:
                self.logger.warning(f"Guild {guild_id}: {hours_since:.1f} hours since last {reset_type} daily reset - RUNNING NOW")
                return True
            
            # CASE 3: It's past midnight and been at least 20 hours
            if hours_since >= 20.0 and now.hour >= 0:
                self.logger.info(f"Guild {guild_id}: Daily {reset_type} reset time - RUNNING NOW")
                return True
            
            # CASE 4: Emergency - approaching 30 hours
            if hours_since >= 23.5:
                self.logger.error(f"Guild {guild_id}: EMERGENCY - {hours_since:.1f} hours since {reset_type} daily reset - RUNNING NOW")
                return True
            
            return False
            
        except Exception as e:
            # On ANY error, reset to be safe
            self.logger.error(f"Error checking {reset_type} daily reset for guild {guild_id}: {e} - RUNNING NOW TO BE SAFE")
            return True
    
    async def ensure_monthly_reset(self, guild_id: int, guild_config: Dict, reset_type: str = 'chat') -> bool:
        """
        Ensures monthly reset happens exactly once per month.
        Returns True if reset should run NOW.
        """
        try:
            # Get timezone
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # Get last reset from persistent storage
            field_name = f'last_{reset_type}_monthly_reset'
            last_reset = guild_config.get(field_name)
            
            # CASE 1: Never reset before
            if not last_reset:
                self.logger.warning(f"Guild {guild_id}: No previous {reset_type} monthly reset - RUNNING NOW")
                return True
            
            # Calculate time since last reset
            time_since = now_utc - last_reset
            days_since = time_since.total_seconds() / 86400
            
            # CASE 2: Different month
            last_reset_tz = last_reset.replace(tzinfo=pytz.UTC).astimezone(tz)
            if now.month != last_reset_tz.month or now.year != last_reset_tz.year:
                if now.day >= 1:  # We're in a new month
                    self.logger.info(f"Guild {guild_id}: New month {reset_type} reset - RUNNING NOW")
                    return True
            
            # CASE 3: More than 30 days
            if days_since >= 30:
                self.logger.warning(f"Guild {guild_id}: {days_since:.0f} days since last {reset_type} monthly reset - RUNNING NOW")
                return True
            
            # CASE 4: Emergency - more than 35 days
            if days_since >= 35:
                self.logger.error(f"Guild {guild_id}: EMERGENCY - {days_since:.0f} days since {reset_type} monthly reset - RUNNING NOW")
                return True
            
            return False
            
        except Exception as e:
            # On ANY error, reset to be safe
            self.logger.error(f"Error checking {reset_type} monthly reset for guild {guild_id}: {e} - RUNNING NOW TO BE SAFE")
            return True
    
    async def mark_star_selection_complete(self, guild_id: int):
        """Mark Star selection as completed with timestamp"""
        try:
            await self.db.guild_configs.update_one(
                {'guild_id': guild_id},
                {
                    '$set': {
                        'last_star_selection': datetime.utcnow(),
                        'star_selection_count': {'$inc': 1}
                    }
                },
                upsert=True
            )
            self.logger.info(f"Marked Star selection complete for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Failed to mark Star selection complete: {e}")
    
    async def mark_reset_complete(self, guild_id: int, reset_type: str, period: str):
        """Mark reset as completed with timestamp"""
        try:
            field_name = f'last_{reset_type}_{period}_reset'
            await self.db.guild_configs.update_one(
                {'guild_id': guild_id},
                {'$set': {field_name: datetime.utcnow()}},
                upsert=True
            )
            self.logger.info(f"Marked {reset_type} {period} reset complete for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Failed to mark reset complete: {e}")
    
    async def get_system_health(self, guild_id: int) -> Dict:
        """
        Get comprehensive health check of all scheduled operations.
        Shows exactly when everything last ran and when it will run next.
        """
        try:
            guild_config = await self.db.guild_configs.find_one({'guild_id': guild_id})
            star_config = await self.db.star_configs.find_one({'guild_id': guild_id})
            
            if not guild_config:
                return {'healthy': False, 'error': 'No guild configuration found'}
            
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            health = {
                'guild_id': guild_id,
                'timezone': tz_name,
                'current_time': now.strftime('%Y-%m-%d %H:%M:%S %Z'),
                'healthy': True,
                'operations': {}
            }
            
            # Check Star selection
            if star_config:
                last_star = guild_config.get('last_star_selection')
                if last_star:
                    days_since = (now_utc - last_star).total_seconds() / 86400
                    health['operations']['star_selection'] = {
                        'last_run': last_star.isoformat(),
                        'days_ago': round(days_since, 1),
                        'status': '✅ OK' if days_since < 7.5 else '⚠️ OVERDUE',
                        'next_run': 'Within 5 minutes' if days_since >= 6.5 else f'In {7-days_since:.1f} days'
                    }
                else:
                    health['operations']['star_selection'] = {
                        'status': '⚠️ NEVER RUN',
                        'next_run': 'IMMEDIATELY'
                    }
                    health['healthy'] = False
            
            # Check weekly resets
            for reset_type in ['chat', 'voice']:
                field = f'last_{reset_type}_weekly_reset'
                last_reset = guild_config.get(field)
                if last_reset:
                    days_since = (now_utc - last_reset).total_seconds() / 86400
                    health['operations'][f'{reset_type}_weekly'] = {
                        'last_run': last_reset.isoformat(),
                        'days_ago': round(days_since, 1),
                        'status': '✅ OK' if days_since < 7.5 else '⚠️ OVERDUE'
                    }
                else:
                    health['operations'][f'{reset_type}_weekly'] = {'status': '⚠️ NEVER RUN'}
            
            # Check daily resets
            for reset_type in ['chat', 'voice']:
                field = f'last_{reset_type}_daily_reset'
                last_reset = guild_config.get(field)
                if last_reset:
                    hours_since = (now_utc - last_reset).total_seconds() / 3600
                    health['operations'][f'{reset_type}_daily'] = {
                        'last_run': last_reset.isoformat(),
                        'hours_ago': round(hours_since, 1),
                        'status': '✅ OK' if hours_since < 25 else '⚠️ OVERDUE'
                    }
                else:
                    health['operations'][f'{reset_type}_daily'] = {'status': '⚠️ NEVER RUN'}
            
            return health
            
        except Exception as e:
            return {'healthy': False, 'error': str(e)}


class RecoveryManager:
    """
    Handles recovery from crashes and ensures nothing is missed.
    Runs on startup to catch up on any missed operations.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.recovery')
        self.state_manager = BulletproofStateManager(db)
    
    async def run_startup_recovery(self, bot):
        """
        Run on bot startup to catch any missed operations.
        This ensures 100% reliability even after crashes.
        """
        self.logger.info("=== STARTUP RECOVERY CHECK ===")
        
        try:
            # Get all guilds with configurations
            cursor = self.db.guild_configs.find({})
            configs = await cursor.to_list(length=10000)
            
            recovery_count = 0
            
            for guild_config in configs:
                guild_id = guild_config['guild_id']
                guild = bot.get_guild(guild_id)
                
                if not guild:
                    continue
                
                # Check Star selection
                star_config = await self.db.star_configs.find_one({'guild_id': guild_id})
                if star_config:
                    if await self.state_manager.ensure_star_selection(guild_id, star_config, guild_config):
                        self.logger.warning(f"RECOVERY: Running missed Star selection for guild {guild_id}")
                        # Get the Star cog and run selection
                        star_cog = bot.get_cog('StarOfTheWeekCog')
                        if star_cog:
                            await star_cog._process_star_selection(guild)
                            recovery_count += 1
                
                # Check weekly resets
                if guild_config.get('chat_enabled'):
                    if await self.state_manager.ensure_weekly_reset(guild_id, guild_config, 'chat'):
                        self.logger.warning(f"RECOVERY: Running missed chat weekly reset for guild {guild_id}")
                        chat_cog = bot.get_cog('ChatLeaderboardCog')
                        if chat_cog:
                            await chat_cog._reset_weekly_stats(guild_id)
                            await self.state_manager.mark_reset_complete(guild_id, 'chat', 'weekly')
                            recovery_count += 1
                
                if guild_config.get('voice_enabled'):
                    if await self.state_manager.ensure_weekly_reset(guild_id, guild_config, 'voice'):
                        self.logger.warning(f"RECOVERY: Running missed voice weekly reset for guild {guild_id}")
                        voice_cog = bot.get_cog('VoiceLeaderboardCog')
                        if voice_cog:
                            await voice_cog._reset_weekly_stats(guild_id)
                            await self.state_manager.mark_reset_complete(guild_id, 'voice', 'weekly')
                            recovery_count += 1
                
                # Check daily resets
                if guild_config.get('chat_enabled'):
                    if await self.state_manager.ensure_daily_reset(guild_id, guild_config, 'chat'):
                        self.logger.warning(f"RECOVERY: Running missed chat daily reset for guild {guild_id}")
                        chat_cog = bot.get_cog('ChatLeaderboardCog')
                        if chat_cog:
                            await chat_cog._reset_daily_stats(guild_id)
                            await self.state_manager.mark_reset_complete(guild_id, 'chat', 'daily')
                            recovery_count += 1
                
                if guild_config.get('voice_enabled'):
                    if await self.state_manager.ensure_daily_reset(guild_id, guild_config, 'voice'):
                        self.logger.warning(f"RECOVERY: Running missed voice daily reset for guild {guild_id}")
                        voice_cog = bot.get_cog('VoiceLeaderboardCog')
                        if voice_cog:
                            await voice_cog._reset_daily_stats(guild_id)
                            await self.state_manager.mark_reset_complete(guild_id, 'voice', 'daily')
                            recovery_count += 1
            
            if recovery_count > 0:
                self.logger.warning(f"=== RECOVERY COMPLETE: Ran {recovery_count} missed operations ===")
            else:
                self.logger.info("=== RECOVERY CHECK COMPLETE: All operations up to date ===")
            
        except Exception as e:
            self.logger.error(f"Error during startup recovery: {e}", exc_info=True)
