"""
Leaderboard System Initialization
==================================
Ensures bulletproof operation with automatic recovery.
"""

import logging
from .state_manager import RecoveryManager

logger = logging.getLogger('discord.bot.leaderboard')

async def setup(bot):
    """
    Setup all leaderboard cogs with recovery system.
    This ensures nothing is ever missed.
    """
    # Import and load all cogs
    from .chat_leaderboard_cog import ChatLeaderboardCog
    from .voice_leaderboard_cog import VoiceLeaderboardCog
    from .star_of_the_week_cog import StarOfTheWeekCog
    from .debug_helper import DebugCommands
    
    # Load cogs
    await bot.add_cog(ChatLeaderboardCog(bot))
    logger.info("✅ Chat leaderboard cog loaded")
    
    await bot.add_cog(VoiceLeaderboardCog(bot))
    logger.info("✅ Voice leaderboard cog loaded")
    
    await bot.add_cog(StarOfTheWeekCog(bot))
    logger.info("✅ Star of the Week cog loaded")
    
    await bot.add_cog(DebugCommands(bot))
    logger.info("✅ Debug commands loaded")
    
    # Run recovery check after all cogs are loaded
    if hasattr(bot, 'mongo_client') and bot.mongo_client:
        db = bot.mongo_client['poison_bot']
        recovery_manager = RecoveryManager(db)
        
        # Schedule recovery to run after bot is fully ready
        @bot.event
        async def on_ready():
            logger.info("🔄 Running startup recovery check...")
            await recovery_manager.run_startup_recovery(bot)
            logger.info("✅ Startup recovery complete")
    
    logger.info("🛡️ BULLETPROOF LEADERBOARD SYSTEM INITIALIZED")
    logger.info("📊 Checks run every 5 minutes")
    logger.info("♻️ Automatic recovery enabled")
    logger.info("💾 Persistent state tracking active")
