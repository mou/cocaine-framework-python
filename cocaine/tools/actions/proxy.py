import json
import logging
from logging import config
import os
from signal import SIGTERM
import time
from cocaine.proxy import Daemon

from cocaine.tools import log
from cocaine.proxy.proxy import CocaineProxy


class Error(Exception):
    pass


class Start(object):
    def __init__(self, port, cache, config, daemon, pidfile):
        self.port = port
        self.count = 4
        self.cache = cache
        self.config = config
        self.daemon = daemon
        self.pidfile = pidfile

    @staticmethod
    def configureLogging():
        formatter = logging.Formatter('[%(asctime)s] %(levelname)-8s: %(message)s')
        handler = logging.FileHandler('/var/log/cocaine-python-proxy.log')
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        proxyLog = logging.getLogger('cocaine.proxy')
        proxyLog.addHandler(handler)
        proxyLog.setLevel(logging.INFO)

    def execute(self):
        log.info('Starting cocaine proxy... ')
        # Load config
        config = {}
        try:
            with open(self.config, 'r') as fh:
                config = json.loads(fh.read())
        except IOError as err:
            log.error(err)

        # Start
        if self.daemon:
            if os.path.exists(self.pidfile):
                log.error('FAIL')
                raise Error('is already running (pid file "{0}" exists)'.format(self.pidfile))
            else:
                try:
                    with open(self.pidfile, 'w'):
                        pass
                    os.remove(self.pidfile)
                    log.info('OK')
                except IOError as err:
                    log.error('FAIL')
                    raise Error('failed to create pid file - {0}'.format(err))

            daemon = Daemon(self.pidfile)
            daemon.run = self.run
            daemon.start(config)
        else:
            log.info('OK')
            self.run(config)

    def run(self, config):
        if 'logging' in config:
            logging.config.dictConfig(config['logging'])
        else:
            self.configureLogging()

        proxy = CocaineProxy(self.port, self.cache, **config)
        proxy.run()


class Stop(object):
    def __init__(self, pidfile):
        self.pidfile = pidfile

    def execute(self):
        log.info('Stopping cocaine proxy... ')
        try:
            with open(self.pidfile, 'r') as fh:
                pid = int(fh.read().strip())
        except IOError:
            pid = None

        if not pid:
            log.error('FAIL')
            log.error('Cocaine proxy is not running')
            exit(1)

        try:
            elapsed = 0
            while True and elapsed < 30.0:
                os.kill(pid, SIGTERM)
                time.sleep(0.5)
                elapsed += 0.5
        except OSError as err:
            err = str(err)
            if err.find('No such process') > 0:
                if os.path.exists(self.pidfile):
                    os.remove(self.pidfile)
                log.info('OK')
            else:
                log.error('FAIL')
                log.error(err)
                exit(1)


class Status(object):
    def __init__(self, pidfile):
        self.pidfile = pidfile

    def execute(self):
        try:
            with file(self.pidfile, 'r') as fh:
                pid = int(fh.read().strip())
        except (IOError, ValueError):
            pid = None

        if not pid:
            log.error('Stopped')
            return

        try:
            os.kill(pid, 0)
            log.info('Running: %d', pid)
        except OSError as err:
            log.error('Pid file exists, but cannot send signal to it - %s', err)