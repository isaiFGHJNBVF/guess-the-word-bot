import asyncio
import os
import random
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands

TOKEN_ENV_NAME = "DISCORD_BOT_TOKEN"
JOINED_ROLE_SUFFIX = "+joined"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@dataclass
class GameState:
    guild_id: int
    channel_id: int
    owner_id: int
    role_id: int
    running: bool = False
    current_player_id: Optional[int] = None
    current_word: Optional[str] = None
    scores: dict[int, int] = field(default_factory=dict)
    round_number: int = 0
    accepting_guess: bool = False


games: dict[int, GameState] = {}


def joined_role_name() -> str:
    bot_name = bot.user.name if bot.user else "bot"
    return f"{bot_name}{JOINED_ROLE_SUFFIX}"


def has_joined_role(member: discord.Member, state: GameState) -> bool:
    return any(role.id == state.role_id for role in member.roles)


async def get_or_create_joined_role(guild: discord.Guild) -> discord.Role:
    name = joined_role_name()
    existing_role = discord.utils.get(guild.roles, name=name)
    if existing_role:
        return existing_role
    return await guild.create_role(name=name, reason="Game joined role")


async def end_game_for_guild(guild: discord.Guild) -> Optional[GameState]:
    state = games.pop(guild.id, None)
    if not state:
        return None
    role = guild.get_role(state.role_id)
    if role:
        for member in list(role.members):
            try:
                await member.remove_roles(role, reason="Game ended")
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass
        try:
            await role.delete(reason="Game ended")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass
    return state


def leaderboard_text(guild: discord.Guild, state: GameState) -> str:
    if not state.scores:
        return "No one scored this game."
    ranked = sorted(state.scores.items(), key=lambda item: item[1], reverse=True)[:10]
    lines = []
    for index, (member_id, score) in enumerate(ranked, start=1):
        member = guild.get_member(member_id)
        name = member.mention if member else f"User {member_id}"
        lines.append(f"{index}. {name} — {score} point{'s' if score != 1 else ''}")
    winner_id = ranked[0][0]
    winner = guild.get_member(winner_id)
    winner_name = winner.mention if winner else f"User {winner_id}"
    return f"Winner: {winner_name}\n\nTop 10 leaderboard:\n" + "\n".join(lines)


async def start_next_round(state: GameState) -> None:
    guild = bot.get_guild(state.guild_id)
    if not guild:
        return
    channel = guild.get_channel(state.channel_id)
    role = guild.get_role(state.role_id)
    if not isinstance(channel, discord.TextChannel) or not role:
        return
    players = [member for member in role.members if not member.bot]
    if not players:
        await channel.send("No joined players are available. Use /join_game to join, then /start again.")
        state.running = False
        state.current_player_id = None
        state.current_word = None
        state.accepting_guess = False
        return
    selected = random.choice(players)
    state.round_number += 1
    state.current_player_id = selected.id
    state.current_word = None
    state.accepting_guess = False
    await channel.send(f"Round {state.round_number}: {selected.mention}, set the secret word with `/word <word>`. Everyone else, get ready to guess.")


async def interaction_game_state(interaction: discord.Interaction) -> Optional[GameState]:
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return None
    state = games.get(interaction.guild.id)
    if not state:
        await interaction.response.send_message("No game is set up yet. Use `/setup #channel` first.", ephemeral=True)
        return None
    return state


async def interaction_owner_game_state(interaction: discord.Interaction) -> Optional[GameState]:
    state = await interaction_game_state(interaction)
    if not state:
        return None
    if interaction.user.id != state.owner_id:
        await interaction.response.send_message("Only the person who used `/setup` can use this command.", ephemeral=True)
        return None
    return state


@tree.command(name="setup", description="Set up the word game in a channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="The channel where the game should run")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only server administrators can use `/setup`.", ephemeral=True)
        return
    try:
        role = await get_or_create_joined_role(interaction.guild)
    except discord.Forbidden:
        await interaction.response.send_message("I need the Manage Roles permission to create the joined role.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("I could not create the joined role. Please try again.", ephemeral=True)
        return
    games[interaction.guild.id] = GameState(
        guild_id=interaction.guild.id,
        channel_id=channel.id,
        owner_id=interaction.user.id,
        role_id=role.id,
    )
    await interaction.response.send_message(
        f"Game set up in {channel.mention}. Messages there will be removed unless the user joins with `/join_game`. Only you can use `/start` and `/end` for this game.",
        ephemeral=True,
    )
    await channel.send(f"A word game was set up here. Use `/join_game` to join. The game owner can use `/start` when ready.")


@tree.command(name="join_game", description="Join the current word game")
async def join_game(interaction: discord.Interaction) -> None:
    state = await interaction_game_state(interaction)
    if not state or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    role = interaction.guild.get_role(state.role_id)
    if not role:
        try:
            role = await get_or_create_joined_role(interaction.guild)
            state.role_id = role.id
        except discord.Forbidden:
            await interaction.response.send_message("I need the Manage Roles permission to give you the joined role.", ephemeral=True)
            return
    if has_joined_role(interaction.user, state):
        await interaction.response.send_message("You already joined this game.", ephemeral=True)
        return
    try:
        await interaction.user.add_roles(role, reason="Joined word game")
    except discord.Forbidden:
        await interaction.response.send_message("I cannot give that role. Move my bot role above the joined role and give me Manage Roles.", ephemeral=True)
        return
    state.scores.setdefault(interaction.user.id, 0)
    await interaction.response.send_message(f"You joined the game and received the {role.name} role.", ephemeral=True)


@tree.command(name="start", description="Start the word game")
async def start(interaction: discord.Interaction) -> None:
    state = await interaction_owner_game_state(interaction)
    if not state or not interaction.guild:
        return
    if state.running:
        await interaction.response.send_message("The game is already running.", ephemeral=True)
        return
    state.running = True
    state.current_word = None
    state.current_player_id = None
    state.accepting_guess = False
    await interaction.response.send_message("Game started.", ephemeral=True)
    await start_next_round(state)


@tree.command(name="word", description="Set the secret word when you are selected")
@app_commands.describe(word="The secret word everyone else should guess")
async def word(interaction: discord.Interaction, word: str) -> None:
    state = await interaction_game_state(interaction)
    if not state:
        return
    if not state.running:
        await interaction.response.send_message("The game is not running yet.", ephemeral=True)
        return
    if interaction.user.id != state.current_player_id:
        await interaction.response.send_message("Only the selected player can set the word right now.", ephemeral=True)
        return
    cleaned_word = word.strip()
    if not cleaned_word:
        await interaction.response.send_message("Please provide a real word.", ephemeral=True)
        return
    state.current_word = cleaned_word.lower()
    state.accepting_guess = True
    channel = bot.get_channel(state.channel_id)
    await interaction.response.send_message("Secret word saved. Everyone else can guess now.", ephemeral=True)
    if isinstance(channel, discord.TextChannel):
        await channel.send("The secret word is set. Start guessing in this channel.")


@tree.command(name="hint", description="Send a hint for the current secret word")
@app_commands.describe(hint="The hint to send")
async def hint(interaction: discord.Interaction, hint: str) -> None:
    state = await interaction_game_state(interaction)
    if not state:
        return
    if not state.running or not state.current_word:
        await interaction.response.send_message("There is no active word to hint yet.", ephemeral=True)
        return
    if interaction.user.id != state.current_player_id:
        await interaction.response.send_message("Only the selected word setter can send hints right now.", ephemeral=True)
        return
    channel = bot.get_channel(state.channel_id)
    await interaction.response.send_message("Hint sent.", ephemeral=True)
    if isinstance(channel, discord.TextChannel):
        await channel.send(f"Hint: {hint}")


@tree.command(name="skip", description="Skip the selected player and choose another one")
async def skip(interaction: discord.Interaction) -> None:
    state = await interaction_owner_game_state(interaction)
    if not state:
        return
    if not state.running:
        await interaction.response.send_message("The game is not running yet.", ephemeral=True)
        return
    state.current_word = None
    state.current_player_id = None
    state.accepting_guess = False
    await interaction.response.send_message("Skipped. Choosing another player now.", ephemeral=True)
    await start_next_round(state)


@tree.command(name="help", description="Show all word game commands")
async def help_command(interaction: discord.Interaction) -> None:
    help_text = (
        "**Word Game Commands**\n"
        "`/setup #channel` — set the game channel and create the joined role.\n"
        "`/join_game` — join the game and get the joined role so your messages are allowed.\n"
        "`/start` — start the game. Only the setup owner can use this.\n"
        "`/word <word>` — selected player sets the secret word privately.\n"
        "`/hint <hint>` — selected player sends a hint for the current word.\n"
        "`/skip` — setup owner skips the current selected word setter.\n"
        "`/del_chat` — setup owner deletes messages from the setup channel.\n"
        "`/end` — setup owner ends the game, shows the top 10 leaderboard, and deletes the joined role.\n\n"
        "In the setup channel, messages from people who have not used `/join_game` are deleted. "
        "Wrong guesses get ❌ and correct guesses get ✔️."
    )
    await interaction.response.send_message(help_text, ephemeral=True)


@tree.command(name="del_chat", description="Delete messages from the setup channel")
async def del_chat(interaction: discord.Interaction) -> None:
    state = await interaction_owner_game_state(interaction)
    if not state or not interaction.guild:
        return
    channel = interaction.guild.get_channel(state.channel_id)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("I could not find the setup channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await channel.purge(limit=None, reason=f"Deleted by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send("I need the Manage Messages permission to delete chat.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.followup.send("I could not delete the channel messages. Discord may block deleting very old messages in bulk.", ephemeral=True)
        return
    await interaction.followup.send(f"Deleted {len(deleted)} messages from {channel.mention}.", ephemeral=True)


@tree.command(name="end", description="End the game and show the leaderboard")
async def end(interaction: discord.Interaction) -> None:
    state = await interaction_owner_game_state(interaction)
    if not state or not interaction.guild:
        return
    text = leaderboard_text(interaction.guild, state)
    await end_game_for_guild(interaction.guild)
    await interaction.response.send_message(f"Game ended.\n\n{text}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    state = games.get(message.guild.id)
    if not state or message.channel.id != state.channel_id:
        return
    if not isinstance(message.author, discord.Member):
        return
    if not has_joined_role(message.author, state):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass
        return
    if not state.running or not state.current_word or not state.accepting_guess:
        return
    if message.author.id == state.current_player_id:
        return
    guess = message.content.strip().lower()
    if guess == state.current_word:
        state.accepting_guess = False
        state.scores[message.author.id] = state.scores.get(message.author.id, 0) + 1
        try:
            await message.add_reaction("✔️")
        except discord.HTTPException:
            pass
        await message.channel.send(f"Correct, {message.author.mention}. The word was `{state.current_word}`. Next round starts in 5 seconds.")
        current_round = state.round_number
        await asyncio.sleep(5)
        if state.running and games.get(message.guild.id) is state and state.round_number == current_round:
            await start_next_round(state)
    else:
        try:
            await message.add_reaction("❌")
        except discord.HTTPException:
            pass


@bot.event
async def on_ready() -> None:
    await tree.sync()
    print(f"Logged in as {bot.user} and synced slash commands.")


def main() -> None:
    token = os.environ.get(TOKEN_ENV_NAME)
    if not token:
        raise RuntimeError(f"Missing {TOKEN_ENV_NAME}. Add your Discord bot token as a secret before starting the bot.")
    bot.run(token)


if __name__ == "__main__":
    main()
