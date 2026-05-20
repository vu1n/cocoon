from importlib.metadata import PackageNotFoundError, version

# Distribution name on PyPI (the bare `cocoon` slot was taken by an
# abandoned 2021 placeholder). Python import path stays `cocoon`; only
# `pip install cocoon-mcp` and metadata-reflective lookups use the
# hyphenated form.
try:
    __version__ = version("cocoon-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
