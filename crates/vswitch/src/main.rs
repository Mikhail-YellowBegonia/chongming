use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

use anyhow::Result;
use clap::{Parser, Subcommand, ValueEnum};
use tokio::sync::oneshot;

use vswitch::hub::{
    run_live_relay, run_record, run_replay, BoundAddresses, LiveRelayConfig, RecordConfig,
    ReplayConfig, ReplayMode, SteppingMode,
};
use vswitch::storage::{
    EncodingPresetSpec, TestSceneReader, DEFAULT_D3B_BUFSIZE_KBPS, DEFAULT_D3B_CODEC,
    DEFAULT_D3B_CONTAINER, DEFAULT_D3B_CRF, DEFAULT_D3B_MAXRATE_KBPS, DEFAULT_D3B_PIXEL_FORMAT,
    DEFAULT_D3B_PRESET_ID, DEFAULT_D3B_X264_PRESET,
};
use vswitch::video::{H264EncodePreset, DEFAULT_D3B_H264_FPS};

#[derive(Debug, Parser)]
#[command(name = "vswitch", about = "Chongming V-Switch hub")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliReplayMode {
    Edge,
    Center,
    Integrated,
}

impl From<CliReplayMode> for ReplayMode {
    fn from(value: CliReplayMode) -> Self {
        match value {
            CliReplayMode::Edge => ReplayMode::Edge,
            CliReplayMode::Center => ReplayMode::Center,
            CliReplayMode::Integrated => ReplayMode::Integrated,
        }
    }
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliSteppingMode {
    LockStep,
    WallClock,
}

impl From<CliSteppingMode> for SteppingMode {
    fn from(value: CliSteppingMode) -> Self {
        match value {
            CliSteppingMode::LockStep => SteppingMode::LockStep,
            CliSteppingMode::WallClock => SteppingMode::WallClock,
        }
    }
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Replay an immutable Test Scene against Edge, Center, and/or Scorer peers.
    Replay {
        #[arg(long)]
        scene: PathBuf,
        #[arg(long)]
        run_output: PathBuf,
        #[arg(long)]
        run_id: String,
        #[arg(long, value_enum, default_value_t = CliReplayMode::Integrated)]
        mode: CliReplayMode,
        #[arg(long, value_enum, default_value_t = CliSteppingMode::LockStep)]
        stepping: CliSteppingMode,
        #[arg(long, default_value = "127.0.0.1:7000")]
        bus_bind: SocketAddr,
        #[arg(long)]
        truth_bind: Option<SocketAddr>,
        #[arg(long)]
        viewer_bind: Option<SocketAddr>,
        #[arg(long)]
        record_voxels: bool,
        #[arg(long)]
        wait_scorer: bool,
        #[arg(long)]
        verify_videos: bool,
        #[arg(long, default_value_t = 256)]
        flush_every: usize,
        #[arg(long, default_value_t = 30.0)]
        handshake_timeout_s: f64,
        #[arg(long, default_value_t = 30.0)]
        step_timeout_s: f64,
        #[arg(long, default_value_t = 1.0)]
        wall_clock_speed: f64,
    },
    /// Live Relay mode: unidirectional Edge -> V-Switch -> Center without Test Scene or Truth.
    Live {
        #[arg(long, default_value = "live")]
        run_id: String,
        #[arg(long, default_value = "127.0.0.1:7000")]
        bus_bind: SocketAddr,
        #[arg(long)]
        viewer_bind: Option<SocketAddr>,
    },
    /// Record mode: ingest Godot Producer streams and write an immutable Test Scene.
    Record {
        #[arg(long)]
        output_scene: PathBuf,
        #[arg(long, default_value = "recorded")]
        scenario_id: String,
        #[arg(long, default_value = "enu")]
        world_frame_id: String,
        #[arg(long, default_value = "sim")]
        clock_domain_id: String,
        #[arg(long, default_value = "known")]
        occlusion_mode: String,
        #[arg(long)]
        random_seed: Option<i64>,
        #[arg(long, default_value = "127.0.0.1:7000")]
        bus_bind: SocketAddr,
        #[arg(long, default_value = "127.0.0.1:7001")]
        truth_bind: SocketAddr,
        #[arg(long)]
        viewer_bind: Option<SocketAddr>,
        #[arg(long, default_value = DEFAULT_D3B_PRESET_ID)]
        preset_id: String,
        #[arg(long, default_value = DEFAULT_D3B_X264_PRESET)]
        x264_preset: String,
        #[arg(long, default_value_t = DEFAULT_D3B_CRF)]
        crf: u32,
        #[arg(long, default_value_t = DEFAULT_D3B_MAXRATE_KBPS)]
        maxrate_kbps: u32,
        #[arg(long, default_value_t = DEFAULT_D3B_BUFSIZE_KBPS)]
        bufsize_kbps: u32,
        #[arg(long)]
        bitrate_kbps: Option<u32>,
        #[arg(long, default_value = DEFAULT_D3B_PIXEL_FORMAT)]
        pixel_format: String,
        #[arg(long)]
        h264_gop: Option<u32>,
        #[arg(long, default_value_t = DEFAULT_D3B_H264_FPS)]
        fps: f64,
        #[arg(long, default_value_t = 256)]
        flush_every: usize,
        #[arg(long, default_value_t = 30.0)]
        handshake_timeout_s: f64,
    },
    /// Verify an immutable Test Scene package manifest and file hashes.
    VerifyScene {
        scene: PathBuf,
        #[arg(long)]
        verify_videos: bool,
    },
}

fn spawn_bound_printer(bound_rx: oneshot::Receiver<BoundAddresses>) {
    tokio::spawn(async move {
        if let Ok(addrs) = bound_rx.await {
            let report = serde_json::json!({
                "bus_addr": addrs.bus_addr.to_string(),
                "truth_addr": addrs.truth_addr.map(|a| a.to_string()),
                "viewer_addr": addrs.viewer_addr.map(|a| a.to_string()),
            });
            eprintln!("vswitch listening: {report}");
        }
    });
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Replay {
            scene,
            run_output,
            run_id,
            mode,
            stepping,
            bus_bind,
            truth_bind,
            viewer_bind,
            record_voxels,
            wait_scorer,
            verify_videos,
            flush_every,
            handshake_timeout_s,
            step_timeout_s,
            wall_clock_speed,
        } => {
            let (bound_tx, bound_rx) = oneshot::channel();
            spawn_bound_printer(bound_rx);
            let manifest = run_replay(
                ReplayConfig {
                    mode: mode.into(),
                    stepping: stepping.into(),
                    scene_dir: scene,
                    run_output_dir: run_output,
                    run_id,
                    bus_bind,
                    truth_bind,
                    viewer_bind,
                    record_voxels,
                    wait_scorer,
                    verify_videos,
                    flush_every,
                    handshake_timeout: Duration::from_secs_f64(handshake_timeout_s.max(0.1)),
                    step_timeout: Duration::from_secs_f64(step_timeout_s.max(0.1)),
                    drain_timeout: Duration::from_millis(500),
                    wall_clock_speed,
                },
                Some(bound_tx),
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&manifest)?);
        }
        Command::Live {
            run_id,
            bus_bind,
            viewer_bind,
        } => {
            let (bound_tx, bound_rx) = oneshot::channel();
            spawn_bound_printer(bound_rx);
            let (shutdown_tx, shutdown_rx) = oneshot::channel();
            tokio::spawn(async move {
                let _ = tokio::signal::ctrl_c().await;
                let _ = shutdown_tx.send(());
            });
            run_live_relay(
                LiveRelayConfig {
                    run_id,
                    bus_bind,
                    viewer_bind,
                },
                Some(bound_tx),
                shutdown_rx,
            )
            .await?;
        }
        Command::Record {
            output_scene,
            scenario_id,
            world_frame_id,
            clock_domain_id,
            occlusion_mode,
            random_seed,
            bus_bind,
            truth_bind,
            viewer_bind,
            preset_id,
            x264_preset,
            crf,
            maxrate_kbps,
            bufsize_kbps,
            bitrate_kbps,
            pixel_format,
            h264_gop,
            fps,
            flush_every,
            handshake_timeout_s,
        } => {
            let (bound_tx, bound_rx) = oneshot::channel();
            spawn_bound_printer(bound_rx);
            let (shutdown_tx, shutdown_rx) = oneshot::channel();
            tokio::spawn(async move {
                let _ = tokio::signal::ctrl_c().await;
                let _ = shutdown_tx.send(());
            });
            let manifest = run_record(
                RecordConfig {
                    output_scene_dir: output_scene,
                    scenario_id,
                    world_frame_id,
                    clock_domain_id,
                    occlusion_mode,
                    random_seed,
                    bus_bind,
                    truth_bind,
                    viewer_bind,
                    encode_preset: H264EncodePreset {
                        spec: EncodingPresetSpec {
                            preset_id,
                            codec: DEFAULT_D3B_CODEC.to_string(),
                            container: DEFAULT_D3B_CONTAINER.to_string(),
                            encoded_pixel_format: pixel_format,
                            x264_preset,
                            crf: Some(crf),
                            maxrate_kbps: Some(maxrate_kbps),
                            bufsize_kbps: Some(bufsize_kbps),
                            target_bitrate_kbps: bitrate_kbps,
                        },
                        gop_size: h264_gop,
                        fps,
                        is_track_override: false,
                    },
                    flush_every,
                    handshake_timeout: Duration::from_secs_f64(handshake_timeout_s.max(0.1)),
                },
                Some(bound_tx),
                shutdown_rx,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&manifest)?);
        }
        Command::VerifyScene {
            scene,
            verify_videos,
        } => {
            let reader = TestSceneReader::open(&scene, true, verify_videos)?;
            let summary = serde_json::json!({
                "status": "valid",
                "scenario_id": reader.scenario_id(),
                "content_sha256": reader.content_sha256(),
                "station_count": reader.station_configs().len(),
                "frame_index_entries": reader.iter_frame_index()?.collect_all()?.len(),
                "truth_frames": reader.iter_truth_frames()?.collect_all()?.len(),
                "virtual_rays": reader.iter_virtual_rays()?.collect_all()?.len(),
                "videos_verified": verify_videos,
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
    }
    Ok(())
}
