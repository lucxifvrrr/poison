"""
Star of the Week Cog
====================
Selects and announces the most active member each week based on combined chat + voice activity.

Features:
- Weekly selection every Sunday 11 AM (guild timezone) - runs BEFORE weekly reset
- Combined scoring: chat messages + voice minutes
- Configurable weights per guild
- Auto role assignment/removal
- DM winner with styled embed
- Optional public announcement
- History tracking in MongoDB
- Tie-breaker: voice minutes > chat messages
- Bot users are automatically excluded

Admin Commands (consolidated under /star):
- /star setup role:@role announce_channel:#channel weight_chat:1.0 weight_voice:2.0
- /star history limit:5
- /star preview

Author: Production-Ready Discord Bot
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
import asyncio
import os
from typing import Optional, Dict, List
import pytz
from dotenv import load_dotenv

load_dotenv()


class StarOfTheWeekCog(commands.Cog):
    """
    Star of the Week System
    Automatically selects and rewards the most active member weekly.
    """
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None
    
    async def cog_load(self):
        """Initialize MongoDB connection on cog load"""
        # Reuse bot's shared MongoDB connection
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.mongo_client = self.bot.mongo_client
            self.db = self.mongo_client['discord_bot']
            print("✅ Star of the Week Cog: Reusing shared MongoDB connection")
        else:
            # Fallback: create new connection if shared one doesn't exist
            mongo_url = os.getenv('MONGO_URL')
            if not mongo_url:
                raise ValueError("MONGO_URL not found in environment variables")
            
            self.mongo_client = AsyncIOMotorClient(mongo_url)
            self.db = self.mongo_client['discord_bot']
            print("✅ Star of the Week Cog: MongoDB connected")
        
        # Start task AFTER database connection is established
        self.weekly_star_selection.start()
    
    async def cog_unload(self):
        """Cleanup on cog unload"""
        self.weekly_star_selection.cancel()
        # Don't close shared MongoDB connection - it's managed by the bot
        # Only close if we created our own connection
        if self.mongo_client and not hasattr(self.bot, 'mongo_client'):
            self.mongo_client.close()
    
    # ==================== DATABASE HELPERS ====================
    
    async def _get_guild_config(self, guild_id: int) -> Optional[Dict]:
        """Fetch guild configuration from MongoDB"""
        return await self.db.guild_configs.find_one({'guild_id': guild_id})
    
    async def _get_star_config(self, guild_id: int) -> Optional[Dict]:
        """Get Star of the Week configuration for a guild"""
        return await self.db.star_configs.find_one({'guild_id': guild_id})
    
    async def _save_star_config(self, guild_id: int, role_id: int, 
                               announce_channel_id: Optional[int],
                               weight_chat: float, weight_voice: float):
        """Save Star of the Week configuration"""
        await self.db.star_configs.update_one(
            {'guild_id': guild_id},
            {
                '$set': {
                    'role_id': role_id,
                    'announce_channel_id': announce_channel_id,
                    'weight_chat': weight_chat,
                    'weight_voice': weight_voice,
                    'last_update': datetime.utcnow()
                }
            },
            upsert=True
        )
    
    async def _get_previous_winner(self, guild_id: int) -> Optional[Dict]:
        """Get the most recent Star of the Week winner"""
        cursor = self.db.star_history.find(
            {'guild_id': guild_id}
        ).sort('awarded_at', -1).limit(1)
        
        results = await cursor.to_list(length=1)
        return results[0] if results else None
    
    async def _save_star_winner(self, guild_id: int, user_id: int, 
                               score: float, chat_count: int, voice_minutes: float):
        """Save Star of the Week winner to history"""
        await self.db.star_history.insert_one({
            'guild_id': guild_id,
            'user_id': user_id,
            'score': score,
            'chat_weekly': chat_count,
            'voice_weekly': voice_minutes,
            'awarded_at': datetime.utcnow()
        })
    
    async def _get_weekly_stats(self, guild_id: int) -> List[Dict]:
        """Get all users with weekly activity (excluding bots)"""
        # Get all stats with activity
        cursor = self.db.user_stats.find({
            'guild_id': guild_id,
            '$or': [
                {'chat_weekly': {'$gt': 0}},
                {'voice_weekly': {'$gt': 0}}
            ]
        })
        
        stats = await cursor.to_list(length=10000)
        
        # Filter out bots
        guild = self.bot.get_guild(guild_id)
        if guild:
            filtered_stats = []
            for stat in stats:
                member = guild.get_member(stat['user_id'])
                if member and not member.bot:
                    filtered_stats.append(stat)
            return filtered_stats
        
        return stats
    
    # ==================== SCORING & SELECTION ====================
    
    def _calculate_score(self, chat_count: int, voice_minutes: float, 
                        weight_chat: float, weight_voice: float) -> float:
        """
        Calculate combined activity score.
        
        Args:
            chat_count: Number of messages sent
            voice_minutes: Minutes spent in voice
            weight_chat: Weight multiplier for chat
            weight_voice: Weight multiplier for voice minutes
        
        Returns:
            Combined score
        """
        return (chat_count * weight_chat) + (voice_minutes * weight_voice)
    
    async def _select_star_of_week(self, guild_id: int) -> Optional[Dict]:
        """
        Select Star of the Week based on combined activity.
        
        Returns:
            Dict with winner info: {user_id, score, chat_weekly, voice_weekly}
            None if no eligible users
        """
        # Get configuration
        star_config = await self._get_star_config(guild_id)
        if not star_config:
            print(f"⚠️ No Star config for guild {guild_id}")
            return None
        
        weight_chat = star_config.get('weight_chat', 1.0)
        weight_voice = star_config.get('weight_voice', 2.0)
        
        # Get weekly stats
        stats = await self._get_weekly_stats(guild_id)
        
        if not stats:
            print(f"⚠️ No weekly activity for guild {guild_id}")
            return None
        
        # Calculate scores for all users
        scored_users = []
        for user_stat in stats:
            chat_count = user_stat.get('chat_weekly', 0)
            voice_minutes = user_stat.get('voice_weekly', 0)
            
            # Skip users with no activity
            if chat_count == 0 and voice_minutes == 0:
                continue
            
            score = self._calculate_score(chat_count, voice_minutes, weight_chat, weight_voice)
            
            scored_users.append({
                'user_id': user_stat['user_id'],
                'score': score,
                'chat_weekly': chat_count,
                'voice_weekly': voice_minutes
            })
        
        if not scored_users:
            return None
        
        # Sort by score (desc), then voice_minutes (desc), then chat (desc)
        scored_users.sort(
            key=lambda x: (x['score'], x['voice_weekly'], x['chat_weekly']),
            reverse=True
        )
        
        return scored_users[0]
    
    # ==================== ROLE MANAGEMENT ====================
    
    async def _assign_star_role(self, guild: discord.Guild, user_id: int, role_id: int) -> bool:
        """
        Assign Star of the Week role to user and remove from previous winner.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            role = guild.get_role(role_id)
            if not role:
                print(f"❌ Star role {role_id} not found in guild {guild.id}")
                return False
            
            # Remove role from previous winner
            previous_winner = await self._get_previous_winner(guild.id)
            if previous_winner:
                prev_member = guild.get_member(previous_winner['user_id'])
                if prev_member and role in prev_member.roles:
                    await prev_member.remove_roles(role, reason="Star of the Week expired")
                    print(f"✅ Removed Star role from {prev_member.display_name}")
            
            # Add role to new winner
            member = guild.get_member(user_id)
            if not member:
                print(f"❌ Winner {user_id} not found in guild {guild.id}")
                return False
            
            await member.add_roles(role, reason="Star of the Week winner")
            print(f"✅ Assigned Star role to {member.display_name}")
            return True
        
        except Exception as e:
            print(f"❌ Error assigning Star role: {e}")
            return False
    
    # ==================== NOTIFICATIONS ====================
    
    def _create_winner_dm_embed(self, guild: discord.Guild, score: float, 
                                chat_count: int, voice_minutes: float) -> discord.Embed:
        """
        Create styled DM embed for the winner.
        
        Args:
            guild: Discord guild
            score: Combined score
            chat_count: Weekly messages
            voice_minutes: Weekly voice minutes
        
        Returns:
            Discord embed
        """
        # Format voice time
        voice_hours = int(voice_minutes // 60)
        voice_mins = int(voice_minutes % 60)
        voice_str = f"{voice_hours}h {voice_mins}m" if voice_hours > 0 else f"{voice_mins}m"
        
        embed = discord.Embed(
            title="⭐ You're the Star of the Week!",
            description=(
                f"Congratulations! You've been selected as **{guild.name}'s** "
                f"Star of the Week for your outstanding activity!\n\n"
                f"**🏆 Your Stats This Week:**\n"
                f"💬 **Messages Sent:** `{chat_count:,}`\n"
                f"🎤 **Voice Time:** `{voice_str}`\n"
                f"📊 **Combined Score:** `{score:.1f}`\n\n"
                f"Keep up the amazing work! 🌟"
            ),
            color=0xFFD700,  # Gold color
            timestamp=datetime.utcnow()
        )
        
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        embed.set_footer(text=f"{guild.name} • Star of the Week", icon_url=guild.icon.url if guild.icon else None)
        
        return embed
    
    def _create_announcement_embed(self, member: discord.Member, score: float,
                                   chat_count: int, voice_minutes: float) -> discord.Embed:
        """
        Create public announcement embed.
        
        Args:
            member: Winner member
            score: Combined score
            chat_count: Weekly messages
            voice_minutes: Weekly voice minutes
        
        Returns:
            Discord embed
        """
        # Format voice time
        voice_hours = int(voice_minutes // 60)
        voice_mins = int(voice_minutes % 60)
        voice_str = f"{voice_hours}h {voice_mins}m" if voice_hours > 0 else f"{voice_mins}m"
        
        embed = discord.Embed(
            title="⭐ Star of the Week Announcement",
            description=(
                f"🎉 Congratulations to {member.mention} for being this week's **Star of the Week**!\n\n"
                f"**📈 Weekly Activity:**\n"
                f"💬 Messages: `{chat_count:,}`\n"
                f"🎤 Voice Time: `{voice_str}`\n"
                f"🏆 Score: `{score:.1f}`\n\n"
                f"Thank you for being such an active and valuable member of our community! 🌟"
            ),
            color=0xFFD700,
            timestamp=datetime.utcnow()
        )
        
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Keep being awesome!", icon_url=member.guild.icon.url if member.guild.icon else None)
        
        return embed
    
    async def _notify_winner(self, guild: discord.Guild, user_id: int,
                            score: float, chat_count: int, voice_minutes: float):
        """
        Send DM to winner and optionally announce in channel.
        
        Args:
            guild: Discord guild
            user_id: Winner user ID
            score: Combined score
            chat_count: Weekly messages
            voice_minutes: Weekly voice minutes
        """
        member = guild.get_member(user_id)
        if not member:
            print(f"❌ Winner {user_id} not found in guild {guild.id}")
            return
        
        # Send DM to winner
        try:
            dm_embed = self._create_winner_dm_embed(guild, score, chat_count, voice_minutes)
            await member.send(embed=dm_embed)
            print(f"✅ Sent Star DM to {member.display_name}")
        except discord.Forbidden:
            print(f"⚠️ Cannot DM {member.display_name} (DMs disabled)")
        except Exception as e:
            print(f"❌ Error sending DM to winner: {e}")
        
        # Announce in channel if configured
        star_config = await self._get_star_config(guild.id)
        if star_config and star_config.get('announce_channel_id'):
            try:
                channel = guild.get_channel(star_config['announce_channel_id'])
                if channel:
                    announce_embed = self._create_announcement_embed(member, score, chat_count, voice_minutes)
                    await channel.send(embed=announce_embed)
                    print(f"✅ Announced Star in {channel.name}")
            except Exception as e:
                print(f"❌ Error announcing Star: {e}")
    
    # ==================== BACKGROUND TASKS ====================
    
    @tasks.loop(hours=1)
    async def weekly_star_selection(self):
        """
        Check for weekly Star selection (Sunday 12 PM guild time).
        Runs every hour and checks if it's time to select.
        """
        try:
            # Get all guilds with Star config
            cursor = self.db.star_configs.find({})
            configs = await cursor.to_list(length=1000)
            
            for star_config in configs:
                guild_id = star_config['guild_id']
                guild = self.bot.get_guild(guild_id)
                
                if not guild:
                    continue
                
                # Get guild timezone
                guild_config = await self._get_guild_config(guild_id)
                tz_name = guild_config.get('timezone', 'UTC') if guild_config else 'UTC'
                
                try:
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    
                    # Check if it's Sunday 11 AM (runs BEFORE weekly reset at 12 PM)
                    if now.weekday() == 6 and now.hour == 11:
                        # Check if we already selected this week
                        previous_winner = await self._get_previous_winner(guild_id)
                        if previous_winner:
                            # Check if selection was within last 6 days (avoid duplicate selections)
                            time_since_last = datetime.utcnow() - previous_winner['awarded_at']
                            if time_since_last < timedelta(days=6):
                                print(f"⏭️ Already selected Star this week for guild {guild_id}")
                                continue
                        
                        # Select Star of the Week
                        await self._process_star_selection(guild)
                
                except Exception as e:
                    print(f"❌ Error checking Star selection for guild {guild_id}: {e}")
        
        except Exception as e:
            print(f"❌ Error in weekly_star_selection task: {e}")
    
    @weekly_star_selection.before_loop
    async def before_weekly_star_selection(self):
        """Wait for bot to be ready"""
        await self.bot.wait_until_ready()
    
    async def _process_star_selection(self, guild: discord.Guild):
        """
        Process Star of the Week selection for a guild.
        
        Args:
            guild: Discord guild
        """
        try:
            print(f"🌟 Processing Star of the Week selection for {guild.name}")
            
            # Select winner
            winner = await self._select_star_of_week(guild.id)
            
            if not winner:
                print(f"⚠️ No eligible users for Star of the Week in {guild.name}")
                return
            
            # Get Star config
            star_config = await self._get_star_config(guild.id)
            if not star_config:
                print(f"❌ No Star config found for guild {guild.id}")
                return
            
            # Assign role
            role_assigned = await self._assign_star_role(
                guild, 
                winner['user_id'], 
                star_config['role_id']
            )
            
            if not role_assigned:
                print(f"❌ Failed to assign Star role in {guild.name}")
                return
            
            # Save to history
            await self._save_star_winner(
                guild.id,
                winner['user_id'],
                winner['score'],
                winner['chat_weekly'],
                winner['voice_weekly']
            )
            
            # Notify winner
            await self._notify_winner(
                guild,
                winner['user_id'],
                winner['score'],
                winner['chat_weekly'],
                winner['voice_weekly']
            )
            
            print(f"✅ Star of the Week selected for {guild.name}: User {winner['user_id']}")
        
        except Exception as e:
            print(f"❌ Error processing Star selection for {guild.name}: {e}")
    
    # ==================== CONSOLIDATED SLASH COMMAND ====================
    
    star_group = app_commands.Group(name="star", description="Star of the Week management")
    
    @star_group.command(name="setup", description="Configure Star of the Week system")
    @app_commands.describe(
        role="Role to assign to Star of the Week",
        announce_channel="Channel to announce winner (optional)",
        weight_chat="Score weight for chat messages (default: 1.0)",
        weight_voice="Score weight per voice minute (default: 2.0)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_star(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        announce_channel: Optional[discord.TextChannel] = None,
        weight_chat: Optional[float] = 1.0,
        weight_voice: Optional[float] = 2.0
    ):
        """
        Configure Star of the Week system.
        
        Admin only command to set up automatic weekly winner selection.
        
        Scoring formula: score = (messages × weight_chat) + (voice_minutes × weight_voice)
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Validate weights
            if weight_chat < 0 or weight_voice < 0:
                await interaction.followup.send(
                    "❌ Weights must be positive numbers!",
                    ephemeral=True
                )
                return
            
            # Save configuration
            await self._save_star_config(
                interaction.guild.id,
                role.id,
                announce_channel.id if announce_channel else None,
                weight_chat,
                weight_voice
            )
            
            # Build response
            response = (
                f"✅ **Star of the Week configured!**\n\n"
                f"🏆 **Role:** {role.mention}\n"
                f"📢 **Announce Channel:** {announce_channel.mention if announce_channel else 'None (DM only)'}\n"
                f"📊 **Scoring Weights:**\n"
                f"   • Chat messages: `{weight_chat}` point(s) each\n"
                f"   • Voice minutes: `{weight_voice}` point(s) each\n\n"
                f"🗓️ **Selection:** Every Sunday at 11 AM (guild timezone)\n"
                f"💡 **Tip:** Higher weights = more impact on score"
            )
            
            await interaction.followup.send(response, ephemeral=True)
            print(f"✅ Star of the Week configured for guild {interaction.guild.id}")
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error configuring Star of the Week: {e}",
                ephemeral=True
            )
            print(f"❌ Setup error: {e}")
    
    @star_group.command(name="history", description="View past Star of the Week winners")
    @app_commands.describe(limit="Number of past winners to show (default: 5)")
    async def star_history(
        self,
        interaction: discord.Interaction,
        limit: Optional[int] = 5
    ):
        """
        Display Star of the Week history.
        
        Shows past winners with their scores and stats.
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Fetch history
            cursor = self.db.star_history.find(
                {'guild_id': interaction.guild.id}
            ).sort('awarded_at', -1).limit(min(limit, 20))
            
            history = await cursor.to_list(length=20)
            
            if not history:
                await interaction.followup.send(
                    "📜 No Star of the Week history yet!",
                    ephemeral=True
                )
                return
            
            # Build embed
            embed = discord.Embed(
                title="⭐ Star of the Week History",
                description=f"Past {len(history)} winner(s)",
                color=0xFFD700,
                timestamp=datetime.utcnow()
            )
            
            for idx, winner in enumerate(history, 1):
                member = interaction.guild.get_member(winner['user_id'])
                username = member.mention if member else f"<@{winner['user_id']}>"
                
                voice_hours = int(winner['voice_weekly'] // 60)
                voice_mins = int(winner['voice_weekly'] % 60)
                voice_str = f"{voice_hours}h {voice_mins}m" if voice_hours > 0 else f"{voice_mins}m"
                
                awarded_timestamp = int(winner['awarded_at'].timestamp())
                
                embed.add_field(
                    name=f"#{idx} • {username}",
                    value=(
                        f"🗓️ <t:{awarded_timestamp}:R>\n"
                        f"💬 {winner['chat_weekly']:,} messages\n"
                        f"🎤 {voice_str}\n"
                        f"🏆 Score: {winner['score']:.1f}"
                    ),
                    inline=True
                )
            
            embed.set_footer(
                text=f"{interaction.guild.name} • Star History",
                icon_url=interaction.guild.icon.url if interaction.guild.icon else None
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error fetching history: {e}",
                ephemeral=True
            )
    
    @star_group.command(name="preview", description="Preview current week's top candidates")
    async def star_preview(self, interaction: discord.Interaction):
        """
        Preview top 5 candidates for Star of the Week based on current weekly stats.
        
        Useful for checking who's leading before Sunday selection.
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get Star config
            star_config = await self._get_star_config(interaction.guild.id)
            if not star_config:
                await interaction.followup.send(
                    "❌ Star of the Week not configured! Use `/star setup` first.",
                    ephemeral=True
                )
                return
            
            weight_chat = star_config.get('weight_chat', 1.0)
            weight_voice = star_config.get('weight_voice', 2.0)
            
            # Get weekly stats
            stats = await self._get_weekly_stats(interaction.guild.id)
            
            if not stats:
                await interaction.followup.send(
                    "📊 No activity this week yet!",
                    ephemeral=True
                )
                return
            
            # Calculate scores
            scored_users = []
            for user_stat in stats:
                chat_count = user_stat.get('chat_weekly', 0)
                voice_minutes = user_stat.get('voice_weekly', 0)
                
                if chat_count == 0 and voice_minutes == 0:
                    continue
                
                score = self._calculate_score(chat_count, voice_minutes, weight_chat, weight_voice)
                scored_users.append({
                    'user_id': user_stat['user_id'],
                    'score': score,
                    'chat_weekly': chat_count,
                    'voice_weekly': voice_minutes
                })
            
            # Sort and get top 5
            scored_users.sort(key=lambda x: (x['score'], x['voice_weekly'], x['chat_weekly']), reverse=True)
            top_5 = scored_users[:5]
            
            # Build embed
            embed = discord.Embed(
                title="🌟 Star of the Week Preview",
                description="Top 5 candidates based on current weekly activity",
                color=0xFFD700,
                timestamp=datetime.utcnow()
            )
            
            for idx, user in enumerate(top_5, 1):
                member = interaction.guild.get_member(user['user_id'])
                username = member.mention if member else f"<@{user['user_id']}>"
                
                voice_hours = int(user['voice_weekly'] // 60)
                voice_mins = int(user['voice_weekly'] % 60)
                voice_str = f"{voice_hours}h {voice_mins}m" if voice_hours > 0 else f"{voice_mins}m"
                
                medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][idx - 1]
                
                embed.add_field(
                    name=f"{medal} {username}",
                    value=(
                        f"💬 {user['chat_weekly']:,} messages\n"
                        f"🎤 {voice_str}\n"
                        f"🏆 Score: {user['score']:.1f}"
                    ),
                    inline=True
                )
            
            embed.set_footer(text=f"Weights: Chat={weight_chat}, Voice={weight_voice} per minute")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Load the cog"""
    await bot.add_cog(StarOfTheWeekCog(bot))
