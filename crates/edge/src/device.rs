//! Per-station device assembly and `EdgeProcess` TCP runner.

use std::collections::{BTreeMap, HashMap};
use std::time::Instant;

use thiserror::Error;

use crate::bus::BusClientError;
use crate::calibration::{AuxiliaryCalibrationPipeline, CalibrationUpdate};
use crate::detection::{capture_midpoint_ns, frame_ref, MainDetectionModule};
use crate::frame_source::{BusWriter, CameraFrame, DecodedFrame, FrameSource, FrameSourceError};
use crate::policy::DevicePolicy;
use crate::proto::{
    bus_message, CameraRole, CaptureTime, ComponentRole, DetectionBatch, DiagnosticEvent,
    EventEnvelope, EventType, ProcessingStatus, ProcessingTime, ProgressReport, StationConfig,
};
use crate::rays::RayGenerationModule;

pub const WARMUP_HOLD_NS: i64 = 10_000_000_000;
pub const WARMUP_OBSERVATION_LIMIT: usize = 10;

#[derive(Debug, Error)]
pub enum EdgeError {
    #[error("bus error: {0}")]
    Bus(#[from] BusClientError),
    #[error("frame source error: {0}")]
    FrameSource(#[from] FrameSourceError),
    #[error("device configuration or stream error: {0}")]
    InvalidState(String),
}

pub struct EventSequencer {
    station_id: String,
    station_session_id: String,
    schema_version: String,
    next_seq: u64,
}

impl EventSequencer {
    pub fn new(station_id: String, station_session_id: String) -> Self {
        Self {
            station_id,
            station_session_id,
            schema_version: "event-v1".to_string(),
            next_seq: 1,
        }
    }

    pub fn next_envelope(&mut self, event_type: EventType, config_digest: &str) -> EventEnvelope {
        let seq = self.next_seq;
        self.next_seq += 1;
        EventEnvelope {
            schema_version: self.schema_version.clone(),
            event_type: event_type as i32,
            station_id: self.station_id.clone(),
            station_session_id: self.station_session_id.clone(),
            sequence_no: seq,
            station_config_digest: config_digest.to_string(),
        }
    }
}

pub struct StationDevice {
    pub config: StationConfig,
    pub station_session_id: String,
    sequencer: EventSequencer,
    detector: MainDetectionModule,
    calibration: AuxiliaryCalibrationPipeline,
    rays: RayGenerationModule,
    clock: Option<String>,
    last_processing_ns: Option<i64>,
    last_exposure_ns: HashMap<i32, i64>,
    first_main_capture_ns: Option<i64>,
    warmup_hold_ns: i64,
    warmup_observation_limit: usize,
}

impl StationDevice {
    pub fn new(
        config: StationConfig,
        policy: DevicePolicy,
        station_session_id: String,
        warmup_hold_ns: i64,
    ) -> Result<Self, EdgeError> {
        let sid = config.station_id.clone();
        let digest = config.config_digest.clone();
        let detector = MainDetectionModule::new(
            sid.clone(),
            station_session_id.clone(),
            policy.detection,
            policy.tracking,
        )
        .map_err(|e| EdgeError::InvalidState(e.to_string()))?;
        let calibration = AuxiliaryCalibrationPipeline::new(
            sid.clone(),
            station_session_id.clone(),
            digest,
            policy.calibration,
        );
        let rays = RayGenerationModule::new(policy.rays);
        Ok(Self {
            config,
            station_session_id: station_session_id.clone(),
            sequencer: EventSequencer::new(sid, station_session_id),
            detector,
            calibration,
            rays,
            clock: None,
            last_processing_ns: None,
            last_exposure_ns: HashMap::new(),
            first_main_capture_ns: None,
            warmup_hold_ns,
            warmup_observation_limit: WARMUP_OBSERVATION_LIMIT,
        })
    }

    fn check_now(&self, now: &ProcessingTime) -> Result<(), EdgeError> {
        if now.clock_domain_id.trim().is_empty() {
            return Err(EdgeError::InvalidState(
                "invalid_processing_time".to_string(),
            ));
        }
        if let Some(c) = &self.clock {
            if c != &now.clock_domain_id {
                return Err(EdgeError::InvalidState(
                    "processing_clock_mismatch".to_string(),
                ));
            }
        }
        if let Some(prev) = self.last_processing_ns {
            if now.time_ns < prev {
                return Err(EdgeError::InvalidState(
                    "processing_time_regressed".to_string(),
                ));
            }
        }
        Ok(())
    }

    fn accept_frame(
        &mut self,
        frame: &CameraFrame,
        expected_role: CameraRole,
        now: &ProcessingTime,
    ) -> Result<(), EdgeError> {
        if frame.station_id != self.config.station_id {
            return Err(EdgeError::InvalidState("station_mismatch".to_string()));
        }
        if frame.station_session_id != self.station_session_id {
            return Err(EdgeError::InvalidState(
                "station_session_mismatch".to_string(),
            ));
        }
        if frame.camera_role != expected_role {
            return Err(EdgeError::InvalidState("camera_role_mismatch".to_string()));
        }
        if frame.camera_config_ref != self.config.config_digest {
            return Err(EdgeError::InvalidState(
                "camera_configuration_mismatch".to_string(),
            ));
        }
        if frame.frame_id.trim().is_empty() {
            return Err(EdgeError::InvalidState("frame_id_missing".to_string()));
        }
        let cam_model = match expected_role {
            CameraRole::Main => self.config.main_camera.as_ref(),
            CameraRole::Auxiliary => self.config.auxiliary_camera.as_ref(),
            _ => None,
        }
        .ok_or_else(|| EdgeError::InvalidState("missing camera model in StationConfig".into()))?;
        let intr = cam_model
            .intrinsics
            .as_ref()
            .ok_or_else(|| EdgeError::InvalidState("missing camera intrinsics".into()))?;
        if (frame.image.width_px, frame.image.height_px)
            != (intr.image_width_px, intr.image_height_px)
        {
            return Err(EdgeError::InvalidState(
                "camera_image_dimensions_mismatch".to_string(),
            ));
        }
        let midpoint = capture_midpoint_ns(&frame.capture_time)
            .map_err(|_| EdgeError::InvalidState("invalid_capture_time".to_string()))?;
        self.check_now(now)?;
        if frame.capture_time.clock_domain_id != now.clock_domain_id {
            return Err(EdgeError::InvalidState(
                "capture_processing_clock_mismatch".to_string(),
            ));
        }
        if midpoint > now.time_ns {
            return Err(EdgeError::InvalidState(
                "exposure_not_completed_at_processing_time".to_string(),
            ));
        }
        if let Some(&prev) = self.last_exposure_ns.get(&(expected_role as i32)) {
            if midpoint <= prev {
                return Err(EdgeError::InvalidState(
                    "exposure_time_not_increasing".to_string(),
                ));
            }
        }
        self.clock = Some(now.clock_domain_id.clone());
        self.last_processing_ns = Some(now.time_ns);
        self.last_exposure_ns.insert(expected_role as i32, midpoint);
        Ok(())
    }

    pub fn tick(&mut self, now: &ProcessingTime) -> Result<Option<CalibrationUpdate>, EdgeError> {
        self.check_now(now)?;
        self.clock = Some(now.clock_domain_id.clone());
        self.last_processing_ns = Some(now.time_ns);
        let config = &self.config;
        let seq = &mut self.sequencer;
        Ok(self
            .calibration
            .tick(now, config, &mut |et, dig| seq.next_envelope(et, dig)))
    }

    pub fn process_auxiliary(
        &mut self,
        frame: &CameraFrame,
        now: &ProcessingTime,
    ) -> Result<CalibrationUpdate, EdgeError> {
        self.accept_frame(frame, CameraRole::Auxiliary, now)?;
        let config = &self.config;
        let seq = &mut self.sequencer;
        Ok(self
            .calibration
            .process(frame, config, now, &mut |et, dig| {
                seq.next_envelope(et, dig)
            }))
    }

    fn make_diagnostic(
        &mut self,
        frame: &CameraFrame,
        category: &str,
        reasons: Vec<String>,
        cal_ref: Option<crate::proto::CalibrationSolutionRef>,
    ) -> bus_message::Payload {
        let env = self
            .sequencer
            .next_envelope(EventType::Diagnostic, &self.config.config_digest);
        bus_message::Payload::DiagnosticEvent(DiagnosticEvent {
            envelope: Some(env),
            related_frame_ref: Some(frame_ref(frame)),
            capture_time: Some(frame.capture_time.clone()),
            category: category.to_string(),
            reason_codes: reasons,
            related_config_ref: Some(self.config.config_digest.clone()),
            related_calibration_ref: cal_ref,
        })
    }

    pub fn process_main(
        &mut self,
        decoded: &DecodedFrame,
        now: &ProcessingTime,
    ) -> Result<(DetectionBatch, Vec<bus_message::Payload>, i64), EdgeError> {
        let frame = &decoded.frame;
        self.accept_frame(frame, CameraRole::Main, now)?;
        let t0 = Instant::now();
        let det = self
            .detector
            .process(frame)
            .map_err(|e| EdgeError::InvalidState(e.to_string()))?;
        let cv_ns = u64::try_from(t0.elapsed().as_nanos()).unwrap_or(u64::MAX);

        let capture_ns = capture_midpoint_ns(&frame.capture_time)
            .map_err(|e| EdgeError::InvalidState(e.to_string()))?;
        let first_ns = *self.first_main_capture_ns.get_or_insert(capture_ns);
        let elapsed_ns = capture_ns - first_ns;

        let mut suppress_warmup_rays = false;
        let mut suppress_reason = String::new();
        if elapsed_ns < self.warmup_hold_ns {
            suppress_warmup_rays = !det.observations.is_empty();
            suppress_reason = "warmup_hold".to_string();
        } else if det.observations.len() > self.warmup_observation_limit {
            suppress_warmup_rays = true;
            suppress_reason = "too_many_observations".to_string();
        }

        let mut events = Vec::new();
        if det.processing_status == ProcessingStatus::Failed {
            events.push(self.make_diagnostic(
                frame,
                "main_processing_failed",
                det.reason_codes.clone(),
                None,
            ));
        } else {
            let calibration = self.calibration.history.snapshot_at(&frame.capture_time);
            if !det.observations.is_empty() {
                if suppress_warmup_rays {
                    events.push(self.make_diagnostic(
                        frame,
                        "warmup_target_limit",
                        vec![suppress_reason],
                        None,
                    ));
                } else {
                    for obs in &det.observations {
                        let ev =
                            self.rays
                                .build(obs, &self.config, &calibration, &mut |et, dig| {
                                    self.sequencer.next_envelope(et, dig)
                                });
                        events.push(ev);
                    }
                }
            } else {
                let category = if det.reason_codes.iter().any(|r| r == "background_warmup") {
                    "background_warmup"
                } else {
                    "no_detections"
                };
                events.push(self.make_diagnostic(frame, category, det.reason_codes.clone(), None));
                if calibration.solution.is_none() || calibration.usage.is_none() {
                    events.push(self.make_diagnostic(
                        frame,
                        "calibration_unavailable",
                        calibration.reason_codes.clone(),
                        None,
                    ));
                }
            }
        }

        let batch = DetectionBatch {
            main_frame_ref: Some(det.main_frame_ref),
            capture_time: Some(det.capture_time),
            processing_status: det.processing_status as i32,
            observations: det.observations,
            reason_codes: det.reason_codes,
            decoded_pixel_sha256: decoded.decoded_pixel_sha256.clone(),
            decode_duration_ns: decoded.decode_duration_ns,
            cv_duration_ns: cv_ns,
        };
        Ok((batch, events, capture_ns))
    }
}

pub struct EdgeProcess<S: FrameSource> {
    writer: BusWriter,
    source: S,
    policies: BTreeMap<String, DevicePolicy>,
    warmup_hold_ns: i64,
    configs: HashMap<String, StationConfig>,
    stations: BTreeMap<String, StationDevice>,
    last_now_ns: HashMap<String, i64>,
    last_clock: HashMap<String, String>,
    last_cal_seq: HashMap<String, u64>,
    last_event_seq: HashMap<String, u64>,
    last_capture: HashMap<String, CaptureTime>,
}

impl<S: FrameSource> EdgeProcess<S> {
    pub fn new(
        writer: BusWriter,
        source: S,
        policies: BTreeMap<String, DevicePolicy>,
        warmup_hold_ns: i64,
    ) -> Self {
        Self {
            writer,
            source,
            policies,
            warmup_hold_ns,
            configs: HashMap::new(),
            stations: BTreeMap::new(),
            last_now_ns: HashMap::new(),
            last_clock: HashMap::new(),
            last_cal_seq: HashMap::new(),
            last_event_seq: HashMap::new(),
            last_capture: HashMap::new(),
        }
    }

    fn register_configs(&mut self, cfgs: Vec<StationConfig>) -> Result<(), EdgeError> {
        for cfg in cfgs {
            if !self.policies.contains_key(&cfg.station_id) {
                return Err(EdgeError::InvalidState(format!(
                    "received StationConfig for unconfigured station {:?}",
                    cfg.station_id
                )));
            }
            if let Some(existing) = self.configs.get(&cfg.station_id) {
                if existing != &cfg {
                    return Err(EdgeError::InvalidState(format!(
                        "conflicting StationConfig received for station {:?}",
                        cfg.station_id
                    )));
                }
            } else {
                self.configs.insert(cfg.station_id.clone(), cfg);
            }
        }
        Ok(())
    }

    fn ensure_station(&mut self, frame: &CameraFrame) -> Result<&mut StationDevice, EdgeError> {
        let sid = &frame.station_id;
        if self.stations.contains_key(sid) {
            let st = self.stations.get_mut(sid).unwrap();
            if st.station_session_id != frame.station_session_id {
                return Err(EdgeError::InvalidState(format!(
                    "frame station_session_id {:?} changed mid-stream",
                    frame.station_session_id
                )));
            }
            return Ok(self.stations.get_mut(sid).unwrap());
        }
        let policy = self.policies.get(sid).cloned().ok_or_else(|| {
            EdgeError::InvalidState(format!("received frame for unconfigured station {sid:?}"))
        })?;
        let config = self.configs.get(sid).cloned().ok_or_else(|| {
            EdgeError::InvalidState(format!("missing StationConfig before frame for {sid:?}"))
        })?;
        if config.config_digest != frame.camera_config_ref {
            return Err(EdgeError::InvalidState(format!(
                "frame config digest {:?} != {:?}",
                frame.camera_config_ref, config.config_digest
            )));
        }
        let device = StationDevice::new(
            config,
            policy,
            frame.station_session_id.clone(),
            self.warmup_hold_ns,
        )?;
        self.stations.insert(sid.clone(), device);
        Ok(self.stations.get_mut(sid).unwrap())
    }

    async fn send_station_event(&mut self, payload: bus_message::Payload) -> Result<(), EdgeError> {
        let env = match &payload {
            bus_message::Payload::RayEvent(ev) => ev.envelope.as_ref(),
            bus_message::Payload::CalibrationState(ev) => ev.envelope.as_ref(),
            bus_message::Payload::DiagnosticEvent(ev) => ev.envelope.as_ref(),
            _ => None,
        };
        if let Some(e) = env {
            let entry = self.last_event_seq.entry(e.station_id.clone()).or_insert(0);
            if e.sequence_no > *entry {
                *entry = e.sequence_no;
            }
        }
        self.writer.send_payload(payload).await?;
        Ok(())
    }

    async fn emit_calibration_update(
        &mut self,
        update: Option<CalibrationUpdate>,
    ) -> Result<(), EdgeError> {
        let Some(upd) = update else {
            return Ok(());
        };
        let Some(env) = upd.state_event.envelope.as_ref() else {
            return Ok(());
        };
        let sid = env.station_id.clone();
        let seq = env.sequence_no;
        let prev = self.last_cal_seq.get(&sid).copied().unwrap_or(0);
        if seq == prev {
            return Ok(());
        }
        if seq < prev {
            return Err(EdgeError::InvalidState(
                "device calibration event sequence regressed".to_string(),
            ));
        }
        self.last_cal_seq.insert(sid, seq);
        self.send_station_event(bus_message::Payload::CalibrationState(upd.state_event))
            .await?;
        for diag in upd.diagnostics {
            self.send_station_event(bus_message::Payload::DiagnosticEvent(diag))
                .await?;
        }
        Ok(())
    }

    async fn process_decoded(&mut self, decoded: DecodedFrame) -> Result<(), EdgeError> {
        let sid = decoded.frame.station_id.clone();
        let midpoint = capture_midpoint_ns(&decoded.frame.capture_time)
            .map_err(|e| EdgeError::InvalidState(e.to_string()))?;
        let clock_id = decoded.frame.capture_time.clock_domain_id.clone();
        if let Some(prev_clock) = self.last_clock.get(&sid) {
            if prev_clock != &clock_id {
                return Err(EdgeError::InvalidState(format!(
                    "station {sid:?} capture clock domain changed"
                )));
            }
        }
        self.last_clock.insert(sid.clone(), clock_id.clone());
        let now_ns = self
            .last_now_ns
            .get(&sid)
            .copied()
            .unwrap_or(midpoint)
            .max(midpoint);
        self.last_now_ns.insert(sid.clone(), now_ns);
        let now = ProcessingTime {
            clock_domain_id: clock_id,
            time_ns: now_ns,
        };

        let tick_upd = {
            let station = self.ensure_station(&decoded.frame)?;
            station.tick(&now)?
        };
        self.emit_calibration_update(tick_upd).await?;

        if decoded.frame.camera_role == CameraRole::Auxiliary {
            let upd = {
                let station = self.stations.get_mut(&sid).unwrap();
                station.process_auxiliary(&decoded.frame, &now)?
            };
            self.emit_calibration_update(Some(upd)).await?;
            return Ok(());
        }

        let (batch, events, completed_ns) = {
            let station = self.stations.get_mut(&sid).unwrap();
            station.process_main(&decoded, &now)?
        };
        let session_id = self.stations[&sid].station_session_id.clone();
        self.writer
            .send_payload(bus_message::Payload::DetectionBatch(batch))
            .await?;
        for ev in events {
            self.send_station_event(ev).await?;
        }
        self.last_capture
            .insert(sid.clone(), decoded.frame.capture_time);
        let seq = self.last_event_seq.get(&sid).copied().unwrap_or(0);
        self.writer
            .send_payload(bus_message::Payload::ProgressReport(ProgressReport {
                source_role: ComponentRole::Edge as i32,
                source_id: sid,
                session_id,
                completed_time_ns: completed_ns,
                completed_sequence_no: seq,
                finite_end_reached: false,
            }))
            .await?;
        Ok(())
    }

    async fn emit_stream_end(&mut self) -> Result<(), EdgeError> {
        let items: Vec<(String, String)> = self
            .stations
            .iter()
            .map(|(sid, st)| (sid.clone(), st.station_session_id.clone()))
            .collect();
        for (sid, session_id) in items {
            let completed_ns = self
                .last_capture
                .get(&sid)
                .and_then(|c| capture_midpoint_ns(c).ok())
                .unwrap_or(0);
            let seq = self.last_event_seq.get(&sid).copied().unwrap_or(0);
            self.writer
                .send_payload(bus_message::Payload::ProgressReport(ProgressReport {
                    source_role: ComponentRole::Edge as i32,
                    source_id: sid,
                    session_id,
                    completed_time_ns: completed_ns,
                    completed_sequence_no: seq,
                    finite_end_reached: true,
                }))
                .await?;
        }
        Ok(())
    }

    pub async fn run(&mut self) -> Result<(), EdgeError> {
        loop {
            let maybe_frame = self.source.next_frame().await?;
            let cfgs = self.source.take_station_configs();
            if !cfgs.is_empty() {
                self.register_configs(cfgs)?;
            }
            let Some(decoded) = maybe_frame else {
                break;
            };
            self.process_decoded(decoded).await?;
        }
        self.emit_stream_end().await?;
        self.writer.shutdown().await;
        self.source.close();
        Ok(())
    }
}
