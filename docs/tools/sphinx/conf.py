# Sphinx configuration of the collections' reference site (`just docs`, tools/docs.py). From
# what `antsibull-docs sphinx-init` writes. The pages link into Ansible's own documentation
# (common return values, configuration settings), so the build fetches its inventory once
# (intersphinx; network). `just docs-lint`, what CI runs, needs no network.
project = "rxrbench"
copyright = "rxrbench contributors"
title = "rxrbench collections"
html_short_title = "rxrbench collections"

extensions = ["sphinx.ext.autodoc", "sphinx.ext.intersphinx", "sphinx_antsibull_ext"]
intersphinx_mapping = {
    "ansible_devel": ("https://docs.ansible.com/projects/ansible/devel/", None),
}

pygments_style = "ansible"
highlight_language = "YAML+Jinja"

html_theme = "sphinx_ansible_theme"
# Every page's top bar links back to the project and to the reference's front page
html_theme_options = {
    "topbar_links": {
        "rxrbench on GitHub": "https://github.com/roperscrossroads/rxrbench",
        "Reference home": "https://roperscrossroads.github.io/rxrbench/",
    },
    "documentation_home_url": "https://roperscrossroads.github.io/rxrbench/",
}
html_show_sphinx = False
display_version = False
html_use_smartypants = True
html_use_modindex = False
html_use_index = False
html_copy_source = False

default_role = "any"
nitpicky = True
