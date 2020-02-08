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

# bot setup
bot = commands.Bot(command_prefix='!')
CHANNEL = 'active-investigations'

# log setup
syslog = SysLogHandler() # sudo service rsyslog start && tail -f /var/log/syslog
log_format = '%(asctime)s atlas dungeon_worker: %(message)s'
log_formatter = logging.Formatter(log_format, datefmt='%b %d %H:%M:%S')
syslog.setFormatter(log_formatter)
logger = logging.getLogger()
logger.addHandler(syslog)
logger.setLevel(logging.INFO)

max_history = 20
generator = GPT2Generator()
story_manager = UnconstrainedStoryManager(generator)
queue = asyncio.Queue()
logger.info('Worker instance started')


def escape(text):
    text = re.sub(r'\\(\*|_|`|~|\\|>)', r'\g<1>', text)
    return re.sub(r'(\*|_|`|~|\\|>)', r'\\\g<1>', text)


@bot.event
async def on_ready():
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

        if story_manager.story is None:
            await bot.get_channel(channel).send("Generating story...")
            result = story_manager.start_new_story(args["text"], context="", upload_story=upload_story)
            await bot.get_channel(channel).send(result)
            continue

        # generate response
        try:
            async with bot.get_channel(channel).typing():
                task = loop.run_in_executor(None, story_manager.act, args["text"])
                response = await asyncio.wait_for(task, 180, loop=loop)
                sent = f'> {args["text"]}\n{escape(response)}'
                await bot.get_channel(channel).send(sent)
        except Exception as err:
            logger.info('Error with message: ', exc_info=True)


@bot.command(name='next', help='Continues AI Dungeon game')
async def game_next(ctx, *, text='continue'):
    if ctx.message.channel.name != CHANNEL:
        return
    message = {'channel': ctx.channel.id, 'text': text}
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


@bot.command(name='restart', help='Starts the game from beginning')
@commands.has_role('Chief')
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
@commands.has_role('Chief')
async def game_save(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    if story_manager.story == None or not story_manager.story.upload_story:
        return

    id = story_manager.story.save_to_storage()
    await ctx.send("Game saved.")
    await ctx.send("To load the game, type 'load' and enter the following ID: {}".format(id))


@bot.command(name='load', help='Load the game with given ID')
@commands.has_role('Chief')
async def game_load(ctx, *, text='save_game_id'):
    if ctx.message.channel.name != CHANNEL:
        return
    
    if story_manager.story == None:
        story_manager.story = Story("", upload_story=True)

    result = story_manager.story.load_from_storage(text)
    await ctx.send("\nLoading Game...\n")
    await ctx.send(result)


@bot.command(name='exit', help='Saves and exits the current game')
@commands.has_role('Chief')
async def game_exit(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    if story_manager.story == None:
        story_manager.story = Story("", upload_story=False)

    await game_save(ctx)
    await ctx.send("Exiting game...")
    exit()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.errors.CommandNotFound): return
    logger.error('Ignoring exception in command {}:'.format(ctx.command))
    # TODO handle errors


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    bot.run(os.getenv('DISCORD_TOKEN'))
