import discord
from discord.ext import commands, tasks
from discord import app_commands

import json
import os
import random
from datetime import datetime


HEIST_FILE = "heist_teams.json"
ECONOMY_FILE = "economy.json"
SHOP_FILE = "blackmarket.json"


# =========================
# JSON HELPERS
# =========================

def load_json(path, default):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f, indent=4)
        return default

    with open(path, "r") as f:
        try:
            return json.load(f)
        except:
            return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


# =========================
# DEFAULT SHOP
# =========================

def setup_shop():
    if not os.path.exists(SHOP_FILE):
        default_shop = {
            "shield": {
                "price": 5000,
                "description": "Blocks the next heist attack"
            },
            "spy": {
                "price": 7000,
                "description": "Reveal another team's balance"
            },
            "double_heist": {
                "price": 10000,
                "description": "Next heist gives double rewards"
            },
            "insurance": {
                "price": 4000,
                "description": "Reduces failed heist penalty"
            },
            "freeze": {
                "price": 8000,
                "description": "Freeze another team's heists"
            }
        }

        save_json(SHOP_FILE, default_shop)


# =========================
# COG
# =========================

class Heist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        setup_shop()

        self.random_events.start()

    # =========================
    # ECONOMY HELPERS
    # =========================

    def get_balance(self, user_id):
        economy = load_json(ECONOMY_FILE, {})
        user_id = str(user_id)

        if user_id not in economy:
            economy[user_id] = {
                "wallet": 0
            }
            save_json(ECONOMY_FILE, economy)

        return economy[user_id]["wallet"]

    def add_balance(self, user_id, amount):
        economy = load_json(ECONOMY_FILE, {})
        user_id = str(user_id)

        if user_id not in economy:
            economy[user_id] = {
                "wallet": 0
            }

        economy[user_id]["wallet"] += amount

        if economy[user_id]["wallet"] < 0:
            economy[user_id]["wallet"] = 0

        save_json(ECONOMY_FILE, economy)

    # =========================
    # TEAM HELPERS
    # =========================

    def get_team_total(self, team_name):
        teams = load_json(HEIST_FILE, {})

        if team_name not in teams:
            return 0

        total = 0

        for member_id in teams[team_name]["members"]:
            total += self.get_balance(member_id)

        return total

    def get_user_team(self, user_id):
        teams = load_json(HEIST_FILE, {})

        for team_name, data in teams.items():
            if user_id in data["members"]:
                return team_name

        return None

    # =========================
    # CREATE TEAM
    # =========================

    @app_commands.command(name="createteam", description="Create a heist team")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_team(self, interaction: discord.Interaction, name: str):
        teams = load_json(HEIST_FILE, {})

        if name in teams:
            await interaction.response.send_message("❌ Team already exists.", ephemeral=True)
            return

        teams[name] = {
            "members": [],
            "inventory": [],
            "created": str(datetime.now())
        }

        save_json(HEIST_FILE, teams)

        embed = discord.Embed(
            title="✅ Team Created",
            description=f"Team **{name}** was successfully created.",
            color=discord.Color.green()
        )

        await interaction.response.send_message(embed=embed)

    # =========================
    # JOIN TEAM
    # =========================

    @app_commands.command(name="jointeam", description="Join a heist team")
    async def join_team(self, interaction: discord.Interaction, team_name: str):
        teams = load_json(HEIST_FILE, {})

        if team_name not in teams:
            await interaction.response.send_message("❌ Team does not exist.", ephemeral=True)
            return

        current_team = self.get_user_team(interaction.user.id)

        if current_team:
            await interaction.response.send_message(
                f"❌ You're already in **{current_team}**.",
                ephemeral=True
            )
            return

        teams[team_name]["members"].append(interaction.user.id)

        save_json(HEIST_FILE, teams)

        embed = discord.Embed(
            title="🎉 Joined Team",
            description=f"{interaction.user.mention} joined **{team_name}**",
            color=discord.Color.blurple()
        )

        await interaction.response.send_message(embed=embed)

    # =========================
    # LEAVE TEAM
    # =========================

    @app_commands.command(name="leaveteam", description="Leave your current team")
    async def leave_team(self, interaction: discord.Interaction):
        teams = load_json(HEIST_FILE, {})

        current_team = self.get_user_team(interaction.user.id)

        if not current_team:
            await interaction.response.send_message("❌ You're not in a team.", ephemeral=True)
            return

        teams[current_team]["members"].remove(interaction.user.id)

        save_json(HEIST_FILE, teams)

        await interaction.response.send_message(
            f"💔 You left **{current_team}**"
        )

    # =========================
    # TEAMS LIST
    # =========================

    @app_commands.command(name="teams", description="View all teams")
    async def teams(self, interaction: discord.Interaction):
        teams = load_json(HEIST_FILE, {})

        if not teams:
            await interaction.response.send_message("❌ No teams created yet.")
            return

        embed = discord.Embed(
            title="🏴 SOLACE MONEY HEIST",
            color=discord.Color.gold()
        )

        sorted_teams = sorted(
            teams.items(),
            key=lambda x: self.get_team_total(x[0]),
            reverse=True
        )

        for team_name, data in sorted_teams:
            total = self.get_team_total(team_name)

            member_mentions = []

            for member in data["members"]:
                member_mentions.append(f"<@{member}>")

            members = ", ".join(member_mentions) if member_mentions else "No members"

            embed.add_field(
                name=f"💰 {team_name} — {total:,}",
                value=members,
                inline=False
            )

        await interaction.response.send_message(embed=embed)

    # =========================
    # GIVE EVENT COINS
    # =========================

    @app_commands.command(name="eventgive", description="Give all players event coins")
    @app_commands.checks.has_permissions(administrator=True)
    async def event_give(self, interaction: discord.Interaction, amount: int):
        teams = load_json(HEIST_FILE, {})

        total_given = 0

        for team_name, data in teams.items():
            for member_id in data["members"]:
                self.add_balance(member_id, amount)
                total_given += 1

        await interaction.response.send_message(
            f"💸 Gave **{amount:,}** coins to **{total_given}** players."
        )

    # =========================
    # HEIST COMMAND
    # =========================

    @app_commands.command(name="heist", description="Attack another team")
    async def heist(self, interaction: discord.Interaction, target_team: str):
        teams = load_json(HEIST_FILE, {})

        user_team = self.get_user_team(interaction.user.id)

        if not user_team:
            await interaction.response.send_message("❌ You're not in a team.", ephemeral=True)
            return

        if target_team not in teams:
            await interaction.response.send_message("❌ Target team doesn't exist.", ephemeral=True)
            return

        if user_team == target_team:
            await interaction.response.send_message("❌ You cannot rob your own team.", ephemeral=True)
            return

        attacker_total = self.get_team_total(user_team)
        target_total = self.get_team_total(target_team)

        if target_total <= 0:
            await interaction.response.send_message("❌ That team has no coins.")
            return

        success = random.randint(1, 100)

        # SUCCESS
        if success <= 55:
            stolen = int(target_total * random.uniform(0.10, 0.25))

            members = teams[user_team]["members"]

            split = int(stolen / max(len(members), 1))

            for member in members:
                self.add_balance(member, split)

            target_members = teams[target_team]["members"]

            penalty = int(stolen / max(len(target_members), 1))

            for member in target_members:
                self.add_balance(member, -penalty)

            embed = discord.Embed(
                title="🏦 HEIST SUCCESSFUL",
                description=(
                    f"**{user_team}** robbed **{target_team}**\n\n"
                    f"💰 Stolen Amount: **{stolen:,}**"
                ),
                color=discord.Color.green()
            )

        # FAILURE
        else:
            loss = int(attacker_total * random.uniform(0.05, 0.15))

            members = teams[user_team]["members"]

            split = int(loss / max(len(members), 1))

            for member in members:
                self.add_balance(member, -split)

            embed = discord.Embed(
                title="🚨 HEIST FAILED",
                description=(
                    f"**{user_team}** failed to rob **{target_team}**\n\n"
                    f"💸 Team Loss: **{loss:,}**"
                ),
                color=discord.Color.red()
            )

        await interaction.response.send_message(embed=embed)

    # =========================
    # BETRAYAL
    # =========================

    @app_commands.command(name="betray", description="Betray your team")
    async def betray(self, interaction: discord.Interaction):
        teams = load_json(HEIST_FILE, {})

        current_team = self.get_user_team(interaction.user.id)

        if not current_team:
            await interaction.response.send_message("❌ You're not in a team.", ephemeral=True)
            return

        stolen = random.randint(2000, 10000)

        self.add_balance(interaction.user.id, stolen)

        teams[current_team]["members"].remove(interaction.user.id)

        save_json(HEIST_FILE, teams)

        embed = discord.Embed(
            title="🗡️ BETRAYAL",
            description=(
                "Someone betrayed their team...\n\n"
                f"💰 Stolen Amount: **{stolen:,}**"
            ),
            color=discord.Color.dark_red()
        )

        await interaction.response.send_message(embed=embed)

    # =========================
    # SHOP
    # =========================

    @app_commands.command(name="shop", description="View black market shop")
    async def shop(self, interaction: discord.Interaction):
        shop = load_json(SHOP_FILE, {})

        embed = discord.Embed(
            title="🛒 BLACK MARKET",
            color=discord.Color.purple()
        )

        for item, data in shop.items():
            embed.add_field(
                name=f"{item} — {data['price']:,}",
                value=data['description'],
                inline=False
            )

        await interaction.response.send_message(embed=embed)

    # =========================
    # BUY ITEM
    # =========================

    @app_commands.command(name="buy", description="Buy an item from the black market")
    async def buy(self, interaction: discord.Interaction, item: str):
        shop = load_json(SHOP_FILE, {})
        teams = load_json(HEIST_FILE, {})

        if item not in shop:
            await interaction.response.send_message("❌ Item not found.")
            return

        balance = self.get_balance(interaction.user.id)

        price = shop[item]["price"]

        if balance < price:
            await interaction.response.send_message("❌ Not enough coins.")
            return

        team = self.get_user_team(interaction.user.id)

        if not team:
            await interaction.response.send_message("❌ You're not in a team.")
            return

        self.add_balance(interaction.user.id, -price)

        teams[team]["inventory"].append(item)

        save_json(HEIST_FILE, teams)

        await interaction.response.send_message(
            f"🛍️ Purchased **{item}** for **{price:,}** coins."
        )

    # =========================
    # MYSTERY BOX
    # =========================

    @app_commands.command(name="openbox", description="Open a mystery box")
    async def open_box(self, interaction: discord.Interaction):
        outcomes = [
            ("gain", random.randint(1000, 7000)),
            ("lose", random.randint(1000, 5000)),
            ("jackpot", random.randint(10000, 25000))
        ]

        result = random.choice(outcomes)

        if result[0] == "gain":
            self.add_balance(interaction.user.id, result[1])

            msg = f"🎁 You gained **{result[1]:,}** coins!"

        elif result[0] == "lose":
            self.add_balance(interaction.user.id, -result[1])

            msg = f"💀 You lost **{result[1]:,}** coins!"

        else:
            self.add_balance(interaction.user.id, result[1])

            msg = f"💎 JACKPOT! You won **{result[1]:,}** coins!"

        await interaction.response.send_message(msg)

    # =========================
    # LEADERBOARD
    # =========================

    @app_commands.command(name="heistleaderboard", description="View team leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        teams = load_json(HEIST_FILE, {})

        sorted_teams = sorted(
            teams.items(),
            key=lambda x: self.get_team_total(x[0]),
            reverse=True
        )

        embed = discord.Embed(
            title="🏆 HEIST LEADERBOARD",
            color=discord.Color.gold()
        )

        medals = ["🥇", "🥈", "🥉"]

        for index, (team_name, data) in enumerate(sorted_teams):
            total = self.get_team_total(team_name)

            medal = medals[index] if index < 3 else "🏴"

            embed.add_field(
                name=f"{medal} {team_name}",
                value=f"💰 {total:,} coins",
                inline=False
            )

        await interaction.response.send_message(embed=embed)

    # =========================
    # RANDOM EVENTS
    # =========================

    @tasks.loop(minutes=45)
    async def random_events(self):
        channel_id = None

        # PUT YOUR EVENT CHANNEL ID HERE
        EVENT_CHANNEL_ID = 123456789

        channel_id = EVENT_CHANNEL_ID

        channel = self.bot.get_channel(channel_id)

        if not channel:
            return

        events = [
            "🚚 A bank truck has appeared somewhere in the city.",
            "💣 Someone sabotaged another team.",
            "🧊 A random team has been frozen for 10 minutes.",
            "🎰 Gambling rewards are doubled for 15 minutes.",
            "🏦 A secret vault has been discovered.",
            "🚨 Police raids are increasing across the city."
        ]

        event_message = random.choice(events)

        embed = discord.Embed(
            title="🌍 RANDOM EVENT",
            description=event_message,
            color=discord.Color.orange()
        )

        await channel.send(embed=embed)

    # =========================
    # END EVENT
    # =========================

    @app_commands.command(name="endheist", description="End the event")
    @app_commands.checks.has_permissions(administrator=True)
    async def end_heist(self, interaction: discord.Interaction):
        teams = load_json(HEIST_FILE, {})

        if not teams:
            await interaction.response.send_message("❌ No teams found.")
            return

        winner = max(
            teams.items(),
            key=lambda x: self.get_team_total(x[0])
        )

        winner_name = winner[0]
        winner_total = self.get_team_total(winner_name)

        embed = discord.Embed(
            title="🏆 EVENT ENDED",
            description=(
                f"🥇 Winning Team: **{winner_name}**\n"
                f"💰 Final Total: **{winner_total:,}**"
            ),
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed)


# =========================
# SETUP
# =========================

async def setup(bot):
    await bot.add_cog(Heist(bot))
