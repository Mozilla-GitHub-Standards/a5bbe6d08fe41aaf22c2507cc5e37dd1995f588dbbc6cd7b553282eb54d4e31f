#!/usr/bin/env python3.6
# -*- coding: utf-8 -*-
'''
config
'''

import os
import re
import pwd
import sys
import time
import logging
import sh

from fnmatch import fnmatch
from decouple import UndefinedValueError, AutoConfig, config, Config, RepositoryIni, RepositoryEnv, RepositoryEmpty

LOG_LEVELS = [
    'DEBUG',
    'INFO',
    'WARNING',
    'ERROR',
    'CRITICAL',
]

LOG_LEVEL = config('LOG_LEVEL', logging.WARNING, cast=int)

logging.basicConfig(
    stream=sys.stdout,
    level=LOG_LEVEL,
    format='%(asctime)s %(name)s %(message)s')
logging.Formatter.converter = time.gmtime
log = logging.getLogger(__name__)

class ProjNameSplitError(Exception):
    '''
    ProjNameSplitError
    '''
    def __init__(self, basename):
        '''
        init
        '''
        msg = f'projname split error on "-" with basename={basename}'
        super(ProjNameSplitError, self).__init__(msg)

class NotGitRepoError(Exception):
    '''
    NotGitRepoError
    '''
    def __init__(self):
        '''
        init
        '''
        msg = 'not a git repository error'
        super(NotGitRepoError, self).__init__(msg)

class NoGitRepoOrEnvError(Exception):
    '''
    NoGitRepoOrEnvError
    '''
    def __init__(self):
        '''
        init
        '''
        msg = 'no git repo or env found error'
        super(NoGitRepoOrEnvError, self).__init__(msg)

def git(*args, strip=True, **kwargs):
    '''
    git
    '''
    try:
        result = str(sh.contrib.git(*args, **kwargs)) #pylint: disable=no-member
        if strip:
            result = result.strip()
        return result
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode('utf-8')
        if 'not a git repository' in stderr.lower():
            raise NotGitRepoError
        log.error(e)

class AutoConfigs(object): #pylint: disable=too-many-public-methods
    '''
    composes multiple config objects similar to how AutoConfig works
    '''

    @property
    def caller_path(self):
        frame = sys._getframe()
        path = os.path.dirname(frame.f_back.f_back.f_code.co_filename)
        if path == '/usr/local/lib/python3.6/dist-packages/IPython/core':
            path = '/home/sidler/repos/mozilla-it/props-bot/props/bot'
        return path

    def __init__(self, search_path=None, *patterns):
        self.search_path = search_path
        self.patterns = patterns or ('*.env', '*.ini')
        self.configs = []

    def _find_files(self, path):
        matches = []
        filenames = [filename for filename in os.listdir(path) if os.path.isfile(filename)]
        for pattern in self.patterns:
            matches += [filename for filename in filenames if fnmatch(filename, pattern)]
        prefix_path = os.path.commonprefix([self.caller_path, path])
        return matches

    def _load_configs(self, path):
        try:
            filenames = self._find_files(os.path.abspath(path))
        except Exception:
            filenames = ['']
        for filename in filenames:
            if filename.endswith('.ini'):
                repo = RepositoryIni(os.path.basename(filename))
            elif filename.endswith('.env'):
                repo = RepositoryEnv(os.path.basename(filename))
            else:
                repo = RepositoryEmpty(filename)
            self.configs += [Config(repo)]
        return self.configs

    def _lookup(self, *args, **kwargs):
        '''
        lookup
        '''
        log.info(f'args={args} kwargs={kwargs}')
        if not self.configs:
            self._load_configs(self.search_path or self.caller_path)
        if len(args) == 0:
            raise TypeError("get() missing 1 required positional argument: 'option'")
        result = UndefinedValueError(f'{args[0]} not found. Declare it as envvar or define a default value.')
        for config in self.configs:
            try:
                result = config(*args, **kwargs)
                break
            except UndefinedValueError:
                continue
        if isinstance(result, UndefinedValueError):
            raise result
        try:
            return int(result)
        except ValueError:
            return result

    def __call__(self, *args, **kwargs):
        '''
        call
        '''
        return self._lookup(*args, **kwargs)

    def __getattr__(self, attr):
        '''
        getattr
        '''
        if attr == 'create_doit_tasks': #note: to keep pydoit's hands off
            return lambda: None
        return self._lookup(attr)

    @property
    def APP_UID(self):
        '''
        uid
        '''
        return os.getuid()

    @property
    def APP_GID(self):
        '''
        gid
        '''
        return pwd.getpwuid(self.APP_UID).pw_gid

    @property
    def APP_USER(self):
        '''
        user
        '''
        return pwd.getpwuid(self.APP_UID).pw_name

    @property
    def APP_PORT(self):
        '''
        port
        '''
        return self('APP_PORT', 5000, cast=int)

    @property
    def APP_JOBS(self):
        '''
        jobs
        '''
        try:
            return call('nproc')[1].strip()
        except: #pylint: disable=bare-except
            return 1

    @property
    def APP_TIMEOUT(self):
        '''
        timeout
        '''
        return self('APP_TIMEOUT', 120, cast=int)

    @property
    def APP_WORKERS(self):
        '''
        workers
        '''
        return self('APP_WORKERS', 2, cast=int)

    @property
    def APP_MODULE(self):
        '''
        module
        '''
        return self('APP_MODULE', 'main:app')

    @property
    def APP_REPOROOT(self):
        '''
        reporoot
        '''
        try:
            return git('rev-parse', '--show-toplevel')
        except NotGitRepoError:
            return self('APP_REPOROOT')

    @property
    def APP_TAGNAME(self):
        '''
        tagname
        '''
        tag = self.APP_VERSION.split('-')[0]
        depenv = {
            'prod': '',
            'stage': '-stage',
            'dev': '-dev'
        }[self.APP_DEPENV]
        return f'{tag}{depenv}'

    @property
    def APP_VERSION(self):
        '''
        version
        '''
        try:
            return git('describe', '--abbrev=7', '--always')
        except NotGitRepoError:
            return self('APP_VERSION')

    @property
    def APP_BRANCH(self):
        '''
        branch
        '''
        try:
            return git('rev-parse', '--abbrev-ref', 'HEAD')
        except NotGitRepoError:
            return self('APP_BRANCH')

    @property
    def APP_DEPENV(self):
        '''
        depenv
        '''
        env = 'dev'
        if self.APP_BRANCH == 'master':
            env = 'prod'
        elif self.APP_BRANCH.startswith('stage/'):
            env = 'stage'
        return env

    @property
    def APP_SRCTAR(self):
        '''
        srctar
        '''
        try:
            return self('APP_SRCTAR')
        except UndefinedValueError:
            return '.src.tar.gz'

    @property
    def APP_REVISION(self):
        '''
        revision
        '''
        try:
            return git('rev-parse', 'HEAD')
        except NotGitRepoError:
            return self('APP_REVISION')

    @property
    def APP_REMOTE_ORIGIN_URL(self):
        '''
        remote origin url
        '''
        try:
            return git('config', '--get', 'remote.origin.url')
        except NotGitRepoError:
            return self('APP_REMOTE_ORIGIN_URL')

    @property
    def APP_REPONAME(self):
        '''
        reponame
        '''
        pattern = r'((ssh|https)://)?(git@)?github.com[:/](?P<reponame>[A-Za-z0-9\/\-_]+)(.git)?'
        match = re.search(pattern, self.APP_REMOTE_ORIGIN_URL)
        return match.group('reponame')

    @property
    def APP_PROJNAME(self):
        '''
        projname
        '''
        basename = os.path.basename(self.APP_REPONAME)
        parts = os.path.basename(basename).split('-')
        if len(parts) == 2:
            return parts[0]
        raise ProjNameSplitError(basename)

    @property
    def APP_PROJPATH(self):
        '''
        projpath
        '''
        return os.path.join(self.APP_REPOROOT, self.APP_PROJNAME)

    @property
    def APP_BOTPATH(self):
        '''
        botpath
        '''
        return os.path.join(self.APP_PROJPATH, 'bot')

    @property
    def APP_DBPATH(self):
        '''
        dbpath
        '''
        return os.path.join(self.APP_PROJPATH, 'db')

    @property
    def APP_TESTPATH(self):
        '''
        testpath
        '''
        return os.path.join(self.APP_REPOROOT, 'tests')

    @property
    def APP_LS_REMOTE(self):
        '''
        ls-remote
        '''
        try:
            result = git('ls-remote', f'https://github.com/{self.APP_REPONAME}')
        except NotGitRepoError:
            result = self('APP_LS_REMOTE')
        return {
            refname: revision for revision, refname in [line.split() for line in result.split('\n')]
        }

    @property
    def APP_GSM_STATUS(self):
        '''
        gsm status
        '''
        try:
            result = git('submodule', 'status', strip=False)
        except NotGitRepoError:
            result = self('APP_GSM_STATUS')
        pattern = r'([ +-])([a-f0-9]{40}) ([A-Za-z0-9\/\-_.]+)( .*)?'
        matches = re.findall(pattern, result)
        states = {
            ' ': True,  # submodule is checked out the correct revision
            '+': False, # submodule is checked out to a different revision
            '-': None,  # submodule is not checked out
        }
        return {
            repopath: [revision, states[state]] for state, revision, repopath, _ in matches
        }


CFG = AutoConfigs()
