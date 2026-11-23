"""Inspect composition construction and contract boundaries without legacy modules."""

from __future__ import annotations

import ast
import inspect
import shutil
import subprocess
import sys
import unittest
from collections import Counter
from dataclasses import is_dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import get_args, get_type_hints
from unittest.mock import patch

from chongming.composition.center import CenterAssembly, declare_center, reference_policy
from chongming.composition.station import StationAssembly, declare_station
from chongming.contracts.device import (
    CalibrationPolicy,
    DevicePolicy,
    RayGenerationPolicy,
)
from chongming.contracts.fusion import CandidateMethod
from chongming.contracts.model import SessionId
from chongming.contracts.runtime import ModuleNode
from chongming.modules import base
from chongming.modules.base import ModuleShell
from chongming.modules.fusion import DirectGeometryCandidateModule
from chongming.modules.fusion_geometry import VoxelCandidateModule
from chongming.ports import development, device, fusion
from tests.online_fixtures import DEFAULT_SEARCH, default_clock_mapping
from tests.test_center_process import _station_config_and_aux
from tests.test_detection import detection_policy, tracking_policy


def _construct_center(
    *, method: CandidateMethod = CandidateMethod.COARSE_VOXEL
) -> tuple[CenterAssembly, tuple[ModuleNode, ...], tuple[ModuleShell, ...]]:
    created: list[ModuleShell] = []

    def record_new(cls: type[ModuleShell], *args: object, **kwargs: object) -> ModuleShell:
        instance = object.__new__(cls)
        created.append(instance)
        return instance

    policy = reference_policy(
        search=DEFAULT_SEARCH,
        clock_domain_id="sim-clock",
        window_width_ns=20_000_000,
    )
    policy = replace(policy, candidates=replace(policy.candidates, primary_method=method))
    with patch.object(ModuleShell, "__new__", new=record_new):
        assembly, bindings = declare_center(
            center_session_id=SessionId("center-arch"),
            policy=policy,
            clock_mapping=default_clock_mapping("sim-clock"),
        )
    nodes = tuple(binding.node for binding in bindings)
    return assembly, nodes, tuple(created)


def _construct_station() -> tuple[StationAssembly, tuple[ModuleNode, ...], tuple[ModuleShell, ...]]:
    created: list[ModuleShell] = []

    def record_new(cls: type[ModuleShell], *args: object, **kwargs: object) -> ModuleShell:
        instance = object.__new__(cls)
        created.append(instance)
        return instance

    config, _ = _station_config_and_aux("west", (-20.0, -40.0, 4.0))
    policy = DevicePolicy(
        detection=detection_policy(),
        tracking=tracking_policy(),
        calibration=CalibrationPolicy(
            estimator_id="opencv_pnp_ransac_v1",
            update_period_ns=1_000_000_000,
            stale_after_ns=5_000_000_000,
            recovery_success_count=2,
            minimum_reference_count=4,
            maximum_reprojection_rmse_px=2.0,
            maximum_rotation_only_parallax_px=5.0,
        ),
        rays=RayGenerationPolicy(
            uncertainty_model_id="tangent_gaussian_v1",
            fallback_centroid_covariance_px2=((0.25, 0.0), (0.0, 0.25)),
            unmodelled_high_frequency_jitter_covariance_rad2=None,
            unmodelled_relative_flex_covariance_rad2=None,
            residual_independence_basis=None,
        ),
    )
    with patch.object(ModuleShell, "__new__", new=record_new):
        assembly, bindings = declare_station(
            config=config,
            policy=policy,
            image_session_id=SessionId("sess-arch"),
        )
    nodes = tuple(binding.node for binding in bindings)
    return assembly, nodes, tuple(created)


def _port_classes(module: ModuleType) -> tuple[type[object], ...]:
    return tuple(
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ == module.__name__ and getattr(cls, "_is_protocol", False)
    )


def _assert_pending_stubs(
    case: unittest.TestCase, tree: ast.Module, expected: dict[str, set[str]]
) -> None:
    """Check only named pending methods, including that every entry still exists."""
    discovered: dict[str, set[str]] = {}
    for declaration in tree.body:
        if not isinstance(declaration, ast.ClassDef) or declaration.name not in expected:
            continue
        discovered[declaration.name] = set()
        for method in declaration.body:
            if (
                not isinstance(method, ast.FunctionDef)
                or method.name not in expected[declaration.name]
            ):
                continue
            discovered[declaration.name].add(method.name)
            route = f"{declaration.name}.{method.name}"
            body = list(method.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
            case.assertEqual(len(body), 1, f"{route}: pending stub must only raise")
            case.assertIsInstance(body[0], ast.Raise, f"{route}: pending stub must only raise")
            assert isinstance(body[0], ast.Raise)
            exception = body[0].exc
            case.assertIsInstance(exception, ast.Call, route)
            assert isinstance(exception, ast.Call)
            case.assertIsInstance(exception.func, ast.Name, route)
            assert isinstance(exception.func, ast.Name)
            case.assertEqual(exception.func.id, "NotImplementedError", route)
            case.assertFalse(exception.keywords, route)
            case.assertLessEqual(len(exception.args), 1, route)
            for argument in exception.args:
                case.assertIsInstance(argument, (ast.Name, ast.Constant), route)
    case.assertEqual(discovered, expected, "Pending stub entries must resolve")


def _assert_resolvable_dag(case: unittest.TestCase, nodes: tuple[ModuleNode, ...]) -> None:
    node_map = {node.module_key: node for node in nodes}
    case.assertEqual(len(node_map), len(nodes), "Module keys must be unique")
    for node in node_map.values():
        with case.subTest(module=node.module_key):
            case.assertTrue(set(node.dependencies) <= node_map.keys())
            case.assertEqual(len(node.dependencies), len(set(node.dependencies)))

    remaining = {key: set(node.dependencies) for key, node in node_map.items()}
    resolved: set[str] = set()
    while remaining:
        ready = {key for key, dependencies in remaining.items() if dependencies <= resolved}
        case.assertTrue(ready, f"Dependency cycle among {sorted(remaining)}")
        resolved.update(ready)
        for key in ready:
            del remaining[key]
    case.assertEqual(resolved, set(node_map))


class ArchitectureStructureTests(unittest.TestCase):
    def test_every_constructed_shell_is_bound_once_in_resolvable_dag(self) -> None:
        center, center_nodes, center_created = _construct_center()
        self.assertTrue(center_created)
        _assert_resolvable_dag(self, center_nodes)
        self.assertIs(type(center), CenterAssembly)

        station, station_nodes, station_created = _construct_station()
        self.assertTrue(station_created)
        _assert_resolvable_dag(self, station_nodes)
        self.assertIs(type(station), StationAssembly)

    def test_public_center_exports_resolve_to_the_bound_implementations(self) -> None:
        from chongming import composition, modules

        assembly, _, _ = _construct_center()
        self.assertIs(composition.CenterAssembly, type(assembly))
        self.assertIs(modules.CenterFusionModule, type(assembly.fusion))
        self.assertIs(modules.CenterTrackingModule, type(assembly.tracking))
        self.assertIs(modules.CrossStationAssociationModule, type(assembly.association))
        self.assertIs(modules.CalibrationWeightingModule, type(assembly.weights))
        self.assertTrue(assembly.weights.business_implemented)
        self.assertIs(assembly.voxels.weights, assembly.weights)
        assert isinstance(assembly.association, modules.CrossStationAssociationModule)
        self.assertIs(assembly.association.weights, assembly.weights)
        self.assertIs(assembly.geometry.weights, assembly.weights)

    def test_candidate_methods_keep_all_center_modules_constructed(self) -> None:
        signatures: set[frozenset[tuple[str, type[object]]]] = set()
        for method in CandidateMethod:
            with self.subTest(method=method):
                assembly, nodes, constructed = _construct_center(method=method)
                signatures.add(frozenset((node.module_key, ModuleNode) for node in nodes))
                self.assertEqual(assembly.candidates.policy.primary_method, method)
                self.assertIs(assembly.candidates.voxel, assembly.voxels)
                self.assertIs(assembly.candidates.direct, assembly.direct_candidates)
                self.assertTrue(any(isinstance(m, VoxelCandidateModule) for m in constructed))
                self.assertTrue(
                    any(isinstance(m, DirectGeometryCandidateModule) for m in constructed)
                )
        self.assertEqual(len(signatures), 1)
        self.assertEqual(Counter(m.__class__ for m in constructed)[VoxelCandidateModule], 1)

    def test_station_sequencer_ownership_is_consistent(self) -> None:
        station, _, _ = _construct_station()
        seq = station.sequencer
        self.assertIs(station.calibration_states.sequencer, seq)
        self.assertIs(station.calibration.states, station.calibration_states)
        self.assertIs(station.rays.sequencer, seq)
        self.assertIs(station.device.sequencer, seq)
        self.assertIs(station.device.calibrator, station.calibration)
        self.assertIs(station.device.ray_builder, station.rays)
        self.assertEqual(station.device.station_session_id, seq.station_session_id)
        self.assertEqual(station.history.station_config_digest, station.config.config_digest)
        self.assertEqual(
            station.calibration.update_period_ns,
            station.calibration_states.policy.update_period_ns,
        )

    def test_online_port_types_cannot_reach_scene_truth_or_generation_settings(self) -> None:
        ports: tuple[type[object], ...] = (
            *_port_classes(device),
            *_port_classes(fusion),
            *_port_classes(development),
        )
        checked_methods = 0

        def check_type(annotation: object, route: str, visited: set[object]) -> None:
            if annotation in visited:
                return
            visited.add(annotation)
            module_name = getattr(annotation, "__module__", "") or ""
            self.assertFalse(
                module_name.startswith(("chongming_proto.truth", "chongming_scorer")),
                f"Truth/scorer type leaked through {route}: {annotation}",
            )
            for argument in get_args(annotation):
                check_type(argument, route, visited)
            underlying = getattr(annotation, "__supertype__", None)
            if underlying is not None:
                check_type(underlying, route, visited)
            if inspect.isclass(annotation) and is_dataclass(annotation):
                for field, nested in get_type_hints(annotation).items():
                    check_type(nested, f"{route}.{annotation.__name__}.{field}", visited)

        for port in ports:
            for name, method in inspect.getmembers(port, inspect.isfunction):
                if name.startswith("_"):
                    continue
                checked_methods += 1
                with self.subTest(port=port.__name__, method=name):
                    for parameter, annotation in get_type_hints(method).items():
                        check_type(annotation, f"{port.__name__}.{name}.{parameter}", set())
        self.assertGreater(checked_methods, 0)

    def test_remaining_shell_methods_are_explicit_stubs(self) -> None:
        module_directory = Path(inspect.getfile(base)).parent
        pending = {
            "fusion.py": {
                "DirectGeometryCandidateModule": {"propose"},
            },
        }
        for filename, classes in pending.items():
            source = module_directory / filename
            with self.subTest(source=filename):
                _assert_pending_stubs(self, ast.parse(source.read_text()), classes)

    def test_stub_guard_allows_implemented_methods_and_rejects_silent_stubs(self) -> None:
        for classname in ("MotionDetectionModule", "StationRegistryModule"):
            with self.subTest(cls=classname):
                tree = ast.parse(
                    f"class {classname}:\n"
                    "    def implemented(self):\n"
                    "        return 1\n"
                    "    def pending(self):\n"
                    "        raise NotImplementedError('pending')\n"
                )
                _assert_pending_stubs(self, tree, {classname: {"pending"}})
                with self.assertRaisesRegex(AssertionError, "pending stub must only raise"):
                    _assert_pending_stubs(self, tree, {classname: {"implemented"}})

    def test_scorer_package_never_imports_chongming_business_package(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        scorer_dir = repo_root / "src" / "chongming_scorer"
        for path in sorted(scorer_dir.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name == "chongming" or alias.name.startswith("chongming."),
                            f"{path}: forbidden business import {alias.name!r}",
                        )
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    self.assertFalse(
                        node.module == "chongming" or node.module.startswith("chongming."),
                        f"{path}: forbidden business import from {node.module!r}",
                    )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys, chongming_scorer.cli; "
                    "assert 'chongming' not in sys.modules, "
                    "[m for m in sys.modules if m == 'chongming' or m.startswith('chongming.')]; "
                    "assert not any(m.startswith('chongming.') for m in sys.modules)"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.returncode, 0)

    def test_business_package_never_imports_truth_proto_or_scorer(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        business_dir = repo_root / "src" / "chongming"
        forbidden_prefixes = (
            "chongming_scorer",
            "chongming_proto.truth",
            "chongming_proto.v1.truth_pb2",
        )
        for path in sorted(business_dir.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for prefix in forbidden_prefixes:
                            self.assertFalse(
                                alias.name == prefix or alias.name.startswith(prefix + "."),
                                f"{path}: forbidden test-framework import {alias.name!r}",
                            )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for prefix in forbidden_prefixes:
                        self.assertFalse(
                            module == prefix or module.startswith(prefix + "."),
                            f"{path}: forbidden test-framework import from {module!r}",
                        )
                    if module == "chongming_proto" or module.startswith("chongming_proto."):
                        for alias in node.names:
                            self.assertNotIn(
                                alias.name,
                                {"truth", "truth_pb2"},
                                f"{path}: forbidden truth import {alias.name!r} from {module!r}",
                            )
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys, chongming_proto.online; "
                    "assert 'chongming_proto.truth' not in sys.modules; "
                    "assert 'chongming_proto.v1.truth_pb2' not in sys.modules"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_proto_bindings_register_expected_v1_messages(self) -> None:
        from chongming_proto.online import common_pb2, online_pb2
        from chongming_proto.truth import truth_pb2

        self.assertTrue(hasattr(common_pb2, "CaptureTime"))
        self.assertTrue(hasattr(online_pb2, "BusMessage"))
        self.assertTrue(hasattr(online_pb2, "StationConfig"))
        self.assertTrue(hasattr(truth_pb2, "FrameIndexEntry"))
        self.assertTrue(hasattr(truth_pb2, "TruthFrame"))
        self.assertTrue(hasattr(truth_pb2, "ScoreEvent"))
        if shutil.which("protoc") is not None:
            repo_root = Path(__file__).resolve().parents[1]
            subprocess.run(
                [sys.executable, str(repo_root / "scripts" / "generate_proto.py"), "--check"],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_online_bus_and_vision_never_import_legacy_scene_or_rendering(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        src_root = repo_root / "src" / "chongming"
        targets = [
            *sorted((src_root / "bus").rglob("*.py")),
            *sorted((src_root / "vision").rglob("*.py")),
            src_root / "composition" / "center.py",
            src_root / "composition" / "station.py",
            src_root / "contracts" / "json_codec.py",
            src_root / "reference.py",
        ]
        forbidden_prefixes = (
            "chongming.scenarios",
            "chongming.rendering",
            "chongming.contracts.scene",
            "chongming.modules.scene",
            "chongming.preview",
            "chongming.development",
            "chongming.edge_runtime",
            "chongming.center_ui",
        )
        for path in targets:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for prefix in forbidden_prefixes:
                            self.assertFalse(
                                alias.name == prefix or alias.name.startswith(prefix + "."),
                                f"{path}: forbidden legacy import {alias.name!r}",
                            )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for prefix in forbidden_prefixes:
                        self.assertFalse(
                            module == prefix or module.startswith(prefix + "."),
                            f"{path}: forbidden legacy import from {module!r}",
                        )


if __name__ == "__main__":
    unittest.main()
