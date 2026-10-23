"""Inspect graph construction and contract boundaries, without running algorithms.

The temporary AST guard covers only the explicit remaining stub methods. Remove
each entry when its implementation gains behavioral regression coverage; existing
implementations and new business methods are outside this guard.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from collections import Counter
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import get_args, get_type_hints
from unittest.mock import patch

from chongming.composition.graph import ArchitectureAssembly, declare_assembly
from chongming.contracts.fusion import CandidateMethod
from chongming.contracts.runtime import EventPath
from chongming.contracts.scene import (
    ImagingBatch,
    ImagingTruthBatch,
    ScenarioPacketOutput,
    ScenarioStep,
    ScenarioTruthOutput,
    ScenarioVideoOutput,
    VirtualObservationBatch,
    VirtualObservationTruthBatch,
)
from chongming.modules import base
from chongming.modules.base import ModuleShell
from chongming.modules.fusion import DirectGeometryCandidateModule, VoxelCandidateModule
from chongming.ports import development, device, fusion, runtime
from tests.fixtures import architecture_specs


def _construct(
    *,
    path: EventPath = EventPath.IMAGE_PIPELINE,
    method: CandidateMethod = CandidateMethod.COARSE_VOXEL,
) -> tuple[ArchitectureAssembly, tuple[ModuleShell, ...]]:
    """Capture actual managed shell allocations, including any discarded by assembly."""

    created: list[ModuleShell] = []

    def record_new(cls: type[ModuleShell], *args: object, **kwargs: object) -> ModuleShell:
        instance = object.__new__(cls)
        created.append(instance)
        return instance

    spec, run = architecture_specs(center_input=path, candidate_method=method)
    with patch.object(ModuleShell, "__new__", new=record_new):
        assembly = declare_assembly(spec, run)
    return assembly, tuple(created)


def _port_classes(module: ModuleType) -> tuple[type[object], ...]:
    return tuple(
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ == module.__name__ and getattr(cls, "_is_protocol", False)
    )


def _assert_pending_stubs(
    case: unittest.TestCase, tree: ast.Module, expected_stubs: Mapping[str, set[str]]
) -> None:
    """Enforce only named pending methods; implementations need behavioral tests."""

    discovered_stubs: dict[str, set[str]] = {}
    for declaration in tree.body:
        if not isinstance(declaration, ast.ClassDef):
            continue
        expected_methods = expected_stubs.get(declaration.name)
        if expected_methods is None:
            continue
        discovered_stubs[declaration.name] = set()
        for method in declaration.body:
            if not isinstance(method, ast.FunctionDef) or method.name not in expected_methods:
                continue
            discovered_stubs[declaration.name].add(method.name)
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
    case.assertEqual(discovered_stubs, expected_stubs, "Pending stub entries must resolve")


class ArchitectureStructureTests(unittest.TestCase):
    def test_scenario_step_has_three_explicit_typed_exits(self) -> None:
        field_names = tuple(field.name for field in fields(ScenarioStep))
        self.assertEqual(field_names, ("video", "packets", "truth"))
        self.assertTrue(is_dataclass(ScenarioVideoOutput))
        self.assertTrue(is_dataclass(ScenarioPacketOutput))
        self.assertTrue(is_dataclass(ScenarioTruthOutput))
        video_types = get_type_hints(ScenarioVideoOutput)
        packet_types = get_type_hints(ScenarioPacketOutput)
        truth_types = get_type_hints(ScenarioTruthOutput)
        self.assertNotIn("truth_object_id", video_types)
        self.assertNotIn("truth_object_id", packet_types)
        self.assertIn("online_correspondences", truth_types)

    def test_generation_batches_keep_online_and_truth_data_separate(self) -> None:
        self.assertEqual(tuple(field.name for field in fields(ImagingBatch)), ("frames",))
        self.assertEqual(tuple(field.name for field in fields(ImagingTruthBatch)), ("frame_truth",))
        self.assertNotIn(
            "truth_correspondences", {field.name for field in fields(VirtualObservationBatch)}
        )
        self.assertEqual(
            tuple(field.name for field in fields(VirtualObservationTruthBatch)),
            ("truth_correspondences",),
        )

    def test_every_constructed_shell_is_bound_once_in_resolvable_dag(self) -> None:
        assembly, constructed = _construct()
        constructed_ids = [id(module) for module in constructed]
        binding_ids = [id(binding.module) for binding in assembly.bindings]
        self.assertTrue(constructed_ids, "The factory must construct managed modules")
        self.assertEqual(Counter(binding_ids), Counter(constructed_ids))
        self.assertTrue(all(count == 1 for count in Counter(binding_ids).values()))

        nodes = {node.module_key: node for node in assembly.startup.modules}
        self.assertEqual(len(nodes), len(assembly.startup.modules), "Module keys must be unique")
        self.assertEqual(
            tuple(binding.node for binding in assembly.bindings), assembly.startup.modules
        )
        for node in nodes.values():
            with self.subTest(module=node.module_key):
                self.assertTrue(set(node.dependencies) <= nodes.keys())
                self.assertEqual(len(node.dependencies), len(set(node.dependencies)))

        remaining = {key: set(node.dependencies) for key, node in nodes.items()}
        resolved: set[str] = set()
        while remaining:
            ready = {key for key, dependencies in remaining.items() if dependencies <= resolved}
            self.assertTrue(ready, f"Dependency cycle among {sorted(remaining)}")
            resolved.update(ready)
            for key in ready:
                del remaining[key]
        self.assertEqual(resolved, set(nodes))

    def test_routes_and_candidate_methods_keep_all_modules_constructed(self) -> None:
        signatures: set[frozenset[tuple[str, type[object]]]] = set()
        for path in EventPath:
            for method in CandidateMethod:
                with self.subTest(path=path, method=method):
                    assembly, constructed = _construct(path=path, method=method)
                    self.assertEqual(
                        Counter(id(binding.module) for binding in assembly.bindings),
                        Counter(id(module) for module in constructed),
                    )
                    signatures.add(
                        frozenset(
                            (binding.node.module_key, type(binding.module))
                            for binding in assembly.bindings
                        )
                    )
                    self.assertEqual(assembly.runner.run_spec.center_input, path)
                    self.assertEqual(assembly.center.candidates.policy.primary_method, method)
                    self.assertIs(assembly.center.candidates.voxel, assembly.center.voxels)
                    self.assertIs(
                        assembly.center.candidates.direct, assembly.center.direct_candidates
                    )
                    self.assertTrue(any(isinstance(m, VoxelCandidateModule) for m in constructed))
                    self.assertTrue(
                        any(isinstance(m, DirectGeometryCandidateModule) for m in constructed)
                    )
        self.assertEqual(len(signatures), 1, "Selecting evidence must not skip a module")

    def test_station_sessions_and_sequencer_ownership_are_separate(self) -> None:
        assembly, _ = _construct()
        virtual = {s.station_id: s for s in assembly.scene.virtual_sequencers}
        self.assertEqual(len(virtual), len(assembly.stations))
        all_sequencers: list[ModuleShell] = []
        for station in assembly.stations:
            with self.subTest(station=station.config.station_id):
                image_sequence = station.sequencer
                virtual_sequence = virtual[station.config.station_id]
                self.assertNotEqual(
                    image_sequence.station_session_id, virtual_sequence.station_session_id
                )
                self.assertIs(station.calibration_states.sequencer, image_sequence)
                self.assertIs(station.calibration.states, station.calibration_states)
                self.assertIs(station.rays.sequencer, image_sequence)
                self.assertIs(station.device.sequencer, image_sequence)
                self.assertIs(station.device.calibrator, station.calibration)
                self.assertIs(station.device.ray_builder, station.rays)
                all_sequencers.extend((image_sequence, virtual_sequence))
        self.assertEqual(len({id(s) for s in all_sequencers}), len(all_sequencers))
        self.assertEqual(
            tuple(id(s) for s in assembly.scene.virtual_observations.sequencers),
            tuple(id(s) for s in assembly.scene.virtual_sequencers),
        )

    def test_online_port_types_cannot_reach_scene_truth_or_generation_settings(self) -> None:
        ports: tuple[type[object], ...] = (
            *_port_classes(device),
            *_port_classes(fusion),
            *_port_classes(development),
            runtime.EventTransport,
        )
        checked_methods = 0

        def check_type(annotation: object, route: str, visited: set[object]) -> None:
            if annotation in visited:
                return
            visited.add(annotation)
            self.assertNotEqual(
                getattr(annotation, "__module__", None),
                "chongming.contracts.scene",
                f"Privileged scenario type leaked through {route}: {annotation}",
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
        expected_stubs = {
            "device.py": {
                "MotionDetectionModule": {"detect"},
                "Tracking2DModule": {"update"},
                "MainDetectionModule": {"process"},
                "ReferenceRecognitionModule": {"recognize"},
                "CalibrationEstimationModule": {"estimate"},
                "CalibrationHistoryModule": {
                    "record_solution",
                    "record_state",
                    "lookup_solution",
                    "snapshot_at",
                },
                "CalibrationStateModule": {"consider", "tick"},
                "AuxiliaryCalibrationModule": {"process", "tick"},
                "RayGenerationModule": {"build"},
                "DeviceModule": {"process_main", "process_auxiliary", "tick"},
            },
            "fusion.py": {
                "StationRegistryModule": {"register", "lookup", "registered_stations"},
                "CalibrationRegistryModule": {
                    "register_solution",
                    "lookup_solution",
                    "record_state",
                    "state_history",
                },
                "AdmissionJournalModule": {"first_event", "record", "quarantined"},
                "EventAdmissionModule": {"admit", "reconsider_quarantined"},
                "EventClockMappingModule": {"map_processing_time"},
                "EventWindowModule": {"offer", "report_progress", "advance"},
                "VoxelCandidateModule": {"propose"},
                "DirectGeometryCandidateModule": {"propose"},
                "CandidateGenerationModule": {"generate"},
                "CrossStationAssociationModule": {"associate"},
                "CalibrationWeightingModule": {"weight"},
                "GeometryRefinementModule": {"refine"},
                "CenterTrackingModule": {"predict", "update", "advance"},
                "CenterFusionModule": {
                    "register_station",
                    "register_calibration",
                    "process",
                    "report_progress",
                    "tick",
                },
            },
            "runtime.py": {
                "EventTransportModule": {"submit", "submit_progress", "collect", "drain"},
                "EvidenceWriterModule": {
                    "begin",
                    "record_online",
                    "record_transport",
                    "record_drain",
                    "record_truth",
                    "finish",
                },
                "SystemRunnerModule": {"start", "step", "run", "stop"},
            },
            "scene.py": {
                "EvaluationModule": {"observe", "finalize"},
                "MonitoringModule": {"render"},
            },
        }
        for filename, classes in expected_stubs.items():
            source = module_directory / filename
            tree = ast.parse(source.read_text(), filename=str(source))
            with self.subTest(source=filename):
                _assert_pending_stubs(self, tree, classes)

    def test_stub_guard_allows_device_and_fusion_business_migration(self) -> None:
        sources = {
            "device.py": (
                """
class MotionDetectionModule:
    def detect(self, frame):
        return tuple(pixel for pixel in frame if pixel > 127)

class Tracking2DModule:
    def update(self, candidates):
        raise NotImplementedError("pending")
""",
                {"Tracking2DModule": {"update"}},
            ),
            "fusion.py": (
                """
class StationRegistryModule:
    def lookup(self, station_id, digest):
        return self.configs.get((station_id, digest))

    def register(self, config):
        raise NotImplementedError("pending")
""",
                {"StationRegistryModule": {"register"}},
            ),
        }
        for filename, (source, pending) in sources.items():
            with self.subTest(source=filename):
                # Invoke the same guard used for repository modules. Existing
                # class methods and new classes may both leave the pending list.
                _assert_pending_stubs(self, ast.parse(source, filename=filename), pending)

    def test_stub_guard_rejects_silent_return_in_a_pending_method(self) -> None:
        for classname, methodname in (
            ("MotionDetectionModule", "detect"),
            ("StationRegistryModule", "register"),
        ):
            with self.subTest(cls=classname, method=methodname):
                tree = ast.parse(
                    f"class {classname}:\n    def {methodname}(self):\n        return\n"
                )
                with self.assertRaisesRegex(AssertionError, "pending stub must only raise"):
                    _assert_pending_stubs(self, tree, {classname: {methodname}})


if __name__ == "__main__":
    unittest.main()
