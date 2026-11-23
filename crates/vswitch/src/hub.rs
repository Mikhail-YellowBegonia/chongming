//! V-Switch core hub: Replay (edge / center / integrated, lock-step & wall-clock),
//! Live Relay (real hardware / field trial), and Record (Godot Producer -> Test Scene).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::Value;
use thiserror::Error;
use tokio::sync::{mpsc, oneshot};
use tokio::time::{sleep_until, timeout, Instant};

use crate::bus::{BusEvent, BusServer, PeerHandle, TruthEvent, TruthPeerHandle, TruthServer};
use crate::proto::{
    bus_message, now_wall_time_ns, test_framework_message, BusMessage, CalibrationHealth,
    CalibrationQuality, CalibrationSolution, CalibrationState, CalibrationStateEvent, CameraRole,
    ComponentRole, DiagnosticEvent, EventEnvelope, EventType, FrameIndexEntry, FramedVideoPacket,
    FusionMeasurement, Mat3, ProgressReport, RayEvent, ScoreEvent, SparseVoxelGrid, StationConfig,
    TestFrameworkMessage, TrackEvent, TruthFrame, VideoCodec,
};
use crate::storage::{
    validate_relpath, RunOutputConfig, RunOutputWriter, StorageError, TestSceneConfig,
    TestSceneReader, TestSceneWriter,
};
use crate::video::{
    demux_scene_tracks, H264AccessUnit, H264EncodePreset, VideoError, VideoTrackEncoder,
};
use crate::viewer::{
    ViewerHub, ViewerPushFrame, ViewerSnapshotInput, DEFAULT_VIEWER_QUEUE_CAPACITY,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplayMode {
    Edge,
    Center,
    Integrated,
}

impl ReplayMode {
    pub fn as_str(self) -> &'static str {
        match self {
            ReplayMode::Edge => "edge",
            ReplayMode::Center => "center",
            ReplayMode::Integrated => "integrated",
        }
    }

    pub fn needs_edge(self) -> bool {
        matches!(self, ReplayMode::Edge | ReplayMode::Integrated)
    }

    pub fn needs_center(self) -> bool {
        matches!(self, ReplayMode::Center | ReplayMode::Integrated)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SteppingMode {
    LockStep,
    WallClock,
}

#[derive(Debug, Clone, Copy)]
pub struct BoundAddresses {
    pub bus_addr: SocketAddr,
    pub truth_addr: Option<SocketAddr>,
    pub viewer_addr: Option<SocketAddr>,
}

#[derive(Debug, Error)]
pub enum HubError {
    #[error("storage error: {0}")]
    Storage(#[from] StorageError),
    #[error("pb.zst error: {0}")]
    PbZst(#[from] crate::pbzst::PbZstError),
    #[error("video error: {0}")]
    Video(#[from] VideoError),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("timed out waiting for required peers: {0}")]
    HandshakeTimeout(String),
    #[error("lock-step timed out waiting for ProgressReport at {0} ns")]
    StepTimeout(i64),
    #[error("required peer disconnected unexpectedly: {0}")]
    PeerDisconnected(String),
    #[error("invalid packet or state: {0}")]
    InvalidState(String),
}

// --- Replay Configuration & Runner ---

#[derive(Debug, Clone)]
pub struct ReplayConfig {
    pub mode: ReplayMode,
    pub stepping: SteppingMode,
    pub scene_dir: PathBuf,
    pub run_output_dir: PathBuf,
    pub run_id: String,
    pub bus_bind: SocketAddr,
    pub truth_bind: Option<SocketAddr>,
    pub viewer_bind: Option<SocketAddr>,
    pub record_voxels: bool,
    pub wait_scorer: bool,
    pub verify_videos: bool,
    pub flush_every: usize,
    pub handshake_timeout: Duration,
    pub step_timeout: Duration,
    pub drain_timeout: Duration,
    pub wall_clock_speed: f64,
}

impl Default for ReplayConfig {
    fn default() -> Self {
        Self {
            mode: ReplayMode::Integrated,
            stepping: SteppingMode::LockStep,
            scene_dir: PathBuf::new(),
            run_output_dir: PathBuf::new(),
            run_id: "run-1".to_string(),
            bus_bind: "127.0.0.1:0".parse().expect("valid loopback addr"),
            truth_bind: Some("127.0.0.1:0".parse().expect("valid loopback addr")),
            viewer_bind: Some("127.0.0.1:0".parse().expect("valid loopback addr")),
            record_voxels: false,
            wait_scorer: false,
            verify_videos: false,
            flush_every: 256,
            handshake_timeout: Duration::from_secs(30),
            step_timeout: Duration::from_secs(30),
            drain_timeout: Duration::from_millis(500),
            wall_clock_speed: 1.0,
        }
    }
}

#[derive(Debug, Clone)]
struct PeerState {
    handle: PeerHandle,
    completed_time_ns: Option<i64>,
    finite_end_reached: bool,
    dispatched_time_ns: Option<i64>,
}

struct ReplayRuntime {
    mode: ReplayMode,
    stepping: SteppingMode,
    end_notice_sent: bool,
    record_voxels: bool,
    run_id: String,
    run_writer: RunOutputWriter,
    viewer_hub: Option<ViewerHub>,
    edges: HashMap<u64, PeerState>,
    station_to_edge: HashMap<String, u64>,
    centers: HashMap<u64, PeerState>,
    scorers: HashMap<u64, PeerHandle>,
    truth_peers: HashMap<u64, TruthPeerHandle>,
    bus_seq: AtomicU64,
    truth_seq: AtomicU64,
    display_update_id: u64,
    stations: Vec<StationConfig>,
    latest_calibrations: HashMap<String, CalibrationStateEvent>,
    latest_truth: Option<TruthFrame>,
    latest_scores_by_unit: BTreeMap<String, ScoreEvent>,
    latest_voxels: Option<SparseVoxelGrid>,
    step_rays: Vec<RayEvent>,
    step_calibration_states: Vec<CalibrationStateEvent>,
    step_diagnostics: Vec<DiagnosticEvent>,
    step_measurements: Vec<FusionMeasurement>,
    step_tracks: Vec<TrackEvent>,
}

impl ReplayRuntime {
    fn next_bus_seq(&self) -> u64 {
        self.bus_seq.fetch_add(1, Ordering::Relaxed)
    }

    fn next_truth_seq(&self) -> u64 {
        self.truth_seq.fetch_add(1, Ordering::Relaxed)
    }

    fn wrap_bus(&self, payload: bus_message::Payload) -> BusMessage {
        BusMessage {
            bus_sequence_no: self.next_bus_seq(),
            sent_wall_time_ns: now_wall_time_ns(),
            payload: Some(payload),
        }
    }

    async fn send_truth_frame(&mut self, tf: &TruthFrame) {
        self.latest_truth = Some(tf.clone());
        let msg = TestFrameworkMessage {
            sequence_no: self.next_truth_seq(),
            payload: Some(test_framework_message::Payload::TruthFrame(tf.clone())),
        };
        for peer in self.truth_peers.values() {
            let _ = peer.send(msg.clone()).await;
        }
    }

    fn publish_viewer(&mut self, experiment_time_ns: i64) {
        let Some(viewer) = &self.viewer_hub else {
            return;
        };
        self.display_update_id += 1;
        let scores: Vec<ScoreEvent> = self.latest_scores_by_unit.values().cloned().collect();
        let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
            run_id: &self.run_id,
            mode: self.mode.as_str(),
            update_id: self.display_update_id,
            experiment_time_ns,
            has_truth: true,
            stations: &self.stations,
            latest_calibrations: &self.latest_calibrations,
            truth: self.latest_truth.as_ref(),
            rays: &self.step_rays,
            calibration_states: &self.step_calibration_states,
            diagnostics: &self.step_diagnostics,
            measurements: &self.step_measurements,
            tracks: &self.step_tracks,
            scores: &scores,
            sparse_voxel_grid: self.latest_voxels.as_ref(),
        });
        let _ = viewer.publish(&mut frame);
    }

    fn clear_step_overlays(&mut self) {
        self.step_rays.clear();
        self.step_calibration_states.clear();
        self.step_diagnostics.clear();
        self.step_measurements.clear();
        self.step_tracks.clear();
    }

    fn handle_bus_event(&mut self, ev: BusEvent) -> Result<(), HubError> {
        match ev {
            BusEvent::PeerConnected(handle) => {
                match handle.info.role {
                    ComponentRole::Edge => {
                        let pid = handle.info.peer_id;
                        for st in &handle.info.station_ids {
                            self.station_to_edge.insert(st.clone(), pid);
                        }
                        self.edges.insert(
                            pid,
                            PeerState {
                                handle,
                                completed_time_ns: None,
                                finite_end_reached: false,
                                dispatched_time_ns: None,
                            },
                        );
                    }
                    ComponentRole::Center => {
                        let pid = handle.info.peer_id;
                        self.centers.insert(
                            pid,
                            PeerState {
                                handle,
                                completed_time_ns: None,
                                finite_end_reached: false,
                                dispatched_time_ns: None,
                            },
                        );
                    }
                    ComponentRole::Scorer => {
                        self.scorers.insert(handle.info.peer_id, handle);
                    }
                    _ => {}
                }
                Ok(())
            }
            BusEvent::PeerDisconnected {
                peer_id,
                role,
                instance_id,
            } => {
                match role {
                    ComponentRole::Edge => {
                        if let Some(st) = self.edges.remove(&peer_id) {
                            for sid in &st.handle.info.station_ids {
                                if self.station_to_edge.get(sid) == Some(&peer_id) {
                                    self.station_to_edge.remove(sid);
                                }
                            }
                            if self.mode.needs_edge()
                                && !st.finite_end_reached
                                && !(self.end_notice_sent
                                    && self.stepping == SteppingMode::WallClock)
                            {
                                return Err(HubError::PeerDisconnected(format!(
                                    "EDGE {instance_id}"
                                )));
                            }
                        }
                    }
                    ComponentRole::Center => {
                        if let Some(st) = self.centers.remove(&peer_id) {
                            if self.mode.needs_center()
                                && !st.finite_end_reached
                                && !(self.end_notice_sent
                                    && self.stepping == SteppingMode::WallClock)
                            {
                                return Err(HubError::PeerDisconnected(format!(
                                    "CENTER {instance_id}"
                                )));
                            }
                        }
                    }
                    ComponentRole::Scorer => {
                        self.scorers.remove(&peer_id);
                    }
                    _ => {}
                }
                Ok(())
            }
            BusEvent::Message {
                peer_id,
                role,
                message,
                ..
            } => {
                self.process_bus_message(peer_id, role, *message)?;
                Ok(())
            }
        }
    }

    fn process_bus_message(
        &mut self,
        peer_id: u64,
        role: ComponentRole,
        message: BusMessage,
    ) -> Result<(), HubError> {
        let Some(payload) = message.payload.clone() else {
            return Ok(());
        };
        match role {
            ComponentRole::Edge => {
                self.run_writer.write_edge_message(&message)?;
                match &payload {
                    bus_message::Payload::RayEvent(ray) => {
                        self.step_rays.push(ray.clone());
                        if self.mode == ReplayMode::Integrated {
                            self.forward_to_centers(&message);
                        }
                        self.forward_to_scorers(&message);
                    }
                    bus_message::Payload::CalibrationState(cal) => {
                        let station_id = cal
                            .state
                            .as_ref()
                            .map(|s| s.station_id.clone())
                            .or_else(|| cal.envelope.as_ref().map(|e| e.station_id.clone()))
                            .unwrap_or_default();
                        if !station_id.is_empty() {
                            self.latest_calibrations.insert(station_id, cal.clone());
                        }
                        self.step_calibration_states.push(cal.clone());
                        if self.mode == ReplayMode::Integrated {
                            self.forward_to_centers(&message);
                        }
                        self.forward_to_scorers(&message);
                    }
                    bus_message::Payload::DiagnosticEvent(diag) => {
                        self.step_diagnostics.push(diag.clone());
                        if self.mode == ReplayMode::Integrated {
                            self.forward_to_centers(&message);
                        }
                        self.forward_to_scorers(&message);
                    }
                    bus_message::Payload::ProgressReport(pr) => {
                        if let Some(state) = self.edges.get_mut(&peer_id) {
                            state.completed_time_ns = Some(
                                state
                                    .completed_time_ns
                                    .map_or(pr.completed_time_ns, |prev| {
                                        prev.max(pr.completed_time_ns)
                                    }),
                            );
                            if pr.finite_end_reached {
                                state.finite_end_reached = true;
                            }
                        }
                        if self.mode == ReplayMode::Integrated {
                            self.forward_to_centers(&message);
                        }
                        self.forward_to_scorers(&message);
                    }
                    bus_message::Payload::DetectionBatch(_) => {
                        self.forward_to_scorers(&message);
                    }
                    _ => {}
                }
            }
            ComponentRole::Center => match &payload {
                bus_message::Payload::SparseVoxelGrid(grid) => {
                    if self.record_voxels {
                        self.run_writer.write_voxel_grid(grid)?;
                    }
                    self.latest_voxels = Some(grid.clone());
                    self.forward_to_scorers(&message);
                }
                bus_message::Payload::FusionMeasurement(fm) => {
                    self.run_writer.write_center_message(&message)?;
                    self.step_measurements.push(fm.clone());
                    self.forward_to_scorers(&message);
                }
                bus_message::Payload::TrackEvent(tr) => {
                    self.run_writer.write_center_message(&message)?;
                    self.step_tracks.push(tr.clone());
                    self.forward_to_scorers(&message);
                }
                bus_message::Payload::ProgressReport(pr) => {
                    self.run_writer.write_center_message(&message)?;
                    if let Some(state) = self.centers.get_mut(&peer_id) {
                        state.completed_time_ns = Some(
                            state
                                .completed_time_ns
                                .map_or(pr.completed_time_ns, |prev| {
                                    prev.max(pr.completed_time_ns)
                                }),
                        );
                        if pr.finite_end_reached {
                            state.finite_end_reached = true;
                        }
                    }
                    self.forward_to_scorers(&message);
                }
                _ => {
                    self.run_writer.write_center_message(&message)?;
                }
            },
            _ => {}
        }
        Ok(())
    }

    fn forward_to_centers(&self, original: &BusMessage) {
        let Some(payload) = original.payload.clone() else {
            return;
        };
        let forwarded = BusMessage {
            bus_sequence_no: original.bus_sequence_no,
            sent_wall_time_ns: original.sent_wall_time_ns,
            payload: Some(payload),
        };
        for center in self.centers.values() {
            let _ = center.handle.try_send(forwarded.clone());
        }
    }

    fn forward_to_scorers(&self, original: &BusMessage) {
        let Some(payload) = original.payload.clone() else {
            return;
        };
        let forwarded = BusMessage {
            bus_sequence_no: original.bus_sequence_no,
            sent_wall_time_ns: original.sent_wall_time_ns,
            payload: Some(payload),
        };
        if !self.truth_peers.is_empty() {
            let tfm = TestFrameworkMessage {
                sequence_no: self.next_truth_seq(),
                payload: Some(test_framework_message::Payload::BusMessage(
                    forwarded.clone(),
                )),
            };
            for peer in self.truth_peers.values() {
                let _ = peer.try_send(tfm.clone());
            }
        }
        for scorer in self.scorers.values() {
            let _ = scorer.try_send(forwarded.clone());
        }
    }

    fn handle_truth_event(&mut self, ev: TruthEvent) {
        match ev {
            TruthEvent::PeerConnected(handle) => {
                self.truth_peers.insert(handle.peer_id, handle);
            }
            TruthEvent::Message { message, .. } => {
                if let Some(test_framework_message::Payload::ScoreEvent(score)) = message.payload {
                    let t = score.experiment_time_ns;
                    self.latest_scores_by_unit
                        .insert(score.error_unit.clone(), score);
                    self.publish_viewer(t);
                }
            }
            TruthEvent::PeerDisconnected { peer_id } => {
                self.truth_peers.remove(&peer_id);
            }
        }
    }

    fn required_peers_ready(&self, station_ids: &[String], wait_scorer: bool) -> bool {
        if self.mode.needs_edge() {
            if station_ids.is_empty() {
                if self.edges.is_empty() {
                    return false;
                }
            } else if !station_ids
                .iter()
                .all(|sid| self.station_to_edge.contains_key(sid))
            {
                return false;
            }
        }
        if self.mode.needs_center() && self.centers.is_empty() {
            return false;
        }
        if wait_scorer && self.truth_peers.is_empty() && self.scorers.is_empty() {
            return false;
        }
        true
    }

    fn step_completed(&self, group_time_ns: i64) -> bool {
        // Edge reports ProgressReport per exposure group; Center only emits ProgressReport when
        // an observation window seals (on a subsequent watermark >= window.end_ns or stream end),
        // so Center completion is drained via `all_finite_ends_reached()`.
        if self.mode.needs_edge() {
            for edge in self.edges.values() {
                if edge.dispatched_time_ns == Some(group_time_ns) {
                    let ok = edge.finite_end_reached
                        || edge.completed_time_ns.is_some_and(|t| t >= group_time_ns);
                    if !ok {
                        return false;
                    }
                }
            }
        }
        true
    }

    fn all_finite_ends_reached(&self) -> bool {
        if self.mode.needs_edge() && !self.edges.values().all(|e| e.finite_end_reached) {
            return false;
        }
        if self.mode.needs_center() && !self.centers.values().all(|c| c.finite_end_reached) {
            return false;
        }
        true
    }
}

enum ReplayGroup {
    Frames(Vec<FrameIndexEntry>),
    Rays(Vec<RayEvent>),
}

/// Synthesize a baseline `CalibrationStateEvent` for a virtual `RayEvent` in Center-only replay
/// so `Center`'s calibration registry can admit the ray without an Edge producer.
fn build_virtual_calibration_event(
    ray: &RayEvent,
    stations_by_id: &HashMap<String, &StationConfig>,
    sequence_no: u64,
) -> Option<CalibrationStateEvent> {
    let env = ray.envelope.as_ref()?;
    let sol_ref = ray.calibration_solution_ref.as_ref()?;
    let evidence = ray.calibration_evidence.as_ref()?;
    let station = stations_by_id.get(&env.station_id).copied();
    let identity_mat = Mat3 {
        values: vec![1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
    };
    let observed_at = evidence
        .last_trusted_time
        .clone()
        .or_else(|| evidence.solution_accepted_time.clone());
    let usable_interval = evidence
        .usable_interval
        .clone()
        .or_else(|| evidence.solution_valid_interval.clone());
    let main_pos = station
        .and_then(|s| s.main_camera_position_enu_m)
        .or(ray.ray_origin_enu_m);
    let rotation = station
        .and_then(|s| s.baseline_orientation_enu_from_main.clone())
        .unwrap_or(identity_mat);

    Some(CalibrationStateEvent {
        envelope: Some(EventEnvelope {
            schema_version: env.schema_version.clone(),
            event_type: EventType::CalibrationState as i32,
            station_id: env.station_id.clone(),
            station_session_id: env.station_session_id.clone(),
            sequence_no,
            station_config_digest: env.station_config_digest.clone(),
        }),
        state: Some(CalibrationState {
            station_id: env.station_id.clone(),
            station_session_id: env.station_session_id.clone(),
            station_config_digest: env.station_config_digest.clone(),
            state_version: evidence.state_version.max(1),
            health: CalibrationHealth::Valid as i32,
            solution_ref: Some(sol_ref.clone()),
            observed_at,
            last_trusted_time: evidence.last_trusted_time.clone(),
            usable_interval,
            reason_codes: Vec::new(),
        }),
        solution: Some(CalibrationSolution {
            solution_ref: Some(sol_ref.clone()),
            station_config_digest: env.station_config_digest.clone(),
            world_frame_id: ray.world_frame_id.clone(),
            reference_capture_time: evidence.solution_reference_capture_time.clone(),
            accepted_time: evidence.solution_accepted_time.clone(),
            valid_interval: evidence.solution_valid_interval.clone(),
            main_camera_position_enu_m: main_pos,
            rotation_enu_from_main: Some(rotation),
            correction_covariance_enu_rad2: Some(Mat3 {
                values: vec![0.0; 9],
            }),
            quality: Some(CalibrationQuality {
                source: "virtual-scene-baseline".into(),
                matched_reference_count: 0,
                ..Default::default()
            }),
        }),
    })
}

/// Run V-Switch in Replay mode (`edge`, `center`, or `integrated`; `lock-step` or `wall-clock`).
pub async fn run_replay(
    cfg: ReplayConfig,
    bound_tx: Option<oneshot::Sender<BoundAddresses>>,
) -> Result<Value, HubError> {
    let scene = TestSceneReader::open(&cfg.scene_dir, true, cfg.verify_videos)?;
    let stations = scene.station_configs().to_vec();
    let station_ids: Vec<String> = stations.iter().map(|s| s.station_id.clone()).collect();
    let stations_by_id: HashMap<String, &StationConfig> =
        stations.iter().map(|s| (s.station_id.clone(), s)).collect();
    let scene_sha = scene.content_sha256().to_string();

    let mut truth_frames = scene.iter_truth_frames()?.collect_all()?;
    truth_frames.sort_by_key(|tf| tf.experiment_time_ns);

    let mut next_virtual_cal_seq: u64 = 1;
    let (groups, tracks): (
        BTreeMap<i64, ReplayGroup>,
        HashMap<String, Vec<H264AccessUnit>>,
    ) = if cfg.mode.needs_edge() {
        let entries = scene.iter_frame_index()?.collect_all()?;
        let tracks = demux_scene_tracks(&scene, &entries)?;
        let mut grouped: BTreeMap<i64, Vec<FrameIndexEntry>> = BTreeMap::new();
        for entry in entries {
            let t = entry
                .capture_time
                .as_ref()
                .map(|c| c.exposure_start_ns)
                .unwrap_or(0);
            grouped.entry(t).or_default().push(entry);
        }
        (
            grouped
                .into_iter()
                .map(|(k, v)| (k, ReplayGroup::Frames(v)))
                .collect(),
            tracks,
        )
    } else {
        let rays = scene.iter_virtual_rays()?.collect_all()?;
        next_virtual_cal_seq = rays
            .iter()
            .filter_map(|r| r.envelope.as_ref().map(|e| e.sequence_no))
            .max()
            .unwrap_or(0)
            + 1;
        let mut grouped: BTreeMap<i64, Vec<RayEvent>> = BTreeMap::new();
        for ray in rays {
            let t = ray
                .capture_time
                .as_ref()
                .map(|c| c.exposure_start_ns)
                .unwrap_or(0);
            grouped.entry(t).or_default().push(ray);
        }
        (
            grouped
                .into_iter()
                .map(|(k, v)| (k, ReplayGroup::Rays(v)))
                .collect(),
            HashMap::new(),
        )
    };

    let mut provenance = BTreeMap::new();
    provenance.insert("hub".into(), Value::from("vswitch-rust"));
    provenance.insert(
        "stepping".into(),
        Value::from(match cfg.stepping {
            SteppingMode::LockStep => "lock-step",
            SteppingMode::WallClock => "wall-clock",
        }),
    );

    let run_writer = RunOutputWriter::create(
        &cfg.run_output_dir,
        RunOutputConfig {
            run_id: cfg.run_id.clone(),
            mode: cfg.mode.as_str().to_string(),
            test_scene_path: cfg.scene_dir.clone(),
            test_scene_sha256: scene_sha,
            provenance,
            flush_every: cfg.flush_every,
        },
    )?;

    let (bus_tx, mut bus_rx) = mpsc::channel::<BusEvent>(2048);
    let mut allowed_roles = vec![ComponentRole::Scorer];
    if cfg.mode.needs_edge() {
        allowed_roles.push(ComponentRole::Edge);
    }
    if cfg.mode.needs_center() {
        allowed_roles.push(ComponentRole::Center);
    }
    let bus_server =
        BusServer::bind(cfg.bus_bind, cfg.run_id.clone(), allowed_roles, bus_tx).await?;
    let bus_addr = bus_server.local_addr();

    let (truth_tx, mut truth_rx) = mpsc::channel::<TruthEvent>(512);
    let truth_server = match cfg.truth_bind {
        Some(addr) => Some(
            TruthServer::bind_with_handshake(
                addr,
                cfg.run_id.clone(),
                vec![ComponentRole::Scorer],
                false,
                truth_tx,
            )
            .await?,
        ),
        None => None,
    };
    let truth_addr = truth_server.as_ref().map(TruthServer::local_addr);

    let viewer_hub = match cfg.viewer_bind {
        Some(addr) => Some(ViewerHub::bind(addr, DEFAULT_VIEWER_QUEUE_CAPACITY).await?),
        None => None,
    };
    let viewer_addr = viewer_hub.as_ref().map(ViewerHub::local_addr);

    if let Some(tx) = bound_tx {
        let _ = tx.send(BoundAddresses {
            bus_addr,
            truth_addr,
            viewer_addr,
        });
    }

    let mut rt = ReplayRuntime {
        mode: cfg.mode,
        stepping: cfg.stepping,
        end_notice_sent: false,
        record_voxels: cfg.record_voxels,
        run_id: cfg.run_id.clone(),
        run_writer,
        viewer_hub,
        edges: HashMap::new(),
        station_to_edge: HashMap::new(),
        centers: HashMap::new(),
        scorers: HashMap::new(),
        truth_peers: HashMap::new(),
        bus_seq: AtomicU64::new(1),
        truth_seq: AtomicU64::new(1),
        display_update_id: 0,
        stations: stations.clone(),
        latest_calibrations: HashMap::new(),
        latest_truth: None,
        latest_scores_by_unit: BTreeMap::new(),
        latest_voxels: None,
        step_rays: Vec::new(),
        step_calibration_states: Vec::new(),
        step_diagnostics: Vec::new(),
        step_measurements: Vec::new(),
        step_tracks: Vec::new(),
    };

    // Wait for required peers before dispatching StationConfigs.
    let wait_deadline = Instant::now() + cfg.handshake_timeout;
    while !rt.required_peers_ready(&station_ids, cfg.wait_scorer) {
        let now = Instant::now();
        if now >= wait_deadline {
            return Err(HubError::HandshakeTimeout(format!(
                "missing required peers for {:?} mode (stations={:?})",
                cfg.mode, station_ids
            )));
        }
        tokio::select! {
            _ = tokio::time::sleep_until(wait_deadline) => {
                return Err(HubError::HandshakeTimeout(format!(
                    "missing required peers for {:?} mode",
                    cfg.mode
                )));
            }
            ev = bus_rx.recv() => {
                if let Some(ev) = ev {
                    rt.handle_bus_event(ev)?;
                }
            }
            tev = truth_rx.recv() => {
                if let Some(tev) = tev {
                    rt.handle_truth_event(tev);
                }
            }
        }
    }

    // Send StationConfigs to all connected Edge, Center, and Scorer peers.
    for st in &stations {
        let msg = rt.wrap_bus(bus_message::Payload::StationConfig(st.clone()));
        for edge in rt.edges.values() {
            if edge.handle.info.station_ids.contains(&st.station_id) {
                let _ = edge.handle.send(msg.clone()).await;
            }
        }
        for center in rt.centers.values() {
            let _ = center.handle.send(msg.clone()).await;
        }
        rt.forward_to_scorers(&msg);
    }

    // Group dispatch loop.
    let mut truth_cursor = 0usize;
    let first_time_ns = groups.keys().next().copied().unwrap_or(0);
    let wall_start = Instant::now();
    let speed = cfg.wall_clock_speed.max(0.01);
    let mut last_group_time_ns = first_time_ns;
    let mut sent_virtual_cal_keys: HashSet<(String, String, String)> = HashSet::new();

    for (group_time_ns, group) in groups.into_iter() {
        last_group_time_ns = group_time_ns;
        if cfg.stepping == SteppingMode::WallClock && group_time_ns > first_time_ns {
            let delta_ns = (group_time_ns - first_time_ns) as u64;
            let scaled_ns = ((delta_ns as f64) / speed) as u64;
            let target_wall = wall_start + Duration::from_nanos(scaled_ns);
            while Instant::now() < target_wall {
                tokio::select! {
                    _ = sleep_until(target_wall) => break,
                    ev = bus_rx.recv() => {
                        if let Some(ev) = ev {
                            rt.handle_bus_event(ev)?;
                        }
                    }
                    tev = truth_rx.recv() => {
                        if let Some(tev) = tev {
                            rt.handle_truth_event(tev);
                        }
                    }
                }
            }
        }

        rt.clear_step_overlays();

        while truth_cursor < truth_frames.len()
            && truth_frames[truth_cursor].experiment_time_ns <= group_time_ns
        {
            let tf = truth_frames[truth_cursor].clone();
            rt.send_truth_frame(&tf).await;
            truth_cursor += 1;
        }

        match group {
            ReplayGroup::Frames(entries) => {
                for entry in entries {
                    let track = tracks.get(&entry.video_relpath).ok_or_else(|| {
                        HubError::InvalidState(format!(
                            "missing demuxed track {}",
                            entry.video_relpath
                        ))
                    })?;
                    let idx = usize::try_from(entry.frame_index)
                        .map_err(|_| HubError::InvalidState("frame_index overflow".into()))?;
                    let au = track.get(idx).ok_or_else(|| {
                        HubError::InvalidState(format!(
                            "frame_index {} out of bounds for {} (len {})",
                            idx,
                            entry.video_relpath,
                            track.len()
                        ))
                    })?;
                    let pkt = FramedVideoPacket {
                        station_id: entry.station_id.clone(),
                        station_session_id: entry.station_session_id.clone(),
                        camera_role: entry.camera_role,
                        frame_id: entry.frame_id.clone(),
                        frame_index: entry.frame_index,
                        capture_time: entry.capture_time.clone(),
                        station_config_digest: entry.station_config_digest.clone(),
                        width_px: entry.width_px,
                        height_px: entry.height_px,
                        codec: VideoCodec::H264 as i32,
                        pixel_format: crate::proto::PixelFormat::Yuv420p as i32,
                        row_stride_bytes: 0,
                        is_keyframe: au.is_keyframe,
                        payload: au.annex_b.clone(),
                    };
                    let edge_pid = *rt.station_to_edge.get(&entry.station_id).ok_or_else(|| {
                        HubError::PeerDisconnected(format!("EDGE for {}", entry.station_id))
                    })?;
                    let msg = rt.wrap_bus(bus_message::Payload::VideoPacket(pkt));
                    if let Some(edge_state) = rt.edges.get_mut(&edge_pid) {
                        edge_state.dispatched_time_ns = Some(group_time_ns);
                        edge_state
                            .handle
                            .send(msg)
                            .await
                            .map_err(|_| HubError::PeerDisconnected(entry.station_id.clone()))?;
                    }
                }
            }
            ReplayGroup::Rays(rays) => {
                let mut seen_stations: BTreeMap<String, String> = BTreeMap::new();
                for ray in rays {
                    if let Some(env) = &ray.envelope {
                        seen_stations
                            .entry(env.station_id.clone())
                            .or_insert_with(|| env.station_session_id.clone());
                    }
                    if let Some(sol_ref) = &ray.calibration_solution_ref {
                        let key = (
                            sol_ref.station_id.clone(),
                            sol_ref.station_session_id.clone(),
                            sol_ref.solution_id.clone(),
                        );
                        if !sent_virtual_cal_keys.contains(&key) {
                            if let Some(cal_ev) = build_virtual_calibration_event(
                                &ray,
                                &stations_by_id,
                                next_virtual_cal_seq,
                            ) {
                                next_virtual_cal_seq += 1;
                                sent_virtual_cal_keys.insert(key);
                                rt.latest_calibrations
                                    .insert(sol_ref.station_id.clone(), cal_ev.clone());
                                rt.step_calibration_states.push(cal_ev.clone());
                                let cal_msg =
                                    rt.wrap_bus(bus_message::Payload::CalibrationState(cal_ev));
                                for center in rt.centers.values() {
                                    let _ = center.handle.send(cal_msg.clone()).await;
                                }
                                rt.forward_to_scorers(&cal_msg);
                            }
                        }
                    }
                    rt.step_rays.push(ray.clone());
                    let msg = rt.wrap_bus(bus_message::Payload::RayEvent(ray));
                    for center in rt.centers.values() {
                        let _ = center.handle.send(msg.clone()).await;
                    }
                    rt.forward_to_scorers(&msg);
                }
                for st in &stations {
                    let session_id = seen_stations
                        .get(&st.station_id)
                        .cloned()
                        .unwrap_or_else(|| format!("virtual-{}", st.station_id));
                    let pr_msg =
                        rt.wrap_bus(bus_message::Payload::ProgressReport(ProgressReport {
                            source_role: ComponentRole::Edge as i32,
                            source_id: st.station_id.clone(),
                            session_id,
                            completed_time_ns: group_time_ns,
                            completed_sequence_no: 0,
                            finite_end_reached: false,
                        }));
                    for center in rt.centers.values() {
                        let _ = center.handle.send(pr_msg.clone()).await;
                    }
                    rt.forward_to_scorers(&pr_msg);
                }
            }
        }

        match cfg.stepping {
            SteppingMode::LockStep => {
                if cfg.mode.needs_edge() {
                    let deadline = Instant::now() + cfg.step_timeout;
                    while !rt.step_completed(group_time_ns) {
                        if Instant::now() >= deadline {
                            return Err(HubError::StepTimeout(group_time_ns));
                        }
                        tokio::select! {
                            _ = sleep_until(deadline) => {
                                return Err(HubError::StepTimeout(group_time_ns));
                            }
                            ev = bus_rx.recv() => {
                                if let Some(ev) = ev {
                                    rt.handle_bus_event(ev)?;
                                }
                            }
                            tev = truth_rx.recv() => {
                                if let Some(tev) = tev {
                                    rt.handle_truth_event(tev);
                                }
                            }
                        }
                    }
                } else {
                    tokio::task::yield_now().await;
                    while let Ok(ev) = bus_rx.try_recv() {
                        rt.handle_bus_event(ev)?;
                    }
                    while let Ok(tev) = truth_rx.try_recv() {
                        rt.handle_truth_event(tev);
                    }
                }
            }
            SteppingMode::WallClock => {
                // Drain currently queued events non-blockingly without waiting for slow peers.
                while let Ok(ev) = bus_rx.try_recv() {
                    rt.handle_bus_event(ev)?;
                }
                while let Ok(tev) = truth_rx.try_recv() {
                    rt.handle_truth_event(tev);
                }
            }
        }

        rt.publish_viewer(group_time_ns);
    }

    // Flush any remaining truth frames.
    while truth_cursor < truth_frames.len() {
        let tf = truth_frames[truth_cursor].clone();
        rt.send_truth_frame(&tf).await;
        truth_cursor += 1;
    }

    // Signal finite end to downstream peers.
    rt.end_notice_sent = true;
    let end_notice = rt.wrap_bus(bus_message::Payload::ProgressReport(ProgressReport {
        source_role: ComponentRole::Unspecified as i32,
        source_id: "vswitch".to_string(),
        session_id: cfg.run_id.clone(),
        completed_time_ns: last_group_time_ns,
        completed_sequence_no: 0,
        finite_end_reached: true,
    }));
    if cfg.mode.needs_edge() {
        for edge in rt.edges.values() {
            let _ = edge.handle.send(end_notice.clone()).await;
        }
    } else if cfg.mode.needs_center() {
        if stations.is_empty() {
            for center in rt.centers.values() {
                let _ = center.handle.send(end_notice.clone()).await;
            }
        } else {
            for st in &stations {
                let st_end = rt.wrap_bus(bus_message::Payload::ProgressReport(ProgressReport {
                    source_role: ComponentRole::Edge as i32,
                    source_id: st.station_id.clone(),
                    session_id: format!("virtual-{}", st.station_id),
                    completed_time_ns: last_group_time_ns,
                    completed_sequence_no: 0,
                    finite_end_reached: true,
                }));
                for center in rt.centers.values() {
                    let _ = center.handle.send(st_end.clone()).await;
                }
                rt.forward_to_scorers(&st_end);
            }
        }
    }

    // Wait for finite_end_reached / trailing outputs before finalizing.
    let drain_limit = match cfg.stepping {
        SteppingMode::LockStep => cfg.step_timeout,
        SteppingMode::WallClock => cfg.drain_timeout,
    };
    let drain_deadline = Instant::now() + drain_limit;
    while !rt.all_finite_ends_reached() && Instant::now() < drain_deadline {
        tokio::select! {
            _ = sleep_until(drain_deadline) => break,
            ev = bus_rx.recv() => {
                if let Some(ev) = ev {
                    rt.handle_bus_event(ev)?;
                }
            }
            tev = truth_rx.recv() => {
                if let Some(tev) = tev {
                    rt.handle_truth_event(tev);
                }
            }
        }
    }

    // Close outbound Scorer streams cleanly so online Scorer flushes final ScoreEvents before EOF.
    rt.truth_peers.clear();
    rt.scorers.clear();

    // Brief grace drain for final ScoreEvents if any.
    let score_grace = Instant::now() + Duration::from_millis(50);
    while Instant::now() < score_grace {
        tokio::select! {
            _ = sleep_until(score_grace) => break,
            tev = truth_rx.recv() => {
                if let Some(tev) = tev {
                    rt.handle_truth_event(tev);
                }
            }
            ev = bus_rx.recv() => {
                if let Some(ev) = ev {
                    let _ = rt.handle_bus_event(ev);
                }
            }
        }
    }

    let manifest = rt.run_writer.finalize()?;
    if let Some(viewer) = rt.viewer_hub {
        viewer.shutdown();
    }
    if let Some(ts) = truth_server {
        ts.shutdown();
    }
    bus_server.shutdown();
    Ok(manifest)
}

// --- Live Relay Configuration & Runner ---

#[derive(Debug, Clone)]
pub struct LiveRelayConfig {
    pub run_id: String,
    pub bus_bind: SocketAddr,
    pub viewer_bind: Option<SocketAddr>,
}

/// Run V-Switch in Live Relay mode: unidirectional `Edge -> V-Switch -> Center` forwarding
/// without Test Scene or Truth, plus optional Viewer push without Truth/Score.
pub async fn run_live_relay(
    cfg: LiveRelayConfig,
    bound_tx: Option<oneshot::Sender<BoundAddresses>>,
    mut shutdown_rx: oneshot::Receiver<()>,
) -> Result<(), HubError> {
    let (bus_tx, mut bus_rx) = mpsc::channel::<BusEvent>(2048);
    let bus_server = BusServer::bind(
        cfg.bus_bind,
        cfg.run_id.clone(),
        vec![ComponentRole::Edge, ComponentRole::Center],
        bus_tx,
    )
    .await?;
    let bus_addr = bus_server.local_addr();

    let viewer_hub = match cfg.viewer_bind {
        Some(addr) => Some(ViewerHub::bind(addr, DEFAULT_VIEWER_QUEUE_CAPACITY).await?),
        None => None,
    };
    let viewer_addr = viewer_hub.as_ref().map(ViewerHub::local_addr);

    if let Some(tx) = bound_tx {
        let _ = tx.send(BoundAddresses {
            bus_addr,
            truth_addr: None,
            viewer_addr,
        });
    }

    let mut centers: HashMap<u64, PeerHandle> = HashMap::new();
    let mut cached_configs: BTreeMap<String, StationConfig> = BTreeMap::new();
    let mut latest_calibrations: HashMap<String, CalibrationStateEvent> = HashMap::new();
    let mut update_id = 0u64;
    let bus_seq = AtomicU64::new(1);

    loop {
        tokio::select! {
            _ = &mut shutdown_rx => break,
            ev = bus_rx.recv() => {
                let Some(ev) = ev else { break; };
                match ev {
                    BusEvent::PeerConnected(handle) => {
                        if handle.info.role == ComponentRole::Center {
                            for st in cached_configs.values() {
                                let msg = BusMessage {
                                    bus_sequence_no: bus_seq.fetch_add(1, Ordering::Relaxed),
                                    sent_wall_time_ns: now_wall_time_ns(),
                                    payload: Some(bus_message::Payload::StationConfig(st.clone())),
                                };
                                let _ = handle.send(msg).await;
                            }
                            centers.insert(handle.info.peer_id, handle);
                        }
                    }
                    BusEvent::PeerDisconnected { peer_id, role, .. } => {
                        if role == ComponentRole::Center {
                            centers.remove(&peer_id);
                        }
                    }
                    BusEvent::Message { role, message, .. } => {
                        let Some(payload) = message.payload.clone() else {
                            continue;
                        };
                        match role {
                            ComponentRole::Edge => match &payload {
                                bus_message::Payload::StationConfig(st) => {
                                    cached_configs.insert(st.station_id.clone(), st.clone());
                                    for center in centers.values() {
                                        let _ = center.try_send((*message).clone());
                                    }
                                }
                                bus_message::Payload::RayEvent(ray) => {
                                    for center in centers.values() {
                                        let _ = center.try_send((*message).clone());
                                    }
                                    if let Some(viewer) = &viewer_hub {
                                        let t = ray
                                            .capture_time
                                            .as_ref()
                                            .map(|c| c.exposure_start_ns)
                                            .unwrap_or(0);
                                        update_id += 1;
                                        let stations: Vec<StationConfig> =
                                            cached_configs.values().cloned().collect();
                                        let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                                            run_id: &cfg.run_id,
                                            mode: "live",
                                            update_id,
                                            experiment_time_ns: t,
                                            has_truth: false,
                                            stations: &stations,
                                            latest_calibrations: &latest_calibrations,
                                            truth: None,
                                            rays: std::slice::from_ref(ray),
                                            calibration_states: &[],
                                            diagnostics: &[],
                                            measurements: &[],
                                            tracks: &[],
                                            scores: &[],
                                            sparse_voxel_grid: None,
                                        });
                                        let _ = viewer.publish(&mut frame);
                                    }
                                }
                                bus_message::Payload::CalibrationState(cal) => {
                                    let sid = cal
                                        .state
                                        .as_ref()
                                        .map(|s| s.station_id.clone())
                                        .or_else(|| cal.envelope.as_ref().map(|e| e.station_id.clone()))
                                        .unwrap_or_default();
                                    if !sid.is_empty() {
                                        latest_calibrations.insert(sid, cal.clone());
                                    }
                                    for center in centers.values() {
                                        let _ = center.try_send((*message).clone());
                                    }
                                    if let Some(viewer) = &viewer_hub {
                                        let t = cal
                                            .state
                                            .as_ref()
                                            .and_then(|s| s.observed_at.as_ref())
                                            .map_or(0, |o| o.time_ns);
                                        update_id += 1;
                                        let stations: Vec<StationConfig> =
                                            cached_configs.values().cloned().collect();
                                        let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                                            run_id: &cfg.run_id,
                                            mode: "live",
                                            update_id,
                                            experiment_time_ns: t,
                                            has_truth: false,
                                            stations: &stations,
                                            latest_calibrations: &latest_calibrations,
                                            truth: None,
                                            rays: &[],
                                            calibration_states: std::slice::from_ref(cal),
                                            diagnostics: &[],
                                            measurements: &[],
                                            tracks: &[],
                                            scores: &[],
                                            sparse_voxel_grid: None,
                                        });
                                        let _ = viewer.publish(&mut frame);
                                    }
                                }
                                bus_message::Payload::DiagnosticEvent(diag) => {
                                    for center in centers.values() {
                                        let _ = center.try_send((*message).clone());
                                    }
                                    if let Some(viewer) = &viewer_hub {
                                        let t = diag
                                            .capture_time
                                            .as_ref()
                                            .map_or(0, |c| c.exposure_start_ns);
                                        update_id += 1;
                                        let stations: Vec<StationConfig> =
                                            cached_configs.values().cloned().collect();
                                        let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                                            run_id: &cfg.run_id,
                                            mode: "live",
                                            update_id,
                                            experiment_time_ns: t,
                                            has_truth: false,
                                            stations: &stations,
                                            latest_calibrations: &latest_calibrations,
                                            truth: None,
                                            rays: &[],
                                            calibration_states: &[],
                                            diagnostics: std::slice::from_ref(diag),
                                            measurements: &[],
                                            tracks: &[],
                                            scores: &[],
                                            sparse_voxel_grid: None,
                                        });
                                        let _ = viewer.publish(&mut frame);
                                    }
                                }
                                bus_message::Payload::ProgressReport(_) => {
                                    for center in centers.values() {
                                        let _ = center.try_send((*message).clone());
                                    }
                                }
                                _ => {}
                            },
                            ComponentRole::Center => {
                                if let Some(viewer) = &viewer_hub {
                                    let stations: Vec<StationConfig> =
                                        cached_configs.values().cloned().collect();
                                    match &payload {
                                        bus_message::Payload::FusionMeasurement(fm) => {
                                            update_id += 1;
                                            let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                                                run_id: &cfg.run_id,
                                                mode: "live",
                                                update_id,
                                                experiment_time_ns: fm.reference_time_ns,
                                                has_truth: false,
                                                stations: &stations,
                                                latest_calibrations: &latest_calibrations,
                                                truth: None,
                                                rays: &[],
                                                calibration_states: &[],
                                                diagnostics: &[],
                                                measurements: std::slice::from_ref(fm),
                                                tracks: &[],
                                                scores: &[],
                                                sparse_voxel_grid: None,
                                            });
                                            let _ = viewer.publish(&mut frame);
                                        }
                                        bus_message::Payload::TrackEvent(tr) => {
                                            update_id += 1;
                                            let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                                                run_id: &cfg.run_id,
                                                mode: "live",
                                                update_id,
                                                experiment_time_ns: tr.event_time_ns,
                                                has_truth: false,
                                                stations: &stations,
                                                latest_calibrations: &latest_calibrations,
                                                truth: None,
                                                rays: &[],
                                                calibration_states: &[],
                                                diagnostics: &[],
                                                measurements: &[],
                                                tracks: std::slice::from_ref(tr),
                                                scores: &[],
                                                sparse_voxel_grid: None,
                                            });
                                            let _ = viewer.publish(&mut frame);
                                        }
                                        bus_message::Payload::SparseVoxelGrid(grid) => {
                                            update_id += 1;
                                            let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                                                run_id: &cfg.run_id,
                                                mode: "live",
                                                update_id,
                                                experiment_time_ns: grid.window_end_ns,
                                                has_truth: false,
                                                stations: &stations,
                                                latest_calibrations: &latest_calibrations,
                                                truth: None,
                                                rays: &[],
                                                calibration_states: &[],
                                                diagnostics: &[],
                                                measurements: &[],
                                                tracks: &[],
                                                scores: &[],
                                                sparse_voxel_grid: Some(grid),
                                            });
                                            let _ = viewer.publish(&mut frame);
                                        }
                                        _ => {}
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                }
            }
        }
    }

    if let Some(viewer) = viewer_hub {
        viewer.shutdown();
    }
    bus_server.shutdown();
    Ok(())
}

// --- Record Mode Configuration & Runner ---

#[derive(Debug, Clone)]
pub struct RecordConfig {
    pub output_scene_dir: PathBuf,
    pub scenario_id: String,
    pub world_frame_id: String,
    pub clock_domain_id: String,
    pub occlusion_mode: String,
    pub random_seed: Option<i64>,
    pub bus_bind: SocketAddr,
    pub truth_bind: SocketAddr,
    pub viewer_bind: Option<SocketAddr>,
    pub encode_preset: H264EncodePreset,
    pub flush_every: usize,
    pub handshake_timeout: Duration,
}

impl Default for RecordConfig {
    fn default() -> Self {
        Self {
            output_scene_dir: PathBuf::new(),
            scenario_id: "recorded".to_string(),
            world_frame_id: "enu".to_string(),
            clock_domain_id: "sim".to_string(),
            occlusion_mode: "known".to_string(),
            random_seed: None,
            bus_bind: "127.0.0.1:0".parse().expect("valid loopback addr"),
            truth_bind: "127.0.0.1:0".parse().expect("valid loopback addr"),
            viewer_bind: None,
            encode_preset: H264EncodePreset::default(),
            flush_every: 256,
            handshake_timeout: Duration::from_secs(30),
        }
    }
}

struct TrackRecorder {
    relpath: String,
    encoder: VideoTrackEncoder,
    next_frame_index: u64,
}

fn track_relpath(station_id: &str, camera_role: i32) -> Result<String, HubError> {
    let role_str = match CameraRole::try_from(camera_role).unwrap_or(CameraRole::Unspecified) {
        CameraRole::Main => "main",
        CameraRole::Auxiliary => "aux",
        CameraRole::Unspecified => "cam",
    };
    let relpath = format!("video/{station_id}-{role_str}.mp4");
    validate_relpath(&relpath)?;
    Ok(relpath)
}

/// Run V-Switch in Record mode (`Godot Producer -> V-Switch -> immutable Test Scene`).
pub async fn run_record(
    cfg: RecordConfig,
    bound_tx: Option<oneshot::Sender<BoundAddresses>>,
    mut shutdown_rx: oneshot::Receiver<()>,
) -> Result<Value, HubError> {
    let mut scene_writer = TestSceneWriter::create(
        &cfg.output_scene_dir,
        TestSceneConfig {
            scenario_id: cfg.scenario_id.clone(),
            world_frame_id: cfg.world_frame_id.clone(),
            clock_domain_id: cfg.clock_domain_id.clone(),
            start_time_ns: 0,
            end_time_ns: 0,
            stations: Vec::new(),
            occlusion_mode: cfg.occlusion_mode.clone(),
            random_seed: cfg.random_seed,
            targets_meta: BTreeMap::new(),
            encoding_preset: Some(cfg.encode_preset.spec.clone()),
            flush_every: cfg.flush_every,
        },
    )?;

    let (bus_tx, mut bus_rx) = mpsc::channel::<BusEvent>(2048);
    let bus_server = BusServer::bind(
        cfg.bus_bind,
        cfg.scenario_id.clone(),
        vec![ComponentRole::Producer],
        bus_tx,
    )
    .await?;
    let bus_addr = bus_server.local_addr();

    let (truth_tx, mut truth_rx) = mpsc::channel::<TruthEvent>(512);
    let truth_server = TruthServer::bind(cfg.truth_bind, truth_tx).await?;
    let truth_addr = truth_server.local_addr();

    let viewer_hub = match cfg.viewer_bind {
        Some(addr) => Some(ViewerHub::bind(addr, DEFAULT_VIEWER_QUEUE_CAPACITY).await?),
        None => None,
    };
    let viewer_addr = viewer_hub.as_ref().map(ViewerHub::local_addr);

    if let Some(tx) = bound_tx {
        let _ = tx.send(BoundAddresses {
            bus_addr,
            truth_addr: Some(truth_addr),
            viewer_addr,
        });
    }

    let mut active_producers: HashSet<u64> = HashSet::new();
    let mut active_truth_peers: HashSet<u64> = HashSet::new();
    let mut seen_any_producer = false;
    let mut tracks: HashMap<(String, i32), TrackRecorder> = HashMap::new();
    let mut min_time_ns: Option<i64> = None;
    let mut max_time_ns: Option<i64> = None;
    let mut finished_signal = false;
    let mut recorded_stations: BTreeMap<String, StationConfig> = BTreeMap::new();
    let empty_calibrations: HashMap<String, CalibrationStateEvent> = HashMap::new();
    let mut record_update_id = 0u64;

    let update_bounds = |t: i64, min_t: &mut Option<i64>, max_t: &mut Option<i64>| {
        *min_t = Some(min_t.map_or(t, |m| m.min(t)));
        *max_t = Some(max_t.map_or(t, |m| m.max(t)));
    };

    // Wait for Producer to connect within handshake_timeout, then record until finish/disconnect/shutdown.
    let first_connect = timeout(cfg.handshake_timeout, async {
        while !seen_any_producer {
            tokio::select! {
                ev = bus_rx.recv() => {
                    if let Some(BusEvent::PeerConnected(handle)) = ev {
                        active_producers.insert(handle.info.peer_id);
                        seen_any_producer = true;
                    }
                }
                tev = truth_rx.recv() => {
                    if let Some(TruthEvent::PeerConnected(handle)) = tev {
                        active_truth_peers.insert(handle.peer_id);
                    }
                }
            }
        }
    })
    .await;
    if first_connect.is_err() {
        return Err(HubError::HandshakeTimeout(
            "no PRODUCER connected in Record mode".into(),
        ));
    }

    loop {
        if finished_signal && active_producers.is_empty() && active_truth_peers.is_empty() {
            break;
        }
        tokio::select! {
            _ = &mut shutdown_rx => break,
            ev = bus_rx.recv() => {
                let Some(ev) = ev else { break; };
                match ev {
                    BusEvent::PeerConnected(handle) => {
                        active_producers.insert(handle.info.peer_id);
                    }
                    BusEvent::PeerDisconnected { peer_id, .. } => {
                        active_producers.remove(&peer_id);
                        if active_producers.is_empty() && active_truth_peers.is_empty() {
                            break;
                        }
                    }
                    BusEvent::Message { message, .. } => {
                        let Some(payload) = message.payload else { continue; };
                        match payload {
                            bus_message::Payload::StationConfig(st) => {
                                recorded_stations.insert(st.station_id.clone(), st.clone());
                                scene_writer.add_station(st);
                            }
                            bus_message::Payload::VideoPacket(pkt) => {
                                let key = (pkt.station_id.clone(), pkt.camera_role);
                                if !tracks.contains_key(&key) {
                                    let relpath = track_relpath(&pkt.station_id, pkt.camera_role)?;
                                    let dest_path = scene_writer.root().join(&relpath);
                                    let encoder = VideoTrackEncoder::start(
                                        &dest_path,
                                        &pkt,
                                        cfg.encode_preset.clone(),
                                    )?;
                                    tracks.insert(
                                        key.clone(),
                                        TrackRecorder {
                                            relpath,
                                            encoder,
                                            next_frame_index: 0,
                                        },
                                    );
                                }
                                let rec = tracks.get_mut(&key).expect("track inserted");
                                rec.encoder.push_packet(&pkt)?;
                                let assigned_idx = rec.next_frame_index;
                                rec.next_frame_index += 1;

                                if let Some(ct) = &pkt.capture_time {
                                    update_bounds(ct.exposure_start_ns, &mut min_time_ns, &mut max_time_ns);
                                    update_bounds(ct.exposure_end_ns, &mut min_time_ns, &mut max_time_ns);
                                }
                                let entry = FrameIndexEntry {
                                    station_id: pkt.station_id,
                                    station_session_id: pkt.station_session_id,
                                    camera_role: pkt.camera_role,
                                    frame_index: assigned_idx,
                                    frame_id: pkt.frame_id,
                                    capture_time: pkt.capture_time,
                                    station_config_digest: pkt.station_config_digest,
                                    video_relpath: rec.relpath.clone(),
                                    width_px: pkt.width_px,
                                    height_px: pkt.height_px,
                                };
                                scene_writer.add_frame_index(&entry)?;
                            }
                            bus_message::Payload::RayEvent(ray) => {
                                if let Some(ct) = &ray.capture_time {
                                    update_bounds(ct.exposure_start_ns, &mut min_time_ns, &mut max_time_ns);
                                }
                                scene_writer.add_virtual_ray(&ray)?;
                            }
                            bus_message::Payload::ProgressReport(pr) if pr.finite_end_reached => {
                                finished_signal = true;
                            }
                            _ => {}
                        }
                    }
                }
            }
            tev = truth_rx.recv() => {
                let Some(tev) = tev else { continue; };
                match tev {
                    TruthEvent::PeerConnected(handle) => {
                        active_truth_peers.insert(handle.peer_id);
                    }
                    TruthEvent::PeerDisconnected { peer_id } => {
                        active_truth_peers.remove(&peer_id);
                        if finished_signal && active_producers.is_empty() && active_truth_peers.is_empty() {
                            break;
                        }
                    }
                    TruthEvent::Message { message, .. } => {
                        if let Some(test_framework_message::Payload::TruthFrame(tf)) =
                            message.payload
                        {
                            update_bounds(tf.experiment_time_ns, &mut min_time_ns, &mut max_time_ns);
                            scene_writer.add_truth_frame(&tf)?;
                            if let Some(viewer) = &viewer_hub {
                                record_update_id += 1;
                                let stations_vec: Vec<StationConfig> =
                                    recorded_stations.values().cloned().collect();
                                let mut frame = ViewerPushFrame::from_proto(ViewerSnapshotInput {
                                    run_id: &cfg.scenario_id,
                                    mode: "integrated",
                                    update_id: record_update_id,
                                    experiment_time_ns: tf.experiment_time_ns,
                                    has_truth: true,
                                    stations: &stations_vec,
                                    latest_calibrations: &empty_calibrations,
                                    truth: Some(&tf),
                                    rays: &[],
                                    calibration_states: &[],
                                    diagnostics: &[],
                                    measurements: &[],
                                    tracks: &[],
                                    scores: &[],
                                    sparse_voxel_grid: None,
                                });
                                let _ = viewer.publish(&mut frame);
                            }
                        }
                    }
                }
            }
        }
        if finished_signal && active_producers.is_empty() && active_truth_peers.is_empty() {
            break;
        }
    }

    // Drain any remaining buffered truth frames for a brief window before closing encoders.
    let grace = Instant::now() + Duration::from_millis(50);
    while Instant::now() < grace {
        tokio::select! {
            _ = sleep_until(grace) => break,
            tev = truth_rx.recv() => {
                if let Some(TruthEvent::Message { message, .. }) = tev {
                    if let Some(test_framework_message::Payload::TruthFrame(tf)) = message.payload {
                        update_bounds(tf.experiment_time_ns, &mut min_time_ns, &mut max_time_ns);
                        scene_writer.add_truth_frame(&tf)?;
                    }
                }
            }
        }
    }

    for (_, rec) in tracks {
        let meta = rec.encoder.finish()?;
        let meta_val = serde_json::to_value(meta).map_err(StorageError::Json)?;
        scene_writer.register_existing_video_file(&rec.relpath, meta_val)?;
    }

    if let (Some(start_ns), Some(end_ns)) = (min_time_ns, max_time_ns) {
        scene_writer.set_time_bounds(start_ns, end_ns);
    }

    let manifest = scene_writer.finalize()?;
    if let Some(viewer) = viewer_hub {
        viewer.shutdown();
    }
    truth_server.shutdown();
    bus_server.shutdown();
    Ok(manifest)
}
