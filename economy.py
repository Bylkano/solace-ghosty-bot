import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import random
from datetime import datetime, timedelta

ECONOMY_FILE = "economy.json"

class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.load_data()

    def load_data(self):
        if os.path.exists(ECONOMY_FILE):
            try:
                with open(ECONOMY_FILE, "r") as f:
                    self.data = json.load(f)
            except:
                self.data = {"currency": "🪙", "balances": {}, "daily_cooldowns": {}}
        else:
            self.data = {"currency": "🪙", "balances": {}, "daily_cooldowns": {}}
        
        if "currency" not in self.data:
            self.data["currency"] = "🪙"
        if "balances" not in self.data:
            self.data["balances"] = {}
        if "daily_cooldowns" not in self.data:
            self.data["daily_cooldowns"] = {}

    def save_data(self):
        with open(ECONOMY_FILE, "w") as f:
            json.dump(self.data, f, indent=4)

    def get_balance(self, user_id: str) -> int:
        return self.data["balances"].get(user_id, 0)

    def update_balance(self, user_id: str, amount: int):
        current = self.get_balance(user_id)
        self.data["balances"][user_id] = max(0, current + amount)
        self.save_data()

    @app_commands.command(name="setcurrency", description="Set the server's custom currency icon or server emoji.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_currency(self, interaction: discord.Interaction, emoji: str):
        self.data["currency"] = emoji
        self.save_data()
        await interaction.response.send_message(f"✅ The server currency icon has been updated to: {emoji}", ephemeral=True)

    @app_commands.command(name="give", description="Mint money and award it to a specific server member.")
    @app_commands.checks.has_permissions(administrator=True)
    async def give_money(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be a positive number!", ephemeral=True)
            return
        self.update_balance(str(member.id), amount)
        currency = self.data["currency"]
        await interaction.response.send_message(f"💰 Staff has added **{amount}** {currency} to {member.mention}'s wallet!")

    @app_commands.command(name="take", description="Deduct money from a specific server member.")
    @app_commands.checks.has_permissions(administrator=True)
    async def take_money(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be a positive number!", ephemeral=True)
            return
        self.update_balance(str(member.id), -amount)
        currency = self.data["currency"]
        await interaction.response.send_message(f"📉 Staff has removed **{amount}** {currency} from {member.mention}'s wallet.")

    @app_commands.command(name="balance", description="Check your current wallet balance or someone else's.")
    async def check_balance(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        balance = self.get_balance(str(target.id))
        currency = self.data["currency"]
        await interaction.response.send_message(f"👛 {target.mention}'s balance: **{balance}** {currency}")

    @app_commands.command(name="pay", description="Transfer your own money to another server member.")
    async def pay_money(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You can't pay yourself!", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be a positive number!", ephemeral=True)
            return
        
        sender_id = str(interaction.user.id)
        receiver_id = str(member.id)
        
        if self.get_balance(sender_id) < amount:
            await interaction.response.send_message("❌ You don't have enough money in your wallet to complete this transfer!", ephemeral=True)
            return
            
        self.update_balance(sender_id, -amount)
        self.update_balance(receiver_id, amount)
        currency = self.data["currency"]
        await interaction.response.send_message(f"💸 {interaction.user.mention} successfully transferred **{amount}** {currency} to {member.mention}!")

    @app_commands.command(name="daily", description="Claim your free daily cash reward once every 24 hours.")
    async def claim_daily(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        now = datetime.utcnow()
        
        last_claim_str = self.data["daily_cooldowns"].get(user_id)
        if last_claim_str:
            last_claim = datetime.fromisoformat(last_claim_str)
            if now < last_claim + timedelta(hours=24):
                time_left = (last_claim + timedelta(hours=24)) - now
                hours, remainder = divmod(int(time_left.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                await interaction.response.send_message(f"⏳ You need to wait **{hours}h {minutes}m** before claiming your next daily reward!", ephemeral=True)
                return

        reward = random.randint(50, 150)
        self.update_balance(user_id, reward)
        self.data["daily_cooldowns"][user_id] = now.isoformat()
        self.save_data()
        
        currency = self.data["currency"]
        await interaction.response.send_message(f"🎁 {interaction.user.mention} claimed their daily reward and found **{reward}** {currency}!")

    @app_commands.command(name="coinflip", description="Bet your money on a coin flip to double it!")
    async def coin_flip(self, interaction: discord.Interaction, choice: str, bet: int):
        choice = choice.lower().strip()
        if choice not in ["heads", "tails"]:
            await interaction.response.send_message("❌ Choice must be either 'heads' or 'tails'!", ephemeral=True)
            return
        if bet <= 0:
            await interaction.response.send_message("❌ Bet must be a positive number!", ephemeral=True)
            return
            
        user_id = str(interaction.user.id)
        if self.get_balance(user_id) < bet:
            await interaction.response.send_message("❌ You don't have enough money to place that bet!", ephemeral=True)
            return

        result = random.choice(["heads", "tails"])
        currency = self.data["currency"]
        
        if choice == result:
            self.update_balance(user_id, bet)
            await interaction.response.send_message(f"🪙 The coin lands on **{result.upper()}**! {interaction.user.mention} won and doubled their bet, gaining **{bet}** {currency}! 🎉")
        else:
            self.update_balance(user_id, -bet)
            await interaction.response.send_message(f"🪙 The coin lands on **{result.upper()}**... {interaction.user.mention} lost their bet of **{bet}** {currency}. Better luck next time!")

async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
  
