import discord
import random
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
from discord.ext import commands, tasks

# Import from core and config
from cogs.giveaways.giveaway_core import get_current_utc_timestamp
from cogs.giveaways.config import DOT_EMOJI, RED_DOT_EMOJI, EMBED_COLOR, MIN_FAKE_REACTIONS, MAX_FAKE_REACTIONS, MIN_FAKE_DURATION, MAX_FAKE_DURATION

class GiveawayAdminCog(commands.Cog):
    """Admin commands for managing giveaways."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logging.getLogger('GiveawayBot')
        self.active_fake_reaction_tasks: Dict[str, asyncio.Task] = {}
        self._ready = asyncio.Event()

    async def cog_load(self) -> None:
        """Called when cog is loaded."""
        self.process_fake_reactions.start()
        self._ready.set()

    def cog_unload(self) -> None:
        """Called when cog is unloaded."""
        self.process_fake_reactions.cancel()
        for task in self.active_fake_reaction_tasks.values():
            task.cancel()

    @tasks.loop(minutes=1)
    async def process_fake_reactions(self) -> None:
        """Process and resume any active fake reaction plans."""
        await self._ready.wait()
        giveaway_cog = self.bot.get_cog("GiveawayCog")
        if (
            not giveaway_cog
            or not hasattr(giveaway_cog, "db")
            or not giveaway_cog.db.connected
        ):
            return

        try:
            plans = await giveaway_cog.db.fetchall(
                "SELECT * FROM fake_reactions WHERE status = ?", ("active",)
            )
            for plan in plans:
                mid = plan["message_id"]
                if mid in self.active_fake_reaction_tasks:
                    continue

                # Ensure giveaway still active
                gw = await giveaway_cog.db.fetchone(
                    "SELECT * FROM giveaways WHERE message_id = ? AND status = ?",
                    (mid, "active"),
                )
                if not gw:
                    # Cancel stale plan
                    await giveaway_cog.db.execute(
                        "UPDATE fake_reactions SET status = ?, cancelled_at = ? WHERE message_id = ?",
                        ("cancelled", get_current_utc_timestamp(), mid),
                    )
                    continue

                channel = self.bot.get_channel(plan["channel_id"])
                if not channel or not isinstance(channel, discord.TextChannel):
                    continue

                members = [str(m.id) for m in channel.guild.members if not m.bot]
                if not members:
                    continue

                remaining = plan["remaining_reactions"]
                end_time = plan["end_time"]
                if remaining > 0 and end_time > get_current_utc_timestamp():
                    task = asyncio.create_task(
                        self.add_fake_reactions(mid, members, plan["total_reactions"], end_time)
                    )
                    self.active_fake_reaction_tasks[mid] = task

        except Exception as e:
            self.logger.error(f"process_fake_reactions error: {e}")

    @discord.app_commands.command(
        name="fill_giveaway",
        description="Gradually fill a giveaway with fake reactions",
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def fill_giveaway(
        self,
        interaction: discord.Interaction,
        message_id: str,
        total_fake_reactions: int,
        duration_in_minutes: int,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            if not (MIN_FAKE_REACTIONS <= total_fake_reactions <= MAX_FAKE_REACTIONS):
                raise ValueError(f"Total fake reactions must be {MIN_FAKE_REACTIONS}–{MAX_FAKE_REACTIONS}.")
            if not (MIN_FAKE_DURATION <= duration_in_minutes <= MAX_FAKE_DURATION):
                raise ValueError(f"Duration must be {MIN_FAKE_DURATION}–{MAX_FAKE_DURATION} minutes.")

            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if (
                not giveaway_cog
                or not hasattr(giveaway_cog, "db")
                or not giveaway_cog.db.connected
            ):
                return await interaction.followup.send(
                    "Giveaway system not available.", ephemeral=True
                )

            gw = await giveaway_cog.db.fetchone(
                "SELECT * FROM giveaways WHERE message_id = ? AND status = ?",
                (message_id, "active"),
            )
            if not gw:
                return await interaction.followup.send(
                    "Not an active giveaway.", ephemeral=True
                )

            # Cancel existing fake fill
            if message_id in self.active_fake_reaction_tasks:
                self.active_fake_reaction_tasks[message_id].cancel()

            channel = self.bot.get_channel(gw["channel_id"])
            try:
                await channel.fetch_message(int(message_id))
            except:
                return await interaction.followup.send(
                    "Couldn't fetch giveaway message.", ephemeral=True
                )

            members = [str(m.id) for m in channel.guild.members if not m.bot]
            if not members:
                return await interaction.followup.send(
                    "No valid members.", ephemeral=True
                )

            end_time = get_current_utc_timestamp() + duration_in_minutes * 60
            await giveaway_cog.db.execute(
                """
                INSERT OR REPLACE INTO fake_reactions
                (message_id, channel_id, total_reactions, remaining_reactions, end_time, created_by, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    gw["channel_id"],
                    total_fake_reactions,
                    total_fake_reactions,
                    end_time,
                    interaction.user.id,
                    get_current_utc_timestamp(),
                    "active",
                ),
            )

            task = asyncio.create_task(
                self.add_fake_reactions(
                    message_id, members, total_fake_reactions, end_time
                )
            )
            self.active_fake_reaction_tasks[message_id] = task

            await interaction.followup.send(
                f"Started fake fill: {total_fake_reactions} over {duration_in_minutes} minutes.",
                ephemeral=True,
            )

        except ValueError as ve:
            await interaction.followup.send(f"Error: {ve}", ephemeral=True)
        except Exception as e:
            self.logger.error(f"fill_giveaway error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    async def add_fake_reactions(
        self,
        message_id: str,
        member_ids: List[str],
        total_reactions: int,
        end_time: float,
    ) -> None:
        """Gradually add fake reactions to a giveaway message."""
        giveaway_cog = self.bot.get_cog("GiveawayCog")
        if (
            not giveaway_cog
            or not hasattr(giveaway_cog, "db")
            or not giveaway_cog.db.connected
        ):
            return

        try:
            gw = await giveaway_cog.db.fetchone(
                "SELECT * FROM giveaways WHERE message_id = ?", (message_id,)
            )
            if not gw:
                return

            channel = self.bot.get_channel(gw["channel_id"])
            message = await channel.fetch_message(int(message_id))

            remaining = total_reactions
            while remaining > 0:
                task = asyncio.current_task()
                if task and task.cancelled():
                    raise asyncio.CancelledError()

                now = get_current_utc_timestamp()
                if now >= end_time:
                    break

                active = await giveaway_cog.db.fetchone(
                    "SELECT * FROM giveaways WHERE message_id = ? AND status = ?",
                    (message_id, "active"),
                )
                if not active:
                    break

                used = await giveaway_cog.db.fetchall(
                    """
                    SELECT original_user_id FROM participants
                    WHERE message_id = ? AND is_fake = 1
                    """,
                    (message_id,),
                )
                used_ids = {row["original_user_id"] for row in used if row["original_user_id"]}

                available = [uid for uid in member_ids if uid not in used_ids]
                if not available:
                    available = member_ids

                user_id = random.choice(available)
                fake_id = f"{user_id}_fake_{total_reactions - remaining}"

                # Check if this fake user already exists in the participants table
                existing = await giveaway_cog.db.fetchone(
                    "SELECT * FROM participants WHERE message_id = ? AND user_id = ?",
                    (message_id, fake_id)
                )
                
                # Only insert if they don't already exist
                if not existing:
                    await giveaway_cog.db.execute(
                        """
                        INSERT INTO participants
                        (message_id, user_id, original_user_id, joined_at, is_fake, is_forced)
                        VALUES (?, ?, ?, ?, ?, 0)
                        """,
                        (message_id, fake_id, user_id, now, 1),
                    )

                remaining -= 1
                await giveaway_cog.db.execute(
                    "UPDATE fake_reactions SET remaining_reactions = ? WHERE message_id = ?",
                    (remaining, message_id),
                )

                # Update embed with native timestamps & timestamp field
                embed = message.embeds[0]
                embed.description = (
                    f"{DOT_EMOJI} Ends: <t:{gw['end_time']}:R>\n"
                    f"{DOT_EMOJI} Hosted by: <@{gw['host_id']}>"
                )
                embed.timestamp = datetime.fromtimestamp(gw["end_time"], timezone.utc)
                await message.edit(embed=embed)

                # Spread reactions evenly/randomly
                avg = max((end_time - now) / max(1, remaining), 1)
                delay = random.uniform(avg * 0.5, avg * 1.5)
                if now + delay > end_time:
                    break
                await asyncio.sleep(delay)

            # On finish, record fake participants
            rows = await giveaway_cog.db.fetchall(
                "SELECT user_id FROM participants WHERE message_id = ? AND is_fake = 1",
                (message_id,),
            )
            fake_list = [r["user_id"] for r in rows]
            await giveaway_cog.db.execute(
                """
                UPDATE fake_reactions SET
                  status = ?, completed_at = ?, remaining_reactions = 0, fake_participants = ?
                WHERE message_id = ?
                """,
                ("completed", get_current_utc_timestamp(), json.dumps(fake_list), message_id),
            )

        except asyncio.CancelledError:
            await giveaway_cog.db.execute(
                """
                UPDATE fake_reactions
                SET status = ?, cancelled_at = ?
                WHERE message_id = ?
                """,
                ("cancelled", get_current_utc_timestamp(), message_id),
            )
        except Exception as e:
            self.logger.error(f"add_fake_reactions error for {message_id}: {e}")
            await giveaway_cog.db.execute(
                """
                UPDATE fake_reactions
                SET status = ?, error = ?
                WHERE message_id = ?
                """,
                ("error", str(e), message_id),
            )
        finally:
            self.active_fake_reaction_tasks.pop(message_id, None)

    @discord.app_commands.command(
        name="force_winner",
        description="Force specific users to win a giveaway"
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def force_winner(
        self,
        interaction: discord.Interaction,
        message_id: str,
        users: str,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if (
                not giveaway_cog
                or not hasattr(giveaway_cog, "db")
                or not giveaway_cog.db.connected
            ):
                return await interaction.followup.send(
                    "Giveaway system not available.", ephemeral=True
                )

            import re
            mention_ids = re.findall(r"<@!?(\d+)>", users)
            plain_ids = [
                uid.strip()
                for uid in re.sub(r"<@!?(\d+)>", "", users).split(",")
                if uid.strip().isdigit()
            ]
            user_id_list = list({*mention_ids, *plain_ids})

            if not user_id_list:
                return await interaction.followup.send(
                    "Please mention users or provide valid IDs.", ephemeral=True
                )

            gw = await giveaway_cog.db.fetchone(
                "SELECT * FROM giveaways WHERE message_id = ? AND status = ?",
                (message_id, "active"),
            )
            if not gw:
                return await interaction.followup.send(
                    "Not an active giveaway.", ephemeral=True
                )

            channel = self.bot.get_channel(gw["channel_id"])
            try:
                message = await channel.fetch_message(int(message_id))
            except:
                return await interaction.followup.send(
                    "Couldn't fetch giveaway message.", ephemeral=True
                )

            # Verify existence
            for uid in user_id_list:
                try:
                    await self.bot.fetch_user(int(uid))
                except discord.NotFound:
                    return await interaction.followup.send(
                        f"User ID not found: {uid}", ephemeral=True
                    )

            # Persist forced winners
            await giveaway_cog.db.execute(
                "UPDATE giveaways SET forced_winner_ids = ? WHERE message_id = ?",
                (json.dumps(user_id_list), message_id),
            )

            # Add each forced winner as a participant if they don't already exist
            for uid in user_id_list:
                # Check if this user already exists in the participants table
                existing = await giveaway_cog.db.fetchone(
                    "SELECT * FROM participants WHERE message_id = ? AND user_id = ?",
                    (message_id, uid)
                )
                
                # Only insert if they don't already exist
                if not existing:
                    await giveaway_cog.db.execute(
                        """
                        INSERT INTO participants
                        (message_id, user_id, joined_at, is_forced, is_fake, original_user_id)
                        VALUES (?, ?, ?, 1, 0, NULL)
                        """,
                        (message_id, uid, get_current_utc_timestamp()),
                    )
                else:
                    # Update existing entry to mark as forced
                    await giveaway_cog.db.execute(
                        """
                        UPDATE participants
                        SET is_forced = 1
                        WHERE message_id = ? AND user_id = ?
                        """,
                        (message_id, uid)
                    )

            mentions = ", ".join(f"<@{uid}>" for uid in user_id_list)
            await interaction.followup.send(
                f"Forced winners set: {mentions}", ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"force_winner error: {e}")
            await interaction.followup.send(
                f"Error setting forced winners: {e}", ephemeral=True
            )

    @discord.app_commands.command(
        name="cancel_giveaway",
        description="Cancel an active giveaway"
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def cancel_giveaway(
        self,
        interaction: discord.Interaction,
        message_id: str,
        reason: Optional[str] = "Cancelled by administrator"
    ):
        """Cancel an active giveaway."""
        await interaction.response.defer(ephemeral=True)
        try:
            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if (
                not giveaway_cog
                or not hasattr(giveaway_cog, "db")
                or not giveaway_cog.db.connected
            ):
                return await interaction.followup.send(
                    "Giveaway system not available.", ephemeral=True
                )

            gw = await giveaway_cog.db.fetchone(
                "SELECT * FROM giveaways WHERE message_id = ? AND status = ?",
                (message_id, "active"),
            )
            if not gw:
                return await interaction.followup.send(
                    "Not an active giveaway.", ephemeral=True
                )

            channel = self.bot.get_channel(gw["channel_id"])
            if not channel:
                return await interaction.followup.send(
                    "Could not find giveaway channel.", ephemeral=True
                )
            
            try:
                message = await channel.fetch_message(int(message_id))
            except Exception as e:
                return await interaction.followup.send(
                    f"Couldn't fetch giveaway message: {e}", ephemeral=True
                )

            # Cancel any active fake reaction task
            if message_id in self.active_fake_reaction_tasks:
                self.active_fake_reaction_tasks[message_id].cancel()
                await giveaway_cog.db.execute(
                    "UPDATE fake_reactions SET status = ?, cancelled_at = ? WHERE message_id = ?",
                    ("cancelled", get_current_utc_timestamp(), message_id),
                )

            # Update giveaway status
            now_ts = get_current_utc_timestamp()
            await giveaway_cog.db.execute(
                "UPDATE giveaways SET status = ?, cancelled_at = ?, cancelled_by = ?, error = ? WHERE message_id = ?",
                ("cancelled", now_ts, interaction.user.id, reason, message_id),
            )

            # Update embed
            icon = channel.guild.icon.url if channel.guild and channel.guild.icon else None
            embed = discord.Embed(
                description=(
                    f"{RED_DOT_EMOJI} **CANCELLED**\n"
                    f"{DOT_EMOJI} Reason: {reason}\n"
                    f"{DOT_EMOJI} Cancelled by: {interaction.user.mention}\n"
                    f"{DOT_EMOJI} Hosted by: <@{gw['host_id']}>"
                ),
                color=0xFF0000,  # Red color for cancelled
                timestamp=datetime.fromtimestamp(now_ts, timezone.utc)
            )
            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
            embed.set_author(name=f"[CANCELLED] {prize_name}", icon_url=icon)

            await message.clear_reactions()
            await message.edit(embed=embed, view=None)
            
            await interaction.followup.send(
                f"✅ Giveaway cancelled successfully.\nReason: {reason}",
                ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"cancel_giveaway error: {e}")
            await interaction.followup.send(
                f"Error cancelling giveaway: {e}", ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(GiveawayAdminCog(bot))
