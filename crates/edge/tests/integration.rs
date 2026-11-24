use std::collections::BTreeMap;
use std::time::Duration;

use tokio::net::TcpListener;

use edge::bus::OnlineBusClient;
use edge::device::EdgeProcess;
use edge::frame_source::VirtualBusFrameSource;
use edge::math::{
    add3, cross3, mat3_transpose, mat3_vec, norm3, project_point, scale3, sub3, DistortionCoeffs,
    Mat3, Vec3,
};
use edge::policy::{validate_device_policy, DevicePolicy};
use edge::proto::{
    bus_message, BusMessage, CalibrationHealth, CameraDistortion, CameraIntrinsics, CameraModel,
    CameraRole, CaptureTime, ComponentRole, FramedVideoPacket, InstallationStateRef,
    InstallationUncertainty, Mat3 as ProtoMat3, PixelFormat, ProgressReport, RigidTransform,
    SessionAck, StationConfig, SurveyedLandmark, SurveyedReference, Vec3 as ProtoVec3, VideoCodec,
};

fn normalize(v: Vec3) -> Vec3 {
    let n = norm3(v);
    [v[0] / n, v[1] / n, v[2] / n]
}

fn look_rotation_enu_camera(cam_pos: Vec3, target_pos: Vec3) -> Mat3 {
    let fwd = normalize(sub3(target_pos, cam_pos));
    let up_world = [0.0, 0.0, 1.0];
    let right = normalize(cross3(fwd, up_world));
    let down = cross3(fwd, right);
    [
        [right[0], down[0], fwd[0]],
        [right[1], down[1], fwd[1]],
        [right[2], down[2], fwd[2]],
    ]
}

fn build_station_config(station_id: &str, pos: Vec3) -> (StationConfig, Vec3, Mat3) {
    let aim = [0.0, 120.0, 45.0];
    let r_enu_from_main = look_rotation_enu_camera(pos, aim);
    let r_enu_from_aux: Mat3 = [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]];
    let r_aux_from_main = edge::math::mat3_mul(&mat3_transpose(&r_enu_from_aux), &r_enu_from_main);

    let board_center = add3(pos, [0.0, 0.0, -0.75]);
    let hw = 0.297 * 0.5;
    let hh = 0.210 * 0.5;
    let right = [
        r_enu_from_aux[0][0],
        r_enu_from_aux[1][0],
        r_enu_from_aux[2][0],
    ];
    let down = [
        r_enu_from_aux[0][1],
        r_enu_from_aux[1][1],
        r_enu_from_aux[2][1],
    ];
    let offsets = [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]];
    let landmarks = offsets
        .iter()
        .enumerate()
        .map(|(i, [ox, oy])| {
            let pt = add3(board_center, add3(scale3(right, *ox), scale3(down, *oy)));
            SurveyedLandmark {
                landmark_id: format!("board-corner-{i}"),
                position_enu_m: Some(ProtoVec3 {
                    x: pt[0],
                    y: pt[1],
                    z: pt[2],
                }),
                covariance_enu_m2: Some(ProtoMat3 {
                    values: vec![0.0; 9],
                }),
                visual_description: format!("corner {i}"),
            }
        })
        .collect();

    let cam_model = CameraModel {
        intrinsics: Some(CameraIntrinsics {
            image_width_px: 320,
            image_height_px: 240,
            matrix_px: Some(ProtoMat3 {
                values: vec![300.0, 0.0, 160.0, 0.0, 300.0, 120.0, 0.0, 0.0, 1.0],
            }),
        }),
        distortion: Some(CameraDistortion {
            model: "none".to_string(),
            coefficients: vec![0.0, 0.0, 0.0, 0.0],
        }),
    };
    let digest = format!("cfg-{station_id}");
    let cfg = StationConfig {
        station_id: station_id.to_string(),
        config_version: "1".to_string(),
        config_digest: digest.clone(),
        world_frame_id: "enu".to_string(),
        geodetic_anchor: None,
        main_camera_position_enu_m: Some(ProtoVec3 {
            x: pos[0],
            y: pos[1],
            z: pos[2],
        }),
        baseline_orientation_enu_from_main: Some(ProtoMat3 {
            values: r_enu_from_main.into_iter().flatten().collect(),
        }),
        main_camera: Some(cam_model.clone()),
        auxiliary_camera: Some(cam_model),
        fixed_rotation_aux_from_main: Some(ProtoMat3 {
            values: r_aux_from_main.into_iter().flatten().collect(),
        }),
        full_rigid_transform_aux_from_main: Some(RigidTransform {
            rotation_a_from_b: Some(ProtoMat3 {
                values: r_aux_from_main.into_iter().flatten().collect(),
            }),
            translation_a_m: Some(ProtoVec3 {
                x: 0.0,
                y: 0.0,
                z: 0.0,
            }),
        }),
        reference_scene_version: "test-v1".to_string(),
        installation_uncertainty: Some(InstallationUncertainty {
            state_ref: Some(InstallationStateRef {
                station_id: station_id.to_string(),
                config_digest: digest,
                state_version: "1".to_string(),
            }),
            position_covariance_enu_m2: None,
            orientation_covariance_enu_rad2: None,
            fixed_extrinsic_covariance_aux_rad2: None,
            measurement_source: "survey".to_string(),
        }),
        reference_anchor: Some(SurveyedReference {
            reference_id: format!("{station_id}-a4"),
            world_frame_id: "enu".to_string(),
            landmarks,
        }),
    };
    (cfg, board_center, r_enu_from_main)
}

fn render_aux_checkerboard(w: usize, h: usize) -> Vec<u8> {
    // Aux camera looks straight down at z = -0.75 m, board size 0.297x0.210, f=300 => ~118.8x84 px centered at (160,120).
    let sq_px = 14.0_f64;
    let bw = 7.0 * sq_px;
    let bh = 5.0 * sq_px;
    let x0 = (w as f64) * 0.5 - bw * 0.5;
    let y0 = (h as f64) * 0.5 - bh * 0.5;
    let mut rgb = vec![120u8; w * h * 3];
    for y in 0..h {
        for x in 0..w {
            let fx = x as f64;
            let fy = y as f64;
            let v = if fx >= x0 - 6.0 && fx < x0 + bw + 6.0 && fy >= y0 - 6.0 && fy < y0 + bh + 6.0
            {
                if fx >= x0 && fx < x0 + bw && fy >= y0 && fy < y0 + bh {
                    let col = ((fx - x0) / sq_px).floor() as i32;
                    let row = ((fy - y0) / sq_px).floor() as i32;
                    if (row + col) % 2 == 0 {
                        15u8
                    } else {
                        240u8
                    }
                } else {
                    240u8
                }
            } else {
                120u8
            };
            let idx = (y * w + x) * 3;
            rgb[idx] = v;
            rgb[idx + 1] = v;
            rgb[idx + 2] = v;
        }
    }
    rgb
}

fn render_main_target(
    w: usize,
    h: usize,
    target_enu: Option<Vec3>,
    cam_pos: Vec3,
    r_enu_from_main: &Mat3,
) -> Vec<u8> {
    let mut rgb = vec![10u8; w * h * 3];
    if let Some(tgt) = target_enu {
        let r_cam = mat3_transpose(r_enu_from_main);
        let t_cam = scale3(mat3_vec(&r_cam, cam_pos), -1.0);
        let k_norm = [[300.0, 0.0, 160.0], [0.0, 300.0, 120.0], [0.0, 0.0, 1.0]];
        let dist = DistortionCoeffs::from_proto("none", &[]).unwrap();
        let px = project_point(tgt, &r_cam, t_cam, &k_norm, &dist).unwrap();
        let cx = px[0].round() as isize;
        let cy = px[1].round() as isize;
        for dy in -4isize..=4 {
            for dx in -4isize..=4 {
                let x = cx + dx;
                let y = cy + dy;
                if x >= 0 && x < w as isize && y >= 0 && y < h as isize {
                    let idx = ((y as usize) * w + (x as usize)) * 3;
                    rgb[idx] = 245;
                    rgb[idx + 1] = 245;
                    rgb[idx + 2] = 245;
                }
            }
        }
    }
    rgb
}

async fn run_mock_vswitch_edge_session(policy_json: &str) -> Vec<BusMessage> {
    let policies: BTreeMap<String, DevicePolicy> = serde_json::from_str(policy_json).unwrap();
    for p in policies.values() {
        validate_device_policy(p).unwrap();
    }

    let cam_pos = [-60.0, 0.0, 10.0];
    let (cfg, _board_center, r_enu_from_main) = build_station_config("s1", cam_pos);
    let aux_rgb = render_aux_checkerboard(320, 240);

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let (mut r, mut w) = stream.into_split();

        let hello: BusMessage =
            edge::framing::read_length_prefixed_async(&mut r, edge::framing::MAX_FRAME_BYTES)
                .await
                .unwrap()
                .unwrap();
        assert!(matches!(
            hello.payload,
            Some(bus_message::Payload::SessionHello(h)) if h.role == ComponentRole::Edge as i32
        ));

        let ack = BusMessage {
            bus_sequence_no: 1,
            sent_wall_time_ns: 0,
            payload: Some(bus_message::Payload::SessionAck(SessionAck {
                accepted: true,
                reason: String::new(),
                run_id: "test-run".to_string(),
            })),
        };
        edge::framing::write_length_prefixed_async(&mut w, &ack, edge::framing::MAX_FRAME_BYTES)
            .await
            .unwrap();

        let cfg_msg = BusMessage {
            bus_sequence_no: 2,
            sent_wall_time_ns: 0,
            payload: Some(bus_message::Payload::StationConfig(cfg.clone())),
        };
        edge::framing::write_length_prefixed_async(
            &mut w,
            &cfg_msg,
            edge::framing::MAX_FRAME_BYTES,
        )
        .await
        .unwrap();

        let mut seq = 3u64;
        for step in 0..5i64 {
            let t_ns = 1_000_000_000 + step * 100_000_000;
            let ct = CaptureTime {
                clock_domain_id: "sim".to_string(),
                exposure_start_ns: t_ns,
                exposure_end_ns: t_ns,
                clock_uncertainty_ns: 0,
                rolling_shutter_model: "none".to_string(),
            };
            if step == 0 {
                let aux_pkt = BusMessage {
                    bus_sequence_no: seq,
                    sent_wall_time_ns: 0,
                    payload: Some(bus_message::Payload::VideoPacket(FramedVideoPacket {
                        station_id: "s1".to_string(),
                        station_session_id: "sess-1".to_string(),
                        camera_role: CameraRole::Auxiliary as i32,
                        frame_id: format!("s1-aux-{step}"),
                        frame_index: step as u64,
                        capture_time: Some(ct.clone()),
                        station_config_digest: cfg.config_digest.clone(),
                        width_px: 320,
                        height_px: 240,
                        codec: VideoCodec::Raw as i32,
                        pixel_format: PixelFormat::Rgb8 as i32,
                        row_stride_bytes: 320 * 3,
                        is_keyframe: true,
                        payload: aux_rgb.clone(),
                    })),
                };
                seq += 1;
                edge::framing::write_length_prefixed_async(
                    &mut w,
                    &aux_pkt,
                    edge::framing::MAX_FRAME_BYTES,
                )
                .await
                .unwrap();
            }

            let target = if step == 0 {
                None
            } else {
                Some([-20.0 + (step as f64) * 10.0, 120.0, 45.0])
            };
            let main_rgb = render_main_target(320, 240, target, cam_pos, &r_enu_from_main);
            let main_pkt = BusMessage {
                bus_sequence_no: seq,
                sent_wall_time_ns: 0,
                payload: Some(bus_message::Payload::VideoPacket(FramedVideoPacket {
                    station_id: "s1".to_string(),
                    station_session_id: "sess-1".to_string(),
                    camera_role: CameraRole::Main as i32,
                    frame_id: format!("s1-main-{step}"),
                    frame_index: step as u64,
                    capture_time: Some(ct),
                    station_config_digest: cfg.config_digest.clone(),
                    width_px: 320,
                    height_px: 240,
                    codec: VideoCodec::Raw as i32,
                    pixel_format: PixelFormat::Rgb8 as i32,
                    row_stride_bytes: 320 * 3,
                    is_keyframe: true,
                    payload: main_rgb,
                })),
            };
            seq += 1;
            edge::framing::write_length_prefixed_async(
                &mut w,
                &main_pkt,
                edge::framing::MAX_FRAME_BYTES,
            )
            .await
            .unwrap();
        }

        let end_msg = BusMessage {
            bus_sequence_no: seq,
            sent_wall_time_ns: 0,
            payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                source_role: ComponentRole::Producer as i32,
                source_id: "prod".to_string(),
                session_id: "prod-1".to_string(),
                completed_time_ns: 1_400_000_000,
                completed_sequence_no: 5,
                finite_end_reached: true,
            })),
        };
        edge::framing::write_length_prefixed_async(
            &mut w,
            &end_msg,
            edge::framing::MAX_FRAME_BYTES,
        )
        .await
        .unwrap();
        drop(w);

        let mut received = Vec::new();
        while let Some(msg) = edge::framing::read_length_prefixed_async::<_, BusMessage>(
            &mut r,
            edge::framing::MAX_FRAME_BYTES,
        )
        .await
        .unwrap()
        {
            received.push(msg);
        }
        received
    });

    let mut client = OnlineBusClient::connect("127.0.0.1", addr.port(), Duration::from_secs(5))
        .await
        .unwrap();
    client
        .handshake("edge-test", "sess-edge", &["s1".to_string()])
        .await
        .unwrap();
    let (reader, writer) = client.into_split();
    let source = VirtualBusFrameSource::new(reader, Duration::from_secs(5), None);
    let mut process = EdgeProcess::new(writer, source, policies, 0);
    process.run().await.unwrap();

    server.await.unwrap()
}

#[tokio::test]
async fn standard_mog2_edge_calibrates_and_emits_accurate_rays() {
    let policy_json = r#"{
      "s1": {
        "detection": {
          "algorithm_id": "mog2-components-v1",
          "score_definition": {
            "definition_id": "foreground-component-fill-ratio-v1",
            "kind": "ranking",
            "minimum": 0.0,
            "maximum": 1.0,
            "cross_station_comparison_group": null
          },
          "minimum_component_area_px2": 4.0,
          "maximum_component_area_px2": null,
          "background_warmup_frame_count": 1,
          "background_history_frame_count": 10,
          "background_variance_threshold": 16.0,
          "background_learning_rate": 0.0,
          "background_detect_shadows": false
        },
        "tracking": {
          "algorithm_id": "constant-velocity-nearest-neighbor-v1",
          "association_gate_px": 100.0,
          "confirmation_frame_count": 2,
          "maximum_missing_duration_ns": 500000000,
          "quality_definition": {
            "definition_id": "consecutive-detection-fraction-v1",
            "kind": "ranking",
            "minimum": 0.0,
            "maximum": 1.0,
            "cross_station_comparison_group": null
          }
        },
        "calibration": {
          "update_period_ns": 0,
          "stale_after_ns": 5000000000,
          "recovery_success_count": 1,
          "minimum_reference_count": 4,
          "maximum_reprojection_rmse_px": 3.0,
          "maximum_rotation_only_parallax_px": null,
          "estimator_id": "opencv-checkerboard-v1"
        },
        "rays": {
          "uncertainty_model_id": "tangent_gaussian_v1",
          "fallback_centroid_covariance_px2": [[1.0, 0.0], [0.0, 1.0]],
          "unmodelled_high_frequency_jitter_covariance_rad2": [[0.0, 0.0], [0.0, 0.0]],
          "unmodelled_relative_flex_covariance_rad2": [[0.0, 0.0], [0.0, 0.0]]
        }
      }
    }"#;

    let msgs = run_mock_vswitch_edge_session(policy_json).await;
    let cal_valid = msgs.iter().any(|m| {
        matches!(
            &m.payload,
            Some(bus_message::Payload::CalibrationState(c))
            if c.state.as_ref().is_some_and(|s| s.health == CalibrationHealth::Valid as i32)
        )
    });
    assert!(cal_valid, "expected CalibrationHealth::Valid");

    let rays: Vec<_> = msgs
        .iter()
        .filter_map(|m| match &m.payload {
            Some(bus_message::Payload::RayEvent(r)) => Some(r),
            _ => None,
        })
        .collect();
    assert_eq!(rays.len(), 4);
    let last_ray = rays.last().unwrap();
    let dir = last_ray.ray_direction_enu_unit.as_ref().unwrap();
    let expected = normalize(sub3([20.0, 120.0, 45.0], [-60.0, 0.0, 10.0]));
    let ang_err = (dir.x * expected[0] + dir.y * expected[1] + dir.z * expected[2])
        .clamp(-1.0, 1.0)
        .acos();
    assert!(ang_err < 0.01, "angular error too large: {ang_err}");

    let ended = msgs.iter().any(|m| {
        matches!(
            &m.payload,
            Some(bus_message::Payload::ProgressReport(p)) if p.finite_end_reached
        )
    });
    assert!(ended);
}

#[tokio::test]
async fn workbench_frame_diff_occupancy_edge_emits_confirmed_rays() {
    let policy_json = r#"{
      "s1": {
        "detection": {
          "algorithm_id": "frame-difference-occupancy-v1",
          "downsample_factor": 2,
          "mode": "two_frame",
          "threshold": 10.0,
          "morphology_kernel_size": 3,
          "apply_opening": false,
          "apply_closing": false,
          "score_increment": 1.0,
          "score_decay": 0.1,
          "mask_threshold": 50.0,
          "occupancy_dilation_size": 5,
          "minimum_component_area_px2": 2.0,
          "maximum_component_area_px2": null,
          "blob_dilation_size": 3,
          "apply_occupancy": true,
          "apply_erosion": false
        },
        "tracking": {
          "algorithm_id": "workbench-kalman-v1",
          "confirmation_frame_count": 2,
          "maximum_lost_frame_count": 5,
          "association_gate_px": 60.0,
          "covariance_floor_px": 5.0,
          "process_noise_position": 10.0,
          "process_noise_velocity": 5.0,
          "measurement_noise_px": 5.0
        },
        "calibration": {
          "update_period_ns": 0,
          "stale_after_ns": 5000000000,
          "recovery_success_count": 1,
          "minimum_reference_count": 4,
          "maximum_reprojection_rmse_px": 3.0,
          "maximum_rotation_only_parallax_px": null,
          "estimator_id": "opencv-checkerboard-v1"
        },
        "rays": {
          "uncertainty_model_id": "tangent_gaussian_v1",
          "fallback_centroid_covariance_px2": [[1.0, 0.0], [0.0, 1.0]],
          "unmodelled_high_frequency_jitter_covariance_rad2": [[0.0, 0.0], [0.0, 0.0]],
          "unmodelled_relative_flex_covariance_rad2": [[0.0, 0.0], [0.0, 0.0]]
        }
      }
    }"#;

    let msgs = run_mock_vswitch_edge_session(policy_json).await;
    let rays: Vec<_> = msgs
        .iter()
        .filter_map(|m| match &m.payload {
            Some(bus_message::Payload::RayEvent(r)) => Some(r),
            _ => None,
        })
        .collect();
    // Step 0 has no target, step 1 creates tentative track(s) (confirmation_frame_count = 2),
    // so confirmed rays only appear from step 2 onward (two-frame diff emits leading/trailing blobs).
    assert!(
        rays.len() >= 3,
        "expected >= 3 confirmed rays, got {}",
        rays.len()
    );
    assert!(
        rays.iter()
            .all(|r| r.capture_time.as_ref().unwrap().exposure_start_ns >= 1_200_000_000),
        "no unconfirmed rays may be emitted before step 2"
    );
    let expected = normalize(sub3([20.0, 120.0, 45.0], [-60.0, 0.0, 10.0]));
    let min_err = rays
        .iter()
        .filter(|r| r.capture_time.as_ref().unwrap().exposure_start_ns == 1_400_000_000)
        .map(|r| {
            let d = r.ray_direction_enu_unit.as_ref().unwrap();
            (d.x * expected[0] + d.y * expected[1] + d.z * expected[2])
                .clamp(-1.0, 1.0)
                .acos()
        })
        .fold(f64::INFINITY, f64::min);
    assert!(
        min_err < 0.02,
        "target ray angular error too large: {min_err}"
    );
}
