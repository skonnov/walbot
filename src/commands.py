import importlib
import inspect
import os

from src import const
from src.config import Command
from src.config import bc
from src.config import log


class BaseCmd:
    @classmethod
    def get_classname(cls):
        return cls.__name__

    def bind(self):
        raise NotImplementedError(f"Class {self.get_classname()} does not have bind() function")


class Commands:
    def __init__(self):
        if not hasattr(self, "data"):
            self.data = dict()
        if not hasattr(self, "aliases"):
            self.aliases = dict()

    def update(self):
        bc.commands = self
        cmd_directory = os.path.join(os.path.dirname(__file__), "cmd")
        cmd_modules = ['.' + os.path.splitext(path)[0] for path in os.listdir(cmd_directory)
                       if os.path.isfile(os.path.join(cmd_directory, path)) and path.endswith(".py")]
        for module in cmd_modules:
            commands_file = importlib.import_module("src.cmd" + module)
            commands = [obj[1] for obj in inspect.getmembers(commands_file, inspect.isclass)
                        if (obj[1].__module__ == "src.cmd" + module) and issubclass(obj[1], BaseCmd)]
            if len(commands) == 1:
                commands = commands[0]
                if "bind" in [func[0] for func in inspect.getmembers(commands, inspect.isfunction)
                              if not func[0].startswith('_')]:
                    commands.bind(commands)
                else:
                    log.error(f"Class '{commands.__name__}' does not have bind() function")
            elif len(commands) > 1:
                log.error(f"Module 'src.cmd{module}' have more than 1 class in it")
            else:
                log.error(f"Module 'src.cmd{module}' have no classes in it")
        self.export_help(const.COMMANDS_DOC_PATH)

    def export_help(self, file_path):
        with open(file_path, "w", encoding="utf-8", newline='\n') as f:
            f.write("<!-- WARNING! This file is automatically generated, do not change it manually -->\n\n")
            result = []
            repeat = True
            while repeat:
                repeat = False
                for name, command in self.data.items():
                    if command.perform is not None:
                        s = "**" + name + "**: "
                        try:
                            s += " \\\n".join(command.get_actor().__doc__.split('\n'))
                        except AttributeError:
                            del self.data[name]
                            log.warning(f"Command '{name}' is not found and deleted from config and documentation")
                            repeat = True
                            break
                        if command.subcommand:
                            s += " \\\n    *This command can be used as subcommand*"
                        s += '\n'
                        s = s.replace('<', '&lt;').replace('>', '&gt;')
                        result.append(s)
            f.write('\n'.join(sorted(list(set(result)))))

    def register_command(self, module_name, class_name, command_name, **kwargs):
        log.debug2(f"Registering command: {module_name} {class_name} {command_name}")
        if command_name not in self.data.keys():
            if kwargs.get("message", None):
                self.data[command_name] = Command(module_name, class_name, **kwargs)
            else:
                self.data[command_name] = Command(module_name, class_name, '_' + command_name, **kwargs)
            self.data[command_name].is_global = True
