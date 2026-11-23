//! Non-blocking, non-persistent Viewer bypass push (`V-Switch -> Godot Viewer`).
//!
//! Wire format: TCP 4-byte big-endian (`uint32` BE) length-prefixed UTF-8 JSON
//! (`schema_version: "chongming-viewer-v1"` per `docs/protocol.md`).
//! Every `int64` and `uint64` field is encoded as a base-10 decimal string to
//! avoid GDScript `JSON.parse_string()` IEEE-754 float truncation.

use std::collections::HashMap;
use std::io;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use serde::Serialize;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use crate::framing::{write_raw_frame_async, MAX_FRAME_BYTES};
use crate::proto::{
    CalibrationHealth, CalibrationStateEvent, CalibrationUsage, DiagnosticEvent, FusionMeasurement,
    FusionStatus, Mat3, OcclusionStatus, RayEvent, ScoreEvent, SparseVoxelGrid, StationConfig,
    TrackEvent, TrackLifecycle, TruthFrame, Vec2, Vec3,
};

pub const VIEWER_JSON_SCHEMA_VERSION: &str = "chongming-viewer-v1";
pub const DEFAULT_VIEWER_QUEUE_CAPACITY: usize = 8;

/// Top-level `DisplayFrame` JSON envelope matching `docs/protocol.md`.
#[derive(Debug, Clone, Serialize)]
pub struct ViewerPushFrame {
    pub schema_version: &'static str,
    pub run_id: String,
    pub mode: String,
    pub update_id: String,
    pub sequence_no: String,
    pub experiment_time_ns: String,
    pub has_truth: bool,
    pub stations: Vec<StationDisplayState>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub truth: Option<TruthOverlay>,
    pub rays: RaysOverlay,
    pub answer: AnswerOverlay,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub score: Option<ScoreOverlay>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sparse_voxel_grid: Option<SparseVoxelGridOverlay>,
}

#[derive(Debug, Clone, Serialize)]
pub struct StationDisplayState {
    pub station_id: String,
    pub main_camera_position_enu_m: [f64; 3],
    pub baseline_orientation_enu_from_main: [[f64; 3]; 3],
    pub calibrated_orientation_enu_from_main: Option<[[f64; 3]; 3]>,
    pub calibration_health: Option<&'static str>,
    pub calibration_usage: Option<&'static str>,
    pub state_version: Option<String>,
    pub solution_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TruthOverlay {
    pub world_frame_id: String,
    pub experiment_time_ns: String,
    pub step_index: String,
    pub targets: Vec<TargetTruthItem>,
    pub projections: Vec<StationTargetProjectionItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TargetTruthItem {
    pub target_id: String,
    pub target_class: String,
    pub position_enu_m: [f64; 3],
    pub velocity_enu_mps: [f64; 3],
    pub bounding_radius_m: f64,
    pub body_aabb_diagonal_m: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct StationTargetProjectionItem {
    pub station_id: String,
    pub target_id: String,
    pub occlusion_status: &'static str,
    pub projected_centroid_px: Option<[f64; 2]>,
    pub projected_diagonal_px: Option<f64>,
    pub range_m: Option<f64>,
    pub frame_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RaysOverlay {
    pub items: Vec<RayItem>,
    pub calibration_states: Vec<CalibrationStateItem>,
    pub diagnostics: Vec<DiagnosticItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RayItem {
    pub event_id: [String; 3],
    pub frame_id: String,
    pub capture_time_ns: String,
    pub exposure_start_ns: String,
    pub exposure_end_ns: String,
    pub origin_enu_m: [f64; 3],
    pub direction_enu_unit: [f64; 3],
    pub calibration_usage: &'static str,
    pub detection_score: f64,
    pub angular_std_rad: Option<f64>,
    pub local_track_id: Option<String>,
    pub quality_flags: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CalibrationStateItem {
    pub event_id: [String; 3],
    pub station_id: String,
    pub state_version: String,
    pub health: &'static str,
    pub solution_id: Option<String>,
    pub observed_at_ns: String,
    pub reason_codes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DiagnosticItem {
    pub event_id: [String; 3],
    pub station_id: String,
    pub capture_time_ns: Option<String>,
    pub category: String,
    pub reason_codes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AnswerOverlay {
    pub measurements: Vec<MeasurementItem>,
    pub tracks: Vec<TrackItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MeasurementItem {
    pub center_session_id: String,
    pub measurement_id: String,
    pub reference_time_ns: String,
    pub support_exposure_span_ns: String,
    pub status: &'static str,
    pub position_enu_m: Option<[f64; 3]>,
    pub position_std_enu_m: Option<[f64; 3]>,
    pub supporting_ray_event_ids: Vec<[String; 3]>,
    pub supporting_station_ids: Vec<String>,
    pub minimum_intersection_angle_deg: Option<f64>,
    pub geometry_condition: Option<f64>,
    pub reason_codes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TrackItem {
    pub center_session_id: String,
    pub track_id: String,
    pub event_time_ns: String,
    pub lifecycle: &'static str,
    pub position_enu_m: Option<[f64; 3]>,
    pub velocity_enu_mps: Option<[f64; 3]>,
    pub position_std_enu_m: Option<[f64; 3]>,
    pub velocity_std_enu_mps: Option<[f64; 3]>,
    pub source_measurement_ids: Vec<String>,
    pub reason_codes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScoreOverlay {
    pub events: Vec<ScoreEventItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScoreMetricItem {
    pub name: String,
    pub value: Option<f64>,
    pub unit: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScoreEventItem {
    pub run_id: String,
    pub experiment_time_ns: String,
    pub matched_count: String,
    pub unmatched_count: String,
    pub unevaluable_count: String,
    pub mean_error: Option<f64>,
    pub rmse: Option<f64>,
    pub max_error: Option<f64>,
    pub error_unit: String,
    pub extra_metrics: Vec<ScoreMetricItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SparseVoxelCellItem {
    pub ix: i32,
    pub iy: i32,
    pub iz: i32,
    pub station_votes: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct SparseVoxelGridOverlay {
    pub center_session_id: String,
    pub world_frame_id: String,
    pub window_start_ns: String,
    pub window_end_ns: String,
    pub min_enu_m: [f64; 3],
    pub step_enu_m: [f64; 3],
    pub max_score: u32,
    pub cells: Vec<SparseVoxelCellItem>,
}

pub struct ViewerSnapshotInput<'a> {
    pub run_id: &'a str,
    pub mode: &'a str,
    pub update_id: u64,
    pub experiment_time_ns: i64,
    pub has_truth: bool,
    pub stations: &'a [StationConfig],
    pub latest_calibrations: &'a HashMap<String, CalibrationStateEvent>,
    pub truth: Option<&'a TruthFrame>,
    pub rays: &'a [RayEvent],
    pub calibration_states: &'a [CalibrationStateEvent],
    pub diagnostics: &'a [DiagnosticEvent],
    pub measurements: &'a [FusionMeasurement],
    pub tracks: &'a [TrackEvent],
    pub scores: &'a [ScoreEvent],
    pub sparse_voxel_grid: Option<&'a SparseVoxelGrid>,
}

fn vec2_arr(v: &Vec2) -> [f64; 2] {
    [v.x, v.y]
}

fn vec3_arr(v: Option<&Vec3>) -> [f64; 3] {
    v.map_or([0.0, 0.0, 0.0], |v| [v.x, v.y, v.z])
}

fn mat3_arr(m: Option<&Mat3>) -> [[f64; 3]; 3] {
    match m {
        Some(mat) if mat.values.len() == 9 => [
            [mat.values[0], mat.values[1], mat.values[2]],
            [mat.values[3], mat.values[4], mat.values[5]],
            [mat.values[6], mat.values[7], mat.values[8]],
        ],
        _ => [[0.0; 3]; 3],
    }
}

fn mat3_identity_if_absent(m: Option<&Mat3>) -> [[f64; 3]; 3] {
    match m {
        Some(mat) if mat.values.len() == 9 => mat3_arr(Some(mat)),
        _ => [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    }
}

fn midpoint_ns(start_ns: i64, end_ns: i64) -> i64 {
    ((start_ns as i128 + end_ns as i128).div_euclid(2)) as i64
}

fn calibration_health_str(v: i32) -> Option<&'static str> {
    match CalibrationHealth::try_from(v).unwrap_or(CalibrationHealth::Unspecified) {
        CalibrationHealth::Initializing => Some("initializing"),
        CalibrationHealth::Valid => Some("valid"),
        CalibrationHealth::Frozen => Some("frozen"),
        CalibrationHealth::Stale => Some("stale"),
        CalibrationHealth::Recovering => Some("recovering"),
        CalibrationHealth::Invalid => Some("invalid"),
        CalibrationHealth::Unspecified => None,
    }
}

fn calibration_usage_from_health(v: i32) -> Option<&'static str> {
    match CalibrationHealth::try_from(v).unwrap_or(CalibrationHealth::Unspecified) {
        CalibrationHealth::Valid => Some("valid"),
        CalibrationHealth::Frozen => Some("frozen"),
        CalibrationHealth::Stale => Some("stale"),
        _ => None,
    }
}

fn calibration_usage_str(v: i32) -> &'static str {
    match CalibrationUsage::try_from(v).unwrap_or(CalibrationUsage::Unspecified) {
        CalibrationUsage::Valid => "valid",
        CalibrationUsage::Frozen => "frozen",
        CalibrationUsage::Stale | CalibrationUsage::Unspecified => "stale",
    }
}

fn occlusion_status_str(v: i32) -> &'static str {
    match OcclusionStatus::try_from(v).unwrap_or(OcclusionStatus::Unknown) {
        OcclusionStatus::Visible => "visible",
        OcclusionStatus::Occluded => "occluded",
        OcclusionStatus::OutOfFov => "out_of_fov",
        OcclusionStatus::Unknown | OcclusionStatus::Unspecified => "unknown",
    }
}

fn fusion_status_str(v: i32) -> &'static str {
    match FusionStatus::try_from(v).unwrap_or(FusionStatus::Unspecified) {
        FusionStatus::Located => "located",
        FusionStatus::GeometryDegenerate => "geometry_degenerate",
        FusionStatus::AssociationAmbiguous => "association_ambiguous",
        FusionStatus::InsufficientStations => "insufficient_stations",
        FusionStatus::NoCandidateInVolume => "no_candidate_in_volume",
        FusionStatus::RejectedInput | FusionStatus::Unspecified => "rejected_input",
    }
}

fn track_lifecycle_str(v: i32) -> &'static str {
    match TrackLifecycle::try_from(v).unwrap_or(TrackLifecycle::Unspecified) {
        TrackLifecycle::Tentative | TrackLifecycle::Unspecified => "tentative",
        TrackLifecycle::Confirmed => "confirmed",
        TrackLifecycle::Coasting => "coasting",
        TrackLifecycle::Deleted => "deleted",
    }
}

fn event_id_arr(env: Option<&crate::proto::EventEnvelope>) -> [String; 3] {
    match env {
        Some(e) => [
            e.station_id.clone(),
            e.station_session_id.clone(),
            e.sequence_no.to_string(),
        ],
        None => [String::new(), String::new(), "0".to_string()],
    }
}

/// Spectral norm / major standard deviation sqrt(lambda_max) of a 2x2 angular covariance matrix.
fn angular_std_from_mat2(cov: Option<&crate::proto::Mat2>) -> Option<f64> {
    let m = cov?;
    if m.values.len() != 4 {
        return None;
    }
    let a = m.values[0];
    let b = m.values[1];
    let c = m.values[2];
    let d = m.values[3];
    let trace_half = 0.5 * (a + d);
    let diff_half = 0.5 * (a - d);
    let disc = (diff_half * diff_half + b * c).max(0.0).sqrt();
    let lambda_max = (trace_half + disc).max(0.0);
    Some(lambda_max.sqrt())
}

fn mat3_diag_std(cov: Option<&Mat3>) -> Option<[f64; 3]> {
    let m = cov?;
    if m.values.len() != 9 {
        return None;
    }
    Some([
        m.values[0].max(0.0).sqrt(),
        m.values[4].max(0.0).sqrt(),
        m.values[8].max(0.0).sqrt(),
    ])
}

fn mat6_diag_std(cov: Option<&crate::proto::Mat6>, offset: usize) -> Option<[f64; 3]> {
    let m = cov?;
    if m.values.len() != 36 {
        return None;
    }
    let i0 = offset * 6 + offset;
    let i1 = (offset + 1) * 6 + (offset + 1);
    let i2 = (offset + 2) * 6 + (offset + 2);
    Some([
        m.values[i0].max(0.0).sqrt(),
        m.values[i1].max(0.0).sqrt(),
        m.values[i2].max(0.0).sqrt(),
    ])
}

impl ViewerPushFrame {
    pub fn from_proto(input: ViewerSnapshotInput<'_>) -> Self {
        let stations = input
            .stations
            .iter()
            .map(|st| {
                let cal_ev = input.latest_calibrations.get(&st.station_id);
                let cal_state = cal_ev.and_then(|e| e.state.as_ref());
                let cal_sol = cal_ev.and_then(|e| e.solution.as_ref());
                let calibrated_orientation = cal_sol
                    .and_then(|s| s.rotation_enu_from_main.as_ref())
                    .filter(|m| m.values.len() == 9)
                    .map(|m| mat3_arr(Some(m)));
                let health = cal_state.and_then(|s| calibration_health_str(s.health));
                let usage = cal_state.and_then(|s| calibration_usage_from_health(s.health));
                let state_version = cal_state.map(|s| s.state_version.to_string());
                let solution_id = cal_state
                    .and_then(|s| s.solution_ref.as_ref())
                    .or_else(|| cal_sol.and_then(|s| s.solution_ref.as_ref()))
                    .map(|r| r.solution_id.clone())
                    .filter(|s| !s.is_empty());

                StationDisplayState {
                    station_id: st.station_id.clone(),
                    main_camera_position_enu_m: vec3_arr(st.main_camera_position_enu_m.as_ref()),
                    baseline_orientation_enu_from_main: mat3_identity_if_absent(
                        st.baseline_orientation_enu_from_main.as_ref(),
                    ),
                    calibrated_orientation_enu_from_main: calibrated_orientation,
                    calibration_health: health,
                    calibration_usage: usage,
                    state_version,
                    solution_id,
                }
            })
            .collect();

        let truth = if input.has_truth {
            input.truth.map(|tf| TruthOverlay {
                world_frame_id: tf.world_frame_id.clone(),
                experiment_time_ns: tf.experiment_time_ns.to_string(),
                step_index: tf.step_index.to_string(),
                targets: tf
                    .targets
                    .iter()
                    .map(|t| TargetTruthItem {
                        target_id: t.target_id.clone(),
                        target_class: t.target_class.clone(),
                        position_enu_m: vec3_arr(t.position_enu_m.as_ref()),
                        velocity_enu_mps: vec3_arr(t.velocity_enu_mps.as_ref()),
                        bounding_radius_m: t.bounding_radius_m,
                        body_aabb_diagonal_m: t.body_aabb_diagonal_m,
                    })
                    .collect(),
                projections: tf
                    .projections
                    .iter()
                    .map(|p| StationTargetProjectionItem {
                        station_id: p.station_id.clone(),
                        target_id: p.target_id.clone(),
                        occlusion_status: occlusion_status_str(p.occlusion_status),
                        projected_centroid_px: p.projected_centroid_px.as_ref().map(vec2_arr),
                        projected_diagonal_px: p.projected_diagonal_px,
                        range_m: p.range_m,
                        frame_id: p.frame_id.clone(),
                    })
                    .collect(),
            })
        } else {
            None
        };

        let ray_items = input
            .rays
            .iter()
            .map(|r| {
                let (exp_start, exp_end) = r
                    .capture_time
                    .as_ref()
                    .map_or((0, 0), |c| (c.exposure_start_ns, c.exposure_end_ns));
                let mid = midpoint_ns(exp_start, exp_end);
                let angular_std_rad = angular_std_from_mat2(
                    r.uncertainty
                        .as_ref()
                        .and_then(|u| u.independent_angular_covariance_rad2.as_ref()),
                );
                RayItem {
                    event_id: event_id_arr(r.envelope.as_ref()),
                    frame_id: r.frame_id.clone(),
                    capture_time_ns: mid.to_string(),
                    exposure_start_ns: exp_start.to_string(),
                    exposure_end_ns: exp_end.to_string(),
                    origin_enu_m: vec3_arr(r.ray_origin_enu_m.as_ref()),
                    direction_enu_unit: vec3_arr(r.ray_direction_enu_unit.as_ref()),
                    calibration_usage: calibration_usage_str(r.calibration_usage),
                    detection_score: r.detection_score,
                    angular_std_rad,
                    local_track_id: r.local_track_id.clone(),
                    quality_flags: r.quality_flags.clone(),
                }
            })
            .collect();

        let cal_items = input
            .calibration_states
            .iter()
            .map(|c| {
                let st = c.state.as_ref();
                let sol = c.solution.as_ref();
                let health = st
                    .and_then(|s| calibration_health_str(s.health))
                    .unwrap_or("invalid");
                let solution_id = st
                    .and_then(|s| s.solution_ref.as_ref())
                    .or_else(|| sol.and_then(|s| s.solution_ref.as_ref()))
                    .map(|r| r.solution_id.clone())
                    .filter(|s| !s.is_empty());
                CalibrationStateItem {
                    event_id: event_id_arr(c.envelope.as_ref()),
                    station_id: st
                        .map(|s| s.station_id.clone())
                        .or_else(|| c.envelope.as_ref().map(|e| e.station_id.clone()))
                        .unwrap_or_default(),
                    state_version: st.map_or(0, |s| s.state_version).to_string(),
                    health,
                    solution_id,
                    observed_at_ns: st
                        .and_then(|s| s.observed_at.as_ref())
                        .map_or(0, |t| t.time_ns)
                        .to_string(),
                    reason_codes: st.map(|s| s.reason_codes.clone()).unwrap_or_default(),
                }
            })
            .collect();

        let diag_items = input
            .diagnostics
            .iter()
            .map(|d| DiagnosticItem {
                event_id: event_id_arr(d.envelope.as_ref()),
                station_id: d
                    .envelope
                    .as_ref()
                    .map(|e| e.station_id.clone())
                    .unwrap_or_default(),
                capture_time_ns: d
                    .capture_time
                    .as_ref()
                    .map(|c| midpoint_ns(c.exposure_start_ns, c.exposure_end_ns).to_string()),
                category: d.category.clone(),
                reason_codes: d.reason_codes.clone(),
            })
            .collect();

        let measurement_items = input
            .measurements
            .iter()
            .map(|m| {
                let status = fusion_status_str(m.status);
                let is_located = status == "located";
                MeasurementItem {
                    center_session_id: m.center_session_id.clone(),
                    measurement_id: m.measurement_id.clone(),
                    reference_time_ns: m.reference_time_ns.to_string(),
                    support_exposure_span_ns: m.support_exposure_span_ns.to_string(),
                    status,
                    position_enu_m: if is_located {
                        m.position_enu_m.as_ref().map(|v| [v.x, v.y, v.z])
                    } else {
                        None
                    },
                    position_std_enu_m: if is_located {
                        mat3_diag_std(m.conditional_position_covariance_enu_m2.as_ref())
                    } else {
                        None
                    },
                    supporting_ray_event_ids: m
                        .supporting_ray_event_ids
                        .iter()
                        .map(|id| {
                            [
                                id.station_id.clone(),
                                id.station_session_id.clone(),
                                id.sequence_no.to_string(),
                            ]
                        })
                        .collect(),
                    supporting_station_ids: m.supporting_station_ids.clone(),
                    minimum_intersection_angle_deg: m
                        .minimum_intersection_angle_rad
                        .map(f64::to_degrees),
                    geometry_condition: m.geometry_condition,
                    reason_codes: m.reason_codes.clone(),
                }
            })
            .collect();

        let track_items = input
            .tracks
            .iter()
            .map(|tr| TrackItem {
                center_session_id: tr.center_session_id.clone(),
                track_id: tr.track_id.clone(),
                event_time_ns: tr.event_time_ns.to_string(),
                lifecycle: track_lifecycle_str(tr.lifecycle),
                position_enu_m: tr.position_enu_m.as_ref().map(|v| [v.x, v.y, v.z]),
                velocity_enu_mps: tr.velocity_enu_mps.as_ref().map(|v| [v.x, v.y, v.z]),
                position_std_enu_m: mat6_diag_std(tr.conditional_state_covariance.as_ref(), 0),
                velocity_std_enu_mps: mat6_diag_std(tr.conditional_state_covariance.as_ref(), 3),
                source_measurement_ids: tr
                    .source_measurement_refs
                    .iter()
                    .map(|r| r.measurement_id.clone())
                    .collect(),
                reason_codes: tr.reason_codes.clone(),
            })
            .collect();

        let score = if input.has_truth && !input.scores.is_empty() {
            Some(ScoreOverlay {
                events: input
                    .scores
                    .iter()
                    .map(|s| ScoreEventItem {
                        run_id: s.run_id.clone(),
                        experiment_time_ns: s.experiment_time_ns.to_string(),
                        matched_count: s.matched_count.to_string(),
                        unmatched_count: s.unmatched_count.to_string(),
                        unevaluable_count: s.unevaluable_count.to_string(),
                        mean_error: s.mean_error,
                        rmse: s.rmse,
                        max_error: s.max_error,
                        error_unit: s.error_unit.clone(),
                        extra_metrics: s
                            .extra_metrics
                            .iter()
                            .map(|em| ScoreMetricItem {
                                name: em.name.clone(),
                                value: em.value,
                                unit: em.unit.clone(),
                            })
                            .collect(),
                    })
                    .collect(),
            })
        } else {
            None
        };

        let sparse_voxel_grid = input.sparse_voxel_grid.map(|g| {
            let max_score = g.cells.iter().map(|c| c.station_votes).max().unwrap_or(0);
            SparseVoxelGridOverlay {
                center_session_id: g.center_session_id.clone(),
                world_frame_id: g.world_frame_id.clone(),
                window_start_ns: g.window_start_ns.to_string(),
                window_end_ns: g.window_end_ns.to_string(),
                min_enu_m: vec3_arr(g.min_enu_m.as_ref()),
                step_enu_m: vec3_arr(g.step_enu_m.as_ref()),
                max_score,
                cells: g
                    .cells
                    .iter()
                    .map(|c| SparseVoxelCellItem {
                        ix: c.ix,
                        iy: c.iy,
                        iz: c.iz,
                        station_votes: c.station_votes,
                    })
                    .collect(),
            }
        });

        Self {
            schema_version: VIEWER_JSON_SCHEMA_VERSION,
            run_id: input.run_id.to_string(),
            mode: input.mode.to_string(),
            update_id: input.update_id.max(1).to_string(),
            sequence_no: "1".to_string(),
            experiment_time_ns: input.experiment_time_ns.to_string(),
            has_truth: input.has_truth,
            stations,
            truth,
            rays: RaysOverlay {
                items: ray_items,
                calibration_states: cal_items,
                diagnostics: diag_items,
            },
            answer: AnswerOverlay {
                measurements: measurement_items,
                tracks: track_items,
            },
            score,
            sparse_voxel_grid,
        }
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct PublishStats {
    pub delivered: usize,
    pub dropped_full: usize,
    pub pruned_closed: usize,
}

struct ViewerState {
    subscribers: Mutex<HashMap<u64, mpsc::Sender<Bytes>>>,
    next_id: AtomicU64,
    next_sequence_no: AtomicU64,
    dropped_total: AtomicU64,
}

/// Ephemeral TCP fan-out hub pushing JSON frames to optional Viewer subscribers.
#[derive(Clone)]
pub struct ViewerHub {
    local_addr: SocketAddr,
    state: Arc<ViewerState>,
    accept_handle: Arc<Mutex<Option<JoinHandle<()>>>>,
}

impl ViewerHub {
    pub async fn bind(addr: SocketAddr, queue_capacity: usize) -> io::Result<Self> {
        let cap = queue_capacity.max(1);
        let listener = TcpListener::bind(addr).await?;
        let local_addr = listener.local_addr()?;
        let state = Arc::new(ViewerState {
            subscribers: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
            next_sequence_no: AtomicU64::new(1),
            dropped_total: AtomicU64::new(0),
        });
        let state_clone = Arc::clone(&state);
        let handle = tokio::spawn(async move {
            while let Ok((stream, _peer)) = listener.accept().await {
                let _ = stream.set_nodelay(true);
                let id = state_clone.next_id.fetch_add(1, Ordering::Relaxed);
                let (tx, rx) = mpsc::channel::<Bytes>(cap);
                if let Ok(mut subs) = state_clone.subscribers.lock() {
                    subs.insert(id, tx);
                }
                let state_for_writer = Arc::clone(&state_clone);
                tokio::spawn(async move {
                    run_viewer_writer(stream, rx).await;
                    if let Ok(mut subs) = state_for_writer.subscribers.lock() {
                        subs.remove(&id);
                    }
                });
            }
        });

        Ok(Self {
            local_addr,
            state,
            accept_handle: Arc::new(Mutex::new(Some(handle))),
        })
    }

    pub fn local_addr(&self) -> SocketAddr {
        self.local_addr
    }

    pub fn subscriber_count(&self) -> usize {
        self.state.subscribers.lock().map(|s| s.len()).unwrap_or(0)
    }

    pub fn dropped_total(&self) -> u64 {
        self.state.dropped_total.load(Ordering::Relaxed)
    }

    /// Non-blocking publish: serializes only when at least one viewer is connected,
    /// assigns a monotonic `sequence_no`, drops on slow/full queues, and never blocks.
    pub fn publish(&self, frame: &mut ViewerPushFrame) -> PublishStats {
        let has_subs = self
            .state
            .subscribers
            .lock()
            .map(|s| !s.is_empty())
            .unwrap_or(false);
        if !has_subs {
            return PublishStats::default();
        }

        let seq = self.state.next_sequence_no.fetch_add(1, Ordering::Relaxed);
        frame.sequence_no = seq.to_string();

        let Ok(json_bytes) = serde_json::to_vec(frame) else {
            return PublishStats::default();
        };
        if json_bytes.len() > MAX_FRAME_BYTES {
            return PublishStats::default();
        }
        let payload = Bytes::from(json_bytes);

        let mut stats = PublishStats::default();
        let mut dead_ids = Vec::new();
        if let Ok(mut subs) = self.state.subscribers.lock() {
            for (&id, tx) in subs.iter() {
                match tx.try_send(payload.clone()) {
                    Ok(()) => stats.delivered += 1,
                    Err(mpsc::error::TrySendError::Full(_)) => {
                        stats.dropped_full += 1;
                        self.state.dropped_total.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => {
                        dead_ids.push(id);
                        stats.pruned_closed += 1;
                    }
                }
            }
            for id in dead_ids {
                subs.remove(&id);
            }
        }
        stats
    }

    pub fn shutdown(&self) {
        if let Ok(mut guard) = self.accept_handle.lock() {
            if let Some(handle) = guard.take() {
                handle.abort();
            }
        }
        if let Ok(mut subs) = self.state.subscribers.lock() {
            subs.clear();
        }
    }
}

async fn run_viewer_writer(mut stream: TcpStream, mut rx: mpsc::Receiver<Bytes>) {
    while let Some(payload) = rx.recv().await {
        if write_raw_frame_async(&mut stream, &payload, MAX_FRAME_BYTES)
            .await
            .is_err()
        {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::framing::read_raw_frame_async;
    use crate::proto::{
        CalibrationQuality, CalibrationSolution, CalibrationSolutionRef, CalibrationState,
        CaptureTime, EventEnvelope, EventId, EventType, Mat2, Mat6, MeasurementRef, ProcessingTime,
        RayUncertainty, ScoreMetric, SparseVoxelCell, StationTargetProjection, TargetTruth,
    };
    use tokio::time::{timeout, Duration};

    #[tokio::test]
    async fn viewer_push_drops_on_slow_consumer_and_never_blocks() {
        let hub = ViewerHub::bind("127.0.0.1:0".parse().unwrap(), 2)
            .await
            .unwrap();
        let addr = hub.local_addr();

        let mut active = TcpStream::connect(addr).await.unwrap();
        let stalled = TcpStream::connect(addr).await.unwrap();
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert_eq!(hub.subscriber_count(), 2);

        let empty_cal = HashMap::new();
        for step in 0..20u64 {
            let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                run_id: "r-1",
                mode: "live",
                update_id: step + 1,
                experiment_time_ns: 9_007_199_254_740_993 + step as i64,
                has_truth: false,
                stations: &[],
                latest_calibrations: &empty_cal,
                truth: None,
                rays: &[],
                calibration_states: &[],
                diagnostics: &[],
                measurements: &[],
                tracks: &[],
                scores: &[],
                sparse_voxel_grid: None,
            });
            let _ = hub.publish(&mut frame);
        }

        let raw = timeout(
            Duration::from_secs(1),
            read_raw_frame_async(&mut active, MAX_FRAME_BYTES),
        )
        .await
        .unwrap()
        .unwrap()
        .unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&raw).unwrap();
        assert_eq!(parsed["schema_version"], "chongming-viewer-v1");
        assert_eq!(parsed["has_truth"], false);
        assert_eq!(parsed["update_id"], "1");
        assert_eq!(parsed["sequence_no"], "1");
        assert_eq!(parsed["experiment_time_ns"], "9007199254740993");
        assert!(parsed.get("truth").is_none());
        assert!(parsed.get("score").is_none());
        assert!(parsed.get("sparse_voxel_grid").is_none());
        assert!(parsed["stations"].is_array());
        assert!(parsed["rays"]["items"].is_array());
        assert!(parsed["answer"]["measurements"].is_array());

        drop(stalled);
        hub.shutdown();
    }

    #[test]
    fn viewer_json_schema_matches_protocol_spec_types_and_derivations() {
        let station = StationConfig {
            station_id: "station-west".into(),
            main_camera_position_enu_m: Some(Vec3 {
                x: -250.0,
                y: 0.0,
                z: 12.0,
            }),
            baseline_orientation_enu_from_main: Some(Mat3 {
                values: vec![1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            }),
            ..Default::default()
        };
        let cal_ev = CalibrationStateEvent {
            envelope: Some(EventEnvelope {
                event_type: EventType::CalibrationState as i32,
                station_id: "station-west".into(),
                station_session_id: "sess-w1".into(),
                sequence_no: 7,
                ..Default::default()
            }),
            state: Some(CalibrationState {
                station_id: "station-west".into(),
                state_version: 3,
                health: CalibrationHealth::Valid as i32,
                solution_ref: Some(CalibrationSolutionRef {
                    station_id: "station-west".into(),
                    station_session_id: "sess-w1".into(),
                    solution_id: "sol-3".into(),
                }),
                observed_at: Some(ProcessingTime {
                    clock_domain_id: "sim".into(),
                    time_ns: 1_763_784_000_123_456_789,
                }),
                reason_codes: vec![],
                ..Default::default()
            }),
            solution: Some(CalibrationSolution {
                rotation_enu_from_main: Some(Mat3 {
                    values: vec![1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                }),
                quality: Some(CalibrationQuality::default()),
                ..Default::default()
            }),
        };
        let mut latest_cal = HashMap::new();
        latest_cal.insert("station-west".to_string(), cal_ev.clone());

        let tf = TruthFrame {
            world_frame_id: "enu".into(),
            experiment_time_ns: 1_763_784_000_123_456_789,
            step_index: 42,
            targets: vec![TargetTruth {
                target_id: "t1".into(),
                target_class: "f86".into(),
                position_enu_m: Some(Vec3 {
                    x: 120.5,
                    y: 840.0,
                    z: 210.0,
                }),
                velocity_enu_mps: Some(Vec3 {
                    x: 18.0,
                    y: -4.0,
                    z: 0.5,
                }),
                bounding_radius_m: 6.5,
                body_aabb_diagonal_m: Some(13.0),
            }],
            projections: vec![StationTargetProjection {
                station_id: "station-west".into(),
                target_id: "t1".into(),
                occlusion_status: OcclusionStatus::OutOfFov as i32,
                projected_centroid_px: Some(Vec2 { x: 964.2, y: 412.8 }),
                projected_diagonal_px: Some(14.3),
                range_m: Some(939.4),
                frame_id: Some("f-42".into()),
            }],
        };

        let ray = RayEvent {
            envelope: Some(EventEnvelope {
                event_type: EventType::Ray as i32,
                station_id: "station-west".into(),
                station_session_id: "sess-w1".into(),
                sequence_no: 105,
                ..Default::default()
            }),
            frame_id: "f-42".into(),
            capture_time: Some(CaptureTime {
                clock_domain_id: "sim".into(),
                exposure_start_ns: 1_763_784_000_000_000_000,
                exposure_end_ns: 1_763_784_000_000_000_010,
                ..Default::default()
            }),
            calibration_usage: CalibrationUsage::Valid as i32,
            ray_origin_enu_m: Some(Vec3 {
                x: -250.0,
                y: 0.0,
                z: 12.0,
            }),
            ray_direction_enu_unit: Some(Vec3 {
                x: 0.4,
                y: 0.9,
                z: 0.2,
            }),
            uncertainty: Some(RayUncertainty {
                independent_angular_covariance_rad2: Some(Mat2 {
                    values: vec![4.0, 0.0, 0.0, 1.0],
                }),
                ..Default::default()
            }),
            detection_score: 0.88,
            local_track_id: Some("1".into()),
            quality_flags: vec!["ok".into()],
            ..Default::default()
        };

        let fm_non_located = FusionMeasurement {
            center_session_id: "sess-c1".into(),
            measurement_id: "m-42".into(),
            reference_time_ns: 1_763_784_000_123_456_789,
            support_exposure_span_ns: 0,
            status: FusionStatus::InsufficientStations as i32,
            position_enu_m: Some(Vec3 {
                x: 1.0,
                y: 2.0,
                z: 3.0,
            }),
            conditional_position_covariance_enu_m2: Some(Mat3 {
                values: vec![4.0, 0.0, 0.0, 0.0, 9.0, 0.0, 0.0, 0.0, 16.0],
            }),
            supporting_ray_event_ids: vec![EventId {
                station_id: "station-west".into(),
                station_session_id: "sess-w1".into(),
                sequence_no: 105,
            }],
            supporting_station_ids: vec!["station-west".into()],
            minimum_intersection_angle_rad: Some(std::f64::consts::FRAC_PI_6),
            geometry_condition: Some(2.1),
            reason_codes: vec!["one_station".into()],
            ..Default::default()
        };

        let mut cov6 = vec![0.0; 36];
        cov6[0] = 4.0;
        cov6[7] = 9.0;
        cov6[14] = 16.0;
        cov6[21] = 0.25;
        cov6[28] = 1.0;
        cov6[35] = 2.25;
        let tr = TrackEvent {
            center_session_id: "sess-c1".into(),
            track_id: "trk-1".into(),
            event_time_ns: 1_763_784_000_123_456_789,
            lifecycle: TrackLifecycle::Confirmed as i32,
            position_enu_m: Some(Vec3 {
                x: 10.0,
                y: 20.0,
                z: 30.0,
            }),
            velocity_enu_mps: Some(Vec3 {
                x: 1.0,
                y: 2.0,
                z: 3.0,
            }),
            conditional_state_covariance: Some(Mat6 { values: cov6 }),
            source_measurement_refs: vec![MeasurementRef {
                center_session_id: "sess-c1".into(),
                measurement_id: "m-42".into(),
            }],
            ..Default::default()
        };

        let score = ScoreEvent {
            run_id: "run-01".into(),
            experiment_time_ns: 1_763_784_000_123_456_789,
            matched_count: 1,
            unmatched_count: 0,
            unevaluable_count: 0,
            mean_error: Some(1.077),
            rmse: Some(1.077),
            max_error: Some(1.077),
            error_unit: "m".into(),
            extra_metrics: vec![ScoreMetric {
                name: "missed_targets".into(),
                value: Some(0.0),
                unit: "count".into(),
            }],
        };

        let grid = SparseVoxelGrid {
            center_session_id: "sess-c1".into(),
            world_frame_id: "enu".into(),
            window_start_ns: 10,
            window_end_ns: 20,
            min_enu_m: Some(Vec3 {
                x: 0.0,
                y: 0.0,
                z: 0.0,
            }),
            step_enu_m: Some(Vec3 {
                x: 5.0,
                y: 5.0,
                z: 5.0,
            }),
            cells: vec![SparseVoxelCell {
                ix: 1,
                iy: 2,
                iz: 3,
                station_votes: 4,
            }],
        };

        let frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
            run_id: "run-01",
            mode: "integrated",
            update_id: 42,
            experiment_time_ns: 1_763_784_000_123_456_789,
            has_truth: true,
            stations: &[station],
            latest_calibrations: &latest_cal,
            truth: Some(&tf),
            rays: &[ray],
            calibration_states: &[cal_ev],
            diagnostics: &[],
            measurements: &[fm_non_located],
            tracks: &[tr],
            scores: &[score],
            sparse_voxel_grid: Some(&grid),
        });

        let j: serde_json::Value = serde_json::to_value(&frame).unwrap();
        assert_eq!(j["schema_version"], "chongming-viewer-v1");
        assert_eq!(j["update_id"], "42");
        assert_eq!(j["has_truth"], true);
        assert_eq!(j["stations"][0]["calibration_health"], "valid");
        assert_eq!(j["stations"][0]["calibration_usage"], "valid");
        assert_eq!(j["stations"][0]["state_version"], "3");
        assert_eq!(j["stations"][0]["solution_id"], "sol-3");
        assert_eq!(
            j["stations"][0]["main_camera_position_enu_m"],
            serde_json::json!([-250.0, 0.0, 12.0])
        );
        assert_eq!(j["truth"]["step_index"], "42");
        assert_eq!(
            j["truth"]["projections"][0]["occlusion_status"],
            "out_of_fov"
        );
        assert_eq!(
            j["rays"]["items"][0]["event_id"],
            serde_json::json!(["station-west", "sess-w1", "105"])
        );
        assert_eq!(
            j["rays"]["items"][0]["capture_time_ns"],
            "1763784000000000005"
        );
        assert_eq!(j["rays"]["items"][0]["angular_std_rad"], 2.0);
        assert_eq!(j["rays"]["calibration_states"][0]["health"], "valid");
        assert_eq!(
            j["answer"]["measurements"][0]["status"],
            "insufficient_stations"
        );
        assert!(j["answer"]["measurements"][0]["position_enu_m"].is_null());
        assert!(j["answer"]["measurements"][0]["position_std_enu_m"].is_null());
        assert!(
            (j["answer"]["measurements"][0]["minimum_intersection_angle_deg"]
                .as_f64()
                .unwrap()
                - 30.0)
                .abs()
                < 1e-9
        );
        assert_eq!(j["answer"]["tracks"][0]["lifecycle"], "confirmed");
        assert_eq!(
            j["answer"]["tracks"][0]["position_std_enu_m"],
            serde_json::json!([2.0, 3.0, 4.0])
        );
        assert_eq!(
            j["answer"]["tracks"][0]["velocity_std_enu_mps"],
            serde_json::json!([0.5, 1.0, 1.5])
        );
        assert_eq!(
            j["answer"]["tracks"][0]["source_measurement_ids"],
            serde_json::json!(["m-42"])
        );
        assert_eq!(j["score"]["events"][0]["matched_count"], "1");
        assert_eq!(j["sparse_voxel_grid"]["max_score"], 4);
    }
}
