#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sh
import pwd
import sys
import glob

from doit import get_var
from ruamel import yaml
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__)+'/props/bot')
from config import CFG
from utils.shell import cd, call
from utils.dbg import dbg #FIXME: remember to remove this

## https://docs.docker.com/compose/compose-file/compose-versioning/
MINIMUM_DOCKER_COMPOSE_VERSION = '1.13' # allows compose format 3.0

LOG_LEVELS = [
    'DEBUG',
    'INFO',
    'WARNING',
    'ERROR',
    'CRITICAL',
]

DOIT_CONFIG = {
    'default_tasks': [
        'pull',
        'deploy',
        'rmimages',
        'rmvolumes',
        'count'
    ],
    'verbosity': 2,
}

DOCKER_COMPOSE_YML = yaml.safe_load(open(f'{CFG.APP_PROJPATH}/docker-compose.yml'))

class UnknownPkgmgrError(Exception):
    def __init__(self):
        super(UnknownPkgmgrError, self).__init__('unknown pkgmgr!')

def check_hash(program):
    from subprocess import check_call, CalledProcessError, PIPE
    try:
        check_call(f'hash {program}', shell=True, stdout=PIPE, stderr=PIPE)
        return True
    except CalledProcessError:
        return False

def get_pkgmgr():
    if check_hash('dpkg'):
        return 'deb'
    elif check_hash('rpm'):
        return 'rpm'
    elif check_hash('brew'):
        return 'brew'
    raise UnknownPkgmgrError

def pyfiles(path, exclude=None):
    pyfiles = set(Path(path).rglob('*.py')) - set(Path(exclude).rglob('*.py') if exclude else [])
    return [pyfile.as_posix() for pyfile in pyfiles]

def task_count():
    '''
    use the cloc utility to count lines of code
    '''
    excludes = [
        'dist',
        'venv',
        '__pycache__',
        'auto_cert_cli.egg-info',
    ]
    excludes = '--exclude-dir=' + ','.join(excludes)
    scandir = os.path.dirname(__file__)
    return {
        'actions': [
            f'cloc {excludes} {scandir}',
        ],
        'uptodate': [
            lambda: not check_hash('cloc'),
        ],
    }

def task_checkreqs():
    '''
    check for required software
    '''
    DEBS = [
        'docker-ce',
    ]
    RPMS = [
        'docker-ce',
    ]
    return {
        'deb': {
            'actions': [f'dpkg -s {deb} 2>&1 >/dev/null' for deb in DEBS],
        },
        'rpm': {
            'actions': ['rpm -q ' + rpm for rpm in RPMS], #FIXME: probably silent this?
        },
        'brew': {
            'actions': ['true'], #FIXME: check that this works?
        }
    }[get_pkgmgr()]

def task_dockercompose():
    '''
    assert docker-compose version ({0}) or higher
    '''
    from props.bot.utils.function import docstr
    docstr(MINIMUM_DOCKER_COMPOSE_VERSION)
    def check_docker_compose():
        import re
        from subprocess import check_output
        from packaging.version import parse as version_parse
        pattern = '(docker-compose version) ([0-9.]+(-rc[0-9])?)(, build [a-z0-9]+)'
        output = call('docker-compose --version')[1].strip()
        regex = re.compile(pattern)
        match = regex.search(output)
        version = match.groups()[1]
        assert version_parse(version) >= version_parse(MINIMUM_DOCKER_COMPOSE_VERSION)

    return {
        'actions': [
            check_docker_compose,
        ],
    }

def task_noroot():
    '''
    make sure script isn't run as root
    '''
    then = 'echo "   DO NOT RUN AS ROOT!"; echo; exit 1'
    bash = f'if [[ $(id -u) -eq 0 ]]; then {then}; fi'
    return {
        'actions': [
            f'bash -c \'{bash}\'',
        ],
    }

def task_pull():
    '''
    do a safe git pull
    '''
    submods = call("git submodule status | awk '{print $2}'")[1].split()
    test = '`git diff-index --quiet HEAD --`'
    pull = 'git pull --rebase'
    update = 'git submodule update --remote'
    dirty = 'echo "refusing to \'{cmd}\' because the tree is dirty"'
    dirty_pull, dirty_update = [dirty.format(cmd=cmd) for cmd in (pull, update)]

    yield {
        'name': 'mozilla-it/props-bot',
        'actions': [
            f'if {test}; then {pull}; else {dirty_pull}; exit 1; fi',
        ],
    }

    for submod in submods:
        yield {
            'name': submod,
            'actions': [
                f'cd {submod} && if {test}; then {update}; else {dirty_update}; exit 1; fi',
            ],
        }

def task_venv():
    '''
    setup venv
    '''
    yield {
        'name': 'main',
        'task_dep': [
            'noroot',
        ],
        'actions': [
            'virtualenv --python=$(which python3) venv',
            'venv/bin/pip3 install --upgrade pip',
            f'venv/bin/pip3 install -r {CFG.APP_TESTPATH}/requirements.txt',
        ],
    }
    for svc in DOCKER_COMPOSE_YML['services'].keys():
        reqfile = f'{CFG.APP_PROJPATH}/{svc}/requirements.txt'
        yield {
            'name': svc,
            'task_dep': [
                'noroot',
                'venv:main',
            ],
            'actions': [
                f'[ -f {reqfile} ] && venv/bin/pip3 install -r {reqfile} || true',
            ],
        }

def task_pyfiles():
    '''
    list all of the pyfiles
    '''
    pyfiles_list = '\n'.join(pyfiles(CFG.APP_PROJPATH, f'{CFG.APP_BOTPATH}/utils'))
    return {
        'task_dep': [
        ],
        'actions': [
            f'echo "{pyfiles_list}"',
        ],
    }

def task_pylint():
    '''
    run pylint before the build
    '''
    for svc in DOCKER_COMPOSE_YML['services'].keys():
        pyfiles_list = pyfiles(f'{CFG.APP_PROJPATH}/{svc}', f'{CFG.APP_PROJPATH}/{svc}/utils')
        for pyfile in pyfiles_list:
            yield {
                'name': f'{svc}/{pyfile}',
                'task_dep': [
                    'noroot',
                ],
                'actions': [
                    f'cd {CFG.APP_PROJPATH}/{svc} && pylint -j{CFG.APP_JOBS} --rcfile {CFG.APP_TESTPATH}/pylint.rc {pyfile} || true',
                ],
            }

def task_test():
    '''
    run pytest
    '''
    def has_tests(svc):
        try:
            sh.pytest(
                '--collect-only',
                f'{CFG.APP_TESTPATH}/{svc}',
                _env={'PYTHONPATH':f'{CFG.APP_PROJPATH}/{svc}'})
            return True
        except (sh.ErrorReturnCode_4, sh.ErrorReturnCode_5):
            return False
    for svc in DOCKER_COMPOSE_YML['services'].keys():
        PYTHONPATH = f'PYTHONPATH=.:{CFG.APP_PROJPATH}:{CFG.APP_PROJPATH}/{svc}:$PYTHONPATH'
        if has_tests(svc):
            yield {
                'name': svc,
                'task_dep': [
                    'noroot',
                    'pylint',
                    'venv'
                ],
                'actions': [
                    f'{PYTHONPATH} venv/bin/python3 -m pytest -s -vv {CFG.APP_TESTPATH}/{svc}',
                ],
            }

def task_tls():
    '''
    create server key, csr and crt files
    '''
    name = 'server'
    tls = f'/data/{CFG.APP_PROJNAME}/tls'
    env = 'PASS=TEST'
    envp = 'env:PASS'
    targets = [
        f'{tls}/{name}.key',
        f'{tls}/{name}.crt',
    ]
    subject = '/C=US/ST=Oregon/L=Portland/O=Connected-Workplace Server/OU=Server/CN=0.0.0.0'
    def uptodate():
        return all([os.path.isfile(t) for t in targets])
    return {
        'actions': [
            f'mkdir -p {tls}',
            f'{env} openssl genrsa -aes256 -passout {envp} -out {tls}/{name}.key 2048',
            f'{env} openssl req -new -passin {envp} -subj "{subject}" -key {tls}/{name}.key -out {tls}/{name}.csr',
            f'{env} openssl x509 -req -days 365 -in {tls}/{name}.csr -signkey {tls}/{name}.key -passin {envp} -out {tls}/{name}.crt',
            f'{env} openssl rsa -passin {envp} -in {tls}/{name}.key -out {tls}/{name}.key',
        ],
        'targets': targets,
        'uptodate': [uptodate],
    }

def task_tar():
    '''
    tar up source files, dereferncing symlinks
    '''
    tarball = 'app.tar.gz'
    excludes = ' '.join([
        f'--exclude={tarball}',
        '--exclude=__pycache__',
        '--exclude=*.pyc',
        '--exclude-vcs',
    ])
    for svc in DOCKER_COMPOSE_YML['services'].keys():
        imagename = f'itcw/{CFG.APP_PROJNAME}_{svc}'
        yield {
            'name': svc,
            'task_dep': [
                'noroot',
                'gitenv',
                'test',
            ],
            'actions': [
                f'cd {CFG.APP_PROJNAME}/{svc} && touch {tarball} && tar cvfhz {tarball} {excludes} .',
            ],
        }

def task_build():
    '''
    build flask|quart app via docker-compose
    '''
    actions = [
        f'cd {CFG.APP_PROJPATH} && docker-compose build',
    ]
    for svc in DOCKER_COMPOSE_YML['services'].keys():
        tarball = f'{CFG.APP_PROJPATH}/{svc}/app.tar.gz'
        imagename = f'itcw/{CFG.APP_PROJNAME}_{svc}'
        actions += [
            f'[ -f {tarball} ] && rm {tarball}',
            f'docker tag {imagename} {imagename}:{CFG.APP_TAGNAME}',
        ]
    return {
        'task_dep': [
            'noroot',
            'tar',
            'dockercompose',
        ],
        'actions': actions,
    }

def task_publish():
    '''
    publish docker image(s) to docker hub
    '''
    for svc in DOCKER_COMPOSE_YML['services'].keys():
        imagename = f'itcw/{CFG.APP_PROJNAME}_{svc}'
        yield {
            'name': svc,
            'task_dep': [
                'noroot',
                'build',
            ],
            'actions': [
                f'docker push {imagename}:{CFG.APP_TAGNAME}',
            ],
        }

def task_gitenv():
    '''
    create git.env for config to use for git env vars
    '''
    gitenv = f'{CFG.APP_BOTPATH}/git.env'
    envs = [
        f'APP_REPOROOT={CFG.APP_REPOROOT}',
        f'APP_VERSION={CFG.APP_VERSION}',
        f'APP_BRANCH={CFG.APP_BRANCH}',
        f'APP_REVISION={CFG.APP_REVISION}',
        f'APP_REMOTE_ORIGIN_URL={CFG.APP_REMOTE_ORIGIN_URL}',
    ]
    def write_gitenv():
        with open(gitenv, 'w') as f:
            for env in envs:
                f.write(env + '\n')
    return {
        'task_dep': [
            'noroot',
        ],
        'actions': [write_gitenv],
        'targets': [gitenv],
        'uptodate': [False],
    }

def task_deploy():
    '''
    deloy flask|quart app via docker-compose
    '''
    return {
        'task_dep': [
            'noroot',
            'checkreqs',
            'test',
            'build',
            'dockercompose',
        ],
        'actions': [
            f'cd {CFG.APP_PROJPATH} && docker-compose up --remove-orphans -d',
        ],
    }

def task_rmimages():
    '''
    remove dangling docker images
    '''
    query = '`docker images -q -f dangling=true`'
    return {
        'actions': [
            f'docker rmi {query}',
        ],
        'uptodate': [
            f'[ -z "{query}" ] && exit 0 || exit 1',
        ],
    }

def task_rmvolumes():
    '''
    remove dangling docker volumes
    '''
    query = '`docker volume ls -q -f dangling=true`'
    return {
        'actions': [
            f'docker volume rm {query}',
        ],
        'uptodate': [
            f'[ -z "{query}" ] && exit 0 || exit 1',
        ],
    }

def task_logs():
    '''
    simple wrapper that calls 'docker-compose logs'
    '''
    return {
        'actions': [
            f'cd {CFG.APP_PROJPATH} && docker-compose logs',
        ],
    }

def task_rmcache():
    '''
    recursively delete python cache files
    '''
    rmrf = 'rm -rf "{}" \;'
    return dict(
        actions=[
            f'sudo find {CFG.APP_REPOROOT} -depth -name __pycache__ -type d -exec {rmrf}',
            f'sudo find {CFG.APP_REPOROOT} -depth -name *.pyc -type f -exec {rmrf}',
        ]
    )

def task_tidy():
    '''
    delete cached files
    '''
    TIDY_FILES = [
        '.doit.db',
        'venv/',
        '.pytest_cache/',
    ]
    return {
        'actions': [
            'rm -rf ' + ' '.join(TIDY_FILES),
            'find . | grep -E "(__pycache__|\.pyc$)" | xargs rm -rf',
        ],
    }

def task_nuke():
    '''
    git clean and reset
    '''
    return {
        'task_dep': ['tidy'],
        'actions': [
            'docker-compose kill',
            'docker-compose rm -f',
            'git clean -fd',
            'git reset --hard HEAD',
        ],
    }

def task_prune():
    '''
    prune stopped containers
    '''
    return {
        'actions': ['docker rm `docker ps -q -f "status=exited"`'],
        'uptodate': ['[ -n "`docker ps -q -f status=exited`" ] && exit 1 || exit 0']
    }

def task_stop():
    '''
    stop running containers
    '''
    def check_docker_ps():
        cmd = 'docker ps --format "{{.Names}}" | grep ' + CFG.APP_PROJNAME + ' | { grep -v grep || true; }'
        out = call(cmd, throw=True)[1]
        return out.split('\n') if out else []
    containers = ' '.join(check_docker_ps())
    return {
        'actions': [
            f'docker rm -f {containers}',
        ],
        'uptodate': [
            lambda: len(check_docker_ps()) == 0,
        ],
    }

if __name__ == '__main__':
    print('should be run with doit installed')
    import doit
    doit.run(globals())
