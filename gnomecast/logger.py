import logging

from sys import stderr, stdout

# Log levels from https://docs.python.org/3/library/logging.html#levels
# CRITICAL: 50
# ERROR: 40
# WARNING: 30
# INFO: 20
# DEBUG: 10
# NOTSET: 0

C_RED = '\033[91m'
C_YELLOW = '\033[93m'
C_BLUE = '\033[94m'
C_RESET = '\033[0m'


class ColoredFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.l_color_reset = C_RESET
        if record.levelno == logging.DEBUG:
            record.l_color = C_BLUE
        elif record.levelno == logging.WARNING:
            record.l_color = C_YELLOW
        elif record.levelno >= logging.ERROR:
            record.l_color = C_RED
        else:
            record.l_color = ''
            record.l_color_reset = ''
        return True


def init_logger(name: str = 'gnomecast', debug: bool = False):
    log = logging.getLogger(name=name)
    if log.hasHandlers():
        return

    log.setLevel(logging.DEBUG)  # accept everything and let handlers decide

    console_handler = logging.StreamHandler(stream=stdout)
    if debug:
        console_handler.setLevel(logging.DEBUG)
        base_format = '%(l_color)s%(asctime)s [ %(levelname)s ] %(message)s (%(filename)s:%(lineno)d)%(l_color_reset)s'
    else:
        console_handler.setLevel(logging.INFO)
        base_format = '%(l_color)s%(message)s%(l_color_reset)s'

    console_handler.setFormatter(logging.Formatter(fmt=base_format))
    console_handler.addFilter(lambda record: record.levelno < logging.WARNING)
    console_handler.addFilter(ColoredFilter())
    log.addHandler(console_handler)

    error_handler = logging.StreamHandler(stream=stderr)
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(logging.Formatter(fmt=base_format))
    error_handler.addFilter(ColoredFilter())
    log.addHandler(error_handler)
