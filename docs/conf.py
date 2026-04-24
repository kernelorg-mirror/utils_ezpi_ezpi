# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import sys
from pathlib import Path

# Add the src directory to the path for autodoc
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import ezpi

# -- Project information -----------------------------------------------------
project = 'EZPI'
copyright = '2020, The Linux Foundation'
author = 'Konstantin Ryabitsev'
version = ezpi.__VERSION__
release = ezpi.__VERSION__

# -- General configuration ---------------------------------------------------
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# -- Options for HTML output -------------------------------------------------
html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

# Create _static directory if it doesn't exist (needed for RTD)
Path(__file__).parent.joinpath('_static').mkdir(exist_ok=True)

# -- Intersphinx configuration -----------------------------------------------
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
}

# -- Autodoc configuration ---------------------------------------------------
autodoc_member_order = 'bysource'
autodoc_typehints = 'description'
