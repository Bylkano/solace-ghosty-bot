"""
cogs/server_drops_economy.py
-----------------------------
Solace Event drop system — expanded edition.

  /setdrops    →  configure the economy drops channel
  /getdrops    →  view configured drops channel
  /points      →  check point balance
  /leaderboard →  top 10 players

Drop Types (weighted):
  Trivia           (20%)  – first correct text answer wins
  Scramble         (15%)  – unscramble the word
  Lootbox          (10%)  – click the button first
  Boss Raid        (10%)  – co-op !attack the monster
  Hot or Cold      (15%)  – guess a number 1–100
  Emoji Puzzle     (15%)  – identify what the emojis represent
  Reaction Bomb    (10%)  – click AFTER the signal (not before!)
  Multiplier Drop   (5%)  – double-points or bounty event
  Blackjack Duel    (5%)  – community vs House; split 500-pt pool on win
  Hot Potato       (10%)  – pass it or get exploded (bomb or golden sack)
"""

from __future__ import annotations

import asyncio
import random
import sys
import pathlib
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands
import psycopg2
from psycopg2.extras import RealDictCursor

from store import (
    get_drops_channel, set_drops_channel,
    get_drop_trigger, set_drop_trigger,
    get_ping_role, set_ping_role, clear_ping_role,
    get_all_channels_mode, set_all_channels_mode,
    add_disabled_channel, remove_disabled_channel, get_disabled_channels,
    get_drops_paused, set_drops_paused,
)

# ──────────────────────────── constants ───────────────────────────

_DB_URL = os.environ.get("DATABASE_URL", "")
MSG_TRIGGER  = 10
DROP_TIMEOUT = 30


# Colour palette — dark/sleek accents
C_TRIVIA   = 0x5865F2   # discord blurple
C_SCRAMBLE = 0x9B59B6   # deep violet
C_LOOTBOX  = 0x2ECC71   # emerald
C_BOSS     = 0xE74C3C   # crimson
C_HOT_COLD = 0x3498DB   # sky blue
C_EMOJI    = 0xF39C12   # amber
C_BOMB     = 0xFF6B35   # orange-red
C_MULTI    = 0x1ABC9C   # teal
C_WIN      = 0xF1C40F   # gold
C_POINTS   = 0xE67E22   # burnt amber
C_BOARD    = 0x2C2F33   # near-black
C_TIMEOUT  = 0x747F8D   # muted grey
C_SET      = 0x43B581   # green confirmation
C_BOUNTY   = 0xFF4500   # deep orange
C_BLACKJACK = 0x1A472A  # casino green
C_POTATO   = 0xFFD700   # golden yellow
C_FILL     = 0xE91E8C   # pink for fill in the blank
C_MATH     = 0x00BCD4   # cyan for fast math
C_TF       = 0x8BC34A   # green for true/false

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
SEP    = "▬" * 22

# ── Weighted event pool ────────────────────────────────────────────
EVENT_POOL = (
    ["trivia"]     * 18 +
    ["scramble"]   * 12 +
    ["boss"]       *  8 +
    ["emoji"]      * 12 +
    ["bomb"]       *  8 +
    ["multi"]      *  5 +
    ["blackjack"]  *  5 +
    ["fastmath"]   * 12 +
    ["worldboss"]  *  4 +
    ["heist"]      *  5 +
    ["losers"]     *  5
)

# ── Catch-up / event mechanics ─────────────────────────────────────
C_HAPPY           = 0xF1C40F       # gold accent for Happy Hour
UNDERDOG_LOCK_N   = 3              # Top 3: no bonus, and barred from the Losers Bracket
UNDERDOG_MID_MAX  = 7              # ranks 4-7 get the mid catch-up bonus
UNDERDOG_MID_MULT = 1.5            # multiplier for ranks 4-7
UNDERDOG_LOW_MULT = 1.7            # multiplier for rank 8 and below (or unranked)
UNDERDOG_MULT     = UNDERDOG_MID_MULT   # back-compat alias
UNDERDOG_TOP_N    = UNDERDOG_LOCK_N     # back-compat alias (now the podium-lock size)
RARE_DROPS        = ("boss", "blackjack", "multi", "bomb")  # doubled during Happy Hour
HAPPY_DEFAULT_MIN = 60             # default Mega-Drop Happy Hour length (minutes)
HAPPY_MAX_MIN     = 360            # safety cap on Happy Hour length
WORLDBOSS_HP      = 750            # default World Boss health pool
WORLDBOSS_MIN     = 10             # default World Boss duration (minutes)
WORLDBOSS_POOL    = 5000           # default World Boss point payout pool
WORLDBOSS_NAMES   = (
    "Vortharion the Devourer",
    "The Obsidian Leviathan",
    "Mor'Gath, World-Ender",
    "The Crimson Titan",
    "Nyxhaal the Eclipse",
    "Garrukthar the Unbroken",
)

# ──────────────────────── content banks ───────────────────────────

TRIVIA_BANK: dict[str, list[str]] = {
    # Math
    "What is 5 + 7?":                                        ["12", "twelve"],
    "What is 9 × 9?":                                        ["81", "eighty-one", "eighty one"],
    "What is the square root of 144?":                       ["12", "twelve"],
    "How many sides does a hexagon have?":                   ["6", "six"],
    "What is 15% of 200?":                                   ["30", "thirty"],
    "What is 2 to the power of 8?":                         ["256"],
    "What is 7 × 8?":                                        ["56", "fifty-six", "fifty six"],
    "How many degrees are in a right angle?":                ["90", "ninety"],
    "What is 100 divided by 4?":                             ["25", "twenty-five", "twenty five"],
    "What is the value of pi rounded to 2 decimal places?": ["3.14"],
    "What is the next prime number after 7?":               ["11", "eleven"],
    "What is 12 squared?":                                   ["144"],
    "How many minutes are in 3 hours?":                     ["180"],
    "What is 1000 divided by 8?":                            ["125"],
    "What is 17 + 26?":                                      ["43", "forty-three", "forty three"],

    # Science
    "What gas do plants absorb from the air?":              ["carbon dioxide", "co2"],
    "What is H2O commonly known as?":                       ["water"],
    "How many bones are in the adult human body?":          ["206"],
    "What planet is closest to the Sun?":                   ["mercury"],
    "What is the chemical symbol for gold?":                ["au"],
    "What is the chemical symbol for iron?":                ["fe"],
    "What force keeps us on the ground?":                   ["gravity"],
    "What is the speed of light (rounded) in km/s?":       ["300000", "300,000"],
    "What organ pumps blood through the body?":             ["heart"],
    "What is the powerhouse of the cell?":                  ["mitochondria"],
    "What gas do humans breathe out?":                      ["carbon dioxide", "co2"],
    "How many chromosomes do humans have?":                 ["46"],
    "What is the hardest natural substance on Earth?":      ["diamond"],
    "What planet is known as the Red Planet?":              ["mars"],
    "What is the chemical symbol for water?":               ["h2o"],

    # Geography
    "What is the capital of France?":                       ["paris"],
    "What is the capital of Japan?":                        ["tokyo"],
    "Which continent is Brazil on?":                        ["south america"],
    "What is the longest river in the world?":              ["nile"],
    "What is the smallest country in the world?":          ["vatican city", "vatican"],
    "What is the tallest mountain in the world?":          ["mount everest", "everest"],
    "How many continents are there?":                       ["7", "seven"],
    "What is the capital of Australia?":                    ["canberra"],
    "What ocean is the largest?":                           ["pacific", "pacific ocean"],
    "What country has the most natural lakes?":            ["canada"],
    "What is the capital of Canada?":                       ["ottawa"],
    "Which country is the Eiffel Tower in?":               ["france"],
    "What is the capital of Germany?":                      ["berlin"],
    "What is the capital of Spain?":                        ["madrid"],
    "What river flows through Egypt?":                      ["nile", "the nile"],

    # History
    "In what year did World War II end?":                   ["1945"],
    "Who was the first US President?":                      ["george washington", "washington"],
    "What year did the Titanic sink?":                      ["1912"],
    "Which empire built the Colosseum?":                    ["roman", "roman empire"],
    "In what year did the Berlin Wall fall?":               ["1989"],
    "Who invented the telephone?":                         ["alexander graham bell", "bell"],
    "What year did World War I begin?":                     ["1914"],
    "Who was the first person to walk on the Moon?":       ["neil armstrong", "armstrong"],
    "In what year did the French Revolution begin?":        ["1789"],
    "Who wrote the Declaration of Independence?":           ["thomas jefferson", "jefferson"],

    # Pop Culture / Gaming
    "What game features a character named Master Chief?":  ["halo"],
    "In Minecraft, what material is the strongest?":       ["netherite"],
    "What is the name of Link's nemesis in Zelda?":        ["ganon", "ganondorf"],
    "What color is Pikachu?":                               ["yellow"],
    "What game has the Battle Bus?":                        ["fortnite"],
    "How many players start in a standard chess game?":    ["32"],
    "What console did Nintendo release in 2017?":          ["switch", "nintendo switch"],
    "In Among Us, what are the killers called?":           ["impostors", "imposters", "impostor"],
    "What is the max level in Minecraft?":                 ["2147483647"],
    "What game features 'Among Drip'?":                    ["among us"],
    "What is Roblox's virtual currency called?":           ["robux"],
    "In Valorant, what is the objective bomb called?":     ["spike"],
    "What year was Minecraft first released?":             ["2009"],
    "How many players are in a standard League of Legends team?": ["5", "five"],
    "What is the name of the currency in Animal Crossing?": ["bells"],

    # Discord / Internet
    "What does 'DM' stand for on Discord?":               ["direct message"],
    "What does 'AFK' mean?":                               ["away from keyboard"],
    "What does 'GG' stand for in gaming?":                ["good game"],
    "What does 'LFG' stand for?":                          ["looking for group"],
    "What does 'TBH' stand for?":                          ["to be honest"],
    "What color is Discord's logo?":                        ["purple", "blurple"],
    "What does 'NGL' mean?":                               ["not gonna lie", "not going to lie"],
    "What does 'POV' stand for?":                          ["point of view"],
    "What does 'FOMO' stand for?":                         ["fear of missing out"],
    "What does 'IRL' stand for?":                          ["in real life"],

    # General Knowledge
    "How many planets are in our solar system?":           ["8", "eight"],
    "How many sides does a triangle have?":                ["3", "three"],
    "What color is the sky on a clear day?":               ["blue"],
    "How many days are in a leap year?":                   ["366"],
    "What is the largest mammal in the world?":            ["blue whale"],
    "How many strings does a standard guitar have?":       ["6", "six"],
    "What is the fastest land animal?":                    ["cheetah"],
    "How many colors are in a rainbow?":                   ["7", "seven"],
    "What is the largest ocean on Earth?":                 ["pacific", "pacific ocean"],
    "How many hours are in a day?":                        ["24", "twenty-four"],
    "What language has the most native speakers?":         ["mandarin", "chinese", "mandarin chinese"],
    "How many teeth do adult humans have?":                ["32", "thirty-two"],
    "What is the tallest animal on Earth?":                ["giraffe"],
    "What is the currency of Japan?":                      ["yen"],
    "What is the currency of the UK?":                     ["pound", "pounds", "pound sterling"],
    "How many sides does a pentagon have?":                ["5", "five"],
    "What instrument has 88 keys?":                        ["piano"],
    "What is the largest planet in our solar system?":    ["jupiter"],
    "Who painted the Mona Lisa?":                          ["leonardo da vinci", "da vinci", "leonardo"],
    "What is the national animal of Australia?":          ["kangaroo"],

    # Extra — Movies & TV
    "Who directed the movie Inception?":                  ["christopher nolan", "nolan"],
    "What is the name of Tony Stark's AI assistant?":     ["jarvis", "j.a.r.v.i.s"],
    "In which film does the line 'I am your father' appear?": ["star wars", "empire strikes back", "the empire strikes back"],
    "What streaming service produced Stranger Things?":   ["netflix"],
    "How many episodes are in the first season of Breaking Bad?": ["7", "seven"],
    "What is the highest-grossing movie of all time?":    ["avengers endgame", "endgame"],
    "What movie features the song 'Let It Go'?":          ["frozen"],
    "In Game of Thrones, what is the name of Jon Snow's direwolf?": ["ghost"],

    # Extra — Sports
    "How many players are on a basketball team on the court?": ["5", "five"],
    "How many points is a touchdown worth in American football?": ["6", "six"],
    "What country invented basketball?":                  ["usa", "united states", "america", "united states of america"],
    "How many holes are in a standard round of golf?":   ["18", "eighteen"],
    "What is the diameter of a basketball hoop in inches?": ["18", "eighteen"],
    "In tennis, what is a score of zero called?":         ["love"],
    "How many rings does the Olympic logo have?":         ["5", "five"],
    "What sport is played at Wimbledon?":                 ["tennis"],
    "How many players are on a soccer team on the field?": ["11", "eleven"],

    # Extra — Science & Tech
    "What does CPU stand for?":                           ["central processing unit"],
    "What does RAM stand for?":                           ["random access memory"],
    "What is the most abundant gas in Earth's atmosphere?": ["nitrogen"],
    "What planet has the most moons?":                    ["saturn"],
    "How many bytes are in a kilobyte?":                  ["1024"],
    "What does HTML stand for?":                          ["hypertext markup language"],
    "What language is primarily used for web styling?":   ["css"],
    "Who invented the World Wide Web?":                   ["tim berners-lee", "berners-lee"],
    "What element has the atomic number 1?":              ["hydrogen"],
    "What is the unit of electrical resistance?":         ["ohm", "ohms"],

    # Extra — General
    "How many seconds are in one hour?":                  ["3600"],
    "How many weeks are in a year?":                      ["52"],
    "What is the most spoken language in the world?":     ["mandarin", "chinese", "mandarin chinese"],
    "What shape has 8 sides?":                            ["octagon"],
    "What is the square root of 256?":                    ["16", "sixteen"],
    "How many zeros are in one million?":                 ["6", "six"],
    "What is 17 × 17?":                                   ["289"],
    "What is 25% of 400?":                                ["100"],
    "How many days are in September?":                    ["30", "thirty"],
    "What is the Roman numeral for 100?":                 ["c"],

    # More Science
    "What is the chemical symbol for sodium?": ["na"],
    "What is the chemical symbol for oxygen?": ["o"],
    "What is the chemical symbol for potassium?": ["k"],
    "What is the freezing point of water in Celsius?": ["0", "zero"],
    "What is the largest organ in the human body?": ["skin"],
    "How many legs does a spider have?": ["8", "eight"],
    "What is the closest star to Earth?": ["the sun", "sun"],
    "What is the study of living organisms called?": ["biology"],
    "What is the most abundant element in the universe?": ["hydrogen"],
    "What is the center of an atom called?": ["nucleus"],

    # More Geography
    "What is the capital of Italy?": ["rome"],
    "What is the capital of Russia?": ["moscow"],
    "What is the capital of Egypt?": ["cairo"],
    "What is the capital of South Korea?": ["seoul"],
    "How many states are in the USA?": ["50", "fifty"],
    "What is the capital of China?": ["beijing"],
    "What is the smallest continent?": ["australia", "oceania"],
    "What is the capital of India?": ["new delhi", "delhi"],
    "What is the largest country by land area?": ["russia"],
    "What is the tallest building in the world?": ["burj khalifa"],

    # More History
    "Who was the British PM during most of WWII?": ["winston churchill", "churchill"],
    "Who painted the ceiling of the Sistine Chapel?": ["michelangelo"],
    "In what year did humans first land on the Moon?": ["1969"],
    "What ancient civilization built the Giza pyramids?": ["egyptians", "ancient egyptians", "egypt"],
    "Who was the first man in space?": ["yuri gagarin", "gagarin"],

    # More Gaming
    "What is the best-selling video game of all time?": ["minecraft"],
    "What company makes the PlayStation?": ["sony"],
    "What company makes the Xbox?": ["microsoft"],
    "What is the name of the princess in most Mario games?": ["peach", "princess peach"],
    "What is the currency in Fortnite called?": ["v-bucks", "vbucks", "v bucks"],
    "In Tetris, how many lines at once make a 'Tetris'?": ["4", "four"],
    "What does Mario usually stomp on?": ["goomba", "goombas"],
    "How many players are in a standard Fortnite match?": ["100"],

    # Music
    "How many keys are on a standard piano?": ["88"],
    "What does 'DJ' stand for?": ["disc jockey"],
    "How many musicians are in a quartet?": ["4", "four"],
    "What is the highest female singing voice?": ["soprano"],

    # Food
    "What fruit is used to make wine?": ["grapes", "grape"],
    "What is the main ingredient in guacamole?": ["avocado", "avocados"],
    "What spice is the most expensive by weight?": ["saffron"],
    "What beans are used to make chocolate?": ["cocoa", "cacao", "cocoa beans"],
    "What is the main ingredient in bread?": ["flour"],

    # More Math
    "What is 8 × 12?": ["96"],
    "What is half of 250?": ["125"],
    "What is 13 + 28?": ["41", "forty-one", "forty one"],
    "What is 6 × 6 × 6?": ["216"],
    "What is 9 × 6?": ["54", "fifty-four", "fifty four"],

    # More General
    "How many colors are on a traffic light?": ["3", "three"],
    "How many sides does a stop sign have?": ["8", "eight"],
    "What is the freezing point of water in Fahrenheit?": ["32"],
    "How many millimeters are in a centimeter?": ["10", "ten"],
    "How many legs does an insect have?": ["6", "six"],
    "What is the only planet that rotates on its side?": ["uranus"],
    "What is the largest land animal?": ["elephant", "african elephant"],
    "What is the chemical formula for table salt?": ["nacl"],
    "What is the chemical symbol for silver?": ["ag"],
    "What is the chemical symbol for helium?": ["he"],
    "What is the chemical symbol for carbon?": ["c"],
    "What planet is famous for its rings?": ["saturn"],
    "What is the boiling point of water in Fahrenheit?": ["212"],
    "What type of animal is a Komodo dragon?": ["lizard", "reptile"],
    "What is the largest moon of Jupiter?": ["ganymede"],
    "What blood type is the universal donor?": ["o negative", "o-"],
    "What vitamin does skin produce from sunlight?": ["vitamin d", "d"],
    "What is the name of our galaxy?": ["milky way"],
    "What is the smallest unit of matter?": ["atom"],
    "What process do plants use to make food?": ["photosynthesis"],
    "How many teeth does an adult dog typically have?": ["42"],
    "What is the chemical symbol for lead?": ["pb"],
    "What is the most common blood type?": ["o positive", "o+", "o"],
    "What is the capital of Brazil?": ["brasilia"],
    "What is the capital of Greece?": ["athens"],
    "What is the capital of Portugal?": ["lisbon"],
    "What is the capital of Norway?": ["oslo"],
    "What is the largest hot desert in the world?": ["sahara"],
    "What country is shaped like a boot?": ["italy"],
    "On which continent is the Sahara Desert?": ["africa"],
    "What is the capital of Mexico?": ["mexico city"],
    "What is the longest mountain range in the world?": ["andes"],
    "What sea separates Europe and Africa?": ["mediterranean", "mediterranean sea"],
    "What is the capital of Turkey?": ["ankara"],
    "What is the capital of Argentina?": ["buenos aires"],
    "Who was the first Emperor of Rome?": ["augustus"],
    "In what year did the American Civil War begin?": ["1861"],
    "Who is credited with reaching America in 1492?": ["christopher columbus", "columbus"],
    "What wall divided a German city until 1989?": ["berlin wall", "the berlin wall"],
    "Who was known as the Iron Lady?": ["margaret thatcher", "thatcher"],
    "What ship did the Pilgrims sail to America?": ["mayflower", "the mayflower"],
    "In what year did India gain independence?": ["1947"],
    "Who led India's nonviolent independence movement?": ["gandhi", "mahatma gandhi"],
    "Which Egyptian queen was famously linked to Caesar?": ["cleopatra"],
    "Who played Jack in the film Titanic?": ["leonardo dicaprio", "dicaprio", "leo dicaprio"],
    "What is the name of the wizarding school in Harry Potter?": ["hogwarts"],
    "What fictional metal coats Wolverine's skeleton?": ["adamantium"],
    "Who is the green ogre in DreamWorks films?": ["shrek"],
    "In The Matrix, which pill does Neo take?": ["red", "red pill"],
    "What is the name of Harry Potter's owl?": ["hedwig"],
    "Who directed Jurassic Park?": ["steven spielberg", "spielberg"],
    "What superhero team includes Iron Man and Thor?": ["avengers", "the avengers"],
    "Which band performed Bohemian Rhapsody?": ["queen"],
    "What was Beyonce's former girl group called?": ["destiny's child", "destinys child"],
    "How many strings does a violin have?": ["4", "four"],
    "Who is known as the King of Pop?": ["michael jackson"],
    "What genre is most associated with Bob Marley?": ["reggae"],
    "What country did pizza originate in?": ["italy"],
    "What is the main ingredient in hummus?": ["chickpeas", "chickpea"],
    "What drink is made from fermented grapes?": ["wine"],
    "What nut is used to make marzipan?": ["almond", "almonds"],
    "What is sushi traditionally wrapped in?": ["seaweed", "nori"],
    "What fruit has its seeds on the outside?": ["strawberry", "strawberries"],
    "What is the name of the hero in The Legend of Zelda?": ["link"],
    "What company developed Fortnite?": ["epic games", "epic"],
    "In Mario Kart, what item makes you invincible?": ["star", "super star"],
    "What is the name of Sega's blue hedgehog?": ["sonic"],
    "How many cards are in a standard deck?": ["52"],
    "In Minecraft, what mob explodes near players?": ["creeper"],
    "What does URL stand for?": ["uniform resource locator"],
    "What company created the iPhone?": ["apple"],
    "What does AI stand for?": ["artificial intelligence"],
    "What does USB stand for?": ["universal serial bus"],
    "What does GPU stand for?": ["graphics processing unit"],
    "What programming language shares its name with a snake?": ["python"],
    "What is the largest species of penguin?": ["emperor", "emperor penguin"],
    "What is a baby kangaroo called?": ["joey"],
    "What is the only bird that can fly backwards?": ["hummingbird"],
    "What is the largest big cat?": ["tiger"],
    "How many hearts does an octopus have?": ["3", "three"],
    "Which egg-laying mammal has a duck-like bill?": ["platypus"],
    "What is a group of lions called?": ["pride"],
    "Who wrote Romeo and Juliet?": ["shakespeare", "william shakespeare"],
    "Who wrote the Harry Potter books?": ["j.k. rowling", "jk rowling", "rowling"],
    "In which novel does a whale named Moby Dick appear?": ["moby dick"],
    "Who wrote the novel 1984?": ["george orwell", "orwell"],
    "What is the first book of the Bible?": ["genesis"],
    "Who created the detective Sherlock Holmes?": ["arthur conan doyle", "conan doyle", "doyle"],
    "How many sides does a dodecagon have?": ["12", "twelve"],
    "What is the largest internal organ in the human body?": ["liver"],
    "How many planets in our solar system have rings?": ["4", "four"],
    "What is the most consumed beverage after water?": ["tea"],
    "What color do you get mixing blue and yellow?": ["green"],
    "How many sides does a heptagon have?": ["7", "seven"],
    "What is the chemical symbol for tin?": ["sn"],
    "How many degrees are in a full circle?": ["360"],
    "What is the currency of the United States?": ["dollar", "us dollar", "dollars"],
    "What gas makes up most of the Sun?": ["hydrogen"],
    "How many sides does a nonagon have?": ["9", "nine"],
    "What is the tallest type of grass?": ["bamboo"],
    "What is the study of weather called?": ["meteorology"],
    "What metal is liquid at room temperature?": ["mercury"],
}

SCRAMBLE_WORDS: list[str] = [
    # Tech / Discord
    "python", "discord", "server", "economy", "lootbox",
    "scramble", "trivia", "reward", "points", "channel",
    "button", "winner", "random", "treasure", "typing",
    "keyboard", "monitor", "laptop", "internet", "browser",
    "network", "router", "firewall", "bandwidth", "discord",
    "streaming", "database", "command", "message", "reaction",
    "webhook", "sidebar", "mention", "profile", "nickname",

    # Gaming
    "fortnite", "minecraft", "roblox", "valorant", "overwatch",
    "pokemon", "dungeon", "dragon", "warrior", "potion",
    "respawn", "loadout", "headshot", "camping", "airdrop",
    "sniper", "grenade", "shotgun", "crossbow", "pistol",
    "crafting", "building", "farming", "grinding", "raiding",
    "checkpoint", "bossfight", "endgame", "sandbox", "speedrun",

    # General
    "elephant", "giraffe", "dolphin", "penguin", "cheetah",
    "volcano", "glacier", "tornado", "thunder", "rainbow",
    "diamond", "emerald", "sapphire", "crystal", "ancient",
    "mystery", "explore", "journey", "courage", "victory",
    "champion", "legendary", "ultimate", "infinite", "dynamic",
    "creative", "imagine", "inspire", "achieve", "succeed",
    "strategy", "tactics", "mission", "stealth", "bounty",
    "goblin", "wizard", "paladin", "hunter", "archer",
    "kingdom", "empire", "castle", "throne", "crown",
    "festival", "parade", "concert", "trophy", "medal",
    "velocity", "gravity", "quantum", "reactor", "particle",
    "universe", "nebula", "galaxy", "orbital", "asteroid",
    "mountain", "canyon", "waterfall", "savanna", "tundra",
    "tropical", "mirage", "horizon", "twilight", "solstice",
    "blossom", "thunder", "whisper", "silence", "eternal",
    # Extra batch
    "phantom", "specter", "wraith", "shadow", "eclipse",
    "crimson", "scarlet", "cobalt", "amber", "violet",
    "blizzard", "cyclone", "tsunami", "volcano", "meteor",
    "labyrinth", "fortress", "sentinel", "vortex", "nexus",
    "plasma", "photon", "neutron", "proton", "electron",
    "compass", "lantern", "anchor", "beacon", "flare",
    "capsule", "shuttle", "payload", "module", "probe",
    "fractal", "cipher", "encrypt", "decode", "signal",
    "pioneer", "venture", "odyssey", "conquest", "triumph",
    "gladiator", "centurion", "legionary", "spartan", "samurai",
    "avalanche", "meadow", "harbor", "cavern", "plateau",
    "prairie", "lagoon", "summit", "valley", "marsh",
    "oasis", "fjord", "geyser", "crater", "ridge",
    "hurricane", "monsoon", "drizzle", "breeze", "sunrise",
    "sunset", "snowfall", "hailstorm", "lightning", "overcast",
    "humidity", "obsidian", "marble", "granite", "quartz",
    "topaz", "opal", "pearl", "copper", "bronze",
    "platinum", "titanium", "graphite", "charcoal", "sorcerer",
    "necromancer", "valkyrie", "griffin", "phoenix", "basilisk",
    "chimera", "minotaur", "kraken", "leviathan", "druid",
    "ranger", "berserker", "assassin", "summoner", "cosmos",
    "comet", "supernova", "singularity", "satellite", "telescope",
    "astronaut", "cosmonaut", "lunar", "stardust", "meteorite",
    "celestial", "interstellar", "spacecraft", "algorithm", "protocol",
    "hardware", "software", "processor", "firmware", "cybernetic",
    "hologram", "android", "automation", "circuit", "voltage",
    "wireless", "expedition", "voyage", "frontier", "wilderness",
    "ambush", "skirmish", "rampart", "citadel", "garrison",
    "catapult", "longbow", "javelin", "halberd", "rapier",
    "scimitar", "cathedral", "chandelier", "gargoyle", "catacomb",
]

EMOJI_PUZZLES: list[tuple[str, str]] = [
    # Movies
    ("🦁👑", "The Lion King"),
    ("🕷️🧑", "Spider-Man"),
    ("🧊❄️👸", "Frozen"),
    ("🤠🐍🌀", "Indiana Jones"),
    ("🦈🌊😱", "Jaws"),
    ("🌌⭐🚀", "Star Wars"),
    ("🤖🚗🔁", "Transformers"),
    ("🧙💍🔥", "Lord of the Rings"),
    ("🦋🦅🤿", "Avatar"),
    ("👻🏚️🕯️", "Ghostbusters"),
    ("🦸‍♂️🦇🌃", "Batman"),
    ("🏎️💨⚡", "Cars"),
    ("🧟🌍💀", "World War Z"),
    ("🐠🌊🐟", "Finding Nemo"),
    ("🐀🍳👨‍🍳", "Ratatouille"),
    ("👶🤖❤️", "A.I. Artificial Intelligence"),
    ("🧸🎠🌈", "Toy Story"),
    ("🐼🥋🐉", "Kung Fu Panda"),
    ("🕰️✈️🧒", "Peter Pan"),
    ("🌕🧑‍🚀🌑", "Apollo 13"),

    # TV Shows
    ("🧪🔵💎", "Breaking Bad"),
    ("🐉🏰🗡️", "Game of Thrones"),
    ("🏝️🌴✈️💥", "Lost"),
    ("🌂☂️💛💙", "How I Met Your Mother"),
    ("🏥💊🩺", "Grey's Anatomy"),
    ("🕵️🔍🧠", "Sherlock"),
    ("👨‍💼📊💰", "The Office"),
    ("🌵🤠🔫", "Breaking Bad"),
    ("🤝😊📋", "Friends"),
    ("🧟🌆🔫", "The Walking Dead"),
    ("🚂⌚🌌", "Dark"),
    ("🤵🍸🎰", "Suits"),
    ("🧹🔮📚", "The Witcher"),
    ("👩‍🔬🧬🔭", "Stranger Things"),
    ("🏫🪄🦉", "Harry Potter"),

    # Games
    ("🍄👷🏰", "Super Mario"),
    ("🧱💎⛏️", "Minecraft"),
    ("🌀🦔💨", "Sonic the Hedgehog"),
    ("⚔️🛡️🧝", "The Legend of Zelda"),
    ("🔴🟡🟢⬆️", "Among Us"),
    ("🦆🎮🏆", "Duck Hunt"),
    ("🃏🎯🔫", "Valorant"),
    ("🤖🌍⚙️", "Horizon Zero Dawn"),
    ("🐉🌐🗡️", "World of Warcraft"),
    ("🔫🏝️🪂", "PUBG"),
    ("👁️🌑👾", "Dead Space"),
    ("🏙️🚗💥", "Grand Theft Auto"),
    ("🦸‍♂️🦹‍♂️🏙️", "Marvel's Spider-Man"),
    ("🌊🤿🐠🏝️", "Subnautica"),
    ("🪐🚀🌌", "Mass Effect"),

    # Brands / Apps
    ("🍎💻📱", "Apple"),
    ("🔍🌐🔎", "Google"),
    ("📘👍👥", "Facebook"),
    ("📸❤️💬", "Instagram"),
    ("🐦💬🌐", "Twitter"),
    ("🎵🎶📱", "TikTok"),
    ("📹🔴▶️", "YouTube"),
    ("💼🤝👔", "LinkedIn"),
    ("📦🚚🛒", "Amazon"),
    ("🚗🗺️📍", "Uber"),
    ("🍔🛵🏠", "DoorDash"),
    ("🎬🍿❤️", "Netflix"),
    ("🎵🎧🟢", "Spotify"),
    ("🎮🕹️🏆", "Steam"),
    ("💬🔒🟢", "WhatsApp"),

    # Food & Misc
    ("🍕🧀🍅", "Pizza"),
    ("🍔🥩🧅", "Burger"),
    ("🌮🌶️🧀", "Taco"),
    ("🍣🐟🍚", "Sushi"),
    ("🍩☕🌅", "Dunkin Donuts"),
    ("🍦🌈🍭", "Ice Cream"),
    ("🍕🎸🎶", "Pizza Party"),
    ("☕📖🌧️", "Coffee Shop"),
    ("🏋️‍♂️💪🥇", "Gym"),
    ("✈️🌍🧳", "Travel"),
    ("🎪🎡🎢", "Amusement Park"),
    ("🎓📚✏️", "School"),
    ("🌙⭐🔭", "Stargazing"),
    ("🎸🥁🎤", "Rock Band"),
    ("🏖️🌊☀️", "Beach Day"),

    # Bonus Pop Culture
    ("💀⚓🦜", "Pirates of the Caribbean"),
    ("🦾🤖❤️‍🔥", "Terminator"),
    ("👽🛸🌽", "Signs"),
    ("🔦👁️🌲", "The Blair Witch Project"),
    ("🧲🦸‍♂️🔴", "Magneto"),
    ("⚡🧒🦉", "Harry Potter"),
    ("🕰️🔮🌀", "Doctor Strange"),
    ("🌻🎩🃏", "The Dark Knight"),
    ("🧬👾🌌", "Interstellar"),
    ("🏔️🧗‍♂️💎", "Everest"),
    # Extra round — games & misc
    ("🧟🪓🌲", "Dead by Daylight"),
    ("🐸🎮🕹️", "Frogger"),
    ("🧩🎯🏅", "Puzzle Game"),
    ("🌍🔫🪖", "Call of Duty"),
    ("🦅🏹🌿", "Far Cry"),
    ("🏰💣🗺️", "Clash of Clans"),
    ("🐉🔥💰", "Dungeons and Dragons"),
    ("🤖🌌⭐", "Star Trek"),
    ("🧜‍♀️🌊🐚", "The Little Mermaid"),
    ("🦸‍♀️⚡🌩️", "Captain Marvel"),
    ("🌋🦖🦕", "Jurassic Park"),
    ("🎩🐇🪄", "Now You See Me"),
    ("🏎️🌍🏆", "Formula One"),
    ("🧊🏒🥅", "Ice Hockey"),
    ("🎭🌹💌", "Romeo and Juliet"),
    # Extra
    ("🕶️🔫💊", "The Matrix"),
    ("🦊🐰🏙️", "Zootopia"),
    ("🧠💡🤯", "Limitless"),
    ("🚂⏱️💣", "Speed"),
    ("🧛🌙❤️", "Twilight"),
    ("🐕👻😱", "Cujo"),
    ("🌀🧑‍🚀🪐", "Interstellar"),
    ("🦴🐕‍🦺🏠", "Lassie"),
    ("🏋️‍♂️🥊🇵🇭", "Manny Pacquiao"),
    ("🧬🦎🏝️", "Jurassic World"),
    ("🌊🏄‍♂️🦈", "Jaws"),
    ("👩‍🔬🧪💥", "Breaking Bad"),
    ("🎸🌵🇲🇽", "Coco"),
    ("🕵️‍♀️👠🔍", "Legally Blonde"),
    ("🏠🔑🌻", "Diary of a Wimpy Kid"),
    ("👽📞🚲🌕", "E.T."),
    ("🤖🗑️🌱", "WALL-E"),
    ("🎈🏠🌄", "Up"),
    ("🧠🎢😢😡", "Inside Out"),
    ("👹🛁⛩️", "Spirited Away"),
    ("🐉🧑‍🦰🪓", "How to Train Your Dragon"),
    ("🚢🧊💔", "Titanic"),
    ("🌪️🏠👠🐶", "The Wizard of Oz"),
    ("🦏🥁🌴🎲", "Jumanji"),
    ("🐯🌊🚣", "Life of Pi"),
    ("🔨⚡🌌", "Thor"),
    ("🦅🛡️🇺🇸", "Captain America"),
    ("🐜🦸‍♂️🔬", "Ant-Man"),
    ("🕶️🤵🔫", "James Bond"),
    ("👸🍎😴", "Snow White"),
    ("👠🕛🎃", "Cinderella"),
    ("🤡🎈🚸", "It"),
    ("🦖🌃💥", "Godzilla"),
    ("🐠🧠🔁", "Finding Dory"),
    ("🤖🔵🟠🚪", "Monsters, Inc."),
    ("🦇🏚️🧛", "Dracula"),
    ("🍫🏭🎫", "Willy Wonka"),
    ("🦊🐔🌽", "Fantastic Mr. Fox"),
    ("🧸🍯🌳", "Winnie the Pooh"),
    ("🐷🕸️🚜", "Charlotte's Web"),
    ("🏀👽🐰", "Space Jam"),
    ("🦸‍♂️👨‍👩‍👧‍👦🟥", "The Incredibles"),
    ("🧙‍♂️🦁🚪❄️", "The Chronicles of Narnia"),
    ("🤠👽🔫", "Cowboys & Aliens"),
    ("🐭🎩🪄", "Fantasia"),
    ("🩸👑🐉", "House of the Dragon"),
    ("🦑💰🟥🟢", "Squid Game"),
    ("👑💂‍♀️🇬🇧", "The Crown"),
    ("🧑‍🍳🔪🍝", "The Bear"),
    ("🤠🐎🤘", "Yellowstone"),
    ("🧟‍♂️🍄🎮", "The Last of Us"),
    ("🏫🔪🖤", "Wednesday"),
    ("🦸‍♂️📺💥", "The Boys"),
    ("🤵💼📊", "Mad Men"),
    ("🚔🍩🟡", "The Simpsons"),
    ("🐎🤖🎩", "Westworld"),
    ("🧛‍♂️🏚️📄", "What We Do in the Shadows"),
    ("🧑‍⚕️🏝️🍸", "The White Lotus"),
    ("🟡🧽🍍", "SpongeBob SquarePants"),
    ("🐴🍌🎬", "BoJack Horseman"),
    ("🍔🤡🍟", "McDonald's"),
    ("👟✔️", "Nike"),
    ("☕🧜‍♀️🟢", "Starbucks"),
    ("🥤🔴⚪", "Coca-Cola"),
    ("👻🟡💬", "Snapchat"),
    ("🎮🟢❎", "Xbox"),
    ("🚗⚡🔴", "Tesla"),
    ("🏠🛏️✈️", "Airbnb"),
    ("📌🖼️", "Pinterest"),
    ("🐧💻", "Linux"),
    ("🥞🍯🧈", "Pancakes"),
    ("🌭🍞🟡", "Hot Dog"),
    ("🍜🥢🍥", "Ramen"),
    ("🥪🥬🍅", "Sandwich"),
    ("🍝🍅🧀", "Spaghetti"),
    ("🥐☕🇫🇷", "Croissant"),
    ("🧁🍫🎉", "Cupcake"),
    ("🍪🥛", "Cookies"),
    ("🥗🥬🥒", "Salad"),
    ("🍿🎬", "Popcorn"),
    ("🟦🟨🧱⬇️", "Tetris"),
    ("🐉👊🥋", "Street Fighter"),
    ("🔫💀🎯", "Counter-Strike"),
    ("🏎️🍌🐢", "Mario Kart"),
    ("🔴🐦💣🟩", "Angry Birds"),
    ("🟡👻🔵", "Pac-Man"),
    ("🐉🌍⚔️", "Elden Ring"),
    ("🚌🪂🏝️", "Fortnite"),
    ("🎮🟢👾", "Space Invaders"),
    ("⚽🎮🏆", "FIFA"),
    ("🏀🎮🔢", "NBA 2K"),
    ("🗡️🛡️🌑", "Dark Souls"),
    ("🧱🔨🏠", "Lego"),
    ("🍬🍭💥", "Candy Crush"),
    ("♟️👑🤴", "Chess"),
    ("🦓⬛⬜", "Zebra"),
    ("🦒🌿🟡", "Giraffe"),
    ("🐘🌍🦷", "Elephant"),
    ("🦋🐛🌸", "Butterfly"),
    ("🐝🍯🌼", "Bee"),
    ("🐙🌊🟣", "Octopus"),
    ("🦅🏔️🪶", "Eagle"),
    ("🐧❄️🐟", "Penguin"),
    ("🦈🌊🦷", "Shark"),
    ("🦁👑🌍", "Lion"),
    ("🌈🦄✨", "Unicorn"),
    ("⚽🏟️🥅", "Soccer"),
    ("🏀⛹️‍♂️🔥", "Basketball"),
    ("🎾🟢🥎", "Tennis"),
    ("🏊‍♂️🌊🥇", "Swimming"),
    ("🚴‍♂️🚵‍♀️🏆", "Cycling"),
    ("🎸🤘🎶", "Rock Music"),
    ("🎻🎼🎩", "Orchestra"),
    ("🏖️🌴🍹", "Vacation"),
    ("🎂🎉🎈", "Birthday"),
]

# ──────────────────────── new content banks ────────────────────────

FILL_BLANK_BANK: list[dict] = [
    {"prompt": "Fill in the blank: 'To be or not to be, that is the ___'",                    "answer": ["question"]},
    {"prompt": "Fill in the blank: 'The early bird catches the ___'",                          "answer": ["worm"]},
    {"prompt": "Fill in the blank: 'Rome wasn't built in a ___'",                              "answer": ["day"]},
    {"prompt": "Fill in the blank: 'Actions speak louder than ___'",                           "answer": ["words"]},
    {"prompt": "Fill in the blank: 'Every cloud has a silver ___'",                            "answer": ["lining"]},
    {"prompt": "Fill in the blank: 'You can't judge a book by its ___'",                       "answer": ["cover"]},
    {"prompt": "Fill in the blank: 'The pen is mightier than the ___'",                        "answer": ["sword"]},
    {"prompt": "Fill in the blank: 'Practice makes ___'",                                      "answer": ["perfect"]},
    {"prompt": "Fill in the blank: 'Better late than ___'",                                    "answer": ["never"]},
    {"prompt": "Fill in the blank: 'Two wrongs don't make a ___'",                             "answer": ["right"]},
    {"prompt": "Fill in the blank: 'Where there's smoke, there's ___'",                        "answer": ["fire"]},
    {"prompt": "Fill in the blank: 'The squeaky wheel gets the ___'",                          "answer": ["grease"]},
    {"prompt": "Fill in the blank: 'Don't count your chickens before they ___'",               "answer": ["hatch"]},
    {"prompt": "Fill in the blank: 'A penny saved is a penny ___'",                            "answer": ["earned"]},
    {"prompt": "Fill in the blank: 'The grass is always greener on the other ___'",            "answer": ["side"]},
    {"prompt": "Fill in the blank: 'Curiosity killed the ___'",                                "answer": ["cat"]},
    {"prompt": "Fill in the blank: 'A picture is worth a thousand ___'",                       "answer": ["words"]},
    {"prompt": "Fill in the blank: 'Don't bite the hand that ___  you'",                       "answer": ["feeds"]},
    {"prompt": "Fill in the blank: 'All that glitters is not ___'",                            "answer": ["gold"]},
    {"prompt": "Fill in the blank: 'Beggars can't be ___'",                                    "answer": ["choosers"]},
    {"prompt": "Fill in the blank: 'The best things in life are ___'",                         "answer": ["free"]},
    {"prompt": "Fill in the blank: 'No pain, no ___'",                                         "answer": ["gain"]},
    {"prompt": "Fill in the blank: 'You reap what you ___'",                                   "answer": ["sow"]},
    {"prompt": "Fill in the blank: 'Keep your friends close, and your enemies ___'",           "answer": ["closer"]},
    {"prompt": "Fill in the blank: 'There's no place like ___'",                               "answer": ["home"]},
    {"prompt": "Fill in the blank: 'Slow and steady wins the ___'",                            "answer": ["race"]},
    {"prompt": "Fill in the blank: 'When in Rome, do as the Romans ___'",                      "answer": ["do"]},
    {"prompt": "Fill in the blank: 'You can lead a horse to water but you can't make it ___'","answer": ["drink"]},
    {"prompt": "Fill in the blank: 'An apple a day keeps the ___ away'",                       "answer": ["doctor"]},
    {"prompt": "Fill in the blank: 'The ___ always rings twice' (1946 film)",                  "answer": ["postman"]},
    {"prompt": "Fill in the blank: 'May the ___ be with you' (Star Wars)",                     "answer": ["force"]},
    {"prompt": "Fill in the blank: 'To infinity and ___' (Toy Story)",                         "answer": ["beyond"]},
    {"prompt": "Fill in the blank: 'Just keep ___' (Finding Nemo)",                            "answer": ["swimming"]},
    {"prompt": "Fill in the blank: 'With great power comes great ___'",                        "answer": ["responsibility"]},
    {"prompt": "Fill in the blank: 'I'll be ___' (Terminator)",                               "answer": ["back"]},
    {"prompt": "Fill in the blank: 'Better safe than ___'", "answer": ["sorry"]},
    {"prompt": "Fill in the blank: 'Honesty is the best ___'", "answer": ["policy"]},
    {"prompt": "Fill in the blank: 'Birds of a feather flock ___'", "answer": ["together"]},
    {"prompt": "Fill in the blank: 'A friend in need is a friend ___'", "answer": ["indeed"]},
    {"prompt": "Fill in the blank: 'Out of sight, out of ___'", "answer": ["mind"]},
    {"prompt": "Fill in the blank: 'Easy come, easy ___'", "answer": ["go"]},
    {"prompt": "Fill in the blank: 'Laughter is the best ___'", "answer": ["medicine"]},
    {"prompt": "Fill in the blank: 'Time is ___'", "answer": ["money"]},
    {"prompt": "Fill in the blank: 'Knowledge is ___'", "answer": ["power"]},
    {"prompt": "Fill in the blank: 'Home is where the ___ is'", "answer": ["heart"]},
    {"prompt": "Fill in the blank: 'Live and let ___'", "answer": ["live"]},
    {"prompt": "Fill in the blank: 'When the going gets tough, the tough get ___'", "answer": ["going"]},
    {"prompt": "Fill in the blank: 'Necessity is the mother of ___'", "answer": ["invention"]},
    {"prompt": "Fill in the blank: 'The apple doesn't fall far from the ___'", "answer": ["tree"]},
    {"prompt": "Fill in the blank: 'A watched pot never ___'", "answer": ["boils"]},
    {"prompt": "Fill in the blank: 'Don't put all your eggs in one ___'", "answer": ["basket"]},
    {"prompt": "Fill in the blank: 'When life gives you lemons, make ___'", "answer": ["lemonade"]},
    {"prompt": "Fill in the blank: 'The ball is in your ___'", "answer": ["court"]},
    {"prompt": "Fill in the blank: 'Break a ___'", "answer": ["leg"]},
    {"prompt": "Fill in the blank: 'Bite the ___'", "answer": ["bullet"]},
    {"prompt": "Fill in the blank: 'Hit the ___ on the head'", "answer": ["nail"]},
    {"prompt": "Fill in the blank: 'Let the cat out of the ___'", "answer": ["bag"]},
    {"prompt": "Fill in the blank: 'Kill two birds with one ___'", "answer": ["stone"]},
    {"prompt": "Fill in the blank: 'Once in a blue ___'", "answer": ["moon"]},
    {"prompt": "Fill in the blank: 'Speak of the ___'", "answer": ["devil"]},
    {"prompt": "Fill in the blank: 'The whole nine ___'", "answer": ["yards"]},
    {"prompt": "Fill in the blank: 'Under the ___'", "answer": ["weather"]},
    {"prompt": "Fill in the blank: 'A piece of ___'", "answer": ["cake"]},
    {"prompt": "Fill in the blank: 'Cost an arm and a ___'", "answer": ["leg"]},
    {"prompt": "Fill in the blank: 'Add insult to ___'", "answer": ["injury"]},
    {"prompt": "Fill in the blank: 'Barking up the wrong ___'", "answer": ["tree"]},
    {"prompt": "Fill in the blank: 'Beat around the ___'", "answer": ["bush"]},
    {"prompt": "Fill in the blank: 'Burn the midnight ___'", "answer": ["oil"]},
    {"prompt": "Fill in the blank: 'Cut to the ___'", "answer": ["chase"]},
    {"prompt": "Fill in the blank: 'Get cold ___'", "answer": ["feet"]},
    {"prompt": "Fill in the blank: 'Go the extra ___'", "answer": ["mile"]},
    {"prompt": "Fill in the blank: 'It takes two to ___'", "answer": ["tango"]},
    {"prompt": "Fill in the blank: 'Jump on the ___'", "answer": ["bandwagon"]},
    {"prompt": "Fill in the blank: 'Let bygones be ___'", "answer": ["bygones"]},
    {"prompt": "Fill in the blank: 'Miss the ___'", "answer": ["boat"]},
    {"prompt": "Fill in the blank: 'On thin ___'", "answer": ["ice"]},
    {"prompt": "Fill in the blank: 'Pull someone's ___'", "answer": ["leg"]},
    {"prompt": "Fill in the blank: 'See eye to ___'", "answer": ["eye"]},
    {"prompt": "Fill in the blank: 'Spill the ___'", "answer": ["beans"]},
    {"prompt": "Fill in the blank: 'Steal someone's ___'", "answer": ["thunder"]},
    {"prompt": "Fill in the blank: 'The last ___'", "answer": ["straw"]},
    {"prompt": "Fill in the blank: 'Throw in the ___'", "answer": ["towel"]},
    {"prompt": "Fill in the blank: 'Up in the ___'", "answer": ["air"]},
    {"prompt": "Fill in the blank: 'A blessing in ___'", "answer": ["disguise"]},
    {"prompt": "Fill in the blank: 'Back to the drawing ___'", "answer": ["board"]},
    {"prompt": "Fill in the blank: 'Ball and ___'", "answer": ["chain"]},
    {"prompt": "Fill in the blank: 'Best of both ___'", "answer": ["worlds"]},
    {"prompt": "Fill in the blank: 'Cry over spilled ___'", "answer": ["milk"]},
    {"prompt": "Fill in the blank: 'Devil's ___'", "answer": ["advocate"]},
    {"prompt": "Fill in the blank: 'Easy does ___'", "answer": ["it"]},
    {"prompt": "Fill in the blank: 'Every dog has its ___'", "answer": ["day"]},
    {"prompt": "Fill in the blank: 'Fit as a ___'", "answer": ["fiddle"]},
    {"prompt": "Fill in the blank: 'Give the benefit of the ___'", "answer": ["doubt"]},
    {"prompt": "Fill in the blank: 'Hang in ___'", "answer": ["there"]},
    {"prompt": "Fill in the blank: 'In the heat of the ___'", "answer": ["moment"]},
    {"prompt": "Fill in the blank: 'Keep your eye on the ___'", "answer": ["prize", "ball"]},
    {"prompt": "Fill in the blank: 'Let sleeping dogs ___'", "answer": ["lie"]},
    {"prompt": "Fill in the blank: 'Make a long story ___'", "answer": ["short"]},
    {"prompt": "Fill in the blank: 'Once bitten, twice ___'", "answer": ["shy"]},
    {"prompt": "Fill in the blank: 'Out of the frying pan and into the ___'", "answer": ["fire"]},
    {"prompt": "Fill in the blank: 'Penny for your ___'", "answer": ["thoughts"]},
    {"prompt": "Fill in the blank: 'Put your best foot ___'", "answer": ["forward"]},
    {"prompt": "Fill in the blank: 'Read between the ___'", "answer": ["lines"]},
    {"prompt": "Fill in the blank: 'Saved by the ___'", "answer": ["bell"]},
    {"prompt": "Fill in the blank: 'Sit on the ___'", "answer": ["fence"]},
    {"prompt": "Fill in the blank: 'Take it with a grain of ___'", "answer": ["salt"]},
    {"prompt": "Fill in the blank: 'The tip of the ___'", "answer": ["iceberg"]},
    {"prompt": "Fill in the blank: 'Through thick and ___'", "answer": ["thin"]},
    {"prompt": "Fill in the blank: 'Time flies when you're having ___'", "answer": ["fun"]},
    {"prompt": "Fill in the blank: 'To Kill a ___'", "answer": ["mockingbird"]},
    {"prompt": "Fill in the blank: 'Turn over a new ___'", "answer": ["leaf"]},
    {"prompt": "Fill in the blank: 'Wear your heart on your ___'", "answer": ["sleeve"]},
    {"prompt": "Fill in the blank: 'When pigs ___'", "answer": ["fly"]},
    {"prompt": "Fill in the blank: 'You can't have your cake and ___ it too'", "answer": ["eat"]},
    {"prompt": "Fill in the blank: 'A leopard can't change its ___'", "answer": ["spots"]},
    {"prompt": "Fill in the blank: 'Caught between a rock and a hard ___'", "answer": ["place"]},
    {"prompt": "Fill in the blank: 'A rolling stone gathers no ___'", "answer": ["moss"]},
    {"prompt": "Fill in the blank: 'Every rose has its ___'", "answer": ["thorn"]},
    {"prompt": "Fill in the blank: 'Good things come to those who ___'", "answer": ["wait"]},
    {"prompt": "Fill in the blank: 'Great minds think ___'", "answer": ["alike"]},
    {"prompt": "Fill in the blank: 'Ignorance is ___'", "answer": ["bliss"]},
    {"prompt": "Fill in the blank: 'If it ain't broke, don't ___ it'", "answer": ["fix"]},
    {"prompt": "Fill in the blank: 'Look before you ___'", "answer": ["leap"]},
    {"prompt": "Fill in the blank: 'Money doesn't grow on ___'", "answer": ["trees"]},
    {"prompt": "Fill in the blank: 'Nip it in the ___'", "answer": ["bud"]},
    {"prompt": "Fill in the blank: 'Old habits die ___'", "answer": ["hard"]},
    {"prompt": "Fill in the blank: 'One man's trash is another man's ___'", "answer": ["treasure"]},
    {"prompt": "Fill in the blank: 'Patience is a ___'", "answer": ["virtue"]},
    {"prompt": "Fill in the blank: 'Practice what you ___'", "answer": ["preach"]},
    {"prompt": "Fill in the blank: 'Rain on someone's ___'", "answer": ["parade"]},
    {"prompt": "Fill in the blank: 'Strike while the iron is ___'", "answer": ["hot"]},
    {"prompt": "Fill in the blank: 'The bigger they are, the harder they ___'", "answer": ["fall"]},
    {"prompt": "Fill in the blank: 'The cherry on ___'", "answer": ["top"]},
    {"prompt": "Fill in the blank: 'There are plenty of fish in the ___'", "answer": ["sea"]},
    {"prompt": "Fill in the blank: 'Variety is the spice of ___'", "answer": ["life"]},
    {"prompt": "Fill in the blank: 'What goes around comes ___'", "answer": ["around"]},
    {"prompt": "Fill in the blank: 'When it rains, it ___'", "answer": ["pours"]},
    {"prompt": "Fill in the blank: 'You snooze, you ___'", "answer": ["lose"]},
    {"prompt": "Fill in the blank: 'A bird in the hand is worth two in the ___'", "answer": ["bush"]},
    {"prompt": "Fill in the blank: 'All is fair in love and ___'", "answer": ["war"]},
    {"prompt": "Fill in the blank: 'Beauty is in the eye of the ___'", "answer": ["beholder"]},
    {"prompt": "Fill in the blank: 'Don't look a gift horse in the ___'", "answer": ["mouth"]},
    {"prompt": "Fill in the blank: 'Fortune favors the ___'", "answer": ["bold", "brave"]},
    {"prompt": "Fill in the blank: 'A chain is only as strong as its weakest ___'", "answer": ["link"]},
]

MATH_BANK: list[dict] = [
    {"prompt": "⚡ Fast Math: What is 13 × 13?",           "answer": ["169"]},
    {"prompt": "⚡ Fast Math: What is 17 × 8?",            "answer": ["136"]},
    {"prompt": "⚡ Fast Math: What is 225 ÷ 15?",          "answer": ["15"]},
    {"prompt": "⚡ Fast Math: What is 12 × 14?",           "answer": ["168"]},
    {"prompt": "⚡ Fast Math: What is 48 × 3?",            "answer": ["144"]},
    {"prompt": "⚡ Fast Math: What is 19 × 7?",            "answer": ["133"]},
    {"prompt": "⚡ Fast Math: What is 360 ÷ 12?",          "answer": ["30"]},
    {"prompt": "⚡ Fast Math: What is 16 × 16?",           "answer": ["256"]},
    {"prompt": "⚡ Fast Math: What is 3 + 7 × 4?",         "answer": ["31"]},
    {"prompt": "⚡ Fast Math: What is (12 + 8) × 5?",      "answer": ["100"]},
    {"prompt": "⚡ Fast Math: What is 100 − 37 + 12?",     "answer": ["75"]},
    {"prompt": "⚡ Fast Math: What is 9² + 9?",            "answer": ["90"]},
    {"prompt": "⚡ Fast Math: What is 500 ÷ 4?",           "answer": ["125"]},
    {"prompt": "⚡ Fast Math: What is 25 × 8?",            "answer": ["200"]},
    {"prompt": "⚡ Fast Math: What is 11 × 11 − 1?",       "answer": ["120"]},
    {"prompt": "⚡ Fast Math: What is 72 ÷ 8 × 6?",        "answer": ["54"]},
    {"prompt": "⚡ Fast Math: What is 15² − 100?",         "answer": ["125"]},
    {"prompt": "⚡ Fast Math: What is 1000 − 237?",        "answer": ["763"]},
    {"prompt": "⚡ Fast Math: What is 6 × 7 × 2?",         "answer": ["84"]},
    {"prompt": "⚡ Fast Math: What is 144 ÷ 12 + 8?",      "answer": ["20"]},
    {"prompt": "⚡ Fast Math: What is 50% of 360?",         "answer": ["180"]},
    {"prompt": "⚡ Fast Math: What is 20% of 250?",         "answer": ["50"]},
    {"prompt": "⚡ Fast Math: What is 3³?",                 "answer": ["27"]},
    {"prompt": "⚡ Fast Math: What is 4⁴?",                 "answer": ["256"]},
    {"prompt": "⚡ Fast Math: What is 7 × 7 × 7?",          "answer": ["343"]},
    {"prompt": "⚡ Fast Math: What is (100 ÷ 5) × 7?",     "answer": ["140"]},
    {"prompt": "⚡ Fast Math: What is 999 + 111?",          "answer": ["1110"]},
    {"prompt": "⚡ Fast Math: What is 18 × 18?",            "answer": ["324"]},
    {"prompt": "⚡ Fast Math: What is 2⁸?",                 "answer": ["256"]},
    {"prompt": "⚡ Fast Math: What is 1000 ÷ 8?",           "answer": ["125"]},
    {"prompt": "⚡ Fast Math: What is 14 × 5?", "answer": ["70"]},
    {"prompt": "⚡ Fast Math: What is 21 × 3?", "answer": ["63"]},
    {"prompt": "⚡ Fast Math: What is 256 ÷ 8?", "answer": ["32"]},
    {"prompt": "⚡ Fast Math: What is 45 + 67?", "answer": ["112"]},
    {"prompt": "⚡ Fast Math: What is 90 ÷ 6 × 4?", "answer": ["60"]},
    {"prompt": "⚡ Fast Math: What is 13²?", "answer": ["169"]},
    {"prompt": "⚡ Fast Math: What is 7 × 9 + 7?", "answer": ["70"]},
    {"prompt": "⚡ Fast Math: What is 800 − 256?", "answer": ["544"]},
    {"prompt": "⚡ Fast Math: What is 30% of 90?", "answer": ["27"]},
    {"prompt": "⚡ Fast Math: What is 5! (factorial)?", "answer": ["120"]},
    {"prompt": "⚡ Fast Math: What is 12 × 12?", "answer": ["144"]},
    {"prompt": "⚡ Fast Math: What is 12 × 13?", "answer": ["156"]},
    {"prompt": "⚡ Fast Math: What is 12 × 15?", "answer": ["180"]},
    {"prompt": "⚡ Fast Math: What is 12 × 16?", "answer": ["192"]},
    {"prompt": "⚡ Fast Math: What is 12 × 17?", "answer": ["204"]},
    {"prompt": "⚡ Fast Math: What is 12 × 18?", "answer": ["216"]},
    {"prompt": "⚡ Fast Math: What is 12 × 19?", "answer": ["228"]},
    {"prompt": "⚡ Fast Math: What is 12 × 20?", "answer": ["240"]},
    {"prompt": "⚡ Fast Math: What is 12 × 21?", "answer": ["252"]},
    {"prompt": "⚡ Fast Math: What is 13 × 12?", "answer": ["156"]},
    {"prompt": "⚡ Fast Math: What is 13 × 14?", "answer": ["182"]},
    {"prompt": "⚡ Fast Math: What is 13 × 15?", "answer": ["195"]},
    {"prompt": "⚡ Fast Math: What is 13 × 16?", "answer": ["208"]},
    {"prompt": "⚡ Fast Math: What is 13 × 17?", "answer": ["221"]},
    {"prompt": "⚡ Fast Math: What is 13 × 18?", "answer": ["234"]},
    {"prompt": "⚡ Fast Math: What is 13 × 19?", "answer": ["247"]},
    {"prompt": "⚡ Fast Math: What is 13 × 20?", "answer": ["260"]},
    {"prompt": "⚡ Fast Math: What is 13 × 21?", "answer": ["273"]},
    {"prompt": "⚡ Fast Math: What is 14 × 12?", "answer": ["168"]},
    {"prompt": "⚡ Fast Math: What is 14 × 13?", "answer": ["182"]},
    {"prompt": "⚡ Fast Math: What is 14 × 14?", "answer": ["196"]},
    {"prompt": "⚡ Fast Math: What is 14 × 15?", "answer": ["210"]},
    {"prompt": "⚡ Fast Math: What is 14 × 16?", "answer": ["224"]},
    {"prompt": "⚡ Fast Math: What is 14 × 17?", "answer": ["238"]},
    {"prompt": "⚡ Fast Math: What is 14 × 18?", "answer": ["252"]},
    {"prompt": "⚡ Fast Math: What is 14 × 19?", "answer": ["266"]},
    {"prompt": "⚡ Fast Math: What is 14 × 20?", "answer": ["280"]},
    {"prompt": "⚡ Fast Math: What is 14 × 21?", "answer": ["294"]},
    {"prompt": "⚡ Fast Math: What is 15 × 12?", "answer": ["180"]},
    {"prompt": "⚡ Fast Math: What is 15 × 13?", "answer": ["195"]},
    {"prompt": "⚡ Fast Math: What is 15 × 14?", "answer": ["210"]},
    {"prompt": "⚡ Fast Math: What is 15 × 15?", "answer": ["225"]},
    {"prompt": "⚡ Fast Math: What is 15 × 16?", "answer": ["240"]},
    {"prompt": "⚡ Fast Math: What is 15 × 17?", "answer": ["255"]},
    {"prompt": "⚡ Fast Math: What is 15 × 18?", "answer": ["270"]},
    {"prompt": "⚡ Fast Math: What is 15 × 19?", "answer": ["285"]},
    {"prompt": "⚡ Fast Math: What is 15 × 20?", "answer": ["300"]},
    {"prompt": "⚡ Fast Math: What is 15 × 21?", "answer": ["315"]},
    {"prompt": "⚡ Fast Math: What is 16 × 12?", "answer": ["192"]},
    {"prompt": "⚡ Fast Math: What is 16 × 13?", "answer": ["208"]},
    {"prompt": "⚡ Fast Math: What is 16 × 14?", "answer": ["224"]},
    {"prompt": "⚡ Fast Math: What is 16 × 15?", "answer": ["240"]},
    {"prompt": "⚡ Fast Math: What is 16 × 17?", "answer": ["272"]},
    {"prompt": "⚡ Fast Math: What is 16 × 18?", "answer": ["288"]},
    {"prompt": "⚡ Fast Math: What is 16 × 19?", "answer": ["304"]},
    {"prompt": "⚡ Fast Math: What is 16 × 20?", "answer": ["320"]},
    {"prompt": "⚡ Fast Math: What is 16 × 21?", "answer": ["336"]},
    {"prompt": "⚡ Fast Math: What is 17 × 12?", "answer": ["204"]},
    {"prompt": "⚡ Fast Math: What is 17 × 13?", "answer": ["221"]},
    {"prompt": "⚡ Fast Math: What is 17 × 14?", "answer": ["238"]},
    {"prompt": "⚡ Fast Math: What is 17 × 15?", "answer": ["255"]},
    {"prompt": "⚡ Fast Math: What is 17 × 16?", "answer": ["272"]},
    {"prompt": "⚡ Fast Math: What is 17 × 17?", "answer": ["289"]},
    {"prompt": "⚡ Fast Math: What is 17 × 18?", "answer": ["306"]},
    {"prompt": "⚡ Fast Math: What is 17 × 19?", "answer": ["323"]},
    {"prompt": "⚡ Fast Math: What is 17 × 20?", "answer": ["340"]},
    {"prompt": "⚡ Fast Math: What is 17 × 21?", "answer": ["357"]},
    {"prompt": "⚡ Fast Math: What is 18 × 12?", "answer": ["216"]},
    {"prompt": "⚡ Fast Math: What is 18 × 13?", "answer": ["234"]},
    {"prompt": "⚡ Fast Math: What is 18 × 14?", "answer": ["252"]},
    {"prompt": "⚡ Fast Math: What is 18 × 15?", "answer": ["270"]},
    {"prompt": "⚡ Fast Math: What is 18 × 16?", "answer": ["288"]},
    {"prompt": "⚡ Fast Math: What is 18 × 17?", "answer": ["306"]},
    {"prompt": "⚡ Fast Math: What is 18 × 19?", "answer": ["342"]},
    {"prompt": "⚡ Fast Math: What is 18 × 20?", "answer": ["360"]},
    {"prompt": "⚡ Fast Math: What is 18 × 21?", "answer": ["378"]},
    {"prompt": "⚡ Fast Math: What is 19 × 12?", "answer": ["228"]},
    {"prompt": "⚡ Fast Math: What is 19 × 13?", "answer": ["247"]},
    {"prompt": "⚡ Fast Math: What is 19 × 14?", "answer": ["266"]},
    {"prompt": "⚡ Fast Math: What is 19 × 15?", "answer": ["285"]},
    {"prompt": "⚡ Fast Math: What is 19 × 16?", "answer": ["304"]},
    {"prompt": "⚡ Fast Math: What is 19 × 17?", "answer": ["323"]},
    {"prompt": "⚡ Fast Math: What is 19 × 18?", "answer": ["342"]},
    {"prompt": "⚡ Fast Math: What is 19 × 19?", "answer": ["361"]},
    {"prompt": "⚡ Fast Math: What is 19 × 20?", "answer": ["380"]},
    {"prompt": "⚡ Fast Math: What is 19 × 21?", "answer": ["399"]},
    {"prompt": "⚡ Fast Math: What is 20 × 12?", "answer": ["240"]},
    {"prompt": "⚡ Fast Math: What is 20 × 13?", "answer": ["260"]},
    {"prompt": "⚡ Fast Math: What is 20 × 14?", "answer": ["280"]},
    {"prompt": "⚡ Fast Math: What is 20 × 15?", "answer": ["300"]},
    {"prompt": "⚡ Fast Math: What is 20 × 16?", "answer": ["320"]},
    {"prompt": "⚡ Fast Math: What is 20 × 17?", "answer": ["340"]},
    {"prompt": "⚡ Fast Math: What is 20 × 18?", "answer": ["360"]},
    {"prompt": "⚡ Fast Math: What is 20 × 19?", "answer": ["380"]},
    {"prompt": "⚡ Fast Math: What is 20 × 20?", "answer": ["400"]},
    {"prompt": "⚡ Fast Math: What is 20 × 21?", "answer": ["420"]},
    {"prompt": "⚡ Fast Math: What is 21 × 12?", "answer": ["252"]},
    {"prompt": "⚡ Fast Math: What is 21 × 13?", "answer": ["273"]},
    {"prompt": "⚡ Fast Math: What is 21 × 14?", "answer": ["294"]},
    {"prompt": "⚡ Fast Math: What is 21 × 15?", "answer": ["315"]},
    {"prompt": "⚡ Fast Math: What is 21 × 16?", "answer": ["336"]},
    {"prompt": "⚡ Fast Math: What is 21 × 17?", "answer": ["357"]},
    {"prompt": "⚡ Fast Math: What is 21 × 18?", "answer": ["378"]},
    {"prompt": "⚡ Fast Math: What is 21 × 19?", "answer": ["399"]},
    {"prompt": "⚡ Fast Math: What is 21 × 20?", "answer": ["420"]},
    {"prompt": "⚡ Fast Math: What is 21 × 21?", "answer": ["441"]},
    {"prompt": "⚡ Fast Math: What is 115 + 87?", "answer": ["202"]},
    {"prompt": "⚡ Fast Math: What is 128 + 87?", "answer": ["215"]},
    {"prompt": "⚡ Fast Math: What is 141 + 87?", "answer": ["228"]},
    {"prompt": "⚡ Fast Math: What is 154 + 87?", "answer": ["241"]},
]

TRUE_FALSE_BANK: list[dict] = [
    {"statement": "The Great Wall of China is visible from space with the naked eye.",          "answer": False,  "fact": "This is a myth — it's too narrow to be seen from orbit."},
    {"statement": "Humans share about 60% of their DNA with bananas.",                          "answer": True,   "fact": "We share roughly 60% of our DNA with bananas."},
    {"statement": "Lightning never strikes the same place twice.",                              "answer": False,  "fact": "Lightning can and does strike the same place multiple times."},
    {"statement": "Goldfish have a memory span of only 3 seconds.",                             "answer": False,  "fact": "Goldfish can remember things for months."},
    {"statement": "The Eiffel Tower grows taller in summer due to heat expansion.",             "answer": True,   "fact": "It can grow up to 15 cm taller in hot weather."},
    {"statement": "Bats are blind.",                                                            "answer": False,  "fact": "Bats have functional eyes and can see — they also use echolocation."},
    {"statement": "An octopus has three hearts.",                                               "answer": True,   "fact": "Octopuses have two branchial hearts and one systemic heart."},
    {"statement": "Water boils at 100°C at sea level.",                                        "answer": True,   "fact": "Water boils at 100°C (212°F) at standard sea-level pressure."},
    {"statement": "The Amazon River is the longest river in the world.",                        "answer": False,  "fact": "The Nile is generally considered the longest river."},
    {"statement": "Venus is the hottest planet in our solar system.",                           "answer": True,   "fact": "Venus is hotter than Mercury due to its thick greenhouse atmosphere."},
    {"statement": "A group of flamingos is called a flamboyance.",                              "answer": True,   "fact": "Yes — a flamboyance of flamingos!"},
    {"statement": "Diamonds are made of carbon.",                                               "answer": True,   "fact": "Diamonds are pure crystallized carbon."},
    {"statement": "Sharks are mammals.",                                                        "answer": False,  "fact": "Sharks are fish, not mammals."},
    {"statement": "Sound travels faster than light.",                                           "answer": False,  "fact": "Light travels at ~300,000 km/s; sound at ~343 m/s."},
    {"statement": "Napoleon Bonaparte was unusually short for his era.",                        "answer": False,  "fact": "He was about 5'7\" — average height for his time."},
    {"statement": "Humans use only 10% of their brain.",                                       "answer": False,  "fact": "Brain scans show we use virtually all of our brain."},
    {"statement": "A group of cats is called a clowder.",                                      "answer": True,   "fact": "A clowder is the correct term for a group of cats."},
    {"statement": "The sun is a star.",                                                        "answer": True,   "fact": "The sun is a G-type main-sequence star."},
    {"statement": "Snakes have eyelids.",                                                      "answer": False,  "fact": "Snakes have a clear scale over their eyes, not eyelids."},
    {"statement": "The tongue is the strongest muscle in the human body.",                     "answer": False,  "fact": "The masseter (jaw muscle) is considered the strongest."},
    {"statement": "Elephants are the only animals that can't jump.",                          "answer": False,  "fact": "Several other animals can't jump either (hippos, rhinos, etc.)."},
    {"statement": "A duck's quack does not echo.",                                             "answer": False,  "fact": "A duck's quack does echo — this is a myth."},
    {"statement": "The Pacific Ocean is larger than all land on Earth combined.",              "answer": True,   "fact": "The Pacific Ocean covers more area than all land masses combined."},
    {"statement": "Honey never expires.",                                                      "answer": True,   "fact": "Honey found in ancient Egyptian tombs was still edible."},
    {"statement": "A day on Venus is longer than a year on Venus.",                           "answer": True,   "fact": "Venus rotates so slowly its day is longer than its orbit around the Sun."},
    {"statement": "Mount Everest is the tallest mountain above sea level.", "answer": True, "fact": "Everest stands about 8,849 m above sea level."},
    {"statement": "Tomatoes are vegetables, not fruits.", "answer": False, "fact": "Botanically, tomatoes are fruits."},
    {"statement": "Humans have two lungs.", "answer": True, "fact": "Humans have a left and right lung."},
    {"statement": "Spiders are insects.", "answer": False, "fact": "Spiders are arachnids, not insects."},
    {"statement": "Penguins can fly.", "answer": False, "fact": "Penguins are flightless birds; they swim instead."},
    {"statement": "Mercury is the closest planet to the Sun.", "answer": True, "fact": "Mercury orbits closest to the Sun."},
    {"statement": "Bananas grow on trees.", "answer": False, "fact": "Banana plants are giant herbs, not trees."},
    {"statement": "Gold is denser than iron.", "answer": True, "fact": "Gold is denser and heavier than iron by volume."},
    {"statement": "Owls can rotate their heads a full 360 degrees.", "answer": False, "fact": "Owls rotate their heads about 270 degrees."},
    {"statement": "The Sun appears to rise in the east.", "answer": True, "fact": "Earth's rotation makes the Sun appear to rise in the east."},
    {"statement": "There are 100 minutes in an hour.", "answer": False, "fact": "There are 60 minutes in an hour."},
    {"statement": "A shrimp's heart is located in its head.", "answer": True, "fact": "A shrimp's heart is in its head region."},
    {"statement": "Bananas are berries, botanically speaking.", "answer": True, "fact": "Botanically, bananas qualify as berries."},
    {"statement": "Strawberries are true berries, botanically.", "answer": False, "fact": "Strawberries are not true berries botanically."},
    {"statement": "The human heart has four chambers.", "answer": True, "fact": "Two atria and two ventricles."},
    {"statement": "Sound cannot travel through the vacuum of space.", "answer": True, "fact": "Space is a vacuum, so sound cannot travel."},
    {"statement": "The Sun is mostly made of helium.", "answer": False, "fact": "The Sun is mostly hydrogen."},
    {"statement": "Mount Kilimanjaro is located in Africa.", "answer": True, "fact": "It is in Tanzania."},
    {"statement": "Sharks existed before trees did.", "answer": True, "fact": "Sharks predate trees by millions of years."},
    {"statement": "A jiffy is an actual unit of time.", "answer": True, "fact": "A jiffy is a real measure of time in physics."},
    {"statement": "Glass is a slow-moving liquid at room temperature.", "answer": False, "fact": "Glass is an amorphous solid, not a liquid."},
    {"statement": "The Eiffel Tower was originally meant to be temporary.", "answer": True, "fact": "It was built for the 1889 World's Fair."},
    {"statement": "Octopuses have blue blood.", "answer": True, "fact": "Their blood is copper-based (hemocyanin)."},
    {"statement": "The shortest war in history lasted under an hour.", "answer": True, "fact": "The Anglo-Zanzibar War lasted about 38 minutes."},
    {"statement": "Polar bears have white skin under their fur.", "answer": False, "fact": "Polar bears have black skin under clear fur."},
    {"statement": "A bolt of lightning is hotter than the surface of the Sun.", "answer": True, "fact": "Lightning can reach around 30,000 K."},
    {"statement": "Wombat poop is cube-shaped.", "answer": True, "fact": "Wombats produce cube-shaped droppings."},
    {"statement": "Carrots were originally purple.", "answer": True, "fact": "Early cultivated carrots were purple."},
    {"statement": "There are more stars in the universe than grains of sand on Earth.", "answer": True, "fact": "Estimates suggest far more stars than sand grains."},
    {"statement": "Lobsters are biologically immortal.", "answer": False, "fact": "Lobsters are not biologically immortal."},
    {"statement": "A cloud can weigh more than a million pounds.", "answer": True, "fact": "An average cumulus cloud weighs about 1.1 million pounds."},
    {"statement": "Hot water can freeze faster than cold water under some conditions.", "answer": True, "fact": "This is known as the Mpemba effect."},
    {"statement": "Bananas are slightly radioactive.", "answer": True, "fact": "Due to their potassium-40 content."},
    {"statement": "Sneezes can travel at over 100 mph.", "answer": True, "fact": "Sneezes can exceed 100 mph."},
    {"statement": "Antarctica is technically a desert.", "answer": True, "fact": "It is the driest continent."},
    {"statement": "Earth is the only planet not named after a god.", "answer": True, "fact": "Earth's name has Germanic and English origins."},
    {"statement": "A group of crows is called a murder.", "answer": True, "fact": "Yes, it is called a murder of crows."},
    {"statement": "Your heart stops beating when you sneeze.", "answer": False, "fact": "Your heart does not stop when you sneeze."},
    {"statement": "Chewing gum takes seven years to digest if swallowed.", "answer": False, "fact": "It passes through; it does not linger for years."},
    {"statement": "The dot over a lowercase i is called a tittle.", "answer": True, "fact": "That dot is called a tittle."},
    {"statement": "Bubble wrap was originally invented as wallpaper.", "answer": True, "fact": "It was first marketed as wallpaper."},
    {"statement": "The unicorn is the national animal of Scotland.", "answer": True, "fact": "Scotland's national animal is the unicorn."},
    {"statement": "Saturn would float in water if there were a tub big enough.", "answer": True, "fact": "Its average density is less than water."},
    {"statement": "Sharks must keep swimming or they will die.", "answer": False, "fact": "Many sharks can rest; only some must keep moving."},
    {"statement": "Hummingbirds are the only birds that can fly backward.", "answer": True, "fact": "They can fly backward."},
    {"statement": "The Statue of Liberty was a gift from France.", "answer": True, "fact": "France gifted it to the United States."},
    {"statement": "An ostrich's eye is bigger than its brain.", "answer": True, "fact": "Its eye is larger than its brain."},
    {"statement": "Water always drains the opposite way in the Southern Hemisphere.", "answer": False, "fact": "The Coriolis effect does not control small sink drains."},
    {"statement": "Honeybees can be trained to recognize human faces.", "answer": True, "fact": "Bees can learn to recognize faces."},
    {"statement": "A day on Mars is about 24 hours long.", "answer": True, "fact": "A Martian sol is about 24 hours 39 minutes."},
    {"statement": "The Pacific is the deepest ocean.", "answer": True, "fact": "It contains the Mariana Trench."},
    {"statement": "Tigers have striped skin, not just striped fur.", "answer": True, "fact": "Their skin is striped too."},
    {"statement": "Penguins live naturally at the North Pole.", "answer": False, "fact": "Penguins live in the Southern Hemisphere."},
    {"statement": "Frogs can absorb water through their skin.", "answer": True, "fact": "Frogs drink through their skin."},
    {"statement": "Cleopatra lived closer in time to the Moon landing than to the building of the pyramids.", "answer": True, "fact": "The pyramids predate Cleopatra by about 2,500 years."},
    {"statement": "Some turtles can breathe through their rear ends.", "answer": True, "fact": "Certain turtles use cloacal respiration."},
    {"statement": "A flea can jump over 100 times its body length.", "answer": True, "fact": "Fleas are extraordinary jumpers."},
    {"statement": "The Earth is a perfect sphere.", "answer": False, "fact": "Earth is an oblate spheroid."},
    {"statement": "Light from the Sun takes about 8 minutes to reach Earth.", "answer": True, "fact": "About 8 minutes and 20 seconds."},
    {"statement": "Koalas have fingerprints similar to humans.", "answer": True, "fact": "Their prints are nearly indistinguishable from ours."},
    {"statement": "A blue whale's heart is roughly the size of a small car.", "answer": True, "fact": "It is enormous, around that size."},
    {"statement": "The Amazon rainforest produces 20% of Earth's oxygen.", "answer": False, "fact": "This common claim is widely disputed and overstated."},
    {"statement": "There are more possible chess games than atoms in the observable universe.", "answer": True, "fact": "The Shannon number exceeds atom estimates."},
    {"statement": "Vatican City is the smallest country in the world by area.", "answer": True, "fact": "It is the smallest sovereign state."},
    {"statement": "Humans are born with all the brain cells they will ever have.", "answer": False, "fact": "Some neurogenesis continues into adulthood."},
    {"statement": "A baker's dozen is 13.", "answer": True, "fact": "A baker's dozen equals 13."},
    {"statement": "Absolute zero is the coldest temperature possible.", "answer": True, "fact": "Absolute zero is 0 Kelvin."},
    {"statement": "Jupiter is the largest planet in the solar system.", "answer": True, "fact": "Jupiter is the biggest planet."},
    {"statement": "Camels store water in their humps.", "answer": False, "fact": "Humps store fat, not water."},
    {"statement": "There are more trees on Earth than stars in the Milky Way.", "answer": True, "fact": "About 3 trillion trees versus billions of stars."},
    {"statement": "Bulls are enraged by the color red.", "answer": False, "fact": "Bulls are colorblind to red; they react to movement."},
    {"statement": "A leap year occurs every four years without exception.", "answer": False, "fact": "Century years not divisible by 400 are skipped."},
    {"statement": "Mosquitoes are the deadliest animals to humans.", "answer": True, "fact": "They cause the most human deaths via disease."},
    {"statement": "Glass is made primarily from sand.", "answer": True, "fact": "Silica sand is the main ingredient."},
    {"statement": "A googol is a 1 followed by 100 zeros.", "answer": True, "fact": "That is the definition of a googol."},
    {"statement": "The Sahara is the largest desert on Earth.", "answer": False, "fact": "Antarctica is the largest desert overall."},
    {"statement": "Dolphins rest one half of their brain at a time.", "answer": True, "fact": "They sleep with one hemisphere awake."},
    {"statement": "All mammals give live birth.", "answer": False, "fact": "Monotremes like the platypus lay eggs."},
    {"statement": "Sharks have bones.", "answer": False, "fact": "Shark skeletons are made of cartilage."},
    {"statement": "Sound travels faster in water than in air.", "answer": True, "fact": "Sound moves faster through water."},
    {"statement": "A year on Mercury is shorter than a year on Earth.", "answer": True, "fact": "Mercury orbits the Sun in about 88 days."},
    {"statement": "Most of Earth's oxygen comes from the ocean.", "answer": True, "fact": "Marine plankton produce a majority of oxygen."},
    {"statement": "The brain itself feels no pain.", "answer": True, "fact": "The brain has no pain receptors."},
    {"statement": "Cats cannot taste sweetness.", "answer": True, "fact": "Cats lack functional sweet taste receptors."},
    {"statement": "Pluto is still classified as a full planet.", "answer": False, "fact": "Pluto is now a dwarf planet."},
    {"statement": "Most types of wood float because they are less dense than water.", "answer": True, "fact": "Most wood is less dense than water."},
    {"statement": "The largest volcano in the solar system is on Mars.", "answer": True, "fact": "Olympus Mons is on Mars."},
    {"statement": "An adult human has fewer bones than a newborn baby.", "answer": True, "fact": "Babies have about 300 bones; adults about 206."},
    {"statement": "Venus rotates in the opposite direction to most planets.", "answer": True, "fact": "Venus has retrograde rotation."},
    {"statement": "Bees die after stinging a human.", "answer": True, "fact": "Honeybees die after stinging due to a barbed stinger."},
    {"statement": "Most countries in the world use the metric system.", "answer": True, "fact": "The metric system is used by most countries."},
    {"statement": "Goldfish have a memory of only three seconds.", "answer": False, "fact": "Goldfish can remember things for months."},
    {"statement": "Humans use only 10% of their brains.", "answer": False, "fact": "This is a myth; we use virtually all of our brain."},
    {"statement": "A snail can sleep for up to three years.", "answer": True, "fact": "Snails can enter very long periods of dormancy."},
    {"statement": "The fingernails of a dead body keep growing.", "answer": False, "fact": "Skin retracts, only making nails appear longer."},
    {"statement": "Honey never spoils if stored properly.", "answer": True, "fact": "Honey can last thousands of years."},
    {"statement": "A shrimp's brain is located in its tail.", "answer": False, "fact": "A shrimp's brain is in its head region."},
    {"statement": "Tomatoes are botanically a fruit.", "answer": True, "fact": "Tomatoes are botanically fruits."},
    {"statement": "The human body has more than 600 muscles.", "answer": True, "fact": "There are over 600 named muscles."},
    {"statement": "Mount Everest is the tallest mountain measured from base to peak.", "answer": False, "fact": "Mauna Kea is taller measured base to peak."},
    {"statement": "The blue whale is the largest animal ever known to have lived.", "answer": True, "fact": "It is larger than any known dinosaur."},
    {"statement": "Diamonds are made of pure carbon.", "answer": True, "fact": "Diamonds are crystallized carbon."},
    {"statement": "The Atlantic Ocean is the largest ocean on Earth.", "answer": False, "fact": "The Pacific is the largest ocean."},
    {"statement": "A rhino's horn is made of keratin.", "answer": True, "fact": "It is made of keratin, like hair and nails."},
    {"statement": "The smallest bone in the human body is in the ear.", "answer": True, "fact": "The stapes in the ear is the smallest bone."},
    {"statement": "Pure water conducts electricity very well.", "answer": False, "fact": "Pure water is actually a poor conductor."},
    {"statement": "The Moon is slowly drifting away from Earth.", "answer": True, "fact": "The Moon moves away about 3.8 cm per year."},
    {"statement": "Penguins can fly short distances.", "answer": False, "fact": "Penguins cannot fly; they swim."},
]


# ─────────────────────── PostgreSQL helpers ──────────────────────

def _pg_connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


def init_db() -> None:
    with _pg_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS economy_users (
                    user_id BIGINT PRIMARY KEY,
                    points  INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        con.commit()


def add_points(user_id: int, amount: int) -> int:
    """UPSERT points for a user and return their new total."""
    with _pg_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO economy_users (user_id, points) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET points = economy_users.points + EXCLUDED.points
                RETURNING points
                """,
                (user_id, amount),
            )
            row = cur.fetchone()
        con.commit()
        return row[0] if row else amount


def deduct_points(user_id: int, amount: int) -> int:
    """Deduct points (floor at 0) and return new total."""
    with _pg_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO economy_users (user_id, points) VALUES (%s, 0)
                ON CONFLICT (user_id) DO UPDATE
                    SET points = GREATEST(0, economy_users.points - %s)
                RETURNING points
                """,
                (user_id, amount),
            )
            row = cur.fetchone()
        con.commit()
        return row[0] if row else 0


def get_points(user_id: int) -> int:
    with _pg_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT points FROM economy_users WHERE user_id = %s", (user_id,)
            )
            row = cur.fetchone()
            return row[0] if row else 0


def get_leaderboard(limit: int = 10) -> list[dict]:
    with _pg_connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, points FROM economy_users ORDER BY points DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()


def get_top_user() -> tuple[int, int] | None:
    """Return (user_id, points) of the user with the highest balance, or None."""
    with _pg_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT user_id, points FROM economy_users ORDER BY points DESC LIMIT 1"
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else None


# ──────────────────────── embed builders ──────────────────────────

def _base_embed(colour: int) -> discord.Embed:
    return discord.Embed(colour=colour)


def embed_trivia(question: str, payout: int) -> discord.Embed:
    e = _base_embed(C_TRIVIA)
    e.description = (
        f"```ansi\n\u001b[1;34m  ◈  TRIVIA DROP  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**{question}**\n"
        f"{SEP}\n"
        f"⚡ **First correct answer wins `{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` to answer — type it below"
    )
    e.set_footer(text="SOLACE EVENT  •  Trivia Event")
    return e


def embed_scramble(scrambled: str, payout: int) -> discord.Embed:
    e = _base_embed(C_SCRAMBLE)
    e.description = (
        f"```ansi\n\u001b[1;35m  ◈  WORD SCRAMBLE  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**Unscramble this word:**\n"
        f"```\n{scrambled.upper()}\n```"
        f"{SEP}\n"
        f"⚡ **First correct answer wins `{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` remaining — type it below"
    )
    e.set_footer(text="SOLACE EVENT  •  Scramble Event")
    return e


def embed_lootbox(payout_range: str) -> discord.Embed:
    e = _base_embed(C_LOOTBOX)
    e.description = (
        f"```ansi\n\u001b[1;32m  ◈  SUPPLY DROP  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**A mystery crate has landed in the server.**\n\n"
        f"💰 Reward: `{payout_range} pts`  *(random)*\n"
        f"⏳ `{DROP_TIMEOUT}s` before it disappears\n"
        f"{SEP}\n"
        f"*Click the button below — first grab wins.*"
    )
    e.set_footer(text="SOLACE EVENT  •  Supply Drop")
    return e


def embed_boss(hp: int, max_hp: int, attackers: dict[int, int]) -> discord.Embed:
    e = _base_embed(C_BOSS)
    bar_filled = int((hp / max_hp) * 20)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    e.description = (
        f"```ansi\n\u001b[1;31m  ⚔  CO-OP BOSS RAID  ⚔\u001b[0m\n```"
        f"{SEP}\n"
        f"**☠ Shadow Colossus has appeared!**\n\n"
        f"❤️ HP: `{hp}/{max_hp}`\n"
        f"```\n[{bar}]\n```"
        f"{SEP}\n"
        f"Type **`!attack`** to deal damage (1–5 HP)!\n"
        f"⏳ `{DROP_TIMEOUT}s` to slay the beast\n"
        f"👥 Raiders: `{len(attackers)}`"
    )
    e.set_footer(text="SOLACE EVENT  •  Co-Op Boss Raid")
    return e


def embed_boss_dead(participants: dict[int, int], payout_each: int) -> discord.Embed:
    e = _base_embed(C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  BOSS DEFEATED  \u001b[0m\n```"
        f"{SEP}\n"
        f"**The Shadow Colossus has fallen!** 🎉\n\n"
        f"⚔️ Raiders: `{len(participants)}`\n"
        f"💰 Reward per raider: **`{payout_each} pts`**\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Boss Raid Victory")
    return e


def embed_worldboss(name: str, hp: int, max_hp: int, attackers: dict[int, int], mins: int, pool: int = WORLDBOSS_POOL) -> discord.Embed:
    e = _base_embed(C_BOSS)
    bar_filled = int((hp / max_hp) * 20) if max_hp else 0
    bar = "\u2588" * bar_filled + "\u2591" * (20 - bar_filled)
    pct = (hp / max_hp * 100) if max_hp else 0
    e.description = (
        f"```ansi\n\u001b[1;31m  \u2620  WORLD BOSS RAID  \u2620\u001b[0m\n```"
        f"{SEP}\n"
        f"**\U0001f30b {name} threatens the server!**\n\n"
        f"\u2764\ufe0f HP: `{hp:,}/{max_hp:,}`  ({pct:.0f}%)\n"
        f"```\n[{bar}]\n```"
        f"{SEP}\n"
        f"Type **`!attack`** to strike! \U0001f4a5\n"
        f"\u2696\ufe0f *Lower-ranked raiders hit HARDER (up to {UNDERDOG_LOW_MULT}x).*\n"
        f"\U0001f4b0 Pool: **`{pool:,} pts`** \u2014 split by damage dealt\n"
        f"\u23f3 Lasts `{mins}` min  \u2022  \U0001f465 Raiders: `{len(attackers)}`"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  World Boss Raid")
    return e


def embed_worldboss_dead(name: str, participants: dict[int, int], pool: int, defeated: bool) -> discord.Embed:
    e = _base_embed(C_WIN if defeated else C_BOSS)
    total_dmg = sum(participants.values()) or 1
    title = "\u2714  WORLD BOSS DEFEATED" if defeated else "\u231b  WORLD BOSS RETREATED"
    headline = (
        f"**{name} has been slain!** \U0001f389" if defeated
        else f"**{name} retreated \u2014 but you wounded it!** \u2694\ufe0f"
    )
    ranked = sorted(participants.items(), key=lambda kv: kv[1], reverse=True)[:5]
    lines = []
    for i, (uid, dmg) in enumerate(ranked, 1):
        share = int(pool * dmg / total_dmg)
        medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(i, "\U0001f539")
        lines.append(f"{medal} <@{uid}> \u2014 `{dmg}` dmg \u2192 **`{share:,} pts`**")
    board = "\n".join(lines) if lines else "*No raiders joined the fight.*"
    e.description = (
        f"```ansi\n\u001b[1;33m  {title}  \u001b[0m\n```"
        f"{SEP}\n"
        f"{headline}\n\n"
        f"\u2694\ufe0f Raiders: `{len(participants)}`  \u2022  \U0001f4a5 Total damage: `{total_dmg:,}`\n"
        f"\U0001f4b0 Pool split by damage dealt:\n\n"
        f"{board}\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  World Boss Payout")
    return e


def embed_hotcold(clue: str = "") -> discord.Embed:
    e = _base_embed(C_HOT_COLD)
    e.description = (
        f"```ansi\n\u001b[1;34m  🎯  HOT OR COLD  🎯\u001b[0m\n```"
        f"{SEP}\n"
        f"**I'm thinking of a number between 1 and 100.**\n\n"
        f"Type your guess in chat!\n"
        + (f"*Last hint: {clue}*\n" if clue else "")
        + f"{SEP}\n"
        f"⏳ `{DROP_TIMEOUT}s` to guess the number"
    )
    e.set_footer(text="SOLACE EVENT  •  Number Guessing")
    return e


def embed_emoji_puzzle(emojis: str, payout: int) -> discord.Embed:
    e = _base_embed(C_EMOJI)
    e.description = (
        f"```ansi\n\u001b[1;33m  🧩  EMOJI PUZZLE  🧩\u001b[0m\n```"
        f"{SEP}\n"
        f"**What does this represent?**\n\n"
        f"# {emojis}\n\n"
        f"{SEP}\n"
        f"⚡ First correct text answer wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` to solve it"
    )
    e.set_footer(text="SOLACE EVENT  •  Emoji Puzzle")
    return e


def embed_bomb_waiting() -> discord.Embed:
    e = _base_embed(C_BOMB)
    e.description = (
        f"```ansi\n\u001b[1;31m  💣  REACTION TIME BOMB  💣\u001b[0m\n```"
        f"{SEP}\n"
        f"**The fuse is lit...**\n\n"
        f"🕰️ Wait for my signal to **DEFUSE** it!\n"
        f"⚠️ *Click BEFORE the signal = -50 pts penalty!*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Stay patient...")
    return e


def embed_bomb_active(payout: int) -> discord.Embed:
    e = _base_embed(C_BOMB)
    e.description = (
        f"```ansi\n\u001b[1;31m  💥  DEFUSE NOW!  💥\u001b[0m\n```"
        f"{SEP}\n"
        f"**CLICK THE BUTTON — RIGHT NOW!**\n\n"
        f"💰 First to defuse wins **`{payout} pts`**!\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Time Bomb — DEFUSE!")
    return e


def embed_multiplier(duration_mins: int) -> discord.Embed:
    e = _base_embed(C_MULTI)
    e.description = (
        f"```ansi\n\u001b[1;36m  ✨  DOUBLE POINTS ACTIVE  ✨\u001b[0m\n```"
        f"{SEP}\n"
        f"**🚀 Server-wide Double Points Event!**\n\n"
        f"All drop payouts are doubled for the next **{duration_mins} minutes**!\n"
        f"{SEP}\n"
        f"*Keep chatting to trigger more drops!*"
    )
    e.set_footer(text="SOLACE EVENT  •  Multiplier Event")
    return e


def embed_bounty(target_name: str, bounty_amount: int, puzzle_emojis: str) -> discord.Embed:
    e = _base_embed(C_BOUNTY)
    e.description = (
        f"```ansi\n\u001b[1;31m  🎯  POINT BOUNTY  🎯\u001b[0m\n```"
        f"{SEP}\n"
        f"**A bounty has been placed on `{target_name}`!**\n\n"
        f"💰 Solve this puzzle to claim **`{bounty_amount} pts`** from their vault:\n\n"
        f"# {puzzle_emojis}\n\n"
        f"{SEP}\n"
        f"⏳ `{DROP_TIMEOUT}s` — first solver wins the bounty!"
    )
    e.set_footer(text="SOLACE EVENT  •  Bounty Hunt")
    return e


def embed_heist(target_name: str, steal: int, a: int, b: int) -> discord.Embed:
    e = _base_embed(C_BOUNTY)
    e.description = (
        f"```ansi\n\u001b[1;31m  \U0001f4b0  VAULT HEIST  \U0001f4b0\u001b[0m\n```"
        f"{SEP}\n"
        f"**`{target_name}`'s vault is exposed!**\n\n"
        f"\U0001f513 Crack the lock to steal **`{steal} pts`** from them:\n\n"
        f"# `{a} + {b} = ?`\n\n"
        f"{SEP}\n"
        f"\u23f3 `{DROP_TIMEOUT}s` \u2014 first to crack it robs the vault!"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  Heist")
    return e


def embed_losers(question: str, payout: int) -> discord.Embed:
    e = _base_embed(C_TF)
    e.description = (
        f"```ansi\n\u001b[1;32m  \u267b  LOSERS BRACKET  \u267b\u001b[0m\n```"
        f"{SEP}\n"
        f"\U0001f6ab **Top {UNDERDOG_LOCK_N} are locked out \u2014 underdogs only!**\n\n"
        f"**{question}**\n"
        f"{SEP}\n"
        f"\u26a1 **First eligible correct answer wins `{payout} pts`**\n"
        f"\u23f3 `{DROP_TIMEOUT}s` to answer \u2014 type it below"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  Losers Bracket")
    return e


def embed_win_text(user: discord.Member | discord.User, payout: int, new_total: int, drop_type: str) -> discord.Embed:
    colour = {
        "trivia":    C_TRIVIA,    "scramble": C_SCRAMBLE,
        "emoji":     C_EMOJI,     "hotcold":  C_HOT_COLD,
        "bounty":    C_BOUNTY,    "fillblank": C_FILL,
        "fastmath":  C_MATH,
    }.get(drop_type, C_WIN)
    e = _base_embed(colour)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  CORRECT  \u001b[0m\n```"
        f"{SEP}\n"
        f"{user.mention} answered correctly\n\n"
        f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_win_lootbox(user: discord.Member | discord.User, payout: int, new_total: int) -> discord.Embed:
    e = _base_embed(C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  LOOT CLAIMED  \u001b[0m\n```"
        f"{SEP}\n"
        f"{user.mention} intercepted the crate\n\n"
        f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE EVENT  •  Supply Drop")
    return e


def embed_timeout() -> discord.Embed:
    e = _base_embed(C_TIMEOUT)
    e.description = (
        f"```ansi\n\u001b[1;30m  ✖  TIME EXPIRED  \u001b[0m\n```"
        f"{SEP}\n"
        f"Nobody claimed the drop in time.\n"
        f"*Keep chatting — the next one is coming.*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_points(user: discord.Member | discord.User, balance: int) -> discord.Embed:
    e = _base_embed(C_POINTS)
    e.description = (
        f"```ansi\n\u001b[1;33m  ◈  BALANCE  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**{user.display_name}**\n"
        f"```\n{balance:,} pts\n```"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE EVENT  •  /leaderboard to see rankings")
    return e


async def embed_leaderboard(guild: discord.Guild, rows: list) -> discord.Embed:
    e = _base_embed(C_BOARD)
    e.description = (
        f"```ansi\n\u001b[1;37m  ◈  LEADERBOARD  ◈\u001b[0m\n```"
        f"{SEP}\n"
    )
    lines: list[str] = []
    for rank, row in enumerate(rows, start=1):
        medal  = MEDALS.get(rank, f"`#{rank:>2}`")
        member = guild.get_member(row["user_id"])
        name   = member.display_name if member else f"User {row['user_id']}"
        lines.append(f"{medal}  **{name}** — `{row['points']:,} pts`")
    e.description += "\n".join(lines) if lines else "*No entries yet.*"
    e.description += f"\n{SEP}"
    e.set_footer(text=f"SOLACE EVENT  •  Top {len(rows)} players")
    return e


def embed_set_confirm(label: str, channel: discord.TextChannel) -> discord.Embed:
    e = _base_embed(C_SET)
    e.description = (
        f"```ansi\n\u001b[1;32m  ✔  CHANNEL SET  \u001b[0m\n```"
        f"{SEP}\n"
        f"**{label}** → {channel.mention}\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_get_channel(label: str, channel_id: int | None) -> discord.Embed:
    if channel_id:
        e = _base_embed(C_SET)
        e.description = f"{SEP}\n**{label}** is set to <#{channel_id}>\n{SEP}"
    else:
        e = _base_embed(C_TIMEOUT)
        e.description = f"{SEP}\n**{label}** has not been configured yet.\n{SEP}"
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_fillblank(prompt: str, payout: int) -> discord.Embed:
    e = _base_embed(C_FILL)
    e.description = (
        f"```ansi\n\u001b[1;35m  ✏️  FILL IN THE BLANK  ✏️\u001b[0m\n```"
        f"{SEP}\n"
        f"**{prompt}**\n\n"
        f"{SEP}\n"
        f"⚡ First correct answer wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` — type your answer below"
    )
    e.set_footer(text="SOLACE EVENT  •  Fill in the Blank")
    return e


def embed_fastmath(prompt: str, payout: int) -> discord.Embed:
    e = _base_embed(C_MATH)
    e.description = (
        f"```ansi\n\u001b[1;36m  🔢  FAST MATH  🔢\u001b[0m\n```"
        f"{SEP}\n"
        f"**{prompt}**\n\n"
        f"{SEP}\n"
        f"⚡ First correct answer wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` — type the number below"
    )
    e.set_footer(text="SOLACE EVENT  •  Fast Math")
    return e


def embed_truefalse(statement: str, payout: int) -> discord.Embed:
    e = _base_embed(C_TF)
    e.description = (
        f"```ansi\n\u001b[1;32m  🟢  TRUE OR FALSE  🟢\u001b[0m\n```"
        f"{SEP}\n"
        f"**{statement}**\n\n"
        f"{SEP}\n"
        f"⚡ First correct click wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` — click a button below"
    )
    e.set_footer(text="SOLACE EVENT  •  True or False")
    return e


def embed_truefalse_result(winner: discord.Member | discord.User | None,
                           statement: str, correct: bool, fact: str,
                           payout: int, new_total: int) -> discord.Embed:
    e = _base_embed(C_WIN if winner else C_TIMEOUT)
    answer_str = "✅ TRUE" if correct else "❌ FALSE"
    if winner:
        e.description = (
            f"```ansi\n\u001b[1;33m  ✔  CORRECT  \u001b[0m\n```"
            f"{SEP}\n"
            f"{winner.mention} got it right!\n\n"
            f"The answer was **{answer_str}**\n"
            f"*{fact}*\n\n"
            f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
            f"{SEP}"
        )
        if hasattr(winner, 'display_avatar'):
            e.set_thumbnail(url=winner.display_avatar.url)
    else:
        e.description = (
            f"```ansi\n\u001b[1;30m  ✖  TIME EXPIRED  \u001b[0m\n```"
            f"{SEP}\n"
            f"Nobody answered in time!\n\n"
            f"The answer was **{answer_str}**\n"
            f"*{fact}*\n"
            f"{SEP}"
        )
    e.set_footer(text="SOLACE EVENT  •  True or False")
    return e


# ──────────────────────────── utilities ───────────────────────────

def scramble_word(word: str) -> str:
    letters = list(word)
    for _ in range(100):
        random.shuffle(letters)
        if "".join(letters) != word:
            return "".join(letters)
    letters[0], letters[1] = letters[1], letters[0]
    return "".join(letters)


# ─────────── Blackjack helpers ────────────────────────────────────

CARD_SUITS  = ["♠", "♥", "♦", "♣"]
CARD_RANKS  = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def _new_deck() -> list[str]:
    """Return a freshly shuffled single-deck list of card strings."""
    deck = [f"{r}{s}" for s in CARD_SUITS for r in CARD_RANKS]
    random.shuffle(deck)
    return deck

def _card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11          # Aces start as 11; soft-ace logic applied in hand_value()
    return int(rank)

def _hand_value(cards: list[str]) -> int:
    total = 0
    aces  = 0
    for card in cards:
        rank = card[:-1]   # strip the suit character(s) — handles "10♠" etc.
        if rank == "A":
            aces += 1
        total += _card_value(rank)
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total

def _fmt_hand(cards: list[str], hide_second: bool = False) -> str:
    if hide_second and len(cards) > 1:
        return f"`{cards[0]}`  `??`"
    return "  ".join(f"`{c}`" for c in cards)

def _dealer_play(deck: list[str], hand: list[str]) -> list[str]:
    """Dealer draws until 17+."""
    while _hand_value(hand) < 17:
        hand.append(deck.pop())
    return hand

def embed_blackjack(chat_hand: list[str], house_hand: list[str], phase: str, payout: int,
                    clicks: int, needed: int) -> discord.Embed:
    chat_total  = _hand_value(chat_hand)
    house_shown = _hand_value([house_hand[0]])   # only the visible card
    e = _base_embed(C_BLACKJACK)
    hide = phase == "playing"
    status_line = ""
    if phase == "playing":
        status_line = (
            f"\n🗳️ **Community vote:** `{clicks}/{needed}` clicks recorded\n"
            f"*(majority of next {needed} clicks decides — Hit or Stand)*"
        )
    elif phase == "chat_bust":
        status_line = "\n💥 **Chat busted!** House wins."
    elif phase == "house_bust":
        status_line = "\n🎉 **House busted!** Chat wins!"
    elif phase == "chat_win":
        status_line = "\n🏆 **Chat wins!**"
    elif phase == "house_win":
        status_line = "\n🃏 **House wins!**"
    elif phase == "push":
        status_line = "\n🤝 **Push — it's a tie!**"

    e.description = (
        f"```ansi\n\u001b[1;32m  ♠  BLACKJACK DUEL  ♠\u001b[0m\n```"
        f"{SEP}\n"
        f"🏠 **The House** — {_fmt_hand(house_hand, hide_second=hide)}"
        f"  *(showing {house_shown})*\n\n"
        f"💬 **The Chat** — {_fmt_hand(chat_hand)}"
        f"  *(total {chat_total})*\n"
        f"{status_line}\n"
        f"{SEP}\n"
        f"💰 Win pool: **`{payout} pts`** split among participants"
    )
    e.set_footer(text="SOLACE EVENT  •  Blackjack Duel")
    return e


def embed_potato(holder: discord.Member | discord.User, seconds_left: int) -> discord.Embed:
    e = _base_embed(C_POTATO)
    e.description = (
        f"```ansi\n\u001b[1;33m  🥔  HOT POTATO  🥔\u001b[0m\n```"
        f"{SEP}\n"
        f"🔥 **{holder.mention}** is holding the Hot Potato!\n\n"
        f"Type **`!pass @username`** within `{seconds_left}s` to pass it!\n"
        f"⚠️ *When the timer hits 0 — BOOM or GOLD.*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Hot Potato")
    return e


def embed_potato_explode_bomb(holder: discord.Member | discord.User) -> discord.Embed:
    e = _base_embed(0xFF0000)
    e.description = (
        f"```ansi\n\u001b[1;31m  💣  BOOM!  💣\u001b[0m\n```"
        f"{SEP}\n"
        f"💥 The potato **EXPLODED** on {holder.mention}!\n\n"
        f"**-200 pts** deducted from their balance.\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Hot Potato — Bomb!")
    return e


def embed_potato_explode_gold(holder: discord.Member | discord.User) -> discord.Embed:
    e = _base_embed(C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  🎁  GOLDEN LOOT SACK!  🎁\u001b[0m\n```"
        f"{SEP}\n"
        f"🏆 The potato turned into a **Golden Loot Sack** for {holder.mention}!\n\n"
        f"**+500 pts** added to their balance!\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Hot Potato — Golden Sack!")
    return e


# ─────────────────────────── UI Views ────────────���────────��───────

class BlackjackView(discord.ui.View):
    """
    Community Blackjack Duel.
    Collects the first VOTE_THRESHOLD total button clicks from any users
    in the channel to decide Hit vs Stand, then resolves dealer logic.
    """
    VOTE_THRESHOLD = 3   # clicks needed to lock in a decision

    def __init__(self, cog: "ServerDropsEconomy", channel_id: int,
                 deck: list[str], chat_hand: list[str], house_hand: list[str],
                 payout: int, msg: discord.Message):
        super().__init__(timeout=None)
        self.cog         = cog
        self.channel_id  = channel_id
        self.deck        = deck
        self.chat_hand   = chat_hand
        self.house_hand  = house_hand
        self.payout      = payout
        self.msg         = msg
        self.resolved    = False
        self.participants: set[int] = set()   # user_ids who clicked anything
        self._hit_clicks  = 0
        self._stand_clicks = 0

    def _total_clicks(self) -> int:
        return self._hit_clicks + self._stand_clicks

    async def _record_click(self, interaction: discord.Interaction, action: str):
        if self.resolved:
            await interaction.response.send_message("This round has already ended.", ephemeral=True)
            return

        self.participants.add(interaction.user.id)
        if action == "hit":
            self._hit_clicks += 1
        else:
            self._stand_clicks += 1

        total = self._total_clicks()
        needed = self.VOTE_THRESHOLD

        await interaction.response.send_message(
            f"🗳️ Your **{'Hit' if action == 'hit' else 'Stand'}** vote registered! "
            f"(`{total}/{needed}` clicks so far)",
            ephemeral=True,
        )

        if total >= needed:
            await self._resolve()

    async def _resolve(self):
        if self.resolved:
            return
        self.resolved = True

        # Disable buttons immediately
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await self.msg.edit(view=self)
        except discord.HTTPException:
            pass

        channel = self.cog.bot.get_channel(self.channel_id)
        if channel is None:
            return

        # Majority rules
        action = "hit" if self._hit_clicks >= self._stand_clicks else "stand"

        if action == "hit":
            self.chat_hand.append(self.deck.pop())
            chat_val = _hand_value(self.chat_hand)

            if chat_val > 21:
                # Chat busted
                phase = "chat_bust"
                await self.msg.edit(embed=embed_blackjack(
                    self.chat_hand, self.house_hand, phase, self.payout,
                    self._total_clicks(), self.VOTE_THRESHOLD))
                await channel.send("💥 The Chat **busted**! Better luck next time.")
                self.cog.active_drops.pop(self.channel_id, None)
                self.stop()
                return

            # After a hit that didn't bust, automatically stand (simplified flow)
            # This avoids infinite vote loops; a new round of votes could be added here.

        # Stand / post-hit — dealer plays out
        self.house_hand = _dealer_play(self.deck, self.house_hand)
        chat_val  = _hand_value(self.chat_hand)
        house_val = _hand_value(self.house_hand)

        if house_val > 21:
            phase = "house_bust"
            outcome = "win"
        elif chat_val > house_val:
            phase = "chat_win"
            outcome = "win"
        elif chat_val < house_val:
            phase = "house_win"
            outcome = "lose"
        else:
            phase = "push"
            outcome = "push"

        await self.msg.edit(embed=embed_blackjack(
            self.chat_hand, self.house_hand, phase, self.payout,
            self._total_clicks(), self.VOTE_THRESHOLD))

        self.cog.active_drops.pop(self.channel_id, None)

        if outcome == "win" and self.participants:
            share = max(1, self.payout // len(self.participants))
            lines = []
            for uid in self.participants:
                new_total = self.cog._award_win(uid, share)
                member = channel.guild.get_member(uid)  # type: ignore[union-attr]
                name   = member.mention if member else f"<@{uid}>"
                lines.append(f"{name} **+{share} pts** → `{new_total} pts`")
            await channel.send(
                f"🏆 **Chat wins the Blackjack Duel!**\n"
                f"The `{self.payout} pt` pool is split `{share} pts` each:\n"
                + "\n".join(lines)
            )
        elif outcome == "push":
            await channel.send("🤝 **Push — nobody wins or loses this round.**")
        else:
            await channel.send("🃏 **House wins!** The chat couldn't beat the dealer.")

        # Cancel the timeout task
        drop = self.cog.active_drops.get(self.channel_id)
        if drop:
            task: asyncio.Task | None = drop.get("task")
            if task and not task.done():
                task.cancel()
        self.stop()

    @discord.ui.button(label="  🟢 Hit  ", style=discord.ButtonStyle.success)
    async def btn_hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record_click(interaction, "hit")

    @discord.ui.button(label="  🔴 Stand  ", style=discord.ButtonStyle.danger)
    async def btn_stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record_click(interaction, "stand")


class LootboxView(discord.ui.View):
    def __init__(self, cog: "ServerDropsEconomy", channel_id: int):
        super().__init__(timeout=None)
        self.cog        = cog
        self.channel_id = channel_id
        self.claimed    = False

    @discord.ui.button(label="  GRAB LOOT  ", style=discord.ButtonStyle.success, emoji="📦")
    async def grab_loot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            await interaction.response.send_message("This crate has already been claimed.", ephemeral=True)
            return

        drop = self.cog.active_drops.get(self.channel_id)
        if drop is None or drop.get("type") != "lootbox":
            await interaction.response.send_message("This drop has expired.", ephemeral=True)
            return

        self.claimed = True
        del self.cog.active_drops[self.channel_id]  # memory before DB

        payout    = drop["payout"]
        new_total = self.cog._award_win(interaction.user.id, payout)

        button.disabled = True
        button.label    = f"  Claimed by {interaction.user.display_name}  "
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=embed_win_lootbox(interaction.user, payout, new_total))

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()
        self.stop()


class BombView(discord.ui.View):
    """
    Two-phase time bomb.
    Phase 1 (armed=False): clicking deducts 5 pts (too early).
    Phase 2 (armed=True):  first click wins the payout.
    """
    def __init__(self, cog: "ServerDropsEconomy", channel_id: int, payout: int):
        super().__init__(timeout=None)
        self.cog        = cog
        self.channel_id = channel_id
        self.payout     = payout
        self.armed      = False
        self.defused    = False

    @discord.ui.button(label="  💥 DEFUSE  ", style=discord.ButtonStyle.danger)
    async def defuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.defused:
            await interaction.response.send_message("Already defused!", ephemeral=True)
            return

        if not self.armed:
            # Too early — penalise
            new_total = deduct_points(interaction.user.id, 50)
            await interaction.response.send_message(
                f"⚠️ Too early! You lost **50 pts**. Balance: `{new_total} pts`",
                ephemeral=True,
            )
            return

        # Armed — first click wins
        drop = self.cog.active_drops.get(self.channel_id)
        if drop is None or drop.get("type") != "bomb":
            await interaction.response.send_message("This drop has expired.", ephemeral=True)
            return

        self.defused = True
        del self.cog.active_drops[self.channel_id]

        new_total = self.cog._award_win(interaction.user.id, self.payout)

        button.disabled = True
        button.label    = f"  Defused by {interaction.user.display_name}  "
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"💥 **{interaction.user.mention}** defused the bomb and earned **`{self.payout} pts`**! "
            f"Balance: `{new_total} pts`"
        )

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()
        self.stop()


class TrueFalseView(discord.ui.View):
    """Button-based True/False drop. First correct click wins."""

    def __init__(self, cog: "ServerDropsEconomy", channel_id: int,
                 correct: bool, fact: str, statement: str, payout: int):
        super().__init__(timeout=None)
        self.cog        = cog
        self.channel_id = channel_id
        self.correct    = correct
        self.fact       = fact
        self.statement  = statement
        self.payout     = payout
        self.resolved   = False

    async def _attempt(self, interaction: discord.Interaction, chosen: bool):
        if self.resolved:
            await interaction.response.send_message("This round has already ended.", ephemeral=True)
            return

        drop = self.cog.active_drops.get(self.channel_id)
        if drop is None or drop.get("type") != "truefalse":
            await interaction.response.send_message("This drop has expired.", ephemeral=True)
            return

        if chosen != self.correct:
            await interaction.response.send_message(
                "❌ Wrong! Keep trying — someone else might still win.", ephemeral=True
            )
            return

        self.resolved = True
        del self.cog.active_drops[self.channel_id]

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()

        new_total = self.cog._award_win(interaction.user.id, self.payout)

        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            await interaction.response.defer()

        await interaction.followup.send(
            embed=embed_truefalse_result(
                interaction.user, self.statement, self.correct, self.fact,
                self.payout, new_total,
            )
        )
        self.stop()

    @discord.ui.button(label="  ✅  TRUE  ", style=discord.ButtonStyle.success)
    async def btn_true(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._attempt(interaction, True)

    @discord.ui.button(label="  ❌  FALSE  ", style=discord.ButtonStyle.danger)
    async def btn_false(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._attempt(interaction, False)


# ──────────────────────────── the Cog ─────────────────────────────

class ServerDropsEconomy(commands.Cog, name="ServerDropsEconomy"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.msg_counters:  dict[int, int]  = {}  # channel_id → message count
        self.active_drops:  dict[int, dict] = {}  # channel_id → drop state
        self.double_points: bool            = False  # global multiplier flag
        self.double_until:  float           = 0.0    # epoch time when multiplier expires
        self.underdog_enabled: bool         = True   # Underdog catch-up multiplier (on by default)
        self.happy_until:   float           = 0.0    # epoch time when Happy Hour ends
        # Boss attack cooldowns: {channel_id: {user_id: last_attack_epoch}}
        self._boss_cooldowns: dict[int, dict[int, float]] = {}

    # ──────────────────── helpers ──────────────────────

    def _effective_payout(self, base: int) -> int:
        import time
        if self.double_points and time.time() < self.double_until:
            return base * 2
        elif self.double_points:
            self.double_points = False  # expired
        return base

    def _underdog_mult(self, user_id: int) -> float:
        """Tiered catch-up multiplier by leaderboard rank.

        Top 3 -> 1.0x (no bonus), ranks 4-7 -> 1.5x, rank 8+ / unranked -> 1.7x.
        """
        if not self.underdog_enabled:
            return 1.0
        top = get_leaderboard(UNDERDOG_MID_MAX)  # fetch top 7
        if len(top) < 4:
            return 1.0  # too few ranked players for tiers to matter yet
        ids = [r["user_id"] for r in top]
        if user_id in ids[:UNDERDOG_LOCK_N]:        # ranks 1-3 (podium)
            return 1.0
        if user_id in ids[UNDERDOG_LOCK_N:]:        # ranks 4-7
            return UNDERDOG_MID_MULT
        return UNDERDOG_LOW_MULT                     # rank 8 and below / unranked

    def _is_underdog(self, user_id: int) -> bool:
        """True if the user qualifies for any catch-up bonus (outside the Top 3)."""
        return self._underdog_mult(user_id) > 1.0

    def _award_win(self, user_id: int, base: int) -> int:
        """Award drop winnings, applying the tiered Underdog catch-up bonus."""
        mult = self._underdog_mult(user_id)
        amount = int(round(base * mult)) if mult > 1.0 else base
        return add_points(user_id, amount)

    def _happy_active(self) -> bool:
        """True while a Mega-Drop Happy Hour window is running."""
        return time.time() < self.happy_until

    def _pick_drop(self) -> str:
        """Pick a drop type, doubling rare drops during Happy Hour."""
        if self._happy_active():
            return random.choice(list(EVENT_POOL) + [d for d in EVENT_POOL if d in RARE_DROPS])
        return random.choice(EVENT_POOL)

    # ──────────────────── message listener ────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.guild is None:
            return

        # Master switch: if drops are paused server-wide, fire nothing.
        if get_drops_paused(message.guild.id):
            return

        # Channel gating: all-channels mode (minus blacklist) or the single configured channel.
        if get_all_channels_mode(message.guild.id):
            if message.channel.id in get_disabled_channels(message.guild.id):
                return
        else:
            drops_id = get_drops_channel(message.guild.id)
            if drops_id is None or message.channel.id != drops_id:
                return

        cid = message.channel.id

        # ── Route to active drop if one is running ──
        if cid in self.active_drops:
            drop = self.active_drops[cid]
            dtype = drop.get("type")
            if dtype in ("trivia", "scramble", "emoji", "bounty", "fillblank", "fastmath", "heist"):
                await self._check_text_answer(message, drop)
            elif dtype == "hotcold":
                await self._check_number_guess(message, drop)
            elif dtype == "boss":
                await self._handle_boss_attack(message, drop)
            elif dtype == "worldboss":
                await self._handle_worldboss_attack(message, drop)
            elif dtype == "hotpotato":
                await self._handle_potato_pass(message, drop)
            elif dtype == "losers":
                await self._handle_losers_answer(message, drop)
            # truefalse is button-only; no text routing needed
            return

        # ── Increment counter ──
        self.msg_counters[cid] = self.msg_counters.get(cid, 0) + 1
        trigger = get_drop_trigger(message.guild.id)
        if self._happy_active():
            trigger = max(3, trigger // 2)  # Mega-Drop Happy Hour: drops fire twice as fast
        if self.msg_counters[cid] >= trigger:
            self.msg_counters[cid] = 0
            await self._trigger_drop(message.channel)

    # ──────────────────── drop triggering ─────────────

    async def _trigger_drop(self, channel: discord.TextChannel):
        # Ping the configured drops role (if set) right before a drop fires.
        if channel.guild is not None:
            ping_role_id = get_ping_role(channel.guild.id)
            if ping_role_id:
                role = channel.guild.get_role(ping_role_id)
                if role is not None:
                    try:
                        await channel.send(
                            f"{role.mention} 🎉 **A drop is landing — get ready!**",
                            allowed_mentions=discord.AllowedMentions(roles=True),
                        )
                    except discord.HTTPException:
                        pass
        choice = self._pick_drop()
        dispatch = {
            "trivia":     self._start_trivia,
            "scramble":   self._start_scramble,
            "lootbox":    self._start_lootbox,
            "boss":       self._start_boss,
            "hotcold":    self._start_hotcold,
            "emoji":      self._start_emoji,
            "bomb":       self._start_bomb,
            "multi":      self._start_multiplier,
            "blackjack":  self._start_blackjack,
            "hotpotato":  self._start_hotpotato,
            "fillblank":  self._start_fillblank,
            "fastmath":   self._start_fastmath,
            "truefalse":  self._start_truefalse,
            "worldboss":  self._start_worldboss,
            "heist":      self._start_heist,
            "losers":     self._start_losers,
        }
        await dispatch[choice](channel)

    # ─────────────── Trivia ───────────────────────────

    async def _start_trivia(self, channel: discord.TextChannel):
        question, answers = random.choice(list(TRIVIA_BANK.items()))
        payout = self._effective_payout(random.randint(50, 150))
        await channel.send(embed=embed_trivia(question, payout))
        state = {"type": "trivia", "answer": [a.lower().strip() for a in answers], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Scramble ─────────────────────────

    async def _start_scramble(self, channel: discord.TextChannel):
        word      = random.choice(SCRAMBLE_WORDS)
        scrambled = scramble_word(word)
        payout    = self._effective_payout(random.randint(50, 150))
        await channel.send(embed=embed_scramble(scrambled, payout))
        state = {"type": "scramble", "answer": [word.lower().strip()], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Lootbox ──────────────────────────

    async def _start_lootbox(self, channel: discord.TextChannel):
        payout = self._effective_payout(random.randint(150, 300))
        view   = LootboxView(cog=self, channel_id=channel.id)
        msg    = await channel.send(embed=embed_lootbox("150–300"), view=view)
        state  = {"type": "lootbox", "payout": payout, "view": view, "msg": msg, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Co-Op Boss Raid ──────────────────

    async def _start_boss(self, channel: discord.TextChannel):
        max_hp = 50
        state = {
            "type":         "boss",
            "hp":           max_hp,
            "max_hp":       max_hp,
            "attackers":    {},     # user_id → total damage dealt
            "task":         None,
        }
        self.active_drops[channel.id] = state
        self._boss_cooldowns[channel.id] = {}
        msg = await channel.send(embed=embed_boss(max_hp, max_hp, {}))
        state["msg"] = msg
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _handle_boss_attack(self, message: discord.Message, drop: dict):
        if message.content.strip().lower() != "!attack":
            return

        import time
        cid     = message.channel.id
        uid     = message.author.id
        now     = time.time()
        cds     = self._boss_cooldowns.get(cid, {})
        last_at = cds.get(uid, 0.0)

        if now - last_at < 3.0:
            remaining = 3.0 - (now - last_at)
            await message.channel.send(
                f"⏱️ {message.author.mention} cooldown! Wait `{remaining:.1f}s`.",
                delete_after=2,
            )
            return

        cds[uid] = now
        damage = random.randint(1, 5)
        drop["hp"] = max(0, drop["hp"] - damage)
        drop["attackers"][uid] = drop["attackers"].get(uid, 0) + damage

        await message.add_reaction("⚔️")

        # Update the boss embed
        try:
            await drop["msg"].edit(
                embed=embed_boss(drop["hp"], drop["max_hp"], drop["attackers"])
            )
        except discord.HTTPException:
            pass

        if drop["hp"] <= 0:
            # Boss defeated
            task: asyncio.Task | None = drop.get("task")
            if task and not task.done():
                task.cancel()

            del self.active_drops[cid]
            self._boss_cooldowns.pop(cid, None)

            attackers = drop["attackers"]
            total_pool = 1000
            payout_each = max(1, total_pool // max(len(attackers), 1))
            payout_each = self._effective_payout(payout_each)

            await message.channel.send(embed=embed_boss_dead(attackers, payout_each))

            for raider_id in attackers:
                self._award_win(raider_id, payout_each)

    # \u2500\u2500\u2500\u2500\u2500 World Boss Raid (catch-up event) \u2500\u2500
    async def _handle_worldboss_attack(self, message: discord.Message, drop: dict):
        if message.content.strip().lower() != "!attack":
            return
        cid = message.channel.id
        uid = message.author.id
        now = time.time()
        cds = self._boss_cooldowns.setdefault(cid, {})
        last_at = cds.get(uid, 0.0)
        if now - last_at < 3.0:
            remaining = 3.0 - (now - last_at)
            await message.channel.send(
                f"\u23f1\ufe0f {message.author.mention} cooldown! Wait `{remaining:.1f}s`.",
                delete_after=2,
            )
            return
        cds[uid] = now
        base_dmg = random.randint(8, 16)
        boss_mult = self._underdog_mult(uid)
        if boss_mult > 1.0:
            base_dmg = int(round(base_dmg * boss_mult))  # lower-ranked players hit harder
        drop["hp"] = max(0, drop["hp"] - base_dmg)
        drop["attackers"][uid] = drop["attackers"].get(uid, 0) + base_dmg
        try:
            await message.add_reaction("\U0001f4a5")
        except discord.HTTPException:
            pass
        drop["_hits"] = drop.get("_hits", 0) + 1
        # Throttle embed edits (every 3 hits or on kill) to avoid rate limits
        if drop["hp"] <= 0 or drop["_hits"] % 3 == 0:
            try:
                await drop["msg"].edit(
                    embed=embed_worldboss(
                        drop["name"], drop["hp"], drop["max_hp"], drop["attackers"], drop["mins"], drop.get("pool", WORLDBOSS_POOL)
                    )
                )
            except discord.HTTPException:
                pass
        if drop["hp"] <= 0:
            await self._finish_worldboss(message.channel, defeated=True)

    async def _finish_worldboss(self, channel: discord.TextChannel, defeated: bool):
        cid = channel.id
        drop = self.active_drops.get(cid)
        if drop is None or drop.get("type") != "worldboss":
            return
        task = drop.get("task")
        if task and not task.done():
            task.cancel()
        self.active_drops.pop(cid, None)
        self._boss_cooldowns.pop(cid, None)
        attackers = drop["attackers"]
        max_hp = drop["max_hp"]
        dmg_done = sum(attackers.values())
        pool_total = drop.get("pool", WORLDBOSS_POOL)
        if defeated:
            pool = pool_total
        else:
            pool = int(pool_total * min(1.0, dmg_done / max_hp)) if max_hp else 0
        total_dmg = dmg_done or 1
        for uid, dmg in attackers.items():
            share = int(pool * dmg / total_dmg)
            if share > 0:
                add_points(uid, share)  # Underdog edge already baked into damage share
        await channel.send(embed=embed_worldboss_dead(drop["name"], attackers, pool, defeated))

    async def _worldboss_timeout(self, channel: discord.TextChannel, seconds: int):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        await self._finish_worldboss(channel, defeated=False)

    # ─────────────── Hot or Cold ──────────────────────

    async def _start_hotcold(self, channel: discord.TextChannel):
        secret = random.randint(1, 100)
        payout = self._effective_payout(random.randint(100, 200))
        msg    = await channel.send(embed=embed_hotcold())
        state  = {
            "type":   "hotcold",
            "secret": secret,
            "payout": payout,
            "msg":    msg,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _check_number_guess(self, message: discord.Message, drop: dict):
        try:
            guess = int(message.content.strip())
        except ValueError:
            return

        secret = drop["secret"]
        cid    = message.channel.id

        if guess == secret:
            del self.active_drops[cid]
            task: asyncio.Task | None = drop.get("task")
            if task and not task.done():
                task.cancel()

            payout    = drop["payout"]
            new_total = self._award_win(message.author.id, payout)
            await message.channel.send(
                f"🎯 **{message.author.mention}** guessed it! The number was **{secret}**!\n"
                f"**＋{payout} pts** earned  �����  Balance: `{new_total} pts`"
            )
        elif guess < secret:
            await message.add_reaction("👆")   # Higher
            try:
                await drop["msg"].edit(embed=embed_hotcold("Higher 👆"))
            except discord.HTTPException:
                pass
        else:
            await message.add_reaction("👇")   # Lower
            try:
                await drop["msg"].edit(embed=embed_hotcold("Lower 👇"))
            except discord.HTTPException:
                pass

    # ─────────────── Emoji Puzzle ─────────────────────

    async def _start_emoji(self, channel: discord.TextChannel):
        emojis, answer = random.choice(EMOJI_PUZZLES)
        payout = self._effective_payout(random.randint(100, 200))
        await channel.send(embed=embed_emoji_puzzle(emojis, payout))
        state = {
            "type":   "emoji",
            "answer": [answer.lower().strip()],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Reaction Time Bomb ───────────────

    async def _start_bomb(self, channel: discord.TextChannel):
        payout = self._effective_payout(random.randint(200, 300))
        view   = BombView(cog=self, channel_id=channel.id, payout=payout)
        msg    = await channel.send(embed=embed_bomb_waiting(), view=view)
        state  = {"type": "bomb", "payout": payout, "view": view, "msg": msg, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._bomb_sequence(channel, view, msg, state))

    async def _bomb_sequence(self, channel: discord.TextChannel, view: BombView, msg: discord.Message, state: dict):
        """Wait a random delay, then arm the bomb, then impose a timeout."""
        fuse_delay = random.uniform(3, 10)
        await asyncio.sleep(fuse_delay)

        if channel.id not in self.active_drops:
            return  # Already resolved

        view.armed = True
        try:
            await msg.edit(embed=embed_bomb_active(state["payout"]), view=view)
        except discord.HTTPException:
            pass

        # Now run the 30-second timeout from when the bomb went live
        await asyncio.sleep(DROP_TIMEOUT)
        drop = self.active_drops.pop(channel.id, None)
        if drop is None:
            return
        view.stop()
        await channel.send(embed=embed_timeout())

    # ─────────────── Blackjack Duel ───────────────────

    async def _start_blackjack(self, channel: discord.TextChannel):
        deck      = _new_deck()
        # Deal: chat gets 2 open cards; house gets 2 (one hidden)
        chat_hand  = [deck.pop(), deck.pop()]
        house_hand = [deck.pop(), deck.pop()]
        payout     = self._effective_payout(500)   # fixed 500-pt pool

        # Send a placeholder message first so we have the object for BlackjackView
        placeholder = await channel.send("🃏 *Dealing cards…*")
        view = BlackjackView(
            cog=self, channel_id=channel.id,
            deck=deck, chat_hand=chat_hand, house_hand=house_hand,
            payout=payout, msg=placeholder,
        )
        embed = embed_blackjack(chat_hand, house_hand, "playing", payout,
                                0, BlackjackView.VOTE_THRESHOLD)
        await placeholder.edit(content=None, embed=embed, view=view)

        state = {"type": "blackjack", "view": view, "msg": placeholder, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._blackjack_timeout(channel, view))

    async def _blackjack_timeout(self, channel: discord.TextChannel, view: BlackjackView):
        await asyncio.sleep(DROP_TIMEOUT)
        if view.resolved:
            return
        view.resolved = True
        # Disable buttons
        for child in view.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await view.msg.edit(view=view)
        except discord.HTTPException:
            pass
        self.active_drops.pop(channel.id, None)
        await channel.send(embed=embed_timeout())
        view.stop()

    # ─────────────── Hot Potato / Loot Sack ───────────

    async def _start_hotpotato(self, channel: discord.TextChannel):
        # Pick a random recent chatter as the first holder
        # We pull from the guild's member cache filtered to online/recently active
        members = [m for m in channel.members if not m.bot and m.status != discord.Status.offline]
        if not members:
            members = [m for m in channel.members if not m.bot]
        if not members:
            return   # No valid users — skip this drop

        holder = random.choice(members)
        explode_at = asyncio.get_event_loop().time() + random.uniform(20, 30)

        state = {
            "type":       "hotpotato",
            "holder_id":  holder.id,
            "explode_at": explode_at,
            "task":       None,
        }
        self.active_drops[channel.id] = state

        await channel.send(
            f"🥔 **HOT POTATO!** {holder.mention} just caught the potato!\n"
            f"Type **`!pass @username`** within **10 seconds** to toss it!",
            embed=embed_potato(holder, 10),
        )
        state["task"] = asyncio.create_task(self._potato_timer(channel, state))

    async def _potato_timer(self, channel: discord.TextChannel, state: dict):
        """Runs until the hidden explode_at deadline, then resolves."""
        loop      = asyncio.get_event_loop()
        remaining = state["explode_at"] - loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

        drop = self.active_drops.pop(channel.id, None)
        if drop is None:
            return   # Already resolved (e.g. another coroutine popped it)

        holder_id = drop["holder_id"]
        member    = channel.guild.get_member(holder_id)
        if member is None:
            await channel.send("🥔 The potato disappeared — nobody around to hold it!")
            return

        # 50/50 coin flip
        if random.random() < 0.5:
            # BOMB
            new_total = deduct_points(holder_id, 200)
            await channel.send(
                embed=embed_potato_explode_bomb(member),
            )
            await channel.send(
                f"💣 {member.mention} **lost 200 pts**!  Balance: `{new_total} pts`"
            )
        else:
            # GOLDEN LOOT SACK
            new_total = self._award_win(holder_id, 500)
            await channel.send(
                embed=embed_potato_explode_gold(member),
            )
            await channel.send(
                f"🎁 {member.mention} **gained 500 pts**!  Balance: `{new_total} pts`"
            )

    async def _handle_potato_pass(self, message: discord.Message, drop: dict):
        """Called from on_message when a hot potato drop is active."""
        content = message.content.strip()
        if not content.lower().startswith("!pass"):
            return
        if message.author.id != drop["holder_id"]:
            return   # Only the current holder can pass

        # Parse target mention
        if not message.mentions:
            await message.channel.send(
                f"⚠️ {message.author.mention}, use `!pass @username` to pass the potato.",
                delete_after=5,
            )
            return

        new_holder = message.mentions[0]
        if new_holder.bot:
            await message.channel.send("🤖 You can't pass the potato to a bot!", delete_after=5)
            return
        if new_holder.id == message.author.id:
            await message.channel.send("🙃 You can't pass it to yourself!", delete_after=5)
            return

        # Transfer
        drop["holder_id"] = new_holder.id
        # Reset the 10-second personal pass window (the explode timer keeps running)
        loop      = asyncio.get_event_loop()
        remaining = max(0, drop["explode_at"] - loop.time())
        secs_show = min(10, int(remaining))

        await message.channel.send(
            f"🥔 {message.author.mention} **passed** the potato to {new_holder.mention}!\n"
            f"Type **`!pass @username`** within **{secs_show}s** to toss it!",
            embed=embed_potato(new_holder, secs_show),
        )

    # ─────────────── Multiplier / Bounty ──────────────

    async def _start_multiplier(self, channel: discord.TextChannel):
        import time

        # 60% chance double points, 40% chance bounty
        if random.random() < 0.6:
            # Double points for 5 minutes
            self.double_points = True
            self.double_until  = time.time() + (5 * 60)
            await channel.send(embed=embed_multiplier(5))
            # No active drop state needed — just a passive buff
            # Still register a dummy drop so the counter is blocked, then clear it
            state = {"type": "multi_buff", "task": None}
            self.active_drops[channel.id] = state
            state["task"] = asyncio.create_task(self._multi_buff_clear(channel))
        else:
            # Bounty on the richest user
            top = get_top_user()
            if top is None:
                # Fallback to double points if no users exist
                self.double_points = True
                self.double_until  = time.time() + (5 * 60)
                await channel.send(embed=embed_multiplier(5))
                state = {"type": "multi_buff", "task": None}
                self.active_drops[channel.id] = state
                state["task"] = asyncio.create_task(self._multi_buff_clear(channel))
                return

            target_id, target_pts = top
            bounty_amount = min(250, target_pts)  # Cap at their actual balance
            emojis, answer = random.choice(EMOJI_PUZZLES)

            try:
                target_member = channel.guild.get_member(target_id)
                target_name   = target_member.display_name if target_member else f"User {target_id}"
            except Exception:
                target_name = f"User {target_id}"

            await channel.send(embed=embed_bounty(target_name, bounty_amount, emojis))
            state = {
                "type":      "bounty",
                "answer":    [answer.lower().strip()],
                "payout":    bounty_amount,
                "target_id": target_id,
                "task":      None,
            }
            self.active_drops[channel.id] = state
            state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _multi_buff_clear(self, channel: discord.TextChannel):
        """Remove the dummy drop state after a brief delay so messages resume."""
        await asyncio.sleep(3)
        self.active_drops.pop(channel.id, None)

    async def _start_heist(self, channel: discord.TextChannel):
        top = get_top_user()
        if top is None:
            await self._start_trivia(channel)
            return
        target_id, target_pts = top
        if target_pts <= 0:
            await self._start_trivia(channel)
            return
        steal = max(50, min(500, int(target_pts * 0.15)))
        a = random.randint(20, 99)
        b = random.randint(20, 99)
        try:
            m = channel.guild.get_member(target_id) if channel.guild else None
            target_name = m.display_name if m else f"User {target_id}"
        except Exception:
            target_name = f"User {target_id}"
        await channel.send(embed=embed_heist(target_name, steal, a, b))
        state = {
            "type":      "heist",
            "answer":    [str(a + b)],
            "payout":    steal,
            "target_id": target_id,
            "task":      None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _start_losers(self, channel: discord.TextChannel):
        question, answers = random.choice(list(TRIVIA_BANK.items()))
        payout = self._effective_payout(random.randint(120, 220))
        blocked = {r["user_id"] for r in get_leaderboard(UNDERDOG_LOCK_N)}
        await channel.send(embed=embed_losers(question, payout))
        state = {
            "type":    "losers",
            "answer":  [ans.lower().strip() for ans in answers],
            "payout":  payout,
            "blocked": blocked,
            "task":    None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _handle_losers_answer(self, message: discord.Message, drop: dict):
        if message.content.lower().strip() not in drop["answer"]:
            return
        uid = message.author.id
        if uid in drop["blocked"]:
            await message.channel.send(
                f"\U0001f512 {message.author.mention} the **Losers Bracket** is underdogs only \u2014 Top {UNDERDOG_LOCK_N} can't claim this one!",
                delete_after=4,
            )
            return
        cid = message.channel.id
        del self.active_drops[cid]
        task = drop.get("task")
        if task and not task.done():
            task.cancel()
        payout = drop["payout"]
        new_total = self._award_win(uid, payout)
        await message.channel.send(
            embed=embed_win_text(message.author, payout, new_total, "losers")
        )

    async def _start_worldboss(self, channel: discord.TextChannel):
        max_hp = random.randint(120, 220)
        mins   = 2
        pool   = random.randint(800, 1500)
        name   = random.choice(WORLDBOSS_NAMES)
        state = {
            "type": "worldboss", "name": name,
            "hp": max_hp, "max_hp": max_hp, "mins": mins,
            "pool": pool, "attackers": {}, "task": None, "_hits": 0,
        }
        self.active_drops[channel.id] = state
        self._boss_cooldowns[channel.id] = {}
        msg = await channel.send(embed=embed_worldboss(name, max_hp, max_hp, {}, mins, pool))
        state["msg"] = msg
        state["task"] = asyncio.create_task(self._worldboss_timeout(channel, mins * 60))

    # ─────────────── Fill in the Blank ────────────────

    async def _start_fillblank(self, channel: discord.TextChannel):
        entry  = random.choice(FILL_BLANK_BANK)
        payout = self._effective_payout(random.randint(80, 180))
        await channel.send(embed=embed_fillblank(entry["prompt"], payout))
        state = {
            "type":   "fillblank",
            "answer": [a.lower().strip() for a in entry["answer"]],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Fast Math ────────────────────────

    async def _start_fastmath(self, channel: discord.TextChannel):
        entry  = random.choice(MATH_BANK)
        payout = self._effective_payout(random.randint(100, 200))
        await channel.send(embed=embed_fastmath(entry["prompt"], payout))
        state = {
            "type":   "fastmath",
            "answer": [a.lower().strip() for a in entry["answer"]],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── True or False ────────────────────

    async def _start_truefalse(self, channel: discord.TextChannel):
        entry   = random.choice(TRUE_FALSE_BANK)
        payout  = self._effective_payout(random.randint(80, 180))
        view    = TrueFalseView(
            cog=self,
            channel_id=channel.id,
            correct=entry["answer"],
            fact=entry["fact"],
            statement=entry["statement"],
            payout=payout,
        )
        msg = await channel.send(embed=embed_truefalse(entry["statement"], payout), view=view)
        state = {
            "type":      "truefalse",
            "payout":    payout,
            "view":      view,
            "msg":       msg,
            "task":      None,
            "statement": entry["statement"],
            "correct":   entry["answer"],
            "fact":      entry["fact"],
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Text answer checker ──────────────

    async def _check_text_answer(self, message: discord.Message, drop: dict):
        if message.content.lower().strip() not in drop["answer"]:
            return

        cid  = message.channel.id
        dtype = drop["type"]
        del self.active_drops[cid]  # memory before DB

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()

        payout = drop["payout"]

        # Bounty: deduct from target before awarding
        if dtype in ("bounty", "heist"):
            target_id = drop.get("target_id")
            if target_id and target_id != message.author.id:
                deduct_points(target_id, payout)

        new_total = self._award_win(message.author.id, payout)
        await message.channel.send(
            embed=embed_win_text(message.author, payout, new_total, dtype)
        )

    # ─────────────── Timeout handler ──────────────────

    async def _drop_timeout(self, channel: discord.TextChannel):
        await asyncio.sleep(DROP_TIMEOUT)
        drop = self.active_drops.pop(channel.id, None)
        if drop is None:
            return

        dtype = drop.get("type")

        if dtype == "lootbox":
            view: LootboxView = drop["view"]
            view.claimed = True
            view.stop()
            try:
                for child in view.children:
                    child.disabled = True  # type: ignore[attr-defined]
                await drop["msg"].edit(view=view)
            except discord.HTTPException:
                pass

        elif dtype == "boss":
            self._boss_cooldowns.pop(channel.id, None)

        elif dtype == "bomb":
            view_b: BombView = drop["view"]
            view_b.stop()

        elif dtype == "blackjack":
            view_bj: BlackjackView = drop["view"]
            if not view_bj.resolved:
                view_bj.resolved = True
                for child in view_bj.children:
                    child.disabled = True  # type: ignore[attr-defined]
                try:
                    await drop["msg"].edit(view=view_bj)
                except discord.HTTPException:
                    pass
                view_bj.stop()

        elif dtype == "truefalse":
            view_tf: TrueFalseView = drop["view"]
            if not view_tf.resolved:
                view_tf.resolved = True
                for child in view_tf.children:
                    child.disabled = True  # type: ignore[attr-defined]
                try:
                    await drop["msg"].edit(view=view_tf)
                except discord.HTTPException:
                    pass
                view_tf.stop()
            await channel.send(
                embed=embed_truefalse_result(
                    None,
                    drop.get("statement", ""),
                    drop.get("correct", False),
                    drop.get("fact", ""),
                    drop.get("payout", 0),
                    0,
                )
            )
            return  # skip generic embed_timeout() below

        # hotpotato has its own internal timer; nothing extra to clean up here

        await channel.send(embed=embed_timeout())

    # ─────────────── /setdrops ────────────────────────

    @app_commands.command(name="setdrops", description="Set the channel where event drops will fire (single-channel mode).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The text channel to send drops in.")
    async def setdrops(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        set_drops_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            embed=embed_set_confirm("Drops Channel", channel), ephemeral=True
        )

    @app_commands.command(name="getdrops", description="Show which channel is set for event drops.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getdrops(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_drops_channel(interaction.guild_id)
        await interaction.response.send_message(
            embed=embed_get_channel("Drops Channel", channel_id), ephemeral=True
        )

    # ─────────────── /setpingrole ─────────────────────

    @app_commands.command(name="setpingrole", description="Set a role to ping before every event drop.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The role to ping before each drop.")
    async def setpingrole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert interaction.guild_id is not None
        set_ping_role(interaction.guild_id, role.id)
        e = _base_embed(C_SET)
        e.description = (
            f"```ansi\n\u001b[1;32m  ✔  DROP PING ROLE SET  \u001b[0m\n```"
            f"{SEP}\n"
            f"{role.mention} will be pinged right before every drop.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /clearpingrole ───────────────────

    @app_commands.command(name="clearpingrole", description="Stop pinging a role before drops.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def clearpingrole(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        clear_ping_role(interaction.guild_id)
        e = _base_embed(C_TIMEOUT)
        e.description = (
            f"{SEP}\n"
            f"**Drop pings disabled.** No role will be pinged before drops.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────���────�� /points ──────────────────────────

    @commands.hybrid_command(name="points", description="Check your (or another member's) point balance.")
    @app_commands.describe(member="The member whose points you want to check.")
    async def points_command(self, ctx: commands.Context, member: discord.Member | None = None):
        target  = member or ctx.author
        balance = get_points(target.id)
        await ctx.send(embed=embed_points(target, balance))

    # ─────────────── /leaderboard ─────────────────────

    @commands.hybrid_command(name="leaderboard", description="Show the top 10 players by points.")
    async def leaderboard_command(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        rows = get_leaderboard(10)
        await ctx.send(embed=await embed_leaderboard(ctx.guild, rows))

    # ─────────────── /setdroptrigger ──────────────────

    @app_commands.command(name="setdroptrigger", description="Set how many messages trigger a drop (default: 10).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(count="Number of messages before a drop fires (min 3, max 1000).")
    async def setdroptrigger(self, interaction: discord.Interaction, count: int) -> None:
        assert interaction.guild_id is not None
        if count < 3 or count > 1000:
            await interaction.response.send_message(
                "Count must be between **3** and **1000**.", ephemeral=True
            )
            return
        set_drop_trigger(interaction.guild_id, count)
        e = _base_embed(C_SET)
        e.description = (
            f"```ansi\n\u001b[1;32m  ✔  DROP TRIGGER SET  \u001b[0m\n```"
            f"{SEP}\n"
            f"A drop will now fire every **{count} messages**.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /dropinfo ────────────────────────

    @app_commands.command(name="worldboss", description="Summon a massive co-op World Boss raid (catch-up event).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(hp="Boss health pool (default 750).", minutes="Raid duration in minutes (default 10).")
    async def worldboss(self, interaction: discord.Interaction, hp: int | None = None, minutes: int | None = None) -> None:
        assert interaction.guild_id is not None
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Run this in a text channel.", ephemeral=True)
            return
        if channel.id in self.active_drops:
            await interaction.response.send_message(
                "A drop or boss is already active in this channel \u2014 wait for it to finish.", ephemeral=True
            )
            return
        max_hp = max(50, min(hp or WORLDBOSS_HP, 100000))
        mins   = max(1, min(minutes or WORLDBOSS_MIN, 120))
        name   = random.choice(WORLDBOSS_NAMES)
        state = {
            "type": "worldboss", "name": name,
            "hp": max_hp, "max_hp": max_hp, "mins": mins,
            "pool": WORLDBOSS_POOL, "attackers": {}, "task": None, "_hits": 0,
        }
        self.active_drops[channel.id] = state
        self._boss_cooldowns[channel.id] = {}
        await interaction.response.send_message(
            f"\U0001f30b **{name}** has been summoned in {channel.mention}!", ephemeral=True
        )
        msg = await channel.send(embed=embed_worldboss(name, max_hp, max_hp, {}, mins, WORLDBOSS_POOL))
        state["msg"] = msg
        state["task"] = asyncio.create_task(self._worldboss_timeout(channel, mins * 60))

    @app_commands.command(name="happyhour", description="Start a Mega-Drop Happy Hour (faster drops + double rare odds).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(minutes="Length in minutes (default 60). Use 0 to end it early.")
    async def happyhour(self, interaction: discord.Interaction, minutes: int | None = None) -> None:
        assert interaction.guild_id is not None
        mins = HAPPY_DEFAULT_MIN if minutes is None else minutes
        e = _base_embed(C_HAPPY)
        if mins <= 0:
            self.happy_until = 0.0
            e.description = (
                f"```ansi\n\u001b[1;33m  \u25c8  HAPPY HOUR ENDED  \u25c8\u001b[0m\n```"
                f"{SEP}\n"
                f"Mega-Drop Happy Hour has been switched **off**.\n"
                f"{SEP}"
            )
            e.set_footer(text="SOLACE EVENT  \u2022  Happy Hour")
            await interaction.response.send_message(embed=e, ephemeral=True)
            return
        mins = min(mins, HAPPY_MAX_MIN)
        self.happy_until = time.time() + mins * 60
        e.description = (
            f"```ansi\n\u001b[1;33m  \u26a1  MEGA-DROP HAPPY HOUR  \u26a1\u001b[0m\n```"
            f"{SEP}\n"
            f"**Happy Hour is LIVE for `{mins}` minutes!**\n\n"
            f"\U0001f4ac Drop trigger **halved** \u2014 drops fire twice as fast\n"
            f"\U0001f3b2 Rare drops (boss, blackjack, multiplier, bomb) **2\u00d7 more likely**\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  \u2022  Happy Hour")
        await interaction.response.send_message(embed=e, ephemeral=True)
        drops_id = get_drops_channel(interaction.guild_id)
        if drops_id and interaction.guild is not None:
            ch = interaction.guild.get_channel(drops_id)
            if isinstance(ch, discord.TextChannel):
                ann = _base_embed(C_HAPPY)
                ann.description = (
                    f"```ansi\n\u001b[1;33m  \u26a1  HAPPY HOUR STARTED  \u26a1\u001b[0m\n```"
                    f"{SEP}\n"
                    f"**Drops are now twice as fast for `{mins}` minutes!**\n"
                    f"Rare events are 2\u00d7 more common \u2014 get grinding! \U0001f525\n"
                    f"{SEP}"
                )
                try:
                    await ch.send(embed=ann)
                except discord.HTTPException:
                    pass

    @app_commands.command(name="underdog", description="Toggle the tiered Underdog catch-up bonus (Top 3 none, 4-7 1.5x, 8+ 1.7x).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(enabled="Turn the Underdog multiplier on or off.")
    async def underdog(self, interaction: discord.Interaction, enabled: bool) -> None:
        self.underdog_enabled = enabled
        e = _base_embed(C_SET)
        e.description = (
            f"```ansi\n\u001b[1;32m  \u25c8  UNDERDOG MULTIPLIER  \u25c8\u001b[0m\n```"
            f"{SEP}\n"
            f"Underdog catch-up bonus is now **{'`ON`' if enabled else '`OFF`'}**.\n"
            f"Tiers: Top {UNDERDOG_LOCK_N} none  \u2022  ranks 4-{UNDERDOG_MID_MAX} {UNDERDOG_MID_MULT}x  \u2022  rank {UNDERDOG_MID_MAX + 1}+ {UNDERDOG_LOW_MULT}x.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  \u2022  Catch-Up Mechanics")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="dropinfo", description="Show current drop settings for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def dropinfo(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_drops_channel(interaction.guild_id)
        trigger    = get_drop_trigger(interaction.guild_id)
        ping_role_id = get_ping_role(interaction.guild_id)
        all_mode   = get_all_channels_mode(interaction.guild_id)
        disabled   = get_disabled_channels(interaction.guild_id)
        channel_mention = f"<#{channel_id}>" if channel_id else "*not set*"
        ping_mention    = f"<@&{ping_role_id}>" if ping_role_id else "*not set*"
        mode_str   = "`ALL channels`" if all_mode else "`single channel`"
        disabled_str = ", ".join(f"<#{c}>" for c in disabled) if disabled else "*none*"
        paused     = get_drops_paused(interaction.guild_id)
        active = len(self.active_drops)
        happy_str = (
            f"`{int((self.happy_until - time.time()) // 60) + 1}m left`"
            if self._happy_active() else "`off`"
        )
        e = _base_embed(C_POINTS)
        e.description = (
            f"```ansi\n\u001b[1;33m  ◈  DROP SETTINGS  ◈\u001b[0m\n```"
            f"{SEP}\n"
            f"⏸️ **Drops paused:** {'`YES — all off`' if paused else '`no`'}\n"
            f"🌐 **Drop mode:** {mode_str}\n"
            f"📢 **Drop Channel:** {channel_mention}\n"
            f"🚫 **Disabled channels:** {disabled_str}\n"
            f"🔔 **Drop ping role:** {ping_mention}\n"
            f"💬 **Messages to trigger:** `{trigger}`\n"
            f"🎯 **Active drops right now:** `{active}`\n"
            f"✨ **Double points:** {'`ACTIVE`' if self.double_points else '`off`'}\n"
            f"\u2696\ufe0f **Underdog (4-{UNDERDOG_MID_MAX}: {UNDERDOG_MID_MULT}x \u2022 {UNDERDOG_MID_MAX + 1}+: {UNDERDOG_LOW_MULT}x):** {'`ON`' if self.underdog_enabled else '`off`'}\n"
            f"\u26a1 **Happy Hour:** {happy_str}\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  /setdrops | /setdroptrigger")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /mystats ─────────────────────────

    @commands.hybrid_command(name="mystats", description="Show your event rank and point balance.")
    async def mystats(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        user    = ctx.author
        balance = get_points(user.id)
        rows    = get_leaderboard(200)
        rank    = next((i + 1 for i, r in enumerate(rows) if r["user_id"] == user.id), None)
        total_players = len(rows)

        e = _base_embed(C_POINTS)
        e.set_thumbnail(url=user.display_avatar.url)
        rank_str = f"**#{rank}** of {total_players}" if rank else "*unranked*"
        e.description = (
            f"```ansi\n\u001b[1;33m  ◈  MY STATS  ◈\u001b[0m\n```"
            f"{SEP}\n"
            f"**{user.display_name}**\n\n"
            f"💰 Balance:  `{balance:,} pts`\n"
            f"🏆 Rank:     {rank_str}\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  /leaderboard for full board")
        await ctx.send(embed=e)

    # ─────────────── /givepts ─────────────────────────

    @app_commands.command(name="givepts", description="Give or deduct points from a member. Use negative values to deduct.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member="The member to give or deduct points from.",
        amount="Points to add (positive) or remove (negative).",
    )
    async def givepts(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        if amount == 0:
            await interaction.response.send_message("Amount can't be zero.", ephemeral=True)
            return

        if amount > 0:
            new_total = add_points(member.id, amount)
            sign      = f"＋{amount}"
            colour    = C_SET
            action    = "awarded"
        else:
            new_total = deduct_points(member.id, abs(amount))
            sign      = f"−{abs(amount)}"
            colour    = 0xE74C3C
            action    = "deducted"

        e = discord.Embed(colour=colour)
        e.description = (
            f"```ansi\n\u001b[1;32m  ◈  POINTS {action.upper()}  ◈\u001b[0m\n```"
            f"{SEP}\n"
            f"**{member.display_name}**\n"
            f"{sign} pts  ›  New balance: `{new_total:,} pts`\n"
            f"{SEP}"
        )
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"SOLACE EVENT  •  Adjusted by {interaction.user.display_name}")
        await interaction.response.send_message(embed=e)

    # ─────────────── /allchannels ─────────────────────

    @app_commands.command(name="allchannels", description="Let event drops fire in ALL channels (exclude some with /disablechannel).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(enabled="Turn all-channels drop mode on or off.")
    async def allchannels(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild_id is not None
        set_all_channels_mode(interaction.guild_id, enabled)
        disabled = get_disabled_channels(interaction.guild_id)
        if enabled:
            excl = ", ".join(f"<#{c}>" for c in disabled) if disabled else "*none*"
            body = (
                "✅ **Drops now fire in EVERY channel.**\n"
                f"🚫 Excluded channels: {excl}\n"
                "Use `/disablechannel` to exclude more, `/enablechannel` to re-allow."
            )
            colour = C_SET
        else:
            body = (
                "🛑 **All-channels mode is OFF.**\n"
                "Drops only fire in the channel set with `/setdrops`."
            )
            colour = C_TIMEOUT
        e = _base_embed(colour)
        e.description = f"{SEP}\n{body}\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /disablechannel ──────────────────

    @app_commands.command(name="disablechannel", description="Block event drops in a specific channel (all-channels mode).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The channel where drops should NOT fire.")
    async def disablechannel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        add_disabled_channel(interaction.guild_id, channel.id)
        e = _base_embed(C_TIMEOUT)
        e.description = f"{SEP}\n🚫 Event drops are now **disabled** in {channel.mention}.\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /enablechannel ───────────────────

    @app_commands.command(name="enablechannel", description="Re-allow event drops in a previously disabled channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The channel to re-enable drops in.")
    async def enablechannel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        remove_disabled_channel(interaction.guild_id, channel.id)
        e = _base_embed(C_SET)
        e.description = f"{SEP}\n✅ Event drops are now **allowed** in {channel.mention}.\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /disableall ──────────────────────

    @app_commands.command(name="disableall", description="Disable (or re-enable) event drops in ALL channels server-wide.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(disabled="True = stop all drops everywhere, False = resume.")
    async def disableall(self, interaction: discord.Interaction, disabled: bool) -> None:
        assert interaction.guild_id is not None
        set_drops_paused(interaction.guild_id, disabled)
        if disabled:
            body = (
                "🛑 **All event drops are now DISABLED server-wide.**\n"
                "No drops will fire in any channel until you re-enable them."
            )
            colour = C_TIMEOUT
        else:
            body = (
                "✅ **Event drops re-enabled.**\n"
                "Drops will resume based on your current mode (`/dropinfo`)."
            )
            colour = C_SET
        e = _base_embed(colour)
        e.description = f"{SEP}\n{body}\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /triviarole ──────────────────────

    @app_commands.command(name="triviarole", description="Get or remove the Trivia role to be pinged for events.")
    @app_commands.guild_only()
    async def triviarole(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        role = discord.utils.get(guild.roles, name="Trivia")
        if role is None:
            me = guild.me
            if me is None or not me.guild_permissions.manage_roles:
                await interaction.response.send_message(
                    "⚠️ The **Trivia** role doesn't exist and I lack **Manage Roles** to create it. "
                    "Ask an admin to create a role named `Trivia`.",
                    ephemeral=True,
                )
                return
            try:
                role = await guild.create_role(
                    name="Trivia",
                    colour=discord.Colour(C_TRIVIA),
                    mentionable=True,
                    reason="Self-assignable Trivia event role",
                )
            except discord.HTTPException:
                await interaction.response.send_message(
                    "⚠️ I couldn't create the **Trivia** role. Please check my permissions.",
                    ephemeral=True,
                )
                return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self-removed Trivia role")
                msg = f"➖ Removed the {role.mention} role — you won't be pinged for trivia anymore."
            else:
                await member.add_roles(role, reason="Self-assigned Trivia role")
                msg = f"➕ You now have the {role.mention} role — get ready for trivia events!"
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I can't manage that role. Make sure my role is **above** the Trivia role and I have **Manage Roles**.",
                ephemeral=True,
            )
            return
        e = _base_embed(C_TRIVIA)
        e.description = f"{SEP}\n{msg}\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Trivia Role")
        await interaction.response.send_message(embed=e, ephemeral=True)


# ────────────────────────────── setup ─────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerDropsEconomy(bot))
