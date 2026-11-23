"""All-module dependency and startup declarations."""

from .center import CenterAssembly
from .graph import (
    ArchitectureAssembly,
    SceneAssembly,
    declare_assembly,
)
from .station import StationAssembly

__all__ = [
    "ArchitectureAssembly",
    "CenterAssembly",
    "SceneAssembly",
    "StationAssembly",
    "declare_assembly",
]
