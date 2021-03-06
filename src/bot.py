import asyncio
import datetime
import importlib
import itertools
import os
import re
import signal
import sys
import time

import discord
import psutil

from src import const
from src.algorithms import levenshtein_distance
from src.config import Config, GuildSettings, SecretConfig, User, bc
from src.info import BotInfo
from src.log import log
from src.markov import Markov
from src.message import Msg
from src.message_buffer import MessageBuffer
from src.reminder import Reminder
from src.repl import Repl
from src.utils import Util


class WalBot(discord.Client):
    def __init__(self, config, secret_config):
        super().__init__()
        self.repl = None
        self.config = config
        self.secret_config = secret_config
        self.loop.create_task(self.config_autosave())
        self.loop.create_task(self.process_reminders())
        self.loop.create_task(self._precompile())
        bc.config = self.config
        bc.commands = self.config.commands
        bc.background_loop = self.loop
        bc.latency = lambda: self.latency
        bc.change_status = self.change_status
        bc.change_presence = self.change_presence
        bc.get_channel = self.get_channel
        bc.close = self.close
        bc.secret_config = self.secret_config
        bc.message_buffer = MessageBuffer()
        bc.info = BotInfo()
        if not bc.args.fast_start:
            if bc.markov.check():
                log.info("Markov model has passed all checks")
            else:
                log.info("Markov model has not passed checks, but all errors were fixed")

    async def _precompile(self):
        log.debug("Started precompiling functions...")
        levenshtein_distance("", "")
        log.debug("Finished precompiling functions")

    async def change_status(self, string, type_):
        await self.change_presence(activity=discord.Activity(name=string, type=type_))

    async def config_autosave(self):
        await self.wait_until_ready()
        index = 1
        while not self.is_closed():
            if index % self.config.saving["backup"]["period"] == 0:
                self.config.backup(const.CONFIG_PATH, const.MARKOV_PATH)
            self.config.save(const.CONFIG_PATH, const.MARKOV_PATH, const.SECRET_CONFIG_PATH)
            index += 1
            await asyncio.sleep(self.config.saving["period"] * 60)

    async def process_reminders(self):
        await self.wait_until_ready()
        while not self.is_closed():
            log.debug3("Reminder processing iteration has started")
            now = datetime.datetime.now().replace(second=0).strftime(const.REMINDER_TIME_FORMAT)
            to_remove = []
            to_append = []
            for key, rem in self.config.reminders.items():
                if rem == now:
                    channel = self.get_channel(rem.channel_id)
                    await channel.send(f"{' '.join(rem.ping_users)}\nYou asked to remind at {now} -> {rem.message}")
                    for user_id in rem.whisper_users:
                        await Msg.send_direct_message(
                            self.get_user(user_id), f"You asked to remind at {now} -> {rem.message}", False)
                    if rem.repeat_after > 0:
                        new_time = datetime.datetime.now().replace(
                            second=0, microsecond=0) + datetime.timedelta(minutes=rem.repeat_after)
                        new_time = new_time.strftime(const.REMINDER_TIME_FORMAT)
                        to_append.append(Reminder(str(new_time), rem.message, rem.channel_id))
                        to_append[-1].repeat_after = rem.repeat_after
                        log.debug2(f"Scheduled renew of recurring reminder - old id: {key}")
                    to_remove.append(key)
                elif rem < now:
                    log.debug2(f"Scheduled reminder with id {key} removal")
                    to_remove.append(key)
            for key in to_remove:
                self.config.reminders.pop(key)
            for item in to_append:
                key = self.config.ids["reminder"]
                self.config.reminders[key] = item
                self.config.ids["reminder"] += 1
            log.debug3("Reminder processing iteration has finished")
            await asyncio.sleep(const.REMINDER_POLLING_INTERVAL)

    async def on_ready(self):
        log.info(f"Logged in as: {self.user.name} {self.user.id} ({self.__class__.__name__})")
        self.repl = Repl(self.config.repl["port"])
        bc.guilds = self.guilds
        for guild in self.guilds:
            if guild.id not in self.config.guilds.keys():
                self.config.guilds[guild.id] = GuildSettings(guild.id)
        bc.bot_user = self.user

    async def on_message(self, message):
        try:
            bc.message_buffer.push(message)
            log.info(f"<{message.id}> {message.author} -> {message.content}")
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
                await self.process_repetitions(message)
        except Exception:
            log.error("on_message failed", exc_info=True)

    async def process_repetitions(self, message):
        m = tuple(bc.message_buffer.get(message.channel.id, i) for i in range(3))
        if (all(m) and m[0].content == m[1].content == m[2].content and
            (m[0].author.id != self.user.id and
             m[1].author.id != self.user.id and
             m[2].author.id != self.user.id)):
            await message.channel.send(m[0].content)

    async def process_regular_message(self, message):
        if (self.user.mentioned_in(message) or self.user.id in [
                member.id for member in list(
                    itertools.chain(*[role.members for role in message.role_mentions]))]):
            if message.channel.id in self.config.guilds[message.channel.guild.id].markov_responses_whitelist:
                result = await self.config.disable_pings_in_response(message, bc.markov.generate())
                await message.channel.send(message.author.mention + ' ' + result)
        elif message.channel.id in self.config.guilds[message.channel.guild.id].markov_logging_whitelist:
            bc.markov.add_string(message.content)
        if message.channel.id in self.config.guilds[message.channel.guild.id].responses_whitelist:
            for response in self.config.responses.values():
                if re.search(response.regex, message.content):
                    await Msg.response(message, response.text, False)
        if message.channel.id in self.config.guilds[message.channel.guild.id].reactions_whitelist:
            for reaction in self.config.reactions.values():
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
                await message.channel.send(
                    f"Unknown command '{command[0]}', "
                    f"probably you meant '{self.suggest_similar_command(command[0])}'")
                return
        await self.config.commands.data[command[0]].run(message, command, self.config.users[message.author.id])

    def suggest_similar_command(self, unknown_command):
        min_dist = 100000
        suggestion = ""
        for command in self.config.commands.data.keys():
            dist = levenshtein_distance(unknown_command, command)
            if dist < min_dist:
                suggestion = command
                min_dist = dist
        for command in self.config.commands.aliases.keys():
            dist = levenshtein_distance(unknown_command, command)
            if dist < min_dist:
                suggestion = command
                min_dist = dist
        return suggestion

    async def on_raw_message_edit(self, payload):
        try:
            log.info(f"<{payload.message_id}> (edit) {payload.data['author']['username']}#"
                     f"{payload.data['author']['discriminator']} -> {payload.data['content']}")
        except KeyError:
            pass

    async def on_raw_message_delete(self, payload):
        log.info(f"<{payload.message_id}> (delete)")


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
    bc.restart_flag = False
    bc.args = args
    # Handle --nohup flag
    if sys.platform in ("linux", "darwin") and args.nohup:
        fd = os.open(const.NOHUP_FILE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        log.info(f"Output is redirected to {const.NOHUP_FILE_PATH}")
        os.dup2(fd, sys.stdout.fileno())
        os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
        os.close(fd)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # Selecting YAML parser
    bc.yaml_loader, bc.yaml_dumper = Util.get_yaml(verbose=True)
    # Saving application pd in order to safely stop it later
    with open(const.BOT_CACHE_FILE_PATH, 'w') as f:
        f.write(str(os.getpid()))
    # Executing patch tool if it is necessary
    if args.patch:
        cmd = f"'{sys.executable}' '{os.path.dirname(__file__) + '/../tools/patch.py'}' all"
        log.info("Executing patch tool: " + cmd)
        os.system(cmd)
    # Read configuration files
    config = Util.read_config_file(const.CONFIG_PATH)
    if config is None:
        config = Config()
    config.commands.update()
    secret_config = Util.read_config_file(const.SECRET_CONFIG_PATH)
    if secret_config is None:
        secret_config = SecretConfig()
    bc.markov = Util.read_config_file(const.MARKOV_PATH)
    if bc.markov is None:
        bc.markov = Markov()
    # Check config versions
    ok = True
    ok &= Util.check_version("discord.py", discord.__version__, const.DISCORD_LIB_VERSION,
                             solutions=[
                                 "execute: python -m pip install -r requirements.txt",
                             ])
    ok &= Util.check_version("Config", config.version, const.CONFIG_VERSION,
                             solutions=[
                                 "run patch tool",
                                 "remove config.yaml (settings will be lost!)",
                             ])
    ok &= Util.check_version("Markov config", bc.markov.version, const.MARKOV_CONFIG_VERSION,
                             solutions=[
                                 "run patch tool",
                                 "remove markov.yaml (Markov model will be lost!)",
                             ])
    ok &= Util.check_version("Secret config", secret_config.version, const.SECRET_CONFIG_VERSION,
                             solutions=[
                                 "run patch tool",
                                 "remove secret.yaml (your Discord authentication token will be lost!)",
                             ])
    if not ok:
        sys.exit(1)
    # Constructing bot instance
    if main_bot:
        walbot = WalBot(config, secret_config)
    else:
        walbot = importlib.import_module("src.minibot").MiniWalBot(config, secret_config)
    # Checking authentication token
    if secret_config.token is None:
        secret_config.token = input("Enter your token: ")
    # Starting the bot
    walbot.run(secret_config.token)
    # After stopping the bot
    walbot.repl.stop()
    for event in bc.background_events:
        event.cancel()
    bc.background_loop = None
    log.info("Bot is disconnected!")
    if main_bot:
        config.save(const.CONFIG_PATH, const.MARKOV_PATH, const.SECRET_CONFIG_PATH, wait=True)
    os.remove(const.BOT_CACHE_FILE_PATH)
    if bc.restart_flag:
        cmd = f"'{sys.executable}' '{os.path.dirname(__file__) + '/../walbot.py'}' start"
        log.info("Calling: " + cmd)
        if sys.platform in ("linux", "darwin"):
            fork = os.fork()
            if fork == 0:
                os.system(cmd)
            elif fork > 0:
                log.info("Stopping current instance of the bot")
                sys.exit(0)
        else:
            os.system(cmd)


def stop(_):
    if not os.path.exists(const.BOT_CACHE_FILE_PATH):
        log.error("Could not stop the bot (cache file does not exist)")
        return
    pid = parse_bot_cache()
    if pid is None:
        log.error("Could not stop the bot (cache file does not contain pid)")
        return
    if psutil.pid_exists(pid):
        os.kill(pid, signal.SIGINT)
        while psutil.pid_exists(pid):
            log.debug("Bot is still running. Please, wait...")
            time.sleep(0.5)
        log.info("Bot is stopped!")
    else:
        log.error("Could not stop the bot (bot is not running)")
        os.remove(const.BOT_CACHE_FILE_PATH)
