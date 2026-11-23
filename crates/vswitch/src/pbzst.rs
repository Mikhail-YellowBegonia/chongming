//! Multi-frame Zstandard stream writer and cross-frame reader for `.pb.zst` files.
//!
//! Binary-compatible with `src/chongming_proto/framing.py` (`PbZstWriter` and `iter_pb_zst`).

use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, Write};
use std::path::{Path, PathBuf};

use prost::Message;
use thiserror::Error;
use zstd::stream::read::Decoder as ZstdDecoder;
use zstd::stream::write::Encoder as ZstdEncoder;

use crate::framing::{
    encode_length_prefixed, read_length_prefixed_sync, FramingError, MAX_FRAME_BYTES,
};

pub const DEFAULT_ZSTD_LEVEL: i32 = 3;
pub const DEFAULT_FLUSH_EVERY: usize = 256;

#[derive(Debug, Error)]
pub enum PbZstError {
    #[error("flush_every must be at least 1, got {0}")]
    InvalidFlushEvery(usize),
    #[error("writer is already closed")]
    Closed,
    #[error("framing error: {0}")]
    Framing(#[from] FramingError),
    #[error("I/O error on {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
}

enum WriterState {
    Idle(File),
    Active(ZstdEncoder<'static, File>),
    Closed,
}

/// Multi-frame `.pb.zst` writer that closes and flushes each zstd frame every `flush_every` messages.
pub struct PbZstWriter {
    path: PathBuf,
    level: i32,
    flush_every: usize,
    max_bytes: usize,
    state: WriterState,
    pending_in_frame: usize,
    count: u64,
}

impl PbZstWriter {
    /// Create a `.pb.zst` writer. Refuses to overwrite existing files when `overwrite` is false.
    pub fn create(
        path: impl AsRef<Path>,
        level: i32,
        flush_every: usize,
        overwrite: bool,
        max_bytes: usize,
    ) -> Result<Self, PbZstError> {
        if flush_every == 0 {
            return Err(PbZstError::InvalidFlushEvery(flush_every));
        }
        if max_bytes == 0 || max_bytes > MAX_FRAME_BYTES {
            return Err(PbZstError::Framing(FramingError::InvalidMaxBytes(
                max_bytes,
            )));
        }
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|source| PbZstError::Io {
                    path: path.clone(),
                    source,
                })?;
            }
        }
        let mut opts = OpenOptions::new();
        opts.write(true);
        if overwrite {
            opts.create(true).truncate(true);
        } else {
            opts.create_new(true);
        }
        let file = opts.open(&path).map_err(|source| PbZstError::Io {
            path: path.clone(),
            source,
        })?;
        Ok(Self {
            path,
            level,
            flush_every,
            max_bytes,
            state: WriterState::Idle(file),
            pending_in_frame: 0,
            count: 0,
        })
    }

    pub fn with_defaults(path: impl AsRef<Path>) -> Result<Self, PbZstError> {
        Self::create(
            path,
            DEFAULT_ZSTD_LEVEL,
            DEFAULT_FLUSH_EVERY,
            false,
            MAX_FRAME_BYTES,
        )
    }

    pub fn count(&self) -> u64 {
        self.count
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Encode and write one Protobuf message, flushing the zstd frame every `flush_every` messages.
    pub fn write<M: Message>(&mut self, message: &M) -> Result<(), PbZstError> {
        let frame = encode_length_prefixed(message, self.max_bytes)?;
        self.ensure_active()?;
        match &mut self.state {
            WriterState::Active(encoder) => {
                encoder.write_all(&frame).map_err(|source| PbZstError::Io {
                    path: self.path.clone(),
                    source,
                })?;
            }
            WriterState::Idle(_) | WriterState::Closed => return Err(PbZstError::Closed),
        }
        self.count += 1;
        self.pending_in_frame += 1;
        if self.pending_in_frame >= self.flush_every {
            self.flush_frame()?;
        }
        Ok(())
    }

    fn ensure_active(&mut self) -> Result<(), PbZstError> {
        let prev = std::mem::replace(&mut self.state, WriterState::Closed);
        match prev {
            WriterState::Active(enc) => {
                self.state = WriterState::Active(enc);
                Ok(())
            }
            WriterState::Idle(file) => {
                let encoder =
                    ZstdEncoder::new(file, self.level).map_err(|source| PbZstError::Io {
                        path: self.path.clone(),
                        source,
                    })?;
                self.state = WriterState::Active(encoder);
                self.pending_in_frame = 0;
                Ok(())
            }
            WriterState::Closed => Err(PbZstError::Closed),
        }
    }

    /// Close the active zstd frame and flush OS buffers so earlier messages survive crashes.
    pub fn flush_frame(&mut self) -> Result<(), PbZstError> {
        let prev = std::mem::replace(&mut self.state, WriterState::Closed);
        match prev {
            WriterState::Active(encoder) => {
                let mut file = encoder.finish().map_err(|source| PbZstError::Io {
                    path: self.path.clone(),
                    source,
                })?;
                file.flush().map_err(|source| PbZstError::Io {
                    path: self.path.clone(),
                    source,
                })?;
                self.state = WriterState::Idle(file);
                self.pending_in_frame = 0;
                Ok(())
            }
            WriterState::Idle(mut file) => {
                file.flush().map_err(|source| PbZstError::Io {
                    path: self.path.clone(),
                    source,
                })?;
                self.state = WriterState::Idle(file);
                Ok(())
            }
            WriterState::Closed => Err(PbZstError::Closed),
        }
    }

    /// Flush any open zstd frame and close the underlying file.
    pub fn close(mut self) -> Result<(), PbZstError> {
        self.close_inner()
    }

    fn close_inner(&mut self) -> Result<(), PbZstError> {
        if matches!(self.state, WriterState::Closed) {
            return Ok(());
        }
        let res = self.flush_frame();
        let prev = std::mem::replace(&mut self.state, WriterState::Closed);
        drop(prev);
        res
    }
}

impl Drop for PbZstWriter {
    fn drop(&mut self) {
        let _ = self.close_inner();
    }
}

/// Streaming reader across all zstd frames in a `.pb.zst` file.
pub struct PbZstReader<M> {
    path: PathBuf,
    decoder: Option<ZstdDecoder<'static, BufReader<File>>>,
    max_bytes: usize,
    _marker: std::marker::PhantomData<M>,
}

impl<M: Message + Default> PbZstReader<M> {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, PbZstError> {
        Self::open_with_max_bytes(path, MAX_FRAME_BYTES)
    }

    pub fn open_with_max_bytes(
        path: impl AsRef<Path>,
        max_bytes: usize,
    ) -> Result<Self, PbZstError> {
        if max_bytes == 0 || max_bytes > MAX_FRAME_BYTES {
            return Err(PbZstError::Framing(FramingError::InvalidMaxBytes(
                max_bytes,
            )));
        }
        let path = path.as_ref().to_path_buf();
        let file = File::open(&path).map_err(|source| PbZstError::Io {
            path: path.clone(),
            source,
        })?;
        let file_len = file
            .metadata()
            .map_err(|source| PbZstError::Io {
                path: path.clone(),
                source,
            })?
            .len();
        let decoder = if file_len == 0 {
            None
        } else {
            Some(ZstdDecoder::new(file).map_err(|source| PbZstError::Io {
                path: path.clone(),
                source,
            })?)
        };
        Ok(Self {
            path,
            decoder,
            max_bytes,
            _marker: std::marker::PhantomData,
        })
    }

    /// Read the next message, or `Ok(None)` at clean EOF.
    pub fn next_message(&mut self) -> Result<Option<M>, PbZstError> {
        let Some(decoder) = &mut self.decoder else {
            return Ok(None);
        };
        read_length_prefixed_sync(decoder, self.max_bytes).map_err(PbZstError::Framing)
    }

    /// Collect all remaining messages into a vector.
    pub fn collect_all(mut self) -> Result<Vec<M>, PbZstError> {
        let mut items = Vec::new();
        while let Some(item) = self.next_message()? {
            items.push(item);
        }
        Ok(items)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl<M: Message + Default> Iterator for PbZstReader<M> {
    type Item = Result<M, PbZstError>;

    fn next(&mut self) -> Option<Self::Item> {
        match self.next_message() {
            Ok(Some(msg)) => Some(Ok(msg)),
            Ok(None) => None,
            Err(err) => Some(Err(err)),
        }
    }
}

/// Convenience helper matching Python's `iter_pb_zst`.
pub fn iter_pb_zst<M: Message + Default>(
    path: impl AsRef<Path>,
) -> Result<PbZstReader<M>, PbZstError> {
    PbZstReader::open(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::BusMessage;
    use tempfile::TempDir;

    #[test]
    fn multiframe_roundtrip_overwrite_refusal_and_crash_flush() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("stream.pb.zst");

        {
            let mut writer = PbZstWriter::create(&path, 3, 2, false, MAX_FRAME_BYTES).unwrap();
            for idx in 0..5u64 {
                writer
                    .write(&BusMessage {
                        bus_sequence_no: idx + 1,
                        sent_wall_time_ns: 1_000_000_000_000_000 + idx as i64,
                        payload: None,
                    })
                    .unwrap();
            }
            writer.close().unwrap();
        }

        let loaded: Vec<BusMessage> = iter_pb_zst(&path)
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        let seqs: Vec<u64> = loaded.iter().map(|m| m.bus_sequence_no).collect();
        assert_eq!(seqs, vec![1, 2, 3, 4, 5]);

        // Overwrite refusal
        assert!(PbZstWriter::create(&path, 3, 2, false, MAX_FRAME_BYTES).is_err());

        // Crash-flush recovery: read already-flushed frame while writer remains open
        let partial_path = dir.path().join("partial.pb.zst");
        let mut writer = PbZstWriter::create(&partial_path, 3, 10, false, MAX_FRAME_BYTES).unwrap();
        writer
            .write(&BusMessage {
                bus_sequence_no: 10,
                ..Default::default()
            })
            .unwrap();
        writer
            .write(&BusMessage {
                bus_sequence_no: 20,
                ..Default::default()
            })
            .unwrap();
        writer.flush_frame().unwrap();

        let flushed: Vec<u64> = iter_pb_zst::<BusMessage>(&partial_path)
            .unwrap()
            .map(|r| r.unwrap().bus_sequence_no)
            .collect();
        assert_eq!(flushed, vec![10, 20]);

        // Write one more message and close; full stream should have all 3
        writer
            .write(&BusMessage {
                bus_sequence_no: 30,
                ..Default::default()
            })
            .unwrap();
        writer.close().unwrap();

        let all: Vec<u64> = iter_pb_zst::<BusMessage>(&partial_path)
            .unwrap()
            .map(|r| r.unwrap().bus_sequence_no)
            .collect();
        assert_eq!(all, vec![10, 20, 30]);

        // Empty 0-message .pb.zst file reads cleanly as 0 items
        let empty_path = dir.path().join("empty.pb.zst");
        let empty_writer = PbZstWriter::with_defaults(&empty_path).unwrap();
        empty_writer.close().unwrap();
        let empty_items = iter_pb_zst::<BusMessage>(&empty_path)
            .unwrap()
            .collect_all()
            .unwrap();
        assert!(empty_items.is_empty());
    }
}
