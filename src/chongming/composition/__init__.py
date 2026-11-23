"""All-module dependency and startup declarations."""

from .center import CenterAssembly
from .station import StationAssembly

__all__ = [
    "CenterAssembly",
    "StationAssembly",
]
