"""Online V-Switch TCP bus client and Protobuf contract codecs.

Strictly isolated from scenario truth and scoring packages.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chongming.bus.center_process import (
        CenterProcess,
        default_online_clock_mapping,
        load_fusion_policy,
    )
    from chongming.bus.client import PROTOCOL_VERSION, BusHandshakeError, OnlineBusClient
    from chongming.bus.codec import (
        calibration_solution_from_proto,
        calibration_solution_to_proto,
        calibration_state_event_from_proto,
        calibration_state_event_to_proto,
        calibration_state_from_proto,
        calibration_state_to_proto,
        camera_role_from_proto,
        capture_time_from_proto,
        detection_batch_from_proto,
        detection_batch_to_proto,
        diagnostic_event_from_proto,
        diagnostic_event_to_proto,
        fusion_measurement_from_proto,
        fusion_measurement_to_proto,
        progress_report_from_proto,
        progress_report_to_proto,
        ray_event_from_proto,
        ray_event_to_proto,
        sparse_voxel_grid_from_proto,
        sparse_voxel_grid_to_proto,
        station_config_from_proto,
        station_config_to_proto,
        station_event_from_proto,
        station_event_to_proto,
        track_event_from_proto,
        track_event_to_proto,
    )
    from chongming.bus.edge_process import EdgeProcess, load_device_policy_map
    from chongming.bus.frame_source import (
        DecodedFrame,
        FrameSource,
        H264StreamDecoder,
        VirtualBusFrameSource,
        decode_video_packet,
        split_annex_b_nals,
    )

_EXPORTS: dict[str, str] = {
    "CenterProcess": "chongming.bus.center_process",
    "default_online_clock_mapping": "chongming.bus.center_process",
    "load_fusion_policy": "chongming.bus.center_process",
    "PROTOCOL_VERSION": "chongming.bus.client",
    "BusHandshakeError": "chongming.bus.client",
    "OnlineBusClient": "chongming.bus.client",
    "calibration_solution_from_proto": "chongming.bus.codec",
    "calibration_solution_to_proto": "chongming.bus.codec",
    "calibration_state_event_from_proto": "chongming.bus.codec",
    "calibration_state_event_to_proto": "chongming.bus.codec",
    "calibration_state_from_proto": "chongming.bus.codec",
    "calibration_state_to_proto": "chongming.bus.codec",
    "camera_role_from_proto": "chongming.bus.codec",
    "capture_time_from_proto": "chongming.bus.codec",
    "detection_batch_from_proto": "chongming.bus.codec",
    "detection_batch_to_proto": "chongming.bus.codec",
    "diagnostic_event_from_proto": "chongming.bus.codec",
    "diagnostic_event_to_proto": "chongming.bus.codec",
    "fusion_measurement_from_proto": "chongming.bus.codec",
    "fusion_measurement_to_proto": "chongming.bus.codec",
    "progress_report_from_proto": "chongming.bus.codec",
    "progress_report_to_proto": "chongming.bus.codec",
    "ray_event_from_proto": "chongming.bus.codec",
    "ray_event_to_proto": "chongming.bus.codec",
    "sparse_voxel_grid_from_proto": "chongming.bus.codec",
    "sparse_voxel_grid_to_proto": "chongming.bus.codec",
    "station_config_from_proto": "chongming.bus.codec",
    "station_config_to_proto": "chongming.bus.codec",
    "station_event_from_proto": "chongming.bus.codec",
    "station_event_to_proto": "chongming.bus.codec",
    "track_event_from_proto": "chongming.bus.codec",
    "track_event_to_proto": "chongming.bus.codec",
    "EdgeProcess": "chongming.bus.edge_process",
    "load_device_policy_map": "chongming.bus.edge_process",
    "DecodedFrame": "chongming.bus.frame_source",
    "FrameSource": "chongming.bus.frame_source",
    "H264StreamDecoder": "chongming.bus.frame_source",
    "VirtualBusFrameSource": "chongming.bus.frame_source",
    "decode_video_packet": "chongming.bus.frame_source",
    "split_annex_b_nals": "chongming.bus.frame_source",
}

__all__ = [
    "PROTOCOL_VERSION",
    "BusHandshakeError",
    "CenterProcess",
    "DecodedFrame",
    "EdgeProcess",
    "FrameSource",
    "H264StreamDecoder",
    "OnlineBusClient",
    "VirtualBusFrameSource",
    "calibration_solution_from_proto",
    "calibration_solution_to_proto",
    "calibration_state_event_from_proto",
    "calibration_state_event_to_proto",
    "calibration_state_from_proto",
    "calibration_state_to_proto",
    "camera_role_from_proto",
    "capture_time_from_proto",
    "decode_video_packet",
    "default_online_clock_mapping",
    "detection_batch_from_proto",
    "detection_batch_to_proto",
    "diagnostic_event_from_proto",
    "diagnostic_event_to_proto",
    "fusion_measurement_from_proto",
    "fusion_measurement_to_proto",
    "load_device_policy_map",
    "load_fusion_policy",
    "progress_report_from_proto",
    "progress_report_to_proto",
    "ray_event_from_proto",
    "ray_event_to_proto",
    "sparse_voxel_grid_from_proto",
    "sparse_voxel_grid_to_proto",
    "split_annex_b_nals",
    "station_config_from_proto",
    "station_config_to_proto",
    "station_event_from_proto",
    "station_event_to_proto",
    "track_event_from_proto",
    "track_event_to_proto",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
