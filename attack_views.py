import asyncio
import logging
import time

import discord

from attack_system import ATTACK_DURATION, ATTACK_POLL_SECONDS, ATTACK_REQUIRED_CHESTS, AttackRejected, AttackSystem
from player_saves import SaveError
from image_proxy import proxy_image_url


log = logging.getLogger("loot_bot")
ATTACK_RETRY_SECONDS = 30
ATTACK_FOOTER = "Made by __godly__"
ATTACK_ACTIVE_IMAGE = "https://i.imgur.com/mptiULK.png"
ATTACK_RESULT_IMAGES = {
    0: "https://i.imgur.com/TYRvJ2i.png",
    1: "https://i.imgur.com/eHgaQq3.png",
    2: "https://i.imgur.com/30CqQ2f.png",
    3: "https://i.imgur.com/16gQxiH.png",
}


def attack_thumbnail_url(state):
    if state["status"] == "active":
        return proxy_image_url(ATTACK_ACTIVE_IMAGE)
    if state["status"] == "complete":
        return proxy_image_url(ATTACK_RESULT_IMAGES.get(state["stars"]))
    return None


def attack_embed(state):
    status = state["status"]
    if status == "active":
        title = "Attack in progress"
        description = (
            f"Open **{ATTACK_REQUIRED_CHESTS} Chests** within **{ATTACK_DURATION} seconds**.\n"
            f"Progress: **{state['chests']}/{ATTACK_REQUIRED_CHESTS}** Chests\n"
            f"Ends <t:{state['end']}:R>. Use `/attack` to check your progress."
        )
        color = discord.Color.blue()
    elif status == "cancelled":
        title = "Attack cancelled"
        description = f"You opened **{state['chests']}/{ATTACK_REQUIRED_CHESTS}** required Chests before the deadline. No reward was earned. You can use `/attack` again."
        color = discord.Color.orange()
    else:
        title = "Attack complete"
        description = (
            f"**{state['stars']} stars · {state['percentage']}% destruction**\n"
            f"Town Hall: **{'Destroyed' if state['destroyed'] else 'Survived'}**\n"
            "Choose **one** reward below. Storage limits apply."
        )
        color = discord.Color.green()
    embed = discord.Embed(title=title, description=description, color=color)
    embed.add_field(name="Attack type", value=state["type"])
    if status == "complete":
        chance, expected = AttackSystem.expectations(state["offense"] - state["difficulty"])
        chance = state.get("chance", chance)
        expected = state.get("expected_percentage", expected)
        embed.add_field(name="Offense score", value=f"{state['offense']:.3f} / 1")
        embed.add_field(name="TH destruction chance", value=f"{chance:.1%}")
        embed.add_field(name="Town Hall", value=str(state["town_hall"]))
        embed.add_field(name="Defense score", value=f"{state['difficulty']:.3f} / 1")
        embed.add_field(name="Expected percentage", value=f"{expected:.1f}%")
        embed.add_field(name="Reward choices", value="\n".join(f"**{name}:** {amount:,}" for name, amount in state["rewards"].items()), inline=False)
    else:
        embed.add_field(name="Town Hall", value=str(state["town_hall"]))
        embed.add_field(name="Difficulty", value=f"{state['difficulty']:.3f} / 1")
    thumbnail = attack_thumbnail_url(state)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    embed.set_footer(text=ATTACK_FOOTER)
    return embed


class AttackRewardsView(discord.ui.View):
    def __init__(self, controller, state):
        super().__init__(timeout=None)
        self.controller = controller
        self.user_id = state["user_id"]
        self.end = state["end"]
        for index, (resource, amount) in enumerate(state["rewards"].items()):
            button = discord.ui.Button(
                label=f"{resource}: {amount:,}", style=discord.ButtonStyle.primary,
                custom_id=f"attack:{self.user_id}:{self.end}:{index}", row=index // 3,
            )

            async def callback(interaction, chosen=resource):
                await self.claim(interaction, chosen)

            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This attack belongs to another player.", ephemeral=True)
            return False
        if self.controller.is_migrating():
            await interaction.response.send_message("Player saves are being updated. Please try again shortly.", ephemeral=True)
            return False
        return True

    async def claim(self, interaction, resource):
        await interaction.response.defer()
        try:
            state, receipt, village, caps, treasury_caps = await asyncio.to_thread(self.controller.system.claim, self.user_id, self.end, resource)
        except (AttackRejected, SaveError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("Could not claim attack reward for %s", self.user_id)
            await interaction.followup.send("Your reward could not be claimed. Please try again.", ephemeral=True)
            return
        self.controller.release(self.user_id)
        self.stop()
        self.clear_items()
        embed = attack_embed(state)
        embed.title = "Attack reward collected"
        embed.description = f"**{state['stars']} stars · {state['percentage']}% destruction**\nSelected **{state['rewards'][resource]:,} {resource}**."
        for index in range(len(embed.fields) - 1, -1, -1):
            if embed.fields[index].name == "Reward choices":
                embed.remove_field(index)
        embed.add_field(name=resource, value=f"Added **{receipt.main[resource]:,}**\nStorage: **{village[resource]:,}/{caps[resource]:,}**", inline=False)
        if receipt.treasury[resource]:
            embed.add_field(name=f"Treasury {resource}", value=f"Added **{receipt.treasury[resource]:,}**\nTreasury: **{village['Treasury ' + resource]:,}/{treasury_caps[resource]:,}**", inline=False)
        await interaction.edit_original_response(embed=embed, view=None)


class AttackController:
    def __init__(self, bot, system, is_migrating):
        self.bot = bot
        self.system = system
        self.is_migrating = is_migrating
        self.views = {}
        self.retry_at = {}
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def release(self, user_id):
        view = self.views.pop(user_id, None)
        if view is not None:
            view.stop()
        self.retry_at.pop(user_id, None)

    def view(self, state):
        self.release(state["user_id"])
        result = AttackRewardsView(self, state)
        self.views[state["user_id"]] = result
        return result

    async def open(self, interaction):
        await interaction.response.defer(thinking=True)
        try:
            state = await asyncio.to_thread(self.system.start, interaction.user.id)
            view = self.view(state) if state["status"] == "complete" else None
            message = await interaction.edit_original_response(embed=attack_embed(state), view=view)
            if state["status"] != "cancelled":
                await asyncio.to_thread(self.system.attach_message, interaction.user.id, state["end"], interaction.channel_id, message.id)
        except (AttackRejected, SaveError) as error:
            await interaction.edit_original_response(content=str(error), embed=None, view=None)
        except Exception:
            log.exception("Could not start attack for %s", interaction.user.id)
            await interaction.edit_original_response(content="The attack could not be displayed. Use /attack to resume it.", embed=None, view=None)

    async def run_once(self):
        if self.is_migrating() or not self.system.ready:
            return
        users = await asyncio.to_thread(self.system.pending_users)
        for user_id in set(self.views) - set(users):
            self.release(user_id)
        for user_id in set(self.retry_at) - set(users):
            self.retry_at.pop(user_id, None)
        for user_id in users:
            if self.is_migrating():
                return
            if time.time() < self.retry_at.get(user_id, 0):
                continue
            state = None
            try:
                state = await asyncio.to_thread(self.system.status, user_id)
                if state is None or state["status"] == "active":
                    continue
                if state.get("notified"):
                    if state["status"] == "complete" and user_id not in self.views and state.get("message_id"):
                        self.bot.add_view(self.view(state), message_id=state["message_id"])
                    continue
                if not state.get("channel_id") or not state.get("message_id"):
                    continue
                channel = self.bot.get_channel(state["channel_id"]) or await self.bot.fetch_channel(state["channel_id"])
                view = self.view(state) if state["status"] == "complete" else None
                await channel.get_partial_message(state["message_id"]).edit(embed=attack_embed(state), view=view)
                await asyncio.to_thread(self.system.mark_notified, user_id, state["end"])
                self.retry_at.pop(user_id, None)
            except discord.HTTPException as error:
                if error.status in (403, 404) and state is not None:
                    detached = await asyncio.to_thread(
                        self.system.detach_message, user_id, state["end"],
                        state.get("channel_id"), state.get("message_id"),
                    )
                    if detached:
                        self.release(user_id)
                    log.warning("Attack result for %s could not be shown in channel %s because the channel or message is inaccessible. Use /attack in an accessible channel to retrieve the result.", user_id, state.get("channel_id"))
                else:
                    self.retry_at[user_id] = time.time() + ATTACK_RETRY_SECONDS
                    log.warning("Could not display attack result for %s: %s", user_id, error)
            except Exception:
                self.retry_at[user_id] = time.time() + ATTACK_RETRY_SECONDS
                log.exception("Could not finish attack for %s", user_id)

    async def run(self):
        try:
            while not self.bot.is_closed():
                try:
                    await self.run_once()
                except Exception:
                    log.exception("Attack check failed")
                await asyncio.sleep(ATTACK_POLL_SECONDS)
        finally:
            for user_id in list(self.views):
                self.release(user_id)
