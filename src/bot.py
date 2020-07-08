import asyncio
import datetime
import itertools
import os
import re
import signal
import sys
import time

import discord
import psutil
import yaml

from . import const
from .config import Config
from .config import GuildSettings
from .config import SecretConfig
from .config import User
from .config import bc
from .log import log
from .markov import Markov
from .utils import Util


class WalBot(discord.Client):
    def __init__(self, config, secret_config):
        super(WalBot, self).__init__()
        self.config = config
        self.secret_config = secret_config
        self.loop.create_task(self.config_autosave())
        self.loop.create_task(self.process_reminders())
        bc.config = self.config
        bc.commands = self.config.commands
        bc.background_loop = self.loop
        bc.change_status = self.change_status
        bc.change_presence = self.change_presence
        bc.get_channel = self.get_channel
        bc.close = self.close
        bc.secret_config = self.secret_config
        if not bc.args.fast_start:
            if bc.markov.check():
                log.info("Markov model has passed all checks")
            else:
                log.info("Markov model has not passed checks, but all errors were fixed")

    async def change_status(self, string, type):
        await self.change_presence(activity=discord.Activity(name=string, type=type))

    async def config_autosave(self):
        await self.wait_until_ready()
        index = 1
        while not self.is_closed():
            if index % 10 == 0:
                self.config.backup(const.CONFIG_PATH, const.MARKOV_PATH)
            self.config.save(const.CONFIG_PATH, const.MARKOV_PATH, const.SECRET_CONFIG_PATH)
            index += 1
            await asyncio.sleep(10 * 60)

    async def process_reminders(self):
        await self.wait_until_ready()
        while not self.is_closed():
            now = datetime.datetime.now().replace(second=0).strftime(const.REMINDER_TIME_FORMAT)
            to_remove = []
            for key, rem in self.config.reminders.items():
                if rem == now:
                    channel = self.get_channel(rem.channel_id)
                    await channel.send("You asked to remind at {} -> {}".format(now, rem.message))
                    to_remove.append(key)
                elif rem < now:
                    to_remove.append(key)
            for key in to_remove:
                self.config.reminders.pop(key)
            await asyncio.sleep(30)

    async def on_ready(self):
        log.info("Logged in as: {} {} ({})".format(self.user.name, self.user.id, self.__class__.__name__))
        for guild in self.guilds:
            if guild.id not in self.config.guilds.keys():
                self.config.guilds[guild.id] = GuildSettings(guild.id)
        bc.bot_user = self.user

    async def on_message(self, message):
        try:
            log.info("<" + str(message.id) + "> " + str(message.author) + " -> " + message.content)
            if message.author.id == self.user.id:
                return
            if isinstance(message.channel, discord.DMChannel):
                return
            if message.channel.guild.id is None:
                return
            if self.config.guilds[message.channel.guild.id].is_whitelisted:
                if message.channel.id not in self.config.guilds[message.channel.guild.id].whitelist:
                    return
            if message.author.id not in self.config.users.keys():
                self.config.users[message.author.id] = User(message.author.id)
            if self.config.users[message.author.id].permission_level < 0:
                return
            if message.content.startswith(self.config.commands_prefix):
                await self.process_command(message)
            else:
                await self.process_regular_message(message)
        except Exception:
            log.error("on_message failed", exc_info=True)

    async def process_regular_message(self, message):
        if (self.user.mentioned_in(message) or
            self.user.id in [member.id for member in
                             list(itertools.chain(*[role.members for role in message.role_mentions]))]):
            if message.channel.id in self.config.guilds[message.channel.guild.id].responses_whitelist:
                result = await self.config.disable_pings_in_response(message, bc.markov.generate())
                await message.channel.send(message.author.mention + ' ' + result)
        elif message.channel.id in self.config.guilds[message.channel.guild.id].markov_whitelist:
            bc.markov.add_string(message.content)
        if message.channel.id not in self.config.guilds[message.channel.guild.id].reactions_whitelist:
            return
        for reaction in self.config.reactions:
            if re.search(reaction.regex, message.content):
                log.info("Added reaction " + reaction.emoji)
                try:
                    await message.add_reaction(reaction.emoji)
                except discord.HTTPException:
                    pass

    async def process_command(self, message):
        command = message.content.split(' ')
        command = list(filter(None, command))
        command[0] = command[0][1:]
        if not command[0]:
            log.debug("Ignoring empty command")
            return
        if command[0] not in self.config.commands.data.keys():
            if command[0] in self.config.commands.aliases.keys():
                command[0] = self.config.commands.aliases[command[0]]
            else:
                await message.channel.send("Unknown command '{}'".format(command[0]))
                return
        await self.config.commands.data[command[0]].run(message, command, self.config.users[message.author.id])

    async def on_raw_message_edit(self, payload):
        try:
            log.info("<" + str(payload.message_id) + "> (edit) " +
                     payload.data["author"]["username"] + "#" + payload.data["author"]["discriminator"] +
                     " -> " + payload.data["content"])
        except KeyError:
            pass

    async def on_raw_message_delete(self, payload):
        log.info("<" + str(payload.message_id) + "> (delete)")


def parse_bot_cache():
    pid = None
    if os.path.exists(const.BOT_CACHE_FILE_PATH):
        cache = None
        with open(const.BOT_CACHE_FILE_PATH, 'r') as f:
            cache = f.read()
        if cache is not None:
            try:
                pid = int(cache)
            except ValueError:
                log.warning("Could not read pid from .bot_cache")
                os.remove(const.BOT_CACHE_FILE_PATH)
    return pid


def start(args, main_bot=True):
    # Check whether bot is already running
    pid = parse_bot_cache()
    if pid is not None and psutil.pid_exists(pid):
        log.error("Bot is already running!")
        return
    # Some variable initializations
    config = None
    secret_config = None
    bc._restart = False
    bc.args = args
    # Handle --nohup flag
    if sys.platform in ("linux", "darwin"):
        if args.nohup:
            fd = os.open(const.NOHUP_FILE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            log.info("Output is redirected to {}".format(const.NOHUP_FILE_PATH))
            os.dup2(fd, sys.stdout.fileno())
            os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
            os.close(fd)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # Selecting YAML parser
    try:
        bc.yaml_loader = yaml.CLoader
        log.info("Using fast YAML Loader")
    except Exception:
        bc.yaml_loader = yaml.Loader
        log.info("Using slow YAML Loader")
    try:
        bc.yaml_dumper = yaml.CDumper
        log.info("Using fast YAML Dumper")
    except Exception:
        bc.yaml_dumper = yaml.Dumper
        log.info("Using slow YAML Dumper")
    # Saving application pd in order to safely stop it later
    with open(const.BOT_CACHE_FILE_PATH, 'w') as f:
        f.write(str(os.getpid()))
    # Executing patch tool if it is necessary
    if args.patch:
        cmd = "'{}' '{}' all".format(sys.executable, os.path.dirname(__file__) + "/../patch.py")
        log.info("Executing patch tool: " + cmd)
        os.system(cmd)
    # Read config.yaml
    if os.path.isfile(const.CONFIG_PATH):
        with open(const.CONFIG_PATH, 'r') as f:
            try:
                config = yaml.load(f.read(), Loader=bc.yaml_loader)
            except Exception:
                log.error("yaml.load failed on file: {}".format(const.CONFIG_PATH), exc_info=True)
        config.__init__()
    if config is None:
        config = Config()
    # Read secret.yaml
    if os.path.isfile(const.SECRET_CONFIG_PATH):
        with open(const.SECRET_CONFIG_PATH, 'r') as f:
            try:
                secret_config = yaml.load(f.read(), Loader=bc.yaml_loader)
            except Exception:
                log.error("yaml.load failed on file: {}".format(const.SECRET_CONFIG_PATH), exc_info=True)
        secret_config.__init__()
    if secret_config is None:
        secret_config = SecretConfig()
    # Read markov.yaml
    bc.markov = None
    if os.path.isfile(const.MARKOV_PATH):
        with open(const.MARKOV_PATH, 'rb') as f:
            try:
                bc.markov = yaml.load(f.read(), Loader=bc.yaml_loader)
            except Exception:
                log.error("yaml.load failed on file: {}".format(const.MARKOV_PATH), exc_info=True)
    if bc.markov is None:
        bc.markov = Markov()
    # Check config versions
    ok = True
    ok &= Util.check_version("Config", config.version, const.CONFIG_VERSION)
    ok &= Util.check_version("Markov config", bc.markov.version, const.MARKOV_CONFIG_VERSION)
    ok &= Util.check_version("Secret config", secret_config.version, const.SECRET_CONFIG_VERSION)
    if not ok:
        sys.exit(1)
    # Constructing bot instance
    if main_bot:
        walbot = WalBot(config, secret_config)
    else:
        walbot = __import__("src.minibot", fromlist=['object']).MiniWalBot(config, secret_config)
    # Checking authentication token
    if secret_config.token is None:
        secret_config.token = input("Enter your token: ")
    # Starting the bot
    walbot.run(secret_config.token)
    # After stopping the bot
    for event in bc.background_events:
        event.cancel()
    bc.background_loop = None
    log.info("Bot is disconnected!")
    config.save(const.CONFIG_PATH, const.MARKOV_PATH, const.SECRET_CONFIG_PATH, wait=True)
    os.remove(const.BOT_CACHE_FILE_PATH)
    if bc._restart:
        cmd = "'{}' '{}' start".format(sys.executable, os.path.dirname(__file__) + "/../main.py")
        log.info("Calling: " + cmd)
        os.system(cmd)


def stop():
    if not os.path.exists(const.BOT_CACHE_FILE_PATH):
        log.error("Could not stop the bot (cache file does not exist)")
        return
    pid = parse_bot_cache()
    if pid is None:
        log.error("Could not stop the bot (cache file does not contain pid)")
        return
    if psutil.pid_exists(pid):
        os.kill(pid, signal.SIGINT)
        while True:
            is_running = psutil.pid_exists(pid)
            if not is_running:
                break
            log.debug("Bot is still running. Please, wait...")
            time.sleep(0.5)
        log.info("Bot is stopped!")
    else:
        log.error("Could not stop the bot (bot is not running)")
