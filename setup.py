#!/usr/bin/env python3
# Fallback for older setuptools that don't support pyproject.toml [project] table
import re
from pathlib import Path

from setuptools import setup

init_py = Path('src/ezpi/__init__.py').read_text()
version = re.search(r"^__VERSION__\s*=\s*['\"]([^'\"]+)['\"]", init_py, re.MULTILINE).group(1)
setup(version=version)
