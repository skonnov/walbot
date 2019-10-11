import asyncio
import logging
import logging.config
import threading
import yaml


class RuntimeConfig:
    def __init__(self):
        self.background_events = []
        self.background_loop = None


def setup_logging():
    global log
    logging.config.dictConfig({
        'version': 1,
        'disable_existing_loggers': True,
    })
    log = logging.getLogger("WalBot")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler("log.txt")
    fh.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    log.addHandler(fh)
    log.addHandler(ch)
    log.info("Logging system is set up")


runtime_config = RuntimeConfig()
log = None
setup_logging()


class Command:
    def __init__(self, name, perform=None, message=None, permission=0):
        self.name = name
        self.perform = perform
        self.permission = permission
        self.message = message
        self.is_global = False
        self.channels = []

    def is_available(self, channel_id):
        return self.is_global or (channel_id in self.channels)

    async def run(self, message, command, user):
        if not self.is_available(message.channel.id):
            await message.channel.send("Command '{}' is not available in this channel".format(command[0]))
            return
        if user is not None and self.permission > user.permission_level:
            await message.channel.send("You don't have permission to call command '{}'".format(command[0]))
            return
        if self.perform is not None:
            await self.perform(message, command)
        elif self.message is not None:
            respond = self.message
            respond = respond.replace("@author@", message.author.mention)
            respond = respond.replace("@args@", ' '.join(command[1:]))
            for i in range(len(command)):
                respond = respond.replace("@arg" + str(i) + "@", command[i])
            if (len(respond.strip()) > 0):
                await message.channel.send(respond)
        else:
            await message.channel.send("Command '{}' is not callable".format(command[0]))


class BackgroundEvent:
    def __init__(self, config, channel, message, period):
        self.config = config
        self.channel = channel
        self.message = message
        self.period = period
        self.task = runtime_config.background_loop.create_task(self.run())

    async def run(self):
        command = self.message.content.split(' ')
        command = list(filter(None, command))
        command[0] = command[0][1:]
        while True:
            await asyncio.sleep(self.period)
            if command[0] not in self.config.commands.data.keys():
                await self.channel.send("Unknown command '{}'".format(command[0]))
            else:
                actor = self.config.commands.data[command[0]]
                await actor.run(self.message, command, None)

    def cancel(self):
        self.task.cancel()


class Reaction:
    def __init__(self, regex, emoji):
        self.regex = regex
        self.emoji = emoji


class GuildSettings:
    def __init__(self, id):
        self.id = id
        self.is_whitelisted = False
        self.whilelist = set()


class User:
    def __init__(self, id):
        self.id = id
        self.permission_level = 0


class Config:
    def __init__(self):
        commands = __import__("commands")
        if not hasattr(self, "commands"):
            self.commands = commands.Commands(self)
        self.commands.update_builtins()
        if not hasattr(self, "reactions"):
            self.reactions = []
        if not hasattr(self, "token"):
            self.token = None
        if not hasattr(self, "guilds"):
            self.guilds = dict()
        if not hasattr(self, "users"):
            self.users = dict()
        if not hasattr(self, "commands_prefix"):
            self.commands_prefix = "!"

    def save(self, filename):
        mutex = threading.Lock()
        mutex.acquire()
        log.info("Saving of config is started")
        with open(filename, 'wb') as f:
            try:
                f.write(yaml.dump(self, encoding='utf-8'))
                log.info("Saving of config is finished")
            except Exception:
                log.error("yaml.dump failed", exc_info=True)
        mutex.release()