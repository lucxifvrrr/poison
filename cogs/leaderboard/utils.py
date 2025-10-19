"""
Utility functions for leaderboard system
=========================================
Shared utilities for validation, synchronization, and database operations.
"""

import hashlib
from typing import Optional, Union
import pytz
from datetime import datetime
import logging

logger = logging.getLogger('discord.bot.leaderboard.utils')


class ConfigValidator:
    """Validate configuration values"""
    
    @staticmethod
    def validate_timezone(timezone: str) -> str:
        """
        Validate and return a valid timezone.
        Falls back to UTC if invalid.
        """
        if timezone in pytz.all_timezones:
            return timezone
        logger.warning(f"Invalid timezone '{timezone}', using UTC")
        return 'UTC'
    
    @staticmethod
    def validate_weight(weight: float, name: str = "weight") -> float:
        """
        Validate scoring weight values.
        Must be non-negative and reasonable.
        """
        if weight < 0:
            logger.warning(f"Negative {name} ({weight}), using 0")
            return 0.0
        if weight > 1000:
            logger.warning(f"Excessive {name} ({weight}), capping at 1000")
            return 1000.0
        return weight
    
    @staticmethod
    def validate_limit(limit: int, max_limit: int = 100) -> int:
        """
        Validate and cap limit values.
        """
        if limit < 1:
            return 1
        if limit > max_limit:
            return max_limit
        return limit
    
    @staticmethod
    def validate_channel_id(channel_id: Optional[int]) -> Optional[int]:
        """
        Validate Discord channel ID.
        """
        if channel_id is None:
            return None
        if channel_id < 0:
            logger.warning(f"Invalid channel ID {channel_id}")
            return None
        return channel_id


class UserFormatter:
    """Format user information consistently across leaderboards"""
    
    @staticmethod
    def format_username(user_id: int, display_name: Optional[str] = None, max_length: int = 20) -> str:
        """
        Format username with consistent fallback for users who left.
        Uses a hash-based approach to avoid ID collisions.
        """
        if display_name:
            # Truncate long names
            if len(display_name) > max_length:
                return display_name[:max_length - 3] + "..."
            return display_name
        
        # Generate consistent hash for users who left
        user_id_str = str(user_id)
        # Use last 6 digits plus a hash character for uniqueness
        hash_char = chr(65 + (user_id % 26))  # A-Z based on ID
        username = f"User{hash_char}-{user_id_str[-6:]}"
        return username
    
    @staticmethod
    def get_user_hash(user_id: int) -> str:
        """
        Generate a short hash for a user ID.
        Used for anonymous references.
        """
        hash_obj = hashlib.md5(str(user_id).encode())
        return hash_obj.hexdigest()[:8]


class DatabaseTransactionManager:
    """
    Manage database transactions to prevent concurrent modification issues.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.transactions')
    
    async def atomic_update(self, collection_name: str, filter_dict: dict, 
                           update_dict: dict, upsert: bool = False) -> bool:
        """
        Perform an atomic update with retry logic.
        Returns True if successful.
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                collection = self.db[collection_name]
                result = await collection.update_one(
                    filter_dict,
                    update_dict,
                    upsert=upsert
                )
                return result.acknowledged
            except Exception as e:
                if attempt < max_retries - 1:
                    self.logger.warning(f"Retry {attempt + 1}/{max_retries} for atomic update: {e}")
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    self.logger.error(f"Failed atomic update after {max_retries} attempts: {e}")
                    return False
        return False
    
    async def bulk_update(self, collection_name: str, updates: list) -> int:
        """
        Perform bulk updates efficiently.
        Returns number of successful updates.
        """
        if not updates:
            return 0
        
        successful = 0
        collection = self.db[collection_name]
        
        # Process in batches
        batch_size = 100
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]
            try:
                # Use bulk write for efficiency
                operations = []
                for update in batch:
                    operations.append(
                        pymongo.UpdateOne(
                            update['filter'],
                            update['update'],
                            upsert=update.get('upsert', False)
                        )
                    )
                
                result = await collection.bulk_write(operations, ordered=False)
                successful += result.modified_count + result.upserted_count
            except Exception as e:
                self.logger.error(f"Bulk update failed for batch: {e}")
        
        return successful


class TaskSynchronizer:
    """
    Coordinate task execution between different cogs.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.sync')
    
    async def acquire_lock(self, guild_id: int, lock_type: str, 
                          timeout_seconds: int = 60) -> bool:
        """
        Acquire a distributed lock for a specific operation.
        Returns True if lock acquired.
        """
        lock_doc = {
            'guild_id': guild_id,
            'lock_type': lock_type,
            'acquired_at': datetime.utcnow(),
            'expires_at': datetime.utcnow().timestamp() + timeout_seconds
        }
        
        try:
            # Try to insert lock document
            await self.db.task_locks.insert_one(lock_doc)
            return True
        except:
            # Lock already exists, check if expired
            existing = await self.db.task_locks.find_one({
                'guild_id': guild_id,
                'lock_type': lock_type
            })
            
            if existing and existing['expires_at'] < datetime.utcnow().timestamp():
                # Lock expired, try to update it
                result = await self.db.task_locks.update_one(
                    {
                        'guild_id': guild_id,
                        'lock_type': lock_type,
                        'expires_at': {'$lt': datetime.utcnow().timestamp()}
                    },
                    {'$set': lock_doc}
                )
                return result.modified_count > 0
            
            return False
    
    async def release_lock(self, guild_id: int, lock_type: str):
        """
        Release a distributed lock.
        """
        try:
            await self.db.task_locks.delete_one({
                'guild_id': guild_id,
                'lock_type': lock_type
            })
        except Exception as e:
            self.logger.error(f"Failed to release lock {lock_type} for guild {guild_id}: {e}")
    
    async def check_recent_reset(self, guild_id: int, reset_type: str, 
                                hours_threshold: int = 6) -> bool:
        """
        Check if a reset was performed recently.
        Returns True if reset happened within threshold.
        """
        try:
            config = await self.db.guild_configs.find_one({'guild_id': guild_id})
            if not config:
                return False
            
            reset_field = f'last_{reset_type}_reset'
            last_reset = config.get(reset_field)
            
            if not last_reset:
                return False
            
            time_since = (datetime.utcnow() - last_reset).total_seconds() / 3600
            return time_since < hours_threshold
        except Exception as e:
            self.logger.error(f"Error checking recent reset: {e}")
            return False
    
    async def mark_reset_complete(self, guild_id: int, reset_type: str):
        """
        Mark a reset as completed.
        """
        try:
            await self.db.guild_configs.update_one(
                {'guild_id': guild_id},
                {'$set': {f'last_{reset_type}_reset': datetime.utcnow()}}
            )
        except Exception as e:
            self.logger.error(f"Failed to mark reset complete: {e}")


# Import guard for asyncio
import asyncio
try:
    import pymongo
except ImportError:
    # pymongo not available, bulk operations will be limited
    pymongo = None
