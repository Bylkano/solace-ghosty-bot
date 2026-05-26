import discord
from discord.ext import commands, tasks
import sqlite3
import random
import asyncio
import time

# --- ADVANCED DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('heist_luxury.db')
    cursor = conn.cursor()
    # Users, Shields, and Stock holdings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            team_name TEXT,
            coins INTEGER DEFAULT 2000,
            stock_shares INTEGER DEFAULT 0,
            shield_until REAL DEFAULT 0,
            is_frozen INTEGER DEFAULT 0
        )
    ''')
    # Dynamic Market Pricing Tracking
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS market (
            id INTEGER PRIMARY KEY,
            stock_price INTEGER DEFAULT 100,
            pool_total INTEGER DEFAULT 50000
        )
    ''')
    # Initialize base stock market if empty
    cursor.execute("SELECT COUNT(*) FROM market")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO market (id, stock_price, pool_total) VALUES (1, 100, 50000)")
    
    conn.commit()
    conn.close()

init_db()

class MegaHeistBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.blind_auction = None
        self.active_heists = {} # Tracks active lobby configurations
        self.market_fluctuation.start() # Start background stock ticker

    def cog_unload(self):
        self.market_fluctuation.cancel()

    def get_db(self):
        return sqlite3.connect('heist_luxury.db')

    def get_player(self, user_id):
        conn = self.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT team_name, coins, stock_shares, shield_until, is_frozen FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    # --- AUTOMATED BACKGROUND TRADING SYSTEM ---
    @tasks.loop(minutes=10)
    async def market_fluctuation(self):
        """Simulates an unstable black market economy to create server hype."""
        conn = self.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stock_price FROM market WHERE id = 1")
        current_price = cursor.fetchone()[0]
        
        # Calculate market shift (-25% to +35%)
        change_pct = random.uniform(-0.25, 0.35)
        new_price = max(10, int(current_price * (1 + change_pct)))
        
        cursor.execute("UPDATE market SET stock_price = ? WHERE id = 1", (new_price,))
        conn.commit()
        conn.close()
        # Note: You can optionally hook up a channel broadcast ping here!

    # --- ADVANCED FEATURE 1: VOLATILE INVESTMENT MARKET ---
    @commands.command(name="market")
    async def view_market(self, ctx):
        """Check the current price of volatile Black Market Stocks ($HEIST)."""
        conn = self.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stock_price FROM market WHERE id = 1")
        price = cursor.fetchone()[0]
        conn.close()

        embed = discord.Embed(title="📈 SOLACE BLACK MARKET STOCK EXCHANGE 📉", color=0x00ffcc)
        embed.add_field(name="Current $HEIST Token Value", value=f"🪙 `{price}` Solace Coins per share", inline=False)
        embed.set_description("💡 *Prices change every 10 minutes. Buy low, dump before the crash!*")
        embed.set_thumbnail(url="https://i.imgur.com/wH9wN6t.png") # Optional visual polish
        await ctx.send(embed=embed)

    @commands.command(name="buy_stock")
    async def buy_stock(self, ctx, shares: int):
        """Invest your coins into volatile $HEIST shares."""
        if shares <= 0: return
        player = self.get_player(ctx.author.id)
        if not player: return

        conn = self.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stock_price FROM market WHERE id = 1")
        price = cursor.fetchone()[0]
        total_cost = price * shares

        if player[1] < total_cost:
            await ctx.send(f"❌ Transaction Denied: You need `{total_cost}` coins, but only have `{player[1]}`.")
            conn.close()
            return

        cursor.execute("UPDATE users SET coins = coins - ?, stock_shares = stock_shares + ? WHERE user_id = ?",
                       (total_cost, shares, ctx.author.id))
        conn.commit()
        conn.close()
        await ctx.send(f"✅ Success! Bought `{shares}` shares of **$HEIST** for `{total_cost}` coins.")

    # --- ADVANCED FEATURE 2: COOPERATIVE SQUAD HEISTS (WITH MOLES) ---
    @commands.command(name="start_crew_heist")
    async def start_crew_heist(self, ctx):
        """Assembles a co-op lobby for a bank heist. Teammates can join or play the Traitor!"""
        player = self.get_player(ctx.author.id)
        if not player: return
        team = player[0]

        if ctx.channel.id in self.active_heists:
            await ctx.send("An operations planning session is already active in this channel.")
            return

        self.active_heists[ctx.channel.id] = {
            "leader": ctx.author.id,
            "team": team,
            "crew": [ctx.author.id],
            "moles": []
        }

        embed = discord.Embed(title=f"🚨 TEAM {team.upper()} IS PLANNING A CASINO HEIST 🚨", 
                              description="Click the reactions or use commands to choose your stance.\n"
                                          "• Use `!join_crew` to support honestly.\n"
                                          "• Check your DMs or use `!become_mole` secretly to infiltrate!", color=0xff3333)
        await ctx.send(embed=embed)

    @commands.command(name="become_mole")
    async def become_mole(self, ctx):
        """[DM ONLY] Secretly register as a traitor to rob your own crew from the inside."""
        # Find lobby matching user's team
        player = self.get_player(ctx.author.id)
        if not player: return
        
        found = False
        for channel_id, lobby in self.active_heists.items():
            if lobby["team"] == player[0]:
                if ctx.author.id not in lobby["crew"]:
                    lobby["crew"].append(ctx.author.id)
                if ctx.author.id not in lobby["moles"]:
                    lobby["moles"].append(ctx.author.id)
                found = True
                break
        
        if found:
            await ctx.author.send("🤫 **Objective Confirmed:** You are now an active **Mole**. If this heist succeeds, you will steal a vast portion of your own team's spoils.")
        else:
            await ctx.author.send("❌ No active lobby found for your crew right now.")

    @commands.command(name="join_crew")
    async def join_crew(self, ctx):
        """Publicly join the ongoing heist crew assembly line."""
        player = self.get_player(ctx.author.id)
        lobby = self.active_heists.get(ctx.channel.id)
        if not lobby or player[0] != lobby["team"]:
            await ctx.send("No crew assembly available for your team in this room.")
            return
        
        if ctx.author.id not in lobby["crew"]:
            lobby["crew"].append(ctx.author.id)
            await ctx.send(f"🏃 **{ctx.author.display_name}** grabbed their mask and joined the tactical crew!")

    @commands.command(name="execute_crew_heist")
    async def execute_crew_heist(self, ctx):
        """Launches the heist event loop and parses structural success vs. betrayals."""
        lobby = self.active_heists.get(ctx.channel.id)
        if not lobby or lobby["leader"] != ctx.author.id:
            await ctx.send("Only the mastermind team leader can give the green light.")
            return

        crew_size = len(lobby["crew"])
        await ctx.send(f"⚡ **BREACHING THE VAULT!** A crew of `{crew_size}` specialists are bypassing lasers... processing calculations...")
        await asyncio.sleep(4)

        success = random.random() < (0.4 + (crew_size * 0.08)) # More people increases odds
        
        conn = self.get_db()
        cursor = conn.cursor()

        if success:
            base_payout = random.randint(3000, 7000)
            moles = lobby["moles"]
            
            if moles:
                # Inside Job Scenario triggered
                mole_id = random.choice(moles)
                betrayer = await self.bot.fetch_user(mole_id)
                mole_cut = int(base_payout * 0.50)
                crew_cut = int((base_payout - mole_cut) / crew_size)
                
                # Distribute remainder to crew
                for cid in lobby["crew"]:
                    cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (crew_cut, cid))
                # Feed the mole extra
                cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (mole_cut, mole_id))
                
                await ctx.send(f"⚠️ **HEIST COMPLETE... BUT CONTAINED AN INSIDE JOB!** ⚠️\n"
                               f"The vault was opened for `{base_payout}` total coins, but an **unidentified Mole** "
                               f"siphoned off half the haul (`{mole_cut}` coins) into a ghost vault! The remaining team split `{crew_cut}` each.")
            else:
                # Clean heist payout split evenly
                split_pay = int(base_payout / crew_size)
                for cid in lobby["crew"]:
                    cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (split_pay, cid))
                await ctx.send(f"💎 **PERFECT HEIST EXECUTION!** No trace elements left. The crew split a payload of `{base_payout}` perfectly (`{split_pay}` coins each)!")
        else:
            # Complete operational crash down
            loss = 800
            for cid in lobby["crew"]:
                cursor.execute("UPDATE users SET coins = MAX(0, coins - ?) WHERE user_id = ?", (loss, cid))
            await ctx.send(f"💥 **OPERATION BLOWN!** SWAT arrived early. Every crew member caught dropped a fine of `{loss}` coins to buy bail!")

        conn.commit()
        conn.close()
        del self.active_heists[ctx.channel.id]

    # --- ADVANCED FEATURE 3: BLIND SECRET AUCTIONS ---
    @commands.command(name="start_blind_auction")
    @commands.has_permissions(administrator=True)
    async def start_blind_auction(self, ctx, *, item: str):
        """[ADMIN] Starts an item drop where users DM bids secretly without seeing rivals."""
        self.blind_auction = {
            "item": item,
            "bids": {} # user_id: amount
        }
        embed = discord.Embed(title="🕵️ BLACK MARKET BLIND AUCTION 🕵️", 
                              description=f"An exclusive shipment containing **'{item}'** has hit the harbor!\n"
                                          f"Players must submit bids privately. Use `!secret_bid [amount]` directly in my DMs!", color=0x4b0082)
        await ctx.send(embed=embed)

    @commands.command(name="secret_bid")
    async def secret_bid(self, ctx, amount: int):
        """[DM ONLY] Submit a hidden bid for the ongoing active blind auction."""
        if not self.blind_auction:
            await ctx.author.send("No live blind auctions are available currently.")
            return

        player = self.get_player(ctx.author.id)
        if not player or player[1] < amount:
            await ctx.author.send("❌ Access Denied: You do not possess enough cold cash to secure that bid position.")
            return

        self.blind_auction["bids"][ctx.author.id] = amount
        await ctx.author.send(f"🔒 Bid logged cleanly. Submitted `{amount}` coins for the confidential auction asset.")

    @commands.command(name="resolve_blind_auction")
    @commands.has_permissions(administrator=True)
    async def resolve_blind_auction(self, ctx):
        """[ADMIN] Closes hidden bidding and processes tracking matrix."""
        if not self.blind_auction: return
        
        bids = self.blind_auction["bids"]
        if not bids:
            await ctx.send("The secret drop expired with zero interest profiles generated.")
            self.blind_auction = None
            return

        # Locate maximum bid configuration
        winner_id = max(bids, key=bids.get)
        winning_bid = bids[winner_id]
        winner_user = await self.bot.fetch_user(winner_id)

        conn = self.get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (winning_bid, winner_id))
        conn.commit()
        conn.close()

        await ctx.send(f"🏆 **AUCTION DECLASSIFIED:** The high-tier shipment for **{self.blind_auction['item']}** "
                       f"goes to **{winner_user.mention}**! They secured it with a massive blind bid of **`{winning_bid}`** coins!")
        self.blind_auction = None

    # --- ADVANCED FEATURE 4: THE CHAOS WHEEL ---
    @commands.command(name="chaos_wheel")
    @commands.cooldown(1, 600, commands.BucketType.guild) # Shared 10 min server-wide cooldown
    async def chaos_wheel(self, ctx):
        """Spin the wheel of structural fate. Messes up balances or drops unexpected gold."""
        player = self.get_player(ctx.author.id)
        if not player: return

        events = [
            ("IRS Audit!", "Every player loses 10% of their fluid cash reserves instantly.", "audit"),
            ("Cartel Cargo Drop!", "The person who pulled the lever receives a care package of 2,500 coins!", "gift"),
            ("Anarchist Hack!", "The top team on the leaderboard drops 4,000 coins directly into the global pool!", "hack"),
            ("Stimulus Package!", "All registered players receive a flat injection of 500 coins!", "stimulus")
        ]
        
        title, text, action = random.choice(events)
        embed = discord.Embed(title=f"🎡 THE WHEEL OF CHAOS: {title} 🎡", description=text, color=0xffa500)
        await ctx.send(embed=embed)

        conn = self.get_db()
        cursor = conn.cursor()

        if action == "audit":
            cursor.execute("UPDATE users SET coins = int(coins * 0.90)")
        elif action == "gift":
            cursor.execute("UPDATE users SET coins = coins + 2500 WHERE user_id = ?", (ctx.author.id,))
        elif action == "hack":
            # Find current top team
            cursor.execute("SELECT team_name FROM users GROUP BY team_name ORDER BY SUM(coins) DESC LIMIT 1")
            top_team = cursor.fetchone()
            if top_team:
                cursor.execute("UPDATE users SET coins = MAX(0, coins - 1000) WHERE team_name = ?", (top_team[0],))
        elif action == "stimulus":
            cursor.execute("UPDATE users SET coins = coins + 500")

        conn.commit()
        conn.close()

async def setup(bot):
    await bot.add_cog(MegaHeistBot(bot))
      
