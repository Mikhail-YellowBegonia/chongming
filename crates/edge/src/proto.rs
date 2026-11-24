#[allow(clippy::all)]
pub mod chongming {
    pub mod v1 {
        include!(concat!(env!("OUT_DIR"), "/chongming.v1.rs"));
    }
}

use std::time::{SystemTime, UNIX_EPOCH};

pub use chongming::v1::*;

/// Current wall-clock timestamp in nanoseconds since Unix epoch.
pub fn now_wall_time_ns() -> i64 {
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    i64::try_from(dur.as_nanos()).unwrap_or(i64::MAX)
}
