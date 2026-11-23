use std::collections::BTreeMap;
use std::process::Command;
use std::time::{Duration, Instant};

use serde_json::Value;
use tempfile::TempDir;
use tokio::net::TcpStream;
use tokio::sync::oneshot;
use tokio::time::timeout;

use vswitch::bus::connect_bus_client;
use vswitch::framing::{
    read_length_prefixed_async, read_raw_frame_async, write_length_prefixed_async, MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
};
use vswitch::hub::{
    run_live_relay, run_record, run_replay, BoundAddresses, LiveRelayConfig, RecordConfig,
    ReplayConfig, ReplayMode, SteppingMode,
};
use vswitch::pbzst::{iter_pb_zst, PbZstWriter};
use vswitch::proto::{
    bus_message, now_wall_time_ns, test_framework_message, BusMessage, CalibrationHealth,
    CalibrationState, CalibrationStateEvent, CalibrationUsage, CameraDistortion, CameraIntrinsics,
    CameraModel, CameraRole, CaptureTime, ComponentRole, DetectionBatch, DiagnosticEvent,
    EventEnvelope, EventType, FrameRef, FramedVideoPacket, FusionMeasurement, FusionStatus,
    InstallationStateRef, InstallationUncertainty, Mat3, PixelFormat, ProcessingStatus,
    ProgressReport, RayEvent, ScoreEvent, SessionHello, SparseVoxelCell, SparseVoxelGrid,
    StationConfig, StationTargetProjection, TargetTruth, TestFrameworkMessage, TrackEvent,
    TrackLifecycle, TruthFrame, Vec2, Vec3, VideoCodec,
};
use vswitch::storage::{
    RunOutputConfig, RunOutputWriter, TestSceneConfig, TestSceneReader, TestSceneWriter,
};
use vswitch::video::{H264EncodePreset, VideoTrackEncoder};

fn make_station(station_id: &str, x: f64) -> StationConfig {
    StationConfig {
        station_id: station_id.to_string(),
        config_version: "v1".to_string(),
        config_digest: format!("digest-{station_id}"),
        world_frame_id: "enu".to_string(),
        main_camera_position_enu_m: Some(Vec3 { x, y: 0.0, z: 10.0 }),
        baseline_orientation_enu_from_main: Some(Mat3 {
            values: vec![1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        }),
        main_camera: Some(CameraModel {
            intrinsics: Some(CameraIntrinsics {
                image_width_px: 64,
                image_height_px: 48,
                matrix_px: Some(Mat3 {
                    values: vec![100.0, 0.0, 32.0, 0.0, 100.0, 24.0, 0.0, 0.0, 1.0],
                }),
            }),
            distortion: Some(CameraDistortion {
                model: "brown_conrady".to_string(),
                coefficients: vec![0.0, 0.0],
            }),
        }),
        installation_uncertainty: Some(InstallationUncertainty {
            state_ref: Some(InstallationStateRef {
                station_id: station_id.to_string(),
                config_digest: format!("digest-{station_id}"),
                state_version: "1".to_string(),
            }),
            position_covariance_enu_m2: Some(Mat3 {
                values: vec![1e-6, 0.0, 0.0, 0.0, 1e-6, 0.0, 0.0, 0.0, 1e-6],
            }),
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn make_raw_rgb_packet(station_id: &str, idx: u64, t_ns: i64) -> FramedVideoPacket {
    let w = 64u32;
    let h = 48u32;
    let mut payload = vec![0u8; (w * h * 3) as usize];
    for y in 0..h {
        for x in 0..w {
            let off = ((y * w + x) * 3) as usize;
            payload[off] = ((x * 4 + idx as u32 * 3) % 256) as u8;
            payload[off + 1] = ((y * 4) % 256) as u8;
            payload[off + 2] = 64;
        }
    }
    FramedVideoPacket {
        station_id: station_id.to_string(),
        station_session_id: format!("sess-{station_id}"),
        camera_role: CameraRole::Main as i32,
        frame_id: format!("{station_id}-f-{idx}"),
        frame_index: idx,
        capture_time: Some(CaptureTime {
            clock_domain_id: "sim".to_string(),
            exposure_start_ns: t_ns,
            exposure_end_ns: t_ns,
            ..Default::default()
        }),
        station_config_digest: format!("digest-{station_id}"),
        width_px: w,
        height_px: h,
        codec: VideoCodec::Raw as i32,
        pixel_format: PixelFormat::Rgb8 as i32,
        row_stride_bytes: w * 3,
        is_keyframe: idx == 0,
        payload,
    }
}

fn make_ray(station_id: &str, seq: u64, t_ns: i64, frame_id: &str) -> RayEvent {
    RayEvent {
        envelope: Some(EventEnvelope {
            schema_version: "1".to_string(),
            event_type: EventType::Ray as i32,
            station_id: station_id.to_string(),
            station_session_id: format!("sess-{station_id}"),
            sequence_no: seq,
            station_config_digest: format!("digest-{station_id}"),
        }),
        frame_id: frame_id.to_string(),
        capture_time: Some(CaptureTime {
            clock_domain_id: "sim".to_string(),
            exposure_start_ns: t_ns,
            exposure_end_ns: t_ns,
            ..Default::default()
        }),
        world_frame_id: "enu".to_string(),
        calibration_usage: CalibrationUsage::Valid as i32,
        ray_origin_enu_m: Some(Vec3 {
            x: if station_id == "s1" { 0.0 } else { 100.0 },
            y: 0.0,
            z: 10.0,
        }),
        ray_direction_enu_unit: Some(Vec3 {
            x: 0.0,
            y: 1.0,
            z: 0.0,
        }),
        detection_score: 0.95,
        ..Default::default()
    }
}

fn make_truth(step: u64, t_ns: i64) -> TruthFrame {
    TruthFrame {
        world_frame_id: "enu".to_string(),
        experiment_time_ns: t_ns,
        step_index: step,
        targets: vec![TargetTruth {
            target_id: "tgt-1".to_string(),
            target_class: "uav".to_string(),
            position_enu_m: Some(Vec3 {
                x: 50.0,
                y: 200.0 + step as f64,
                z: 40.0,
            }),
            velocity_enu_mps: Some(Vec3 {
                x: 0.0,
                y: 10.0,
                z: 0.0,
            }),
            bounding_radius_m: 1.5,
            body_aabb_diagonal_m: Some(3.0),
        }],
        projections: vec![StationTargetProjection {
            station_id: "s1".to_string(),
            target_id: "tgt-1".to_string(),
            occlusion_status: 1,
            projected_centroid_px: Some(Vec2 { x: 32.0, y: 24.0 }),
            projected_diagonal_px: Some(12.0),
            range_m: Some(200.0),
            frame_id: Some(format!("s1-f-{step}")),
        }],
    }
}

#[tokio::test]
async fn record_then_replay_integrated_lockstep_with_scorer_and_viewer() {
    let dir = TempDir::new().unwrap();
    let scene_dir = dir.path().join("recorded_scene");
    let run_dir = dir.path().join("run_integrated");
    let times = [0i64, 100_000_000, 200_000_000];

    // --- 1. Record mode: Producer -> V-Switch -> Test Scene ---
    let (rec_bound_tx, rec_bound_rx) = oneshot::channel::<BoundAddresses>();
    let (rec_shutdown_tx, rec_shutdown_rx) = oneshot::channel::<()>();
    let scene_dir_rec = scene_dir.clone();
    let record_task = tokio::spawn(async move {
        run_record(
            RecordConfig {
                output_scene_dir: scene_dir_rec,
                scenario_id: "integ-scene".to_string(),
                world_frame_id: "enu".to_string(),
                clock_domain_id: "sim".to_string(),
                occlusion_mode: "known".to_string(),
                random_seed: Some(7),
                bus_bind: "127.0.0.1:0".parse().unwrap(),
                truth_bind: "127.0.0.1:0".parse().unwrap(),
                viewer_bind: None,
                encode_preset: H264EncodePreset::default(),
                flush_every: 2,
                handshake_timeout: Duration::from_secs(5),
            },
            Some(rec_bound_tx),
            rec_shutdown_rx,
        )
        .await
    });

    let rec_addrs = rec_bound_rx.await.unwrap();
    let (mut prod_bus, ack) = connect_bus_client(
        rec_addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.to_string(),
            role: ComponentRole::Producer as i32,
            instance_id: "godot-prod-1".to_string(),
            session_id: "prod-sess".to_string(),
            station_ids: vec!["s1".to_string(), "s2".to_string()],
        },
    )
    .await
    .unwrap();
    assert!(ack.accepted);
    let mut prod_truth = TcpStream::connect(rec_addrs.truth_addr.unwrap())
        .await
        .unwrap();

    for sid in ["s1", "s2"] {
        let msg = BusMessage {
            bus_sequence_no: 1,
            sent_wall_time_ns: now_wall_time_ns(),
            payload: Some(bus_message::Payload::StationConfig(make_station(
                sid,
                if sid == "s1" { 0.0 } else { 100.0 },
            ))),
        };
        write_length_prefixed_async(&mut prod_bus, &msg, MAX_FRAME_BYTES)
            .await
            .unwrap();
    }

    for (step, &t_ns) in times.iter().enumerate() {
        let step_u64 = step as u64;
        for sid in ["s1", "s2"] {
            let pkt = make_raw_rgb_packet(sid, step_u64, t_ns);
            write_length_prefixed_async(
                &mut prod_bus,
                &BusMessage {
                    bus_sequence_no: 10 + step_u64,
                    sent_wall_time_ns: now_wall_time_ns(),
                    payload: Some(bus_message::Payload::VideoPacket(pkt)),
                },
                MAX_FRAME_BYTES,
            )
            .await
            .unwrap();

            let ray = make_ray(sid, step_u64 + 1, t_ns, &format!("{sid}-f-{step_u64}"));
            write_length_prefixed_async(
                &mut prod_bus,
                &BusMessage {
                    bus_sequence_no: 20 + step_u64,
                    sent_wall_time_ns: now_wall_time_ns(),
                    payload: Some(bus_message::Payload::RayEvent(ray)),
                },
                MAX_FRAME_BYTES,
            )
            .await
            .unwrap();
        }

        let tf = make_truth(step_u64, t_ns);
        write_length_prefixed_async(
            &mut prod_truth,
            &TestFrameworkMessage {
                sequence_no: step_u64 + 1,
                payload: Some(test_framework_message::Payload::TruthFrame(tf)),
            },
            MAX_FRAME_BYTES,
        )
        .await
        .unwrap();
    }

    write_length_prefixed_async(
        &mut prod_bus,
        &BusMessage {
            bus_sequence_no: 99,
            sent_wall_time_ns: now_wall_time_ns(),
            payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                source_role: ComponentRole::Producer as i32,
                source_id: "godot-prod-1".to_string(),
                session_id: "prod-sess".to_string(),
                completed_time_ns: *times.last().unwrap(),
                completed_sequence_no: 99,
                finite_end_reached: true,
            })),
        },
        MAX_FRAME_BYTES,
    )
    .await
    .unwrap();
    drop(prod_truth);
    drop(prod_bus);

    let rec_manifest = timeout(Duration::from_secs(15), record_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    drop(rec_shutdown_tx);
    let scene_sha_before = rec_manifest["content_sha256"].as_str().unwrap().to_string();

    // Verify D3b default preset & video metadata in recorded manifest.json
    assert_eq!(
        rec_manifest["encoding_preset"]["preset_id"],
        "d3b-yuv420p-crf18-max6m-v1"
    );
    assert_eq!(rec_manifest["encoding_preset"]["codec"], "libx264");
    assert_eq!(rec_manifest["encoding_preset"]["container"], "mp4");
    assert_eq!(
        rec_manifest["encoding_preset"]["encoded_pixel_format"],
        "yuv420p"
    );
    assert_eq!(rec_manifest["encoding_preset"]["x264_preset"], "medium");
    assert_eq!(rec_manifest["encoding_preset"]["crf"], 18);
    assert_eq!(rec_manifest["encoding_preset"]["maxrate_kbps"], 6000);
    assert_eq!(rec_manifest["encoding_preset"]["bufsize_kbps"], 12000);
    assert!(rec_manifest["encoding_preset"]["target_bitrate_kbps"].is_null());

    let s1_video = &rec_manifest["video"]["video/s1-main.mp4"];
    assert_eq!(s1_video["station_id"], "s1");
    assert_eq!(s1_video["camera_role"], "main");
    assert_eq!(s1_video["codec"], "libx264");
    assert_eq!(s1_video["container"], "mp4");
    assert_eq!(s1_video["pixel_format"], "rgb8");
    assert_eq!(s1_video["encoded_pixel_format"], "yuv420p");
    assert_eq!(s1_video["quality"], "d3b-yuv420p-crf18-max6m-v1");
    assert!(s1_video["average_bitrate_kbps"].as_f64().unwrap() > 0.0);

    // Verify recorded TestScene strictly in both Rust and Python chongming-scorer
    let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let scorer_verify = Command::new("uv")
        .current_dir(&repo_root)
        .args(["run", "--frozen", "chongming-scorer", "verify-scene"])
        .arg(&scene_dir)
        .arg("--verify-videos")
        .output()
        .unwrap();
    assert!(
        scorer_verify.status.success(),
        "chongming-scorer verify-scene failed on recorded TestScene: {}",
        String::from_utf8_lossy(&scorer_verify.stderr)
    );

    let scene_reader = TestSceneReader::open(&scene_dir, true, true).unwrap();
    assert_eq!(scene_reader.station_configs().len(), 2);
    assert_eq!(
        scene_reader
            .iter_frame_index()
            .unwrap()
            .collect_all()
            .unwrap()
            .len(),
        6
    );
    assert_eq!(
        scene_reader
            .iter_truth_frames()
            .unwrap()
            .collect_all()
            .unwrap()
            .len(),
        3
    );

    // --- 2. Integrated Lock-Step Replay with Edge, Center, Scorer, Active+Slow Viewers ---
    let (rep_bound_tx, rep_bound_rx) = oneshot::channel::<BoundAddresses>();
    let scene_dir_rep = scene_dir.clone();
    let run_dir_rep = run_dir.clone();
    let replay_task = tokio::spawn(async move {
        run_replay(
            ReplayConfig {
                mode: ReplayMode::Integrated,
                stepping: SteppingMode::LockStep,
                scene_dir: scene_dir_rep,
                run_output_dir: run_dir_rep,
                run_id: "run-integ-1".to_string(),
                bus_bind: "127.0.0.1:0".parse().unwrap(),
                truth_bind: Some("127.0.0.1:0".parse().unwrap()),
                viewer_bind: Some("127.0.0.1:0".parse().unwrap()),
                record_voxels: true,
                wait_scorer: true,
                verify_videos: true,
                flush_every: 2,
                handshake_timeout: Duration::from_secs(5),
                step_timeout: Duration::from_secs(5),
                drain_timeout: Duration::from_millis(500),
                wall_clock_speed: 1.0,
            },
            Some(rep_bound_tx),
        )
        .await
    });

    let rep_addrs = rep_bound_rx.await.unwrap();
    let mut active_viewer = TcpStream::connect(rep_addrs.viewer_addr.unwrap())
        .await
        .unwrap();
    let _stalled_viewer = TcpStream::connect(rep_addrs.viewer_addr.unwrap())
        .await
        .unwrap();

    // Connect Scorer (both Online Bus and Truth socket)
    let (mut scorer_bus, _) = connect_bus_client(
        rep_addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.to_string(),
            role: ComponentRole::Scorer as i32,
            instance_id: "scorer-1".to_string(),
            session_id: "sc-sess".to_string(),
            station_ids: vec![],
        },
    )
    .await
    .unwrap();
    let mut scorer_truth = TcpStream::connect(rep_addrs.truth_addr.unwrap())
        .await
        .unwrap();

    // Connect Edge (handling both s1 & s2) and Center
    let (mut edge_bus, _) = connect_bus_client(
        rep_addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.to_string(),
            role: ComponentRole::Edge as i32,
            instance_id: "edge-1".to_string(),
            session_id: "edge-sess".to_string(),
            station_ids: vec!["s1".to_string(), "s2".to_string()],
        },
    )
    .await
    .unwrap();
    let (mut center_bus, _) = connect_bus_client(
        rep_addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.to_string(),
            role: ComponentRole::Center as i32,
            instance_id: "center-1".to_string(),
            session_id: "ctr-sess".to_string(),
            station_ids: vec![],
        },
    )
    .await
    .unwrap();

    // Mock Edge task: verifies 2 StationConfigs, then processes 3 steps (2 stations per step)
    // and enforces lock-step check (no next step packet arrives before ProgressReport is sent).
    let edge_handle = tokio::spawn(async move {
        let mut cfg_count = 0usize;
        while cfg_count < 2 {
            let msg: BusMessage = read_length_prefixed_async(&mut edge_bus, MAX_FRAME_BYTES)
                .await
                .unwrap()
                .unwrap();
            assert!(matches!(
                msg.payload,
                Some(bus_message::Payload::StationConfig(_))
            ));
            cfg_count += 1;
        }

        let mut received_packets = 0usize;
        let mut seq = 1u64;
        for (step, &t_ns) in times.iter().enumerate() {
            for _ in 0..2 {
                let msg: BusMessage = read_length_prefixed_async(&mut edge_bus, MAX_FRAME_BYTES)
                    .await
                    .unwrap()
                    .unwrap();
                let Some(bus_message::Payload::VideoPacket(pkt)) = msg.payload else {
                    panic!("expected VideoPacket");
                };
                assert_eq!(pkt.codec, VideoCodec::H264 as i32);
                assert!(!pkt.payload.is_empty());
                if step == 0 {
                    assert!(pkt.is_keyframe);
                }
                received_packets += 1;

                // Send DetectionBatch + CalibrationStateEvent (once) + RayEvent
                write_length_prefixed_async(
                    &mut edge_bus,
                    &BusMessage {
                        bus_sequence_no: seq,
                        sent_wall_time_ns: now_wall_time_ns(),
                        payload: Some(bus_message::Payload::DetectionBatch(DetectionBatch {
                            main_frame_ref: Some(FrameRef {
                                station_id: pkt.station_id.clone(),
                                station_session_id: pkt.station_session_id.clone(),
                                camera_role: CameraRole::Main as i32,
                                frame_id: pkt.frame_id.clone(),
                            }),
                            capture_time: pkt.capture_time.clone(),
                            processing_status: ProcessingStatus::Processed as i32,
                            decode_duration_ns: 100_000,
                            cv_duration_ns: 200_000,
                            ..Default::default()
                        })),
                    },
                    MAX_FRAME_BYTES,
                )
                .await
                .unwrap();
                seq += 1;

                if step == 0 && pkt.station_id == "s1" {
                    write_length_prefixed_async(
                        &mut edge_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::CalibrationState(
                                CalibrationStateEvent {
                                    envelope: Some(EventEnvelope {
                                        event_type: EventType::CalibrationState as i32,
                                        station_id: pkt.station_id.clone(),
                                        station_session_id: pkt.station_session_id.clone(),
                                        sequence_no: seq,
                                        ..Default::default()
                                    }),
                                    state: Some(CalibrationState {
                                        station_id: pkt.station_id.clone(),
                                        health: CalibrationHealth::Valid as i32,
                                        ..Default::default()
                                    }),
                                    solution: None,
                                },
                            )),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    seq += 1;
                    write_length_prefixed_async(
                        &mut edge_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::DiagnosticEvent(DiagnosticEvent {
                                envelope: Some(EventEnvelope {
                                    event_type: EventType::Diagnostic as i32,
                                    station_id: pkt.station_id.clone(),
                                    station_session_id: pkt.station_session_id.clone(),
                                    sequence_no: seq,
                                    ..Default::default()
                                }),
                                category: "test_diag".to_string(),
                                reason_codes: vec!["verbatim_ok".to_string()],
                                ..Default::default()
                            })),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    seq += 1;
                }

                let ray = make_ray(&pkt.station_id, seq, t_ns, &pkt.frame_id);
                write_length_prefixed_async(
                    &mut edge_bus,
                    &BusMessage {
                        bus_sequence_no: seq,
                        sent_wall_time_ns: now_wall_time_ns(),
                        payload: Some(bus_message::Payload::RayEvent(ray)),
                    },
                    MAX_FRAME_BYTES,
                )
                .await
                .unwrap();
                seq += 1;
            }

            // Lock-step verification: before sending ProgressReport, no next-step packet may arrive.
            if step + 1 < times.len() {
                let premature = timeout(
                    Duration::from_millis(40),
                    read_length_prefixed_async::<_, BusMessage>(&mut edge_bus, MAX_FRAME_BYTES),
                )
                .await;
                assert!(
                    premature.is_err(),
                    "V-Switch released next step before lock-step ProgressReport!"
                );
            }

            write_length_prefixed_async(
                &mut edge_bus,
                &BusMessage {
                    bus_sequence_no: seq,
                    sent_wall_time_ns: now_wall_time_ns(),
                    payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                        source_role: ComponentRole::Edge as i32,
                        source_id: "edge-1".to_string(),
                        session_id: "edge-sess".to_string(),
                        completed_time_ns: t_ns,
                        completed_sequence_no: seq,
                        finite_end_reached: false,
                    })),
                },
                MAX_FRAME_BYTES,
            )
            .await
            .unwrap();
            seq += 1;
        }

        // Wait for finite_end_reached notice from V-Switch and reply with finite_end_reached=true.
        let end_msg: BusMessage = read_length_prefixed_async(&mut edge_bus, MAX_FRAME_BYTES)
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(
            end_msg.payload,
            Some(bus_message::Payload::ProgressReport(pr)) if pr.finite_end_reached
        ));
        write_length_prefixed_async(
            &mut edge_bus,
            &BusMessage {
                bus_sequence_no: seq,
                sent_wall_time_ns: now_wall_time_ns(),
                payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                    source_role: ComponentRole::Edge as i32,
                    source_id: "edge-1".to_string(),
                    session_id: "edge-sess".to_string(),
                    completed_time_ns: *times.last().unwrap(),
                    completed_sequence_no: seq,
                    finite_end_reached: true,
                })),
            },
            MAX_FRAME_BYTES,
        )
        .await
        .unwrap();
        received_packets
    });

    // Mock Center task: receives StationConfigs + verbatim Ray/Calib/Diag/ProgressReport
    let center_handle = tokio::spawn(async move {
        let mut saw_calib = false;
        let mut saw_diag = false;
        let mut saw_rays = 0usize;
        let mut seq = 1u64;

        while let Ok(Some(msg)) =
            read_length_prefixed_async::<_, BusMessage>(&mut center_bus, MAX_FRAME_BYTES).await
        {
            match msg.payload {
                Some(bus_message::Payload::CalibrationState(_)) => saw_calib = true,
                Some(bus_message::Payload::DiagnosticEvent(d)) => {
                    assert_eq!(d.reason_codes, vec!["verbatim_ok".to_string()]);
                    saw_diag = true;
                }
                Some(bus_message::Payload::RayEvent(_)) => saw_rays += 1,
                Some(bus_message::Payload::ProgressReport(pr)) => {
                    // Emit FusionMeasurement, TrackEvent, SparseVoxelGrid, and echo ProgressReport
                    write_length_prefixed_async(
                        &mut center_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::FusionMeasurement(
                                FusionMeasurement {
                                    center_session_id: "ctr-sess".to_string(),
                                    measurement_id: format!("m-{}", pr.completed_time_ns),
                                    clock_domain_id: "sim".to_string(),
                                    world_frame_id: "enu".to_string(),
                                    reference_time_ns: pr.completed_time_ns,
                                    status: FusionStatus::Located as i32,
                                    position_enu_m: Some(Vec3 {
                                        x: 50.0,
                                        y: 200.0,
                                        z: 40.0,
                                    }),
                                    supporting_station_ids: vec!["s1".into(), "s2".into()],
                                    ..Default::default()
                                },
                            )),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    seq += 1;

                    write_length_prefixed_async(
                        &mut center_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::TrackEvent(TrackEvent {
                                center_session_id: "ctr-sess".to_string(),
                                track_id: "trk-1".to_string(),
                                clock_domain_id: "sim".to_string(),
                                world_frame_id: "enu".to_string(),
                                event_time_ns: pr.completed_time_ns,
                                lifecycle: TrackLifecycle::Confirmed as i32,
                                position_enu_m: Some(Vec3 {
                                    x: 50.0,
                                    y: 200.0,
                                    z: 40.0,
                                }),
                                ..Default::default()
                            })),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    seq += 1;

                    if seq <= 4 {
                        write_length_prefixed_async(
                            &mut center_bus,
                            &BusMessage {
                                bus_sequence_no: seq,
                                sent_wall_time_ns: now_wall_time_ns(),
                                payload: Some(bus_message::Payload::SparseVoxelGrid(
                                    SparseVoxelGrid {
                                        center_session_id: "ctr-sess".to_string(),
                                        world_frame_id: "enu".to_string(),
                                        window_start_ns: pr.completed_time_ns,
                                        window_end_ns: pr.completed_time_ns,
                                        cells: vec![SparseVoxelCell {
                                            ix: 1,
                                            iy: 2,
                                            iz: 3,
                                            station_votes: 2,
                                        }],
                                        ..Default::default()
                                    },
                                )),
                            },
                            MAX_FRAME_BYTES,
                        )
                        .await
                        .unwrap();
                        seq += 1;
                    }

                    write_length_prefixed_async(
                        &mut center_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                                source_role: ComponentRole::Center as i32,
                                source_id: "center-1".to_string(),
                                session_id: "ctr-sess".to_string(),
                                completed_time_ns: pr.completed_time_ns,
                                completed_sequence_no: seq,
                                finite_end_reached: pr.finite_end_reached,
                            })),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    seq += 1;

                    if pr.finite_end_reached {
                        break;
                    }
                }
                _ => {}
            }
        }
        (saw_calib, saw_diag, saw_rays)
    });

    // Mock Scorer: reads TruthFrames and responds with ScoreEvents
    let scorer_truth_task = tokio::spawn(async move {
        let mut count = 0usize;
        while let Ok(Some(msg)) = read_length_prefixed_async::<_, TestFrameworkMessage>(
            &mut scorer_truth,
            MAX_FRAME_BYTES,
        )
        .await
        {
            if let Some(test_framework_message::Payload::TruthFrame(tf)) = msg.payload {
                count += 1;
                let score = ScoreEvent {
                    run_id: "run-integ-1".to_string(),
                    experiment_time_ns: tf.experiment_time_ns,
                    matched_count: 1,
                    unmatched_count: 0,
                    unevaluable_count: 0,
                    mean_error: Some(0.25),
                    rmse: Some(0.25),
                    max_error: Some(0.25),
                    error_unit: "m".to_string(),
                    ..Default::default()
                };
                let _ = write_length_prefixed_async(
                    &mut scorer_truth,
                    &TestFrameworkMessage {
                        sequence_no: count as u64,
                        payload: Some(test_framework_message::Payload::ScoreEvent(score)),
                    },
                    MAX_FRAME_BYTES,
                )
                .await;
            }
        }
        count
    });

    let scorer_bus_task = tokio::spawn(async move {
        let mut rays = 0usize;
        let mut tracks = 0usize;
        while let Ok(Some(msg)) =
            read_length_prefixed_async::<_, BusMessage>(&mut scorer_bus, MAX_FRAME_BYTES).await
        {
            match msg.payload {
                Some(bus_message::Payload::RayEvent(_)) => rays += 1,
                Some(bus_message::Payload::TrackEvent(_)) => tracks += 1,
                _ => {}
            }
        }
        (rays, tracks)
    });

    let viewer_task = tokio::spawn(async move {
        let mut frames = Vec::new();
        while let Ok(Some(raw)) = read_raw_frame_async(&mut active_viewer, MAX_FRAME_BYTES).await {
            let v: Value = serde_json::from_slice(&raw).unwrap();
            frames.push(v);
        }
        frames
    });

    let (edge_res, center_res, replay_res) = timeout(Duration::from_secs(15), async {
        tokio::join!(edge_handle, center_handle, replay_task)
    })
    .await
    .unwrap();

    assert_eq!(edge_res.unwrap(), 6);
    assert_eq!(center_res.unwrap(), (true, true, 6));
    let run_manifest = replay_res.unwrap().unwrap();
    assert_eq!(run_manifest["format"], "chongming-run-output-v1");
    assert_eq!(run_manifest["test_scene_sha256"], scene_sha_before);

    let truth_seen = scorer_truth_task.await.unwrap();
    let (scorer_rays, scorer_tracks) = scorer_bus_task.await.unwrap();
    let viewer_frames = viewer_task.await.unwrap();
    assert_eq!(truth_seen, 3);
    assert_eq!(scorer_rays, 6);
    assert!(scorer_tracks >= 3);
    assert!(!viewer_frames.is_empty());
    assert!(viewer_frames.iter().all(|f| {
        f["schema_version"] == "chongming-viewer-v1"
            && f["has_truth"] == true
            && f["update_id"].as_str().is_some()
            && f["sequence_no"].as_str().is_some()
            && f["stations"].is_array()
            && f["rays"]["items"].is_array()
            && f["rays"]["calibration_states"].is_array()
            && f["rays"]["diagnostics"].is_array()
            && f["answer"]["measurements"].is_array()
            && f["answer"]["tracks"].is_array()
    }));
    assert!(viewer_frames.iter().any(|f| {
        f.get("truth").is_some_and(|t| {
            t["step_index"].is_string()
                && t["targets"][0]["position_enu_m"].is_array()
                && t["projections"][0]["occlusion_status"] == "visible"
        })
    }));
    assert!(viewer_frames.iter().any(|f| {
        f["rays"]["items"]
            .as_array()
            .is_some_and(|arr| !arr.is_empty())
            && f["stations"]
                .as_array()
                .is_some_and(|st| st.iter().any(|s| s["calibration_health"] == "valid"))
    }));
    assert!(viewer_frames.iter().any(|f| f
        .get("score")
        .is_some_and(|s| s["events"][0]["matched_count"] == "1")));
    assert!(viewer_frames.iter().any(|f| {
        f.get("sparse_voxel_grid")
            .is_some_and(|g| g["max_score"] == 2 && g["center_session_id"] == "ctr-sess")
    }));

    // Verify TestScene remained strictly immutable
    let scene_after = TestSceneReader::open(&scene_dir, true, true).unwrap();
    assert_eq!(scene_after.content_sha256(), scene_sha_before);
}

#[tokio::test]
async fn replay_center_only_wall_clock_never_blocks_on_missing_progress() {
    let dir = TempDir::new().unwrap();
    let scene_dir = dir.path().join("scene");
    let run_dir = dir.path().join("run_wall");

    {
        let mut writer = TestSceneWriter::create(
            &scene_dir,
            TestSceneConfig {
                scenario_id: "wall-sc".into(),
                world_frame_id: "enu".into(),
                clock_domain_id: "sim".into(),
                start_time_ns: 0,
                end_time_ns: 80_000_000,
                stations: vec![make_station("s1", 0.0)],
                ..Default::default()
            },
        )
        .unwrap();
        for (i, t_ns) in [0i64, 40_000_000, 80_000_000].iter().enumerate() {
            writer
                .add_virtual_ray(&make_ray("s1", i as u64 + 1, *t_ns, &format!("f-{i}")))
                .unwrap();
        }
        writer.finalize().unwrap();
    }

    let (bound_tx, bound_rx) = oneshot::channel();
    let replay_task = tokio::spawn(run_replay(
        ReplayConfig {
            mode: ReplayMode::Center,
            stepping: SteppingMode::WallClock,
            scene_dir,
            run_output_dir: run_dir,
            run_id: "run-wall-1".into(),
            bus_bind: "127.0.0.1:0".parse().unwrap(),
            truth_bind: None,
            viewer_bind: None,
            wall_clock_speed: 2.0, // 80ms -> ~40ms wall clock
            drain_timeout: Duration::from_millis(120),
            ..Default::default()
        },
        Some(bound_tx),
    ));

    let addrs = match bound_rx.await {
        Ok(a) => a,
        Err(_) => {
            let err = replay_task.await.unwrap();
            panic!("replay_task exited before bind: {err:?}");
        }
    };
    let (mut center_bus, _) = connect_bus_client(
        addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.into(),
            role: ComponentRole::Center as i32,
            instance_id: "center-slow".into(),
            session_id: "cs-1".into(),
            station_ids: vec![],
        },
    )
    .await
    .unwrap();

    let center_task = tokio::spawn(async move {
        let mut rays_received = 0usize;
        while let Ok(Some(msg)) =
            read_length_prefixed_async::<_, BusMessage>(&mut center_bus, MAX_FRAME_BYTES).await
        {
            if matches!(msg.payload, Some(bus_message::Payload::RayEvent(_))) {
                rays_received += 1;
            }
            if let Some(bus_message::Payload::ProgressReport(pr)) = msg.payload {
                if pr.finite_end_reached {
                    break;
                }
            }
        }
        rays_received
    });

    let t0 = Instant::now();
    let manifest = timeout(Duration::from_secs(3), replay_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let elapsed = t0.elapsed();
    assert!(
        elapsed >= Duration::from_millis(30),
        "wall-clock pacing returned too fast: {elapsed:?}"
    );
    assert_eq!(manifest["mode"], "center");
    assert_eq!(center_task.await.unwrap(), 3);
}

#[tokio::test]
async fn live_relay_forwards_without_truth_or_score() {
    let (bound_tx, bound_rx) = oneshot::channel();
    let (shutdown_tx, shutdown_rx) = oneshot::channel();

    let relay_task = tokio::spawn(run_live_relay(
        LiveRelayConfig {
            run_id: "live-trial-1".into(),
            bus_bind: "127.0.0.1:0".parse().unwrap(),
            viewer_bind: Some("127.0.0.1:0".parse().unwrap()),
        },
        Some(bound_tx),
        shutdown_rx,
    ));

    let addrs = bound_rx.await.unwrap();
    let mut viewer = TcpStream::connect(addrs.viewer_addr.unwrap())
        .await
        .unwrap();

    let (mut center_bus, _) = connect_bus_client(
        addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.into(),
            role: ComponentRole::Center as i32,
            instance_id: "center-live".into(),
            session_id: "cl-1".into(),
            station_ids: vec![],
        },
    )
    .await
    .unwrap();
    let (mut edge_bus, _) = connect_bus_client(
        addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.into(),
            role: ComponentRole::Edge as i32,
            instance_id: "edge-live".into(),
            session_id: "el-1".into(),
            station_ids: vec!["s1".into()],
        },
    )
    .await
    .unwrap();

    // Give viewer accept task a moment
    tokio::time::sleep(Duration::from_millis(20)).await;

    let ray = make_ray("s1", 1, 555_000_000, "live-f-0");
    write_length_prefixed_async(
        &mut edge_bus,
        &BusMessage {
            bus_sequence_no: 1,
            sent_wall_time_ns: now_wall_time_ns(),
            payload: Some(bus_message::Payload::RayEvent(ray)),
        },
        MAX_FRAME_BYTES,
    )
    .await
    .unwrap();

    let forwarded: BusMessage = timeout(
        Duration::from_secs(2),
        read_length_prefixed_async(&mut center_bus, MAX_FRAME_BYTES),
    )
    .await
    .unwrap()
    .unwrap()
    .unwrap();
    assert!(matches!(
        forwarded.payload,
        Some(bus_message::Payload::RayEvent(r)) if r.frame_id == "live-f-0"
    ));

    let raw_viewer = timeout(
        Duration::from_secs(2),
        read_raw_frame_async(&mut viewer, MAX_FRAME_BYTES),
    )
    .await
    .unwrap()
    .unwrap()
    .unwrap();
    let vj: Value = serde_json::from_slice(&raw_viewer).unwrap();
    assert_eq!(vj["schema_version"], "chongming-viewer-v1");
    assert_eq!(vj["mode"], "live");
    assert_eq!(vj["has_truth"], false);
    assert_eq!(vj["update_id"], "1");
    assert_eq!(vj["sequence_no"], "1");
    assert_eq!(vj["experiment_time_ns"], "555000000");
    assert!(vj.get("truth").is_none());
    assert!(vj.get("score").is_none());
    assert!(vj.get("sparse_voxel_grid").is_none());
    assert_eq!(vj["rays"]["items"].as_array().unwrap().len(), 1);
    assert_eq!(
        vj["rays"]["items"][0]["event_id"],
        serde_json::json!(["s1", "sess-s1", "1"])
    );
    assert_eq!(vj["rays"]["items"][0]["calibration_usage"], "valid");
    assert_eq!(
        vj["rays"]["items"][0]["origin_enu_m"],
        serde_json::json!([0.0, 0.0, 10.0])
    );

    let _ = shutdown_tx.send(());
    relay_task.await.unwrap().unwrap();
}

#[test]
fn python_rust_pbzst_and_scorer_storage_interop() {
    let dir = TempDir::new().unwrap();
    let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();

    // 1. Rust writes TestScene & RunOutput; Python chongming-scorer verifies & inspects both.
    let scene_dir = dir.path().join("rust_scene");
    let run_dir = dir.path().join("rust_run");
    let mp4_fixture = dir.path().join("s1.mp4");
    std::fs::write(&mp4_fixture, b"fixture-mp4").unwrap();

    let mut sw = TestSceneWriter::create(
        &scene_dir,
        TestSceneConfig {
            scenario_id: "interop-1".into(),
            world_frame_id: "enu".into(),
            clock_domain_id: "sim".into(),
            start_time_ns: 0,
            end_time_ns: 1_000_000,
            stations: vec![make_station("s1", 0.0)],
            occlusion_mode: "known".into(),
            random_seed: Some(99),
            ..Default::default()
        },
    )
    .unwrap();
    sw.add_video_file(
        &mp4_fixture,
        "video/s1-main.mp4",
        serde_json::json!({"fps": 30}),
    )
    .unwrap();
    sw.add_frame_index(&vswitch::proto::FrameIndexEntry {
        station_id: "s1".into(),
        station_session_id: "sess-s1".into(),
        camera_role: CameraRole::Main as i32,
        frame_index: 0,
        frame_id: "s1-f-0".into(),
        capture_time: Some(CaptureTime {
            clock_domain_id: "sim".into(),
            exposure_start_ns: 0,
            exposure_end_ns: 0,
            ..Default::default()
        }),
        station_config_digest: "digest-s1".into(),
        video_relpath: "video/s1-main.mp4".into(),
        width_px: 64,
        height_px: 48,
    })
    .unwrap();
    sw.add_truth_frame(&make_truth(0, 0)).unwrap();
    sw.add_virtual_ray(&make_ray("s1", 1, 0, "s1-f-0")).unwrap();
    let scene_manifest = sw.finalize().unwrap();
    let scene_sha = scene_manifest["content_sha256"]
        .as_str()
        .unwrap()
        .to_string();

    let mut rw = RunOutputWriter::create(
        &run_dir,
        RunOutputConfig {
            run_id: "interop-run-1".into(),
            mode: "integrated".into(),
            test_scene_path: scene_dir.clone(),
            test_scene_sha256: scene_sha,
            provenance: BTreeMap::new(),
            flush_every: 2,
        },
    )
    .unwrap();
    rw.write_edge_message(&BusMessage {
        bus_sequence_no: 1,
        sent_wall_time_ns: 100,
        payload: Some(bus_message::Payload::RayEvent(make_ray(
            "s1", 1, 0, "s1-f-0",
        ))),
    })
    .unwrap();
    rw.write_center_message(&BusMessage {
        bus_sequence_no: 2,
        sent_wall_time_ns: 200,
        payload: Some(bus_message::Payload::TrackEvent(TrackEvent {
            center_session_id: "ctr".into(),
            track_id: "trk-1".into(),
            clock_domain_id: "sim".into(),
            world_frame_id: "enu".into(),
            event_time_ns: 0,
            lifecycle: TrackLifecycle::Confirmed as i32,
            position_enu_m: Some(Vec3 {
                x: 50.0,
                y: 200.0,
                z: 40.0,
            }),
            ..Default::default()
        })),
    })
    .unwrap();
    rw.finalize().unwrap();

    let verify_out = Command::new("uv")
        .current_dir(&repo_root)
        .args(["run", "--frozen", "chongming-scorer", "verify-scene"])
        .arg(&scene_dir)
        .arg("--verify-videos")
        .output()
        .unwrap();
    assert!(
        verify_out.status.success(),
        "chongming-scorer verify-scene failed: {}",
        String::from_utf8_lossy(&verify_out.stderr)
    );
    let verify_json: Value = serde_json::from_slice(&verify_out.stdout).unwrap();
    assert_eq!(verify_json["status"], "valid");
    assert_eq!(verify_json["frame_index_entries"], 1);
    assert_eq!(verify_json["truth_frames"], 1);
    assert_eq!(verify_json["virtual_rays"], 1);

    let inspect_out = Command::new("uv")
        .current_dir(&repo_root)
        .args(["run", "--frozen", "chongming-scorer", "inspect-run"])
        .arg(&run_dir)
        .output()
        .unwrap();
    assert!(
        inspect_out.status.success(),
        "chongming-scorer inspect-run failed: {}",
        String::from_utf8_lossy(&inspect_out.stderr)
    );
    let inspect_json: Value = serde_json::from_slice(&inspect_out.stdout).unwrap();
    assert_eq!(inspect_json["run_id"], "interop-run-1");
    assert_eq!(inspect_json["edge_messages"], 1);
    assert_eq!(inspect_json["center_messages"], 1);

    // 2. Python writes multi-frame .pb.zst -> Rust reads all frames across zstd frame boundaries.
    let py_pbzst = dir.path().join("from_py.pb.zst");
    let py_script = r#"
import sys
from pathlib import Path
from chongming_proto.online import PbZstWriter, online_pb2

path = Path(sys.argv[1])
with PbZstWriter(path, flush_every=2) as w:
    for i in range(5):
        w.write(online_pb2.BusMessage(bus_sequence_no=100 + i, sent_wall_time_ns=9007199254740993 + i))
"#;
    let py_res = Command::new("uv")
        .current_dir(&repo_root)
        .args(["run", "--frozen", "python", "-c", py_script])
        .arg(&py_pbzst)
        .output()
        .unwrap();
    assert!(
        py_res.status.success(),
        "python PbZstWriter failed: {}",
        String::from_utf8_lossy(&py_res.stderr)
    );
    let rust_loaded: Vec<BusMessage> = iter_pb_zst(&py_pbzst)
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(rust_loaded.len(), 5);
    assert_eq!(rust_loaded[0].bus_sequence_no, 100);
    assert_eq!(rust_loaded[4].sent_wall_time_ns, 9_007_199_254_740_997);

    // 3. Rust writes multi-frame .pb.zst -> Python iter_pb_zst reads all frames cleanly.
    let rs_pbzst = dir.path().join("from_rust.pb.zst");
    {
        let mut w = PbZstWriter::create(&rs_pbzst, 3, 2, false, MAX_FRAME_BYTES).unwrap();
        for i in 0..5u64 {
            w.write(&BusMessage {
                bus_sequence_no: 200 + i,
                sent_wall_time_ns: 9_007_199_254_741_000 + i as i64,
                payload: None,
            })
            .unwrap();
        }
        w.close().unwrap();
    }
    let py_read_script = r#"
import json, sys
from pathlib import Path
from chongming_proto.online import iter_pb_zst, online_pb2

msgs = list(iter_pb_zst(Path(sys.argv[1]), online_pb2.BusMessage))
print(json.dumps([(m.bus_sequence_no, m.sent_wall_time_ns) for m in msgs]))
"#;
    let py_read_res = Command::new("uv")
        .current_dir(&repo_root)
        .args(["run", "--frozen", "python", "-c", py_read_script])
        .arg(&rs_pbzst)
        .output()
        .unwrap();
    assert!(
        py_read_res.status.success(),
        "python iter_pb_zst failed: {}",
        String::from_utf8_lossy(&py_read_res.stderr)
    );
    let parsed: Vec<(u64, i64)> = serde_json::from_slice(&py_read_res.stdout).unwrap();
    assert_eq!(
        parsed,
        (0..5)
            .map(|i| (200 + i, 9_007_199_254_741_000 + i as i64))
            .collect::<Vec<_>>()
    );
}

#[tokio::test]
async fn replay_edge_only_lockstep_produces_scorable_run_output() {
    let dir = TempDir::new().unwrap();
    let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let scene_dir = dir.path().join("edge_scene");
    let run_dir = dir.path().join("edge_run");

    // Encode a 2-frame H.264 mp4 track and write TestScene
    let mp4_tmp = dir.path().join("s1-main.mp4");
    let p0 = make_raw_rgb_packet("s1", 0, 0);
    let p1 = make_raw_rgb_packet("s1", 1, 50_000_000);
    let mut enc = VideoTrackEncoder::start(&mp4_tmp, &p0, H264EncodePreset::default()).unwrap();
    enc.push_packet(&p0).unwrap();
    enc.push_packet(&p1).unwrap();
    let vmeta = enc.finish().unwrap();

    {
        let mut sw = TestSceneWriter::create(
            &scene_dir,
            TestSceneConfig {
                scenario_id: "edge-sc".into(),
                world_frame_id: "enu".into(),
                clock_domain_id: "sim".into(),
                start_time_ns: 0,
                end_time_ns: 50_000_000,
                stations: vec![make_station("s1", 0.0)],
                ..Default::default()
            },
        )
        .unwrap();
        sw.add_video_file(
            &mp4_tmp,
            "video/s1-main.mp4",
            serde_json::to_value(vmeta).unwrap(),
        )
        .unwrap();
        for (i, t_ns) in [(0u64, 0i64), (1, 50_000_000)] {
            sw.add_frame_index(&vswitch::proto::FrameIndexEntry {
                station_id: "s1".into(),
                station_session_id: "sess-s1".into(),
                camera_role: CameraRole::Main as i32,
                frame_index: i,
                frame_id: format!("s1-f-{i}"),
                capture_time: Some(CaptureTime {
                    clock_domain_id: "sim".into(),
                    exposure_start_ns: t_ns,
                    exposure_end_ns: t_ns,
                    ..Default::default()
                }),
                station_config_digest: "digest-s1".into(),
                video_relpath: "video/s1-main.mp4".into(),
                width_px: 64,
                height_px: 48,
            })
            .unwrap();
            sw.add_truth_frame(&make_truth(i, t_ns)).unwrap();
        }
        sw.finalize().unwrap();
    }

    let (bound_tx, bound_rx) = oneshot::channel();
    let scene_clone = scene_dir.clone();
    let run_clone = run_dir.clone();
    let replay_task = tokio::spawn(run_replay(
        ReplayConfig {
            mode: ReplayMode::Edge,
            stepping: SteppingMode::LockStep,
            scene_dir: scene_clone,
            run_output_dir: run_clone,
            run_id: "run-edge-1".into(),
            bus_bind: "127.0.0.1:0".parse().unwrap(),
            truth_bind: None,
            viewer_bind: None,
            ..Default::default()
        },
        Some(bound_tx),
    ));

    let addrs = bound_rx.await.unwrap();
    let (mut edge_bus, _) = connect_bus_client(
        addrs.bus_addr,
        SessionHello {
            protocol_version: PROTOCOL_VERSION.into(),
            role: ComponentRole::Edge as i32,
            instance_id: "edge-only".into(),
            session_id: "es-1".into(),
            station_ids: vec!["s1".into()],
        },
    )
    .await
    .unwrap();

    let edge_task = tokio::spawn(async move {
        let mut seq = 1u64;
        while let Ok(Some(msg)) =
            read_length_prefixed_async::<_, BusMessage>(&mut edge_bus, MAX_FRAME_BYTES).await
        {
            match msg.payload {
                Some(bus_message::Payload::VideoPacket(pkt)) => {
                    let t_ns = pkt
                        .capture_time
                        .as_ref()
                        .map(|c| c.exposure_start_ns)
                        .unwrap_or(0);
                    write_length_prefixed_async(
                        &mut edge_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::DetectionBatch(DetectionBatch {
                                main_frame_ref: Some(FrameRef {
                                    station_id: pkt.station_id.clone(),
                                    station_session_id: pkt.station_session_id.clone(),
                                    camera_role: CameraRole::Main as i32,
                                    frame_id: pkt.frame_id.clone(),
                                }),
                                capture_time: pkt.capture_time.clone(),
                                processing_status: ProcessingStatus::Processed as i32,
                                decode_duration_ns: 50_000,
                                cv_duration_ns: 100_000,
                                ..Default::default()
                            })),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    seq += 1;

                    write_length_prefixed_async(
                        &mut edge_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                                source_role: ComponentRole::Edge as i32,
                                source_id: "edge-only".into(),
                                session_id: "es-1".into(),
                                completed_time_ns: t_ns,
                                completed_sequence_no: seq,
                                finite_end_reached: false,
                            })),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    seq += 1;
                }
                Some(bus_message::Payload::ProgressReport(pr)) if pr.finite_end_reached => {
                    write_length_prefixed_async(
                        &mut edge_bus,
                        &BusMessage {
                            bus_sequence_no: seq,
                            sent_wall_time_ns: now_wall_time_ns(),
                            payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                                source_role: ComponentRole::Edge as i32,
                                source_id: "edge-only".into(),
                                session_id: "es-1".into(),
                                completed_time_ns: pr.completed_time_ns,
                                completed_sequence_no: seq,
                                finite_end_reached: true,
                            })),
                        },
                        MAX_FRAME_BYTES,
                    )
                    .await
                    .unwrap();
                    break;
                }
                _ => {}
            }
        }
    });

    let manifest = timeout(Duration::from_secs(5), replay_task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    edge_task.await.unwrap();
    assert_eq!(manifest["mode"], "edge");

    let score_out = Command::new("uv")
        .current_dir(&repo_root)
        .args(["run", "--frozen", "chongming-scorer", "score", "--scene"])
        .arg(&scene_dir)
        .arg("--run")
        .arg(&run_dir)
        .output()
        .unwrap();
    assert!(
        score_out.status.success(),
        "chongming-scorer score failed: {}",
        String::from_utf8_lossy(&score_out.stderr)
    );
    assert!(run_dir.join("report.json").is_file());
    assert!(run_dir.join("scores.pb.zst").is_file());
}
