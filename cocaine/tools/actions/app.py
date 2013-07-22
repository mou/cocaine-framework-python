import os
import re
import shutil
import tarfile
import tempfile
import logging
import msgpack

from cocaine.exceptions import ToolsError
from cocaine.futures import chain
from cocaine.futures.chain import Chain
from cocaine.tools import actions
from cocaine.tools.actions import common
from cocaine.tools.installer import PythonModuleInstaller, ModuleInstallError
from cocaine.tools.repository import GitRepositoryDownloader, RepositoryDownloadError
from cocaine.tools.encoders import JsonEncoder, PackageEncoder
from cocaine.tools.tags import APPS_TAGS

__author__ = 'Evgeny Safronov <division494@gmail.com>'


class List(actions.List):
    def __init__(self, storage, **config):
        super(List, self).__init__('manifests', APPS_TAGS, storage, **config)


class View(actions.Storage):
    def __init__(self, storage, **config):
        super(View, self).__init__(storage, **config)
        self.name = config.get('name')
        if not self.name:
            raise ValueError('Specify name of application')

    def execute(self):
        return self.storage.read('manifests', self.name)


class Upload(actions.Storage):
    """
    Storage action class that tries to upload application into storage asynchronously
    """
    def __init__(self, storage, **config):
        super(Upload, self).__init__(storage, **config)
        self.name = config.get('name')
        self.manifest = config.get('manifest')
        self.manifestRaw = config.get('manifest-raw')
        self.package = config.get('package')
        self.jsonEncoder = JsonEncoder()
        self.packageEncoder = PackageEncoder()

        if not self.name:
            raise ValueError('Please specify name of the app')
        if not any([self.manifest, self.manifestRaw]):
            raise ValueError('Please specify manifest of the app')
        if not self.package:
            raise ValueError('Please specify package of the app')

    def execute(self):
        """
        Encodes manifest and package files and (if successful) uploads them into storage
        """
        return Chain().then(self.do)

    def do(self):
        if self.manifest:
            manifest = self.jsonEncoder.encode(self.manifest)
        else:
            manifest = msgpack.dumps(self.manifestRaw)
        package = self.packageEncoder.encode(self.package)
        yield self.storage.write('manifests', self.name, manifest, APPS_TAGS)
        yield self.storage.write('apps', self.name, package, APPS_TAGS)
        yield 'Done'


class Remove(actions.Storage):
    """
    Storage action class that removes application 'name' from storage
    """
    def __init__(self, storage, **config):
        super(Remove, self).__init__(storage, **config)
        self.name = config.get('name')
        if not self.name:
            raise ValueError('Empty application name')

    def execute(self):
        return Chain([self.do])

    def do(self):
        yield self.storage.remove('manifests', self.name)
        yield self.storage.remove('apps', self.name)
        yield 'Done'


class Start(common.Node):
    def __init__(self, node, **config):
        super(Start, self).__init__(node, **config)
        self.name = config.get('name')
        self.profile = config.get('profile')
        if not self.name:
            raise ValueError('Please specify application name')
        if not self.profile:
            raise ValueError('Please specify profile name')

    def execute(self):
        apps = {
            self.name: self.profile
        }
        return self.node.start_app(apps)


class Stop(common.Node):
    def __init__(self, node, **config):
        super(Stop, self).__init__(node, **config)
        self.name = config.get('name')
        if not self.name:
            raise ValueError('Please specify application name')

    def execute(self):
        future = self.node.pause_app([self.name])
        return future


class Restart(common.Node):
    def __init__(self, node, **config):
        super(Restart, self).__init__(node, **config)
        self.name = config.get('name')
        self.profile = config.get('profile')
        if not self.name:
            raise ValueError('Please specify application name')

    def execute(self):
        return Chain([self.doAction])

    def doAction(self):
        try:
            info = yield common.NodeInfo(self.node, **self.config).execute()
            profile = self.profile or info['apps'][self.name]['profile']
            appStopStatus = yield Stop(self.node, **self.config).execute()
            appStartConfig = {
                'host': self.config['host'],
                'port': self.config['port'],
                'name': self.name,
                'profile': profile
            }
            appStartStatus = yield Start(self.node, **appStartConfig).execute()
            yield [appStopStatus, appStartStatus]
        except KeyError:
            raise ToolsError('Application "{0}" is not running and profile not specified'.format(self.name))
        except Exception as err:
            raise ToolsError('Unknown error - {0}'.format(err))


class Check(common.Node):
    def __init__(self, node, **config):
        super(Check, self).__init__(node, **config)
        self.name = config.get('name')
        if not self.name:
            raise ValueError('Please specify application name')

    def execute(self):
        return Chain([self.do])

    def do(self):
        state = 'stopped or missing'
        try:
            info = yield self.node.info()
            apps = info['apps']
            app = apps[self.name]
            state = app['state']
        except KeyError:
            pass
        yield {self.name: state}


class LocalUpload(actions.Storage):
    def __init__(self, storage, **config):
        super(LocalUpload, self).__init__(storage, **config)
        self.path = config.get('path', '.')
        self.name = config.get('name')
        self._log = logging.getLogger(self.__module__ + '.' + self.__class__.__name__)

    def execute(self):
        return Chain().then(self._doMagic)

    def _doMagic(self):
        if self.name is None:
            self.name = os.path.basename(os.path.abspath(self.path))

        if not self.name:
            raise ToolsError('Application has not valid name: "{0}"'.format(self.name))

        # Locate manifests. Priority:
        # root+json     - 111
        # root          - 101
        # other+json    - 11
        # other         - 1
        manifests = []
        for root, dirNames, fileNames in os.walk(self.path):
            for fileName in fileNames:
                if fileName.startswith('manifest'):
                    priority = 1
                    if root == self.path:
                        priority += 100
                    if fileName == 'manifest.json':
                        priority += 10
                    manifests.append((os.path.join(root, fileName), priority))
        manifests = sorted(manifests, key=lambda manifest: manifest[1], reverse=True)
        self._log.debug('Manifests found: {0}'.format(manifests))
        if not manifests:
            raise ToolsError('No manifest file found in "{0}" or subdirectories'.format(os.path.abspath(self.path)))

        manifest, priority = manifests[0]
        manifestPath = os.path.abspath(manifest)

        # Pack all
        repositoryPath = tempfile.mkdtemp()
        repositoryPath = os.path.join(repositoryPath, 'repo')
        shutil.copytree(self.path, repositoryPath)
        packagePath = os.path.join(repositoryPath, 'package.tar.gz')
        with tarfile.open(packagePath, mode='w:gz') as tar:
            tar.add(repositoryPath, arcname='')

        # Upload
        self._log.debug('Repository path: {0}'.format(repositoryPath))
        self._log.debug('Manifest path: {0}'.format(manifestPath))
        self._log.debug('Package path: {0}'.format(packagePath))
        yield Upload(self.storage, **{
            'name': self.name,
            'manifest': manifestPath,
            'package': packagePath
        }).execute()
        yield 'Application {0} has been successfully uploaded'.format(self.name)


class UploadRemote(actions.Storage):
    def __init__(self, storage, **config):
        super(UploadRemote, self).__init__(storage, **config)
        self.name = config.get('name')
        self.url = config.get('url')
        if not self.url:
            raise ValueError('Please specify repository URL')
        if not self.name:
            rx = re.compile(r'^.*/(?P<name>.*?)(\..*)?$')
            match = rx.match(self.url)
            self.name = match.group('name')

    def execute(self):
        return Chain([self.doWork])

    def doWork(self):
        repositoryPath = tempfile.mkdtemp()
        manifestPath = os.path.join(repositoryPath, 'manifest-start.json')
        packagePath = os.path.join(repositoryPath, 'package.tar.gz')
        self.repositoryDownloader = GitRepositoryDownloader()
        self.moduleInstaller = PythonModuleInstaller(repositoryPath, manifestPath)
        print('Repository path: {0}'.format(repositoryPath))
        try:
            yield self.cloneRepository(repositoryPath)
            yield self.installRepository()
            yield self.createPackage(repositoryPath, packagePath)
            yield Upload(self.storage, **{
                'name': self.name,
                'manifest': manifestPath,
                'package': packagePath
            }).execute()
        except (RepositoryDownloadError, ModuleInstallError) as err:
            print(err)

    @chain.concurrent
    def cloneRepository(self, repositoryPath):
        self.repositoryDownloader.download(self.url, repositoryPath)

    @chain.concurrent
    def installRepository(self):
        self.moduleInstaller.install()

    @chain.concurrent
    def createPackage(self, repositoryPath, packagePath):
        tar = tarfile.open(packagePath, mode='w:gz')
        tar.add(repositoryPath, arcname='')