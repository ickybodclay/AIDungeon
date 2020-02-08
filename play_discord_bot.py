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
syslog = SysLogHandler() # /var/log/syslog
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
print('Worker instance started\n', file=sys.stderr)


def escape(text):
    text = re.sub(r'\\(\*|_|`|~|\\|>)', r'\g<1>', text)
    return re.sub(r'(\*|_|`|~|\\|>)', r'\\\g<1>', text)


@bot.event
async def on_ready():
    loop = asyncio.get_event_loop()

    while True:
        # poll queue for messages, block here if empty
        msg = None
        while not msg: msg = await queue.get()
        logger.info(f'Processing message: {msg}'); args = json.loads(msg)
        print(f'message seen: {msg}\n', file=sys.stderr)
        channel, text = args['channel'], f'\n> {args["text"]}\n'

        # generate response
        try:
            async with bot.get_channel(channel).typing():
                task = loop.run_in_executor(None, story_manager.act, text)
                response = await asyncio.wait_for(task, 180, loop=loop)
                print(response + "\n", file=sys.stderr)
                sent = f'> {args["text"]}\n{escape(response)}'
                await bot.get_channel(channel).send(sent)
        except Exception as err:
            print('Exception in response handling:', err, file=sys.stderr)
            logger.info('Error with message: ', exc_info=True)


@bot.command(name='next', help='Continues AI Dungeon game')
async def game_next(ctx, *, text='continue'):
    if ctx.message.channel.name != CHANNEL:
        return
    message = {'channel': ctx.channel.id, 'text': text}
    await queue.put(json.dumps(message))

@bot.command(name='restart', help='Starts the game from beginning')
async def game_restart(ctx):
    if ctx.message.channel.name != CHANNEL:
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
async def game_save(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    id = story_manager.story.save_to_storage()
    await ctx.send("Game saved.")
    await ctx.send("To load the game, type 'load' and enter the following ID: {}".format(id))

@bot.command(name='load', help='Load the game with given ID')
@commands.has_role('chief')
async def game_load(ctx, text='id'):
    if ctx.message.channel.name != CHANNEL:
        return

    result = story_manager.story.load_from_storage(text)
    await ctx.send("\nLoading Game...\n")
    await ctx.send(result)

@bot.command(name='exit', help='Saves and exits the current game')
@commands.has_role('Chief')
async def game_exit(ctx):
    if ctx.message.channel.name != CHANNEL:
        return

    await game_save(ctx)
    await ctx.send("Exiting game...")
    exit()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.errors.CommandNotFound): return
    # TODO handle errors
    print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    bot.run(os.getenv('DISCORD_TOKEN'))
