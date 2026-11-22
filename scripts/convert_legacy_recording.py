"""One-way converter from legacy JSONL recordings/outputs to Test Scene and Run Output."""

from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.rendering.geometry import model_aabb_diagonal
from chongming.scenarios.serialization import load_compiled_scenario
from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.storage import RunOutputWriter, TestScenePackage, TestSceneWriter

_CAMERA_ROLES = {
    "main": common_pb2.CAMERA_ROLE_MAIN,
    "auxiliary": common_pb2.CAMERA_ROLE_AUXILIARY,
}
_EVENT_TYPES = {
    "ray": common_pb2.EVENT_TYPE_RAY,
    "calibration_state": common_pb2.EVENT_TYPE_CALIBRATION_STATE,
    "diagnostic": common_pb2.EVENT_TYPE_DIAGNOSTIC,
}
_SCORE_KINDS = {
    "ranking": common_pb2.SCORE_KIND_RANKING,
    "calibrated_probability": common_pb2.SCORE_KIND_CALIBRATED_PROBABILITY,
}
_CALIBRATION_USAGES = {
    "valid": common_pb2.CALIBRATION_USAGE_VALID,
    "frozen": common_pb2.CALIBRATION_USAGE_FROZEN,
    "stale": common_pb2.CALIBRATION_USAGE_STALE,
}
_FUSION_STATUSES = {
    "located": common_pb2.FUSION_STATUS_LOCATED,
    "geometry_degenerate": common_pb2.FUSION_STATUS_GEOMETRY_DEGENERATE,
    "association_ambiguous": common_pb2.FUSION_STATUS_ASSOCIATION_AMBIGUOUS,
    "insufficient_stations": common_pb2.FUSION_STATUS_INSUFFICIENT_STATIONS,
    "no_candidate_in_volume": common_pb2.FUSION_STATUS_NO_CANDIDATE_IN_VOLUME,
    "rejected_input": common_pb2.FUSION_STATUS_REJECTED_INPUT,
}
_TRACK_LIFECYCLES = {
    "tentative": common_pb2.TRACK_LIFECYCLE_TENTATIVE,
    "confirmed": common_pb2.TRACK_LIFECYCLE_CONFIRMED,
    "coasting": common_pb2.TRACK_LIFECYCLE_COASTING,
    "deleted": common_pb2.TRACK_LIFECYCLE_DELETED,
}


def _vec2(values: Sequence[float]) -> common_pb2.Vec2:
    return common_pb2.Vec2(x=float(values[0]), y=float(values[1]))


def _vec3(values: Sequence[float]) -> common_pb2.Vec3:
    return common_pb2.Vec3(x=float(values[0]), y=float(values[1]), z=float(values[2]))


def _mat_values(matrix: Sequence[Sequence[float]]) -> list[float]:
    return [float(value) for row in matrix for value in row]


def _mat2(matrix: Sequence[Sequence[float]]) -> common_pb2.Mat2:
    return common_pb2.Mat2(values=_mat_values(matrix))


def _mat3(matrix: Sequence[Sequence[float]]) -> common_pb2.Mat3:
    return common_pb2.Mat3(values=_mat_values(matrix))


def _mat6(matrix: Sequence[Sequence[float]]) -> common_pb2.Mat6:
    return common_pb2.Mat6(values=_mat_values(matrix))


def _capture_time_from_dict(raw: Mapping[str, Any]) -> common_pb2.CaptureTime:
    return common_pb2.CaptureTime(
        clock_domain_id=str(raw["clock_domain_id"]),
        exposure_start_ns=int(raw["exposure_start_ns"]),
        exposure_end_ns=int(raw["exposure_end_ns"]),
        clock_uncertainty_ns=int(raw.get("clock_uncertainty_ns", 0)),
        rolling_shutter_model=str(raw.get("rolling_shutter_model", "none")),
    )


def _processing_time_from_dict(raw: Mapping[str, Any]) -> common_pb2.ProcessingTime:
    return common_pb2.ProcessingTime(
        clock_domain_id=str(raw["clock_domain_id"]),
        time_ns=int(raw["time_ns"]),
    )


def _time_interval_from_dict(raw: Mapping[str, Any]) -> common_pb2.TimeInterval:
    end_ns = raw.get("end_ns")
    msg = common_pb2.TimeInterval(
        clock_domain_id=str(raw["clock_domain_id"]),
        start_ns=int(raw["start_ns"]),
    )
    if end_ns is not None:
        msg.end_ns = int(end_ns)
    return msg


def _score_definition_from_dict(raw: Mapping[str, Any]) -> common_pb2.ScoreDefinition:
    msg = common_pb2.ScoreDefinition(
        definition_id=str(raw["definition_id"]),
        kind=_SCORE_KINDS[str(raw["kind"])],
    )
    if raw.get("minimum") is not None:
        msg.minimum = float(raw["minimum"])
    if raw.get("maximum") is not None:
        msg.maximum = float(raw["maximum"])
    if raw.get("cross_station_comparison_group") is not None:
        msg.cross_station_comparison_group = str(raw["cross_station_comparison_group"])
    return msg


def _camera_model_to_proto(camera: m.CameraModel) -> online_pb2.CameraModel:
    return online_pb2.CameraModel(
        intrinsics=online_pb2.CameraIntrinsics(
            image_width_px=int(camera.intrinsics.image_width_px),
            image_height_px=int(camera.intrinsics.image_height_px),
            matrix_px=_mat3(camera.intrinsics.matrix_px),
        ),
        distortion=online_pb2.CameraDistortion(
            model=str(camera.distortion.model),
            coefficients=[float(c) for c in camera.distortion.coefficients],
        ),
    )


def _station_config_to_proto(station: m.StationConfig) -> online_pb2.StationConfig:
    full_tf = station.full_rigid_transform_aux_from_main
    unc = station.installation_uncertainty
    msg = online_pb2.StationConfig(
        station_id=str(station.station_id),
        config_version=str(station.config_version),
        config_digest=str(station.config_digest),
        world_frame_id=str(station.world_frame_id),
        geodetic_anchor=online_pb2.GeodeticAnchor(
            anchor_id=station.geodetic_anchor.anchor_id,
            datum=station.geodetic_anchor.datum,
            datum_epoch=station.geodetic_anchor.datum_epoch,
            latitude_deg=float(station.geodetic_anchor.latitude_deg),
            longitude_deg=float(station.geodetic_anchor.longitude_deg),
            height_m=float(station.geodetic_anchor.height_m),
            height_reference=station.geodetic_anchor.height_reference,
        ),
        main_camera_position_enu_m=_vec3(station.main_camera_position_enu_m),
        baseline_orientation_enu_from_main=_mat3(station.baseline_orientation_enu_from_main),
        main_camera=_camera_model_to_proto(station.main_camera),
        auxiliary_camera=_camera_model_to_proto(station.auxiliary_camera),
        fixed_rotation_aux_from_main=_mat3(station.fixed_rotation_aux_from_main),
        reference_scene_version=str(station.reference_scene_version),
        installation_uncertainty=online_pb2.InstallationUncertainty(
            state_ref=online_pb2.InstallationStateRef(
                station_id=str(unc.state_ref.station_id),
                config_digest=str(unc.state_ref.config_digest),
                state_version=str(unc.state_ref.state_version),
            ),
            position_covariance_enu_m2=_mat3(unc.position_covariance_enu_m2),
            orientation_covariance_enu_rad2=_mat3(unc.orientation_covariance_enu_rad2),
            fixed_extrinsic_covariance_aux_rad2=_mat3(unc.fixed_extrinsic_covariance_aux_rad2),
            measurement_source=str(unc.measurement_source),
        ),
    )
    if full_tf is not None:
        msg.full_rigid_transform_aux_from_main.CopyFrom(
            online_pb2.RigidTransform(
                rotation_a_from_b=_mat3(full_tf.rotation_a_from_b),
                translation_a_m=_vec3(full_tf.translation_a_m),
            )
        )
    return msg


def _ray_event_from_dict(raw: Mapping[str, Any]) -> online_pb2.RayEvent:
    env = raw["envelope"]
    cal_ref = raw["calibration_solution_ref"]
    ev = raw["calibration_evidence"]
    unc = raw["uncertainty"]
    msg = online_pb2.RayEvent(
        envelope=common_pb2.EventEnvelope(
            schema_version=str(env["schema_version"]),
            event_type=_EVENT_TYPES[str(env["event_type"])],
            station_id=str(env["station_id"]),
            station_session_id=str(env["station_session_id"]),
            sequence_no=int(env["sequence_no"]),
            station_config_digest=str(env["station_config_digest"]),
        ),
        frame_id=str(raw["frame_id"]),
        capture_time=_capture_time_from_dict(raw["capture_time"]),
        world_frame_id=str(raw["world_frame_id"]),
        calibration_solution_ref=online_pb2.CalibrationSolutionRef(
            station_id=str(cal_ref["station_id"]),
            station_session_id=str(cal_ref["station_session_id"]),
            solution_id=str(cal_ref["solution_id"]),
        ),
        calibration_usage=_CALIBRATION_USAGES[str(raw["calibration_usage"])],
        calibration_evidence=online_pb2.CalibrationUseEvidence(
            state_version=int(ev["state_version"]),
            last_trusted_time=_processing_time_from_dict(ev["last_trusted_time"]),
            solution_accepted_time=_processing_time_from_dict(ev["solution_accepted_time"]),
            solution_reference_capture_time=_capture_time_from_dict(
                ev["solution_reference_capture_time"]
            ),
            solution_valid_interval=_time_interval_from_dict(ev["solution_valid_interval"]),
            usable_interval=_time_interval_from_dict(ev["usable_interval"]),
            reason_codes=[str(code) for code in ev.get("reason_codes", ())],
        ),
        ray_origin_enu_m=_vec3(raw["ray_origin_enu_m"]),
        ray_direction_enu_unit=_vec3(raw["ray_direction_enu_unit"]),
        uncertainty=online_pb2.RayUncertainty(
            model=str(unc["model"]),
            tangent_u_enu_unit=_vec3(unc["tangent_u_enu_unit"]),
            tangent_v_enu_unit=_vec3(unc["tangent_v_enu_unit"]),
            independent_angular_covariance_rad2=_mat2(unc["independent_angular_covariance_rad2"]),
            independent_origin_covariance_m2=_mat3(unc["independent_origin_covariance_m2"]),
            shared_installation_state_ref=online_pb2.InstallationStateRef(
                station_id=str(unc["shared_installation_state_ref"]["station_id"]),
                config_digest=str(unc["shared_installation_state_ref"]["config_digest"]),
                state_version=str(unc["shared_installation_state_ref"]["state_version"]),
            ),
            shared_calibration_solution_ref=online_pb2.CalibrationSolutionRef(
                station_id=str(unc["shared_calibration_solution_ref"]["station_id"]),
                station_session_id=str(
                    unc["shared_calibration_solution_ref"]["station_session_id"]
                ),
                solution_id=str(unc["shared_calibration_solution_ref"]["solution_id"]),
            ),
        ),
        detection_score=float(raw["detection_score"]),
        detection_score_definition=_score_definition_from_dict(raw["detection_score_definition"]),
        quality_flags=[str(flag) for flag in raw.get("quality_flags", ())],
    )
    if raw.get("local_track_id") is not None:
        msg.local_track_id = str(raw["local_track_id"])
    if raw.get("track_quality") is not None:
        msg.track_quality = float(raw["track_quality"])
    if raw.get("track_quality_definition") is not None:
        msg.track_quality_definition.CopyFrom(
            _score_definition_from_dict(raw["track_quality_definition"])
        )
    return msg


def _observation_from_dict(raw: Mapping[str, Any]) -> online_pb2.Observation2D:
    ref = raw["main_frame_ref"]
    msg = online_pb2.Observation2D(
        main_frame_ref=common_pb2.FrameRef(
            station_id=str(ref["station_id"]),
            station_session_id=str(ref["station_session_id"]),
            camera_role=_CAMERA_ROLES[str(ref["camera_role"])],
            frame_id=str(ref["frame_id"]),
        ),
        capture_time=_capture_time_from_dict(raw["capture_time"]),
        centroid_px=_vec2(raw["centroid_px"]),
        detection_score=float(raw["detection_score"]),
        detection_score_definition=_score_definition_from_dict(raw["detection_score_definition"]),
    )
    if raw.get("centroid_covariance_px2") is not None:
        msg.centroid_covariance_px2.CopyFrom(_mat2(raw["centroid_covariance_px2"]))
    if raw.get("local_track_id") is not None:
        msg.local_track_id = str(raw["local_track_id"])
    if raw.get("track_quality") is not None:
        msg.track_quality = float(raw["track_quality"])
    if raw.get("track_quality_definition") is not None:
        msg.track_quality_definition.CopyFrom(
            _score_definition_from_dict(raw["track_quality_definition"])
        )
    return msg


def _fusion_measurement_from_dict(raw: Mapping[str, Any]) -> online_pb2.FusionMeasurement:
    msg = online_pb2.FusionMeasurement(
        center_session_id=str(raw["center_session_id"]),
        measurement_id=str(raw["measurement_id"]),
        clock_domain_id=str(raw["clock_domain_id"]),
        world_frame_id=str(raw["world_frame_id"]),
        reference_time_ns=int(raw["reference_time_ns"]),
        support_exposure_span_ns=int(raw["support_exposure_span_ns"]),
        status=_FUSION_STATUSES[str(raw["status"])],
        supporting_ray_event_ids=[
            common_pb2.EventId(
                station_id=str(eid[0]),
                station_session_id=str(eid[1]),
                sequence_no=int(eid[2]),
            )
            for eid in raw.get("supporting_ray_event_ids", ())
        ],
        supporting_station_ids=[str(s_id) for s_id in raw.get("supporting_station_ids", ())],
        residuals_m=[float(r) for r in raw.get("residuals_m", ())],
        reason_codes=[str(code) for code in raw.get("reason_codes", ())],
    )
    if raw.get("position_enu_m") is not None:
        msg.position_enu_m.CopyFrom(_vec3(raw["position_enu_m"]))
    if raw.get("conditional_position_covariance_enu_m2") is not None:
        msg.conditional_position_covariance_enu_m2.CopyFrom(
            _mat3(raw["conditional_position_covariance_enu_m2"])
        )
    if raw.get("motion_alignment") is not None:
        ma = raw["motion_alignment"]
        msg.motion_alignment.CopyFrom(
            online_pb2.MotionAlignmentEvidence(
                prior_track_id=str(ma["prior_track_id"]),
                velocity_enu_mps=_vec3(ma["velocity_enu_mps"]),
                velocity_projection_s=_mat3(ma["velocity_projection_s"]),
                independent_position_covariance_enu_m2=_mat3(
                    ma["independent_position_covariance_enu_m2"]
                ),
            )
        )
    if raw.get("geometry_condition") is not None:
        msg.geometry_condition = float(raw["geometry_condition"])
    if raw.get("minimum_intersection_angle_rad") is not None:
        msg.minimum_intersection_angle_rad = float(raw["minimum_intersection_angle_rad"])
    if raw.get("association_hypothesis_id") is not None:
        msg.association_hypothesis_id = str(raw["association_hypothesis_id"])
    return msg


def _track_event_from_dict(raw: Mapping[str, Any]) -> online_pb2.TrackEvent:
    msg = online_pb2.TrackEvent(
        center_session_id=str(raw["center_session_id"]),
        track_id=str(raw["track_id"]),
        clock_domain_id=str(raw["clock_domain_id"]),
        world_frame_id=str(raw["world_frame_id"]),
        event_time_ns=int(raw["event_time_ns"]),
        lifecycle=_TRACK_LIFECYCLES[str(raw["lifecycle"])],
        source_measurement_refs=[
            online_pb2.MeasurementRef(
                center_session_id=str(ref["center_session_id"]),
                measurement_id=str(ref["measurement_id"]),
            )
            for ref in raw.get("source_measurement_refs", ())
        ],
        reason_codes=[str(code) for code in raw.get("reason_codes", ())],
    )
    if raw.get("position_enu_m") is not None:
        msg.position_enu_m.CopyFrom(_vec3(raw["position_enu_m"]))
    if raw.get("velocity_enu_mps") is not None:
        msg.velocity_enu_mps.CopyFrom(_vec3(raw["velocity_enu_mps"]))
    if raw.get("conditional_state_covariance") is not None:
        msg.conditional_state_covariance.CopyFrom(_mat6(raw["conditional_state_covariance"]))
    return msg


def _target_class_label(geometry: s.GeometrySpec) -> str:
    if isinstance(geometry, s.ExternalAssetGeometry):
        return geometry.asset_identifier
    if isinstance(geometry, s.SphereGeometry):
        return "sphere"
    if isinstance(geometry, s.BoxGeometry):
        return "box"
    return type(geometry).__name__


def convert_test_scene(
    recording: Path,
    output: Path,
    *,
    asset_root: Path | None = None,
    link_video: bool = False,
) -> dict[str, Any]:
    """Convert a legacy JSONL recording directory into an immutable Test Scene package."""
    spec = load_compiled_scenario(recording / "scenario.json", asset_root=asset_root)
    manifest = json.loads((recording / "manifest.json").read_text(encoding="utf-8"))
    video_path = recording / "video.json"
    video_info = json.loads(video_path.read_text(encoding="utf-8")) if video_path.is_file() else {}
    streams = video_info.get("streams", {})

    path_map: dict[str, str] = {}
    stream_meta: dict[str, dict[str, Any]] = {}
    for mapping in manifest.get("stream_mapping", ()):
        legacy_rel = str(mapping["path"])
        station_id = str(mapping["station_id"])
        role = str(mapping["camera_role"])
        short_role = "aux" if role == "auxiliary" else role
        new_rel = f"video/{station_id}-{short_role}.mp4"
        path_map[legacy_rel] = new_rel
        stream_meta[legacy_rel] = {
            "station_id": station_id,
            "camera_role": role,
            "legacy_path": legacy_rel,
        }

    diagonals: dict[str, float] = {}
    target_classes: dict[str, str] = {}
    targets_meta: dict[str, dict[str, Any]] = {}
    for target in spec.scene_definition.flying_objects:
        target_id = str(target.truth_object_id)
        diag = model_aabb_diagonal(target.geometry)
        diagonals[target_id] = diag
        cls_name = _target_class_label(target.geometry)
        target_classes[target_id] = cls_name
        targets_meta[target_id] = {
            "target_class": cls_name,
            "body_aabb_diagonal_m": diag,
        }

    occlusion_mode = spec.scene_definition.occlusion_mode.value
    station_protos = [_station_config_to_proto(st) for st in spec.station_configs]

    with TestSceneWriter(
        output,
        scenario_id=str(spec.scenario_id),
        world_frame_id=str(spec.scene_definition.world.world_frame_id),
        clock_domain_id=str(spec.timeline.clock_domain.clock_domain_id),
        start_time_ns=int(spec.timeline.start_time_ns),
        end_time_ns=int(spec.timeline.end_time_ns),
        stations=station_protos,
        occlusion_mode=occlusion_mode,
        random_seed=spec.random_seed,
        targets_meta=targets_meta,
    ) as writer:
        for stream_entry in streams.values():
            legacy_rel = str(stream_entry["path"])
            new_rel = path_map.get(legacy_rel, legacy_rel)
            meta = {
                **stream_meta.get(legacy_rel, {}),
                **{k: v for k, v in stream_entry.items() if k not in {"path", "sha256"}},
            }
            writer.add_video_file(
                recording / legacy_rel,
                new_rel,
                meta,
                symlink=link_video,
            )

        with (recording / "online.jsonl").open(encoding="utf-8") as online_handle:
            for line in online_handle:
                row = json.loads(line)
                for frame in row.get("frames", ()):
                    legacy_rel = frame.get("path")
                    mapped_rel = path_map.get(legacy_rel, legacy_rel or "")
                    writer.add_frame_index(
                        truth_pb2.FrameIndexEntry(
                            station_id=str(frame["station_id"]),
                            station_session_id=str(frame["session_id"]),
                            camera_role=_CAMERA_ROLES[str(frame["camera_role"])],
                            frame_index=int(frame["frame_index"]),
                            frame_id=str(frame["frame_id"]),
                            capture_time=_capture_time_from_dict(frame["capture_time"]),
                            station_config_digest=str(frame["camera_config_ref"]),
                            video_relpath=str(mapped_rel),
                            width_px=int(frame["width_px"]),
                            height_px=int(frame["height_px"]),
                        )
                    )
                for ray in row.get("packets", {}).get("rays", ()):
                    writer.add_virtual_ray(_ray_event_from_dict(ray))

        with (recording / "truth.jsonl").open(encoding="utf-8") as truth_handle:
            for line in truth_handle:
                priv = json.loads(line)
                sample_time = priv["sample_time"]
                truth_frame = truth_pb2.TruthFrame(
                    world_frame_id=str(spec.scene_definition.world.world_frame_id),
                    experiment_time_ns=int(sample_time["time_ns"]),
                    step_index=int(sample_time["step_no"]),
                )
                for obj in priv.get("logical_scene", {}).get("flying_objects", ()):
                    target_id = str(obj["truth_object_id"])
                    diag = diagonals[target_id]
                    state = obj["state"]
                    truth_frame.targets.append(
                        truth_pb2.TargetTruth(
                            target_id=target_id,
                            target_class=target_classes.get(
                                target_id, str(obj.get("model_name", "unknown"))
                            ),
                            position_enu_m=_vec3(state["position_enu_m"]),
                            velocity_enu_mps=_vec3(state["velocity_enu_mps"]),
                            bounding_radius_m=0.5 * diag,
                            body_aabb_diagonal_m=diag,
                        )
                    )
                for cam_frame in priv.get("camera_frames", ()):
                    if cam_frame.get("camera_role") != "main":
                        continue
                    station_id = str(cam_frame["station_id"])
                    frame_id = str(cam_frame["frame_id"])
                    for target_proj in cam_frame.get("targets", ()):
                        projection = target_proj["projection"]
                        in_fov = bool(projection["center_in_field_of_view"])
                        visibility = target_proj.get("visibility", {})
                        occlusions = visibility.get("occlusions", ())
                        if not in_fov:
                            occ_status = truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV
                        elif occlusions:
                            occ_status = truth_pb2.OCCLUSION_STATUS_OCCLUDED
                        elif occlusion_mode == "known":
                            occ_status = truth_pb2.OCCLUSION_STATUS_VISIBLE
                        else:
                            occ_status = truth_pb2.OCCLUSION_STATUS_UNKNOWN
                        proj_msg = truth_pb2.StationTargetProjection(
                            station_id=station_id,
                            target_id=str(target_proj["truth_object_id"]),
                            occlusion_status=occ_status,
                            frame_id=frame_id,
                        )
                        centroid = projection.get("centroid_px")
                        if centroid is not None and all(math.isfinite(float(v)) for v in centroid):
                            proj_msg.projected_centroid_px.CopyFrom(_vec2(centroid))
                        width = target_proj.get("projected_width_px")
                        height = target_proj.get("projected_height_px")
                        if (
                            width is not None
                            and height is not None
                            and float(width) > 0
                            and float(height) > 0
                        ):
                            proj_msg.projected_diagonal_px = math.hypot(float(width), float(height))
                        sight = visibility.get("sight_segment")
                        if sight is not None:
                            proj_msg.range_m = math.dist(
                                sight["optical_center_enu_m"],
                                sight["reference_point_enu_m"],
                            )
                        truth_frame.projections.append(proj_msg)
                writer.add_truth_frame(truth_frame)

        return writer.finalize()


def convert_run_output(
    scene_dir: Path,
    output: Path,
    *,
    mode: str,
    device_dir: Path | None = None,
    center_dir: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Convert legacy device/center JSONL outputs into an independent Run Output package."""
    scene = TestScenePackage.open(scene_dir, verify_hashes=False)
    frame_index: dict[str, truth_pb2.FrameIndexEntry] = {
        entry.frame_id: entry for entry in scene.iter_frame_index()
    }

    provenance: dict[str, Any] = {"converted_from_legacy": True}
    if device_dir is not None:
        provenance["legacy_device_dir"] = str(device_dir.resolve())
    if center_dir is not None:
        provenance["legacy_center_dir"] = str(center_dir.resolve())

    with RunOutputWriter(
        output,
        run_id=run_id or str(uuid4()),
        mode=mode,
        test_scene_path=scene_dir.resolve(),
        test_scene_sha256=scene.content_sha256,
        provenance=provenance,
    ) as writer:
        edge_seq = 0
        if device_dir is not None:
            timings_by_frame: dict[str, tuple[int, int]] = {}
            eval_frames = device_dir / "evaluation" / "frames.jsonl"
            if eval_frames.is_file():
                with eval_frames.open(encoding="utf-8") as handle:
                    for line in handle:
                        row = json.loads(line)
                        timing = row.get("timing_ms", {})
                        decode_ns = round(float(timing.get("decode_ms", 0.0)) * 1e6)
                        cv_ns = round(float(timing.get("pipeline_ms", 0.0)) * 1e6)
                        timings_by_frame[str(row["frame_id"])] = (decode_ns, cv_ns)

            obs_path = device_dir / "observations.jsonl"
            if not obs_path.is_file():
                raise FileNotFoundError(f"missing legacy observations.jsonl in {device_dir}")
            with obs_path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    frame_id = str(row["frame_id"])
                    entry = frame_index.get(frame_id)
                    if entry is None:
                        raise ValueError(f"frame_id {frame_id!r} not found in Test Scene index")
                    decode_ns, cv_ns = timings_by_frame.get(frame_id, (0, 0))
                    batch = online_pb2.DetectionBatch(
                        main_frame_ref=common_pb2.FrameRef(
                            station_id=entry.station_id,
                            station_session_id=entry.station_session_id,
                            camera_role=entry.camera_role,
                            frame_id=entry.frame_id,
                        ),
                        capture_time=entry.capture_time,
                        processing_status=common_pb2.PROCESSING_STATUS_PROCESSED,
                        observations=[
                            _observation_from_dict(obs) for obs in row.get("observations", ())
                        ],
                        decode_duration_ns=decode_ns,
                        cv_duration_ns=cv_ns,
                    )
                    edge_seq += 1
                    writer.write_edge_message(
                        online_pb2.BusMessage(
                            bus_sequence_no=edge_seq,
                            sent_wall_time_ns=0,
                            detection_batch=batch,
                        )
                    )

        if center_dir is not None:
            edge_rays_path = center_dir / "edge-rays.jsonl"
            if edge_rays_path.is_file():
                with edge_rays_path.open(encoding="utf-8") as handle:
                    for line in handle:
                        row = json.loads(line)
                        edge_seq += 1
                        writer.write_edge_message(
                            online_pb2.BusMessage(
                                bus_sequence_no=edge_seq,
                                sent_wall_time_ns=0,
                                ray_event=_ray_event_from_dict(row["event"]),
                            )
                        )

            captures: set[int] = set()
            center_input_path = center_dir / "center-input.jsonl"
            if center_input_path.is_file():
                with center_input_path.open(encoding="utf-8") as handle:
                    for line in handle:
                        for progress in json.loads(line).get("progress", ()):
                            cap = progress.get("completed_capture_time")
                            if cap is not None:
                                captures.add(
                                    (int(cap["exposure_start_ns"]) + int(cap["exposure_end_ns"]))
                                    // 2
                                )
            ordered_captures = sorted(captures)

            center_session_id = "legacy-center"
            reg_path = center_dir / "center-registration.json"
            if reg_path.is_file():
                reg = json.loads(reg_path.read_text(encoding="utf-8"))
                center_session_id = str(reg.get("center_session_id", center_session_id))

            center_seq = 0
            seen_meas: dict[str, dict[str, Any]] = {}
            window_times: set[int] = set()
            window_keys: set[tuple[int, int]] = set()
            center_output_path = center_dir / "center-output.jsonl"
            with center_output_path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    for batch_row in row.get("batches", ()):
                        for win in batch_row.get("window_update", {}).get("sealed_windows", ()):
                            window_times.add(int(win["reference_time_ns"]))
                            if "key" in win:
                                window_keys.add(
                                    (int(win["key"]["start_ns"]), int(win["key"]["end_ns"]))
                                )
                    for meas_raw in row.get("measurements", ()):
                        meas_id = str(meas_raw["measurement_id"])
                        previous = seen_meas.get(meas_id)
                        if previous is not None:
                            if previous != meas_raw:
                                raise ValueError(
                                    f"conflicting duplicate measurement ID {meas_id!r}"
                                )
                            continue
                        seen_meas[meas_id] = meas_raw
                        center_seq += 1
                        writer.write_center_message(
                            online_pb2.BusMessage(
                                bus_sequence_no=center_seq,
                                sent_wall_time_ns=0,
                                fusion_measurement=_fusion_measurement_from_dict(meas_raw),
                            )
                        )
                    for track_raw in row.get("tracks", ()):
                        center_seq += 1
                        writer.write_center_message(
                            online_pb2.BusMessage(
                                bus_sequence_no=center_seq,
                                sent_wall_time_ns=0,
                                track_event=_track_event_from_dict(track_raw),
                            )
                        )

            if ordered_captures and window_keys:
                resolved_times: set[int] = set()
                for start_ns, end_ns in sorted(window_keys):
                    selected = ordered_captures[
                        bisect_left(ordered_captures, start_ns) : bisect_left(
                            ordered_captures, end_ns
                        )
                    ]
                    if len(selected) != 1:
                        raise ValueError(
                            "center conversion requires one synchronous exposure per sealed window"
                        )
                    resolved_times.add(selected[0])
            else:
                resolved_times = window_times

            ordered_schedule = sorted(resolved_times)
            for idx, ns in enumerate(ordered_schedule, start=1):
                center_seq += 1
                writer.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=center_seq,
                        sent_wall_time_ns=0,
                        progress_report=online_pb2.ProgressReport(
                            source_role=common_pb2.COMPONENT_ROLE_CENTER,
                            source_id="center",
                            session_id=center_session_id,
                            completed_time_ns=ns,
                            completed_sequence_no=idx,
                            finite_end_reached=(idx == len(ordered_schedule)),
                        ),
                    )
                )

        return writer.finalize()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scene_cmd = sub.add_parser("test-scene", help="Convert a legacy recording into a Test Scene")
    scene_cmd.add_argument("--recording", required=True, type=Path)
    scene_cmd.add_argument("--output", required=True, type=Path)
    scene_cmd.add_argument("--asset-root", type=Path, default=Path("scenarios"))
    scene_cmd.add_argument("--link-video", action="store_true")

    run_cmd = sub.add_parser("run-output", help="Convert legacy outputs into a Run Output")
    run_cmd.add_argument("--scene", required=True, type=Path)
    run_cmd.add_argument("--output", required=True, type=Path)
    run_cmd.add_argument("--mode", required=True, choices=("edge", "center", "integrated"))
    run_cmd.add_argument("--device-dir", type=Path)
    run_cmd.add_argument("--center-dir", type=Path)
    run_cmd.add_argument("--run-id")

    args = parser.parse_args(argv)
    if args.command == "test-scene":
        manifest = convert_test_scene(
            args.recording,
            args.output,
            asset_root=args.asset_root,
            link_video=args.link_video,
        )
        print(
            json.dumps(
                {
                    "scenario_id": manifest["scenario_id"],
                    "content_sha256": manifest["content_sha256"],
                    "files": manifest["files"],
                },
                indent=2,
            )
        )
        return

    if args.mode in {"edge", "integrated"} and args.device_dir is None:
        parser.error("--device-dir is required for edge and integrated modes")
    if args.mode in {"center", "integrated"} and args.center_dir is None:
        parser.error("--center-dir is required for center and integrated modes")
    run_manifest = convert_run_output(
        args.scene,
        args.output,
        mode=args.mode,
        device_dir=args.device_dir,
        center_dir=args.center_dir,
        run_id=args.run_id,
    )
    print(json.dumps(run_manifest, indent=2))


if __name__ == "__main__":
    main()
