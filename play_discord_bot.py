#!/usr/bin/env python3
import os
import random
import sys
import time

import re, json, logging, asyncio, discord
from logging.handlers import SysLogHandler

from generator.gpt2.gpt2_generator import *
from story import grammars
from story.story_manager import *
from story.utils import *

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

# Discord
from discord.ext import commands

# gTTS
from gtts import gTTS

# bot setup
bot = commands.Bot(command_prefix='!')
CHANNEL = 'active-investigations'
ADMIN_ROLE = 'Chief'

# log setup
syslog = SysLogHandler() # sudo service rsyslog start && less +F /var/log/syslog
log_format = '%(asctime)s local dungeon_worker: %(message)s'
log_formatter = logging.Formatter(log_format, datefmt='%b %d %H:%M:%S')
syslog.setFormatter(log_formatter)
logger = logging.getLogger()
logger.addHandler(syslog)
logger.setLevel(logging.INFO)

generator = GPT2Generator()
generator.censor = False
story_manager = UnconstrainedStoryManager(generator)
queue = asyncio.Queue()
logger.info('Worker instance started')


def escape(text):
    text = re.sub(r'\\(\*|_|`|~|\\|>)', r'\g<1>', text)
    return re.sub(r'(\*|_|`|~|\\|>)', r'\\\g<1>', text)


@bot.event
async def on_ready():
    print("Bot is ready\n")
    logger.info('Bot is ready')
    loop = asyncio.get_event_loop()
    
    upload_story = True
    if story_manager.story != None:
            story_manager.story = None

    while True:
        # poll queue for messages, block here if empty
        msg = None
        while not msg: msg = await queue.get()
        logger.info(f'Processing message: {msg}'); args = json.loads(msg)
        channel, text = args['channel'], f'\n> {args["text"]}\n'
        ai_channel = bot.get_channel(channel)
        guild = ai_channel.guild
        voice_client = guild.voice_client

        if voice_client is not None and not voice_client.is_connected():
            logger.info('original voice client disconnected, finding new client...')
            for vc in guild.voice_clients:
                if vc.is_connected():
                    logger.info('new voice client found!')
                    voice_client = vc
                    break

        if story_manager.story is None:
            await ai_channel.send("Generating story...")
            result = story_manager.start_new_story(args["text"], context="", upload_story=upload_story)
            await ai_channel.send(result)
            continue

        # generate response
        try:
            async with ai_channel.typing():
                task = loop.run_in_executor(None, story_manager.act, args["text"])
                response = await asyncio.wait_for(task, 180, loop=loop)
                sent = f'> {args["text"]}\n{escape(response)}'
                # handle tts if in a voice channel
                if voice_client is not None and voice_client.is_connected():
                    await bot_read_message(voice_client, escape(response))
                await ai_channel.send(sent)
        except Exception as err:
            logger.info('Error with message: ', exc_info=True)

async def bot_read_message(voice_client, message):
    filename = 'tmp/message.mp3'
    await create_tts_mp3(filename, message)
    source = await discord.FFmpegOpusAudio.from_probe(filename, method=gtts_probe)
    voice_client.play(source)

# hack to speed up playback by hardcoding the only codec/bitrate gtts provides
def gtts_probe(source, executable):
    return "mp3", "512"

async def create_tts_mp3(filename, message):
    tts = gTTS(message, lang='en')
    tts.save(filename)

@bot.command(name='next', help='Continues AI Dungeon game')
async def game_next(ctx, *, text='continue'):
    if ctx.message.channel.name != CHANNEL:
        return

    action = text
    if action[0] == '"':
        action = "You say " + action
    else:
        action = action.strip()
        if "you" not in action[:6].lower() and "I" not in action[:6]:
            action = action[0].lower() + action[1:]
            action = "You " + action
        if action[-1] not in [".", "?", "!"]:
            action = action + "."
        action = first_to_second_person(action)
        action = "\n> " + action + "\n"

    message = {'channel': ctx.channel.id, 'text': action}
    await queue.put(json.dumps(message))


@bot.command(name='revert', help='Reverts the previous action')
async def game_revert(ctx):
    if len(story_manager.story.actions) == 0:
        await ctx.send("You can't go back any farther. ")
        return
    story_manager.story.actions = story_manager.story.actions[:-1]
    story_manager.story.results = story_manager.story.results[:-1]
    await ctx.send("Last action reverted. ")
    if len(story_manager.story.results) > 0:
        await ctx.send(story_manager.story.results[-1])
    else:
        await ctx.send(story_manager.story.story_start)


@bot.command(name='newgame', help='Starts a new game')
@commands.has_role(ADMIN_ROLE)
async def game_newgame(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    if story_manager.story == None:
        await ctx.send('Load a story with !load story_id or start a new one with !next')
        return

    # clear queue
    while not queue.empty():
        await queue.get()
    await queue.join()

    story_manager.story = None
    
    await ctx.send('\n==========\nNew game\n==========\nProvide intial prompt with !next')


@bot.command(name='restart', help='Restarts the game from initial prompt')
@commands.has_role(ADMIN_ROLE)
async def game_restart(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    if story_manager.story == None:
        await ctx.send('No story found to restart, load a story with !load story_id or start a new one with !next')
        return

    # clear queue
    while not queue.empty():
        await queue.get()
    await queue.join()

    story_manager.story.actions = []
    story_manager.story.results = []

    await ctx.send('Restarted game from beginning')
    await ctx.send(story_manager.story.story_start)


@bot.command(name='save', help='Saves the current game')
@commands.has_role(ADMIN_ROLE)
async def game_save(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    if story_manager.story == None or not story_manager.story.upload_story:
        return

    id = story_manager.story.save_to_storage()
    await ctx.send("Game saved.")
    await ctx.send("To load the game, type 'load' and enter the following ID: {}".format(id))


@bot.command(name='load', help='Load the game with given ID')
@commands.has_role(ADMIN_ROLE)
async def game_load(ctx, *, text='save_game_id'):
    if ctx.message.channel.name != CHANNEL:
        return
    
    if story_manager.story == None:
        story_manager.story = Story("", upload_story=True)

    result = story_manager.story.load_from_storage(text)
    await ctx.send("\nLoading Game...\n")
    await ctx.send(result)


@bot.command(name='exit', help='Saves and exits the current game')
@commands.has_role(ADMIN_ROLE)
async def game_exit(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    if story_manager.story == None:
        story_manager.story = Story("", upload_story=False)

    await game_save(ctx)
    await ctx.send("Exiting game...")

    guild = ctx.message.guild
    voice_client = guild.voice_client
    if voice_client is not None:
        if voice_client.is_connected():
            await voice_client.disconnect()
        else:
            for vc in guild.voice_clients:
                if vc.is_connected():
                    await voice_client.disconnect()

    exit()

@bot.command(name='join', help='Join the voice channel of the user')
@commands.has_role(ADMIN_ROLE)
async def join_voice(ctx):
    voice_channel = ctx.message.author.voice.channel

    if voice_channel == None:
        await ctx.send("You are not currently in a voice channel")
    else:
        await voice_channel.connect()

@bot.command(name='leave', help='Join the voice channel of the user')
@commands.has_role(ADMIN_ROLE)
async def leave_voice(ctx):
    guild = ctx.message.guild
    voice_client = guild.voice_client

    if voice_client is None:
        await ctx.send("You are not currently in a voice channel")
    else:
        if voice_client.is_connected():
            await voice_client.disconnect()
        else:
            for vc in guild.voice_clients:
                if vc.is_connected():
                    await voice_client.disconnect()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.errors.CommandNotFound): return
    logger.error(error)
    logger.error('Ignoring exception in command {}:'.format(ctx.command))
    # TODO handle errors


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    bot.run(os.getenv('DISCORD_TOKEN'))
