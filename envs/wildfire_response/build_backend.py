"""Build a portable artifact from the shared engine without vendoring source in git."""
import contextlib
import os
from pathlib import Path
import shutil
import tempfile

from setuptools import build_meta

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent


@contextlib.contextmanager
def staged():
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='wildfire-package-') as directory:
        target = Path(directory)
        shutil.copytree(PACKAGE, target/'envs'/'wildfire_response',
                        ignore=shutil.ignore_patterns('__pycache__', 'build', '*.egg-info', 'tests'))
        shutil.copyfile(ROOT/'envs'/'__init__.py', target/'envs'/'__init__.py')
        (target/'simulator'/'static'/'maps').mkdir(parents=True)
        for filename in ('__init__.py', 'engine.py', 'terrain.py', 'contacts.py', 'policy.py', 'oracle.py'):
            shutil.copyfile(ROOT/'simulator'/filename, target/'simulator'/filename)
        shutil.copyfile(ROOT/'simulator'/'static'/'maps'/'brunete-illustrated.json',
                        target/'simulator'/'static'/'maps'/'brunete-illustrated.json')
        config = (PACKAGE/'pyproject.toml').read_text()
        config = config.replace('build-backend = "build_backend"', 'build-backend = "setuptools.build_meta"')
        config = config.replace('backend-path = ["."]\n', '')
        (target/'pyproject.toml').write_text(config)
        shutil.copyfile(PACKAGE/'README.md', target/'README.md')
        try:
            os.chdir(target)
            yield
        finally:
            os.chdir(original)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    destination = str(Path(wheel_directory).resolve())
    with staged():
        return build_meta.build_wheel(destination, config_settings)


def build_sdist(sdist_directory, config_settings=None):
    destination = str(Path(sdist_directory).resolve())
    with staged():
        return build_meta.build_sdist(destination, config_settings)


def get_requires_for_build_wheel(config_settings=None):
    return ['setuptools>=69', 'wheel']


def get_requires_for_build_sdist(config_settings=None):
    return ['setuptools>=69', 'wheel']
