use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Result};
use clap::Parser;

use edge::bus::OnlineBusClient;
use edge::device::{EdgeProcess, WARMUP_HOLD_NS};
use edge::frame_source::VirtualBusFrameSource;
use edge::policy::load_device_policy_map;

#[derive(Debug, Parser)]
#[command(
    name = "edge",
    about = "Run standalone Rust Edge CV pipeline connected to V-Switch over TCP."
)]
struct Cli {
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    #[arg(long)]
    port: u16,
    #[arg(long)]
    policy: PathBuf,
    #[arg(long, default_value = "edge-1")]
    instance_id: String,
    #[arg(long)]
    session_id: Option<String>,
    #[arg(long = "station-id")]
    station_ids: Vec<String>,
    #[arg(long, default_value_t = (WARMUP_HOLD_NS as f64) / 1e9)]
    warmup_hold_s: f64,
    #[arg(long, default_value_t = 30.0)]
    timeout_s: f64,
    #[arg(long)]
    ffmpeg: Option<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    if !cli.warmup_hold_s.is_finite() || cli.warmup_hold_s < 0.0 {
        bail!("--warmup-hold-s must be finite and non-negative");
    }
    if !cli.timeout_s.is_finite() || cli.timeout_s <= 0.0 {
        bail!("--timeout-s must be finite and positive");
    }

    let policies = load_device_policy_map(&cli.policy)?;
    let station_ids = if cli.station_ids.is_empty() {
        policies.keys().cloned().collect()
    } else {
        cli.station_ids
    };
    let session_id = cli.session_id.unwrap_or_else(|| {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        format!("edge-{:x}", nanos & 0xffff_ffff_ffff)
    });
    let timeout_dur = Duration::from_secs_f64(cli.timeout_s);
    let warmup_hold_ns = (cli.warmup_hold_s * 1e9).round() as i64;

    let mut client = OnlineBusClient::connect(&cli.host, cli.port, timeout_dur).await?;
    client
        .handshake(&cli.instance_id, &session_id, &station_ids)
        .await?;
    let (reader, writer) = client.into_split();
    let source = VirtualBusFrameSource::new(reader, timeout_dur, cli.ffmpeg);
    let mut process = EdgeProcess::new(writer, source, policies, warmup_hold_ns);
    process.run().await?;
    Ok(())
}
