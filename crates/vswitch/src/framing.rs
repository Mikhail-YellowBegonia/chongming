//! 4-byte big-endian unsigned length-prefixed Protobuf wire framing.
//!
//! Matches `src/chongming_proto/framing.py` (`MAX_FRAME_BYTES = 64 MiB`).

use std::io::{self, Read, Write};

use prost::Message;
use thiserror::Error;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

/// Maximum single frame payload allowed on the wire or in `.pb.zst` streams (64 MiB).
pub const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;

/// Wire protocol version checked during `SessionHello` / `SessionAck`.
pub const PROTOCOL_VERSION: &str = "chongming.v1";

#[derive(Debug, Error)]
pub enum FramingError {
    #[error("invalid max_bytes {0}: must be in 1..={MAX_FRAME_BYTES}")]
    InvalidMaxBytes(usize),
    #[error("frame payload size {len} exceeds max_bytes limit {max_bytes}")]
    FrameTooLarge { len: usize, max_bytes: usize },
    #[error("truncated frame: {0}")]
    Truncated(String),
    #[error("protobuf decode error: {0}")]
    Decode(#[from] prost::DecodeError),
    #[error("protobuf encode error: {0}")]
    Encode(#[from] prost::EncodeError),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

fn validate_max_bytes(max_bytes: usize) -> Result<(), FramingError> {
    if max_bytes == 0 || max_bytes > MAX_FRAME_BYTES {
        return Err(FramingError::InvalidMaxBytes(max_bytes));
    }
    Ok(())
}

/// Encode raw payload bytes with a 4-byte big-endian unsigned length prefix.
pub fn encode_raw_frame(payload: &[u8], max_bytes: usize) -> Result<Vec<u8>, FramingError> {
    validate_max_bytes(max_bytes)?;
    if payload.len() > max_bytes {
        return Err(FramingError::FrameTooLarge {
            len: payload.len(),
            max_bytes,
        });
    }
    let len_u32 = u32::try_from(payload.len()).map_err(|_| FramingError::FrameTooLarge {
        len: payload.len(),
        max_bytes,
    })?;
    let mut out = Vec::with_capacity(4 + payload.len());
    out.extend_from_slice(&len_u32.to_be_bytes());
    out.extend_from_slice(payload);
    Ok(out)
}

/// Encode a Protobuf message with a 4-byte big-endian unsigned length prefix.
pub fn encode_length_prefixed<M: Message>(
    message: &M,
    max_bytes: usize,
) -> Result<Vec<u8>, FramingError> {
    validate_max_bytes(max_bytes)?;
    let encoded_len = message.encoded_len();
    if encoded_len > max_bytes {
        return Err(FramingError::FrameTooLarge {
            len: encoded_len,
            max_bytes,
        });
    }
    let len_u32 = u32::try_from(encoded_len).map_err(|_| FramingError::FrameTooLarge {
        len: encoded_len,
        max_bytes,
    })?;
    let mut buf = Vec::with_capacity(4 + encoded_len);
    buf.extend_from_slice(&len_u32.to_be_bytes());
    message.encode(&mut buf)?;
    Ok(buf)
}

/// Read exact `buf.len()` bytes from a synchronous reader, returning `Ok(0)` only on clean EOF at byte 0.
fn read_exact_or_eof_sync<R: Read>(reader: &mut R, buf: &mut [u8]) -> io::Result<bool> {
    let mut read_total = 0usize;
    while read_total < buf.len() {
        match reader.read(&mut buf[read_total..]) {
            Ok(0) => {
                if read_total == 0 {
                    return Ok(false);
                }
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    format!(
                        "truncated read: expected {} bytes, got {read_total}",
                        buf.len()
                    ),
                ));
            }
            Ok(n) => read_total += n,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        }
    }
    Ok(true)
}

/// Synchronously read one length-prefixed Protobuf message; returns `Ok(None)` on clean EOF.
pub fn read_length_prefixed_sync<R: Read, M: Message + Default>(
    reader: &mut R,
    max_bytes: usize,
) -> Result<Option<M>, FramingError> {
    validate_max_bytes(max_bytes)?;
    let mut header = [0u8; 4];
    let has_header = match read_exact_or_eof_sync(reader, &mut header) {
        Ok(has) => has,
        Err(err) if err.kind() == io::ErrorKind::UnexpectedEof => {
            return Err(FramingError::Truncated(
                "truncated frame length header".to_string(),
            ));
        }
        Err(err) => return Err(FramingError::Io(err)),
    };
    if !has_header {
        return Ok(None);
    }

    let length = u32::from_be_bytes(header) as usize;
    if length > max_bytes {
        return Err(FramingError::FrameTooLarge {
            len: length,
            max_bytes,
        });
    }

    let mut payload = vec![0u8; length];
    if length > 0 {
        reader.read_exact(&mut payload).map_err(|err| {
            if err.kind() == io::ErrorKind::UnexpectedEof {
                FramingError::Truncated(format!("truncated frame payload of {length} bytes"))
            } else {
                FramingError::Io(err)
            }
        })?;
    }
    let message = M::decode(payload.as_slice())?;
    Ok(Some(message))
}

/// Synchronously write one length-prefixed Protobuf message.
pub fn write_length_prefixed_sync<W: Write, M: Message>(
    writer: &mut W,
    message: &M,
    max_bytes: usize,
) -> Result<(), FramingError> {
    let bytes = encode_length_prefixed(message, max_bytes)?;
    writer.write_all(&bytes)?;
    Ok(())
}

/// Asynchronously read one length-prefixed raw frame; returns `Ok(None)` on clean EOF.
pub async fn read_raw_frame_async<R: AsyncRead + Unpin>(
    reader: &mut R,
    max_bytes: usize,
) -> Result<Option<Vec<u8>>, FramingError> {
    validate_max_bytes(max_bytes)?;
    let mut header = [0u8; 4];
    let mut read_total = 0usize;
    while read_total < 4 {
        let n = reader.read(&mut header[read_total..]).await?;
        if n == 0 {
            if read_total == 0 {
                return Ok(None);
            }
            return Err(FramingError::Truncated(
                "truncated frame length header".to_string(),
            ));
        }
        read_total += n;
    }

    let length = u32::from_be_bytes(header) as usize;
    if length > max_bytes {
        return Err(FramingError::FrameTooLarge {
            len: length,
            max_bytes,
        });
    }

    let mut payload = vec![0u8; length];
    if length > 0 {
        reader.read_exact(&mut payload).await.map_err(|err| {
            if err.kind() == io::ErrorKind::UnexpectedEof {
                FramingError::Truncated(format!("truncated frame payload of {length} bytes"))
            } else {
                FramingError::Io(err)
            }
        })?;
    }
    Ok(Some(payload))
}

/// Asynchronously write raw payload bytes with a 4-byte big-endian unsigned length prefix.
pub async fn write_raw_frame_async<W: AsyncWrite + Unpin>(
    writer: &mut W,
    payload: &[u8],
    max_bytes: usize,
) -> Result<(), FramingError> {
    let frame = encode_raw_frame(payload, max_bytes)?;
    writer.write_all(&frame).await?;
    writer.flush().await?;
    Ok(())
}

/// Asynchronously read one length-prefixed Protobuf message; returns `Ok(None)` on clean EOF.
pub async fn read_length_prefixed_async<R: AsyncRead + Unpin, M: Message + Default>(
    reader: &mut R,
    max_bytes: usize,
) -> Result<Option<M>, FramingError> {
    let Some(payload) = read_raw_frame_async(reader, max_bytes).await? else {
        return Ok(None);
    };
    let message = M::decode(payload.as_slice())?;
    Ok(Some(message))
}

/// Asynchronously write one length-prefixed Protobuf message.
pub async fn write_length_prefixed_async<W: AsyncWrite + Unpin, M: Message>(
    writer: &mut W,
    message: &M,
    max_bytes: usize,
) -> Result<(), FramingError> {
    let frame = encode_length_prefixed(message, max_bytes)?;
    writer.write_all(&frame).await?;
    writer.flush().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::{BusMessage, ComponentRole, ProgressReport};
    use std::io::Cursor;

    #[test]
    fn roundtrip_and_limits() {
        let msg = BusMessage {
            bus_sequence_no: 42,
            sent_wall_time_ns: 1_763_784_000_123_456_789,
            payload: Some(crate::proto::bus_message::Payload::ProgressReport(
                ProgressReport {
                    source_role: ComponentRole::Center as i32,
                    source_id: "center".to_string(),
                    session_id: "s-1".to_string(),
                    completed_time_ns: 9_007_199_254_740_993,
                    completed_sequence_no: 7,
                    finite_end_reached: true,
                },
            )),
        };

        let encoded = encode_length_prefixed(&msg, MAX_FRAME_BYTES).unwrap();
        let mut cursor = Cursor::new(encoded.as_slice());
        let decoded: BusMessage = read_length_prefixed_sync(&mut cursor, MAX_FRAME_BYTES)
            .unwrap()
            .unwrap();
        assert_eq!(decoded.bus_sequence_no, 42);
        assert_eq!(decoded.sent_wall_time_ns, 1_763_784_000_123_456_789);
        assert!(
            read_length_prefixed_sync::<_, BusMessage>(&mut cursor, MAX_FRAME_BYTES)
                .unwrap()
                .is_none()
        );

        assert!(matches!(
            encode_length_prefixed(&msg, 4),
            Err(FramingError::FrameTooLarge { .. })
        ));
        assert!(matches!(
            read_length_prefixed_sync::<_, BusMessage>(&mut Cursor::new(&encoded), 4),
            Err(FramingError::FrameTooLarge { .. })
        ));
        assert!(matches!(
            encode_length_prefixed(&msg, MAX_FRAME_BYTES + 1),
            Err(FramingError::InvalidMaxBytes(_))
        ));

        // 64 MiB boundary check on raw frame header
        let over_64mib_header = u32::try_from(MAX_FRAME_BYTES + 1)
            .unwrap()
            .to_be_bytes()
            .to_vec();
        assert!(matches!(
            read_length_prefixed_sync::<_, BusMessage>(
                &mut Cursor::new(over_64mib_header),
                MAX_FRAME_BYTES
            ),
            Err(FramingError::FrameTooLarge { .. })
        ));
    }

    #[test]
    fn rejects_truncated_and_corrupt_frames() {
        assert!(matches!(
            read_length_prefixed_sync::<_, BusMessage>(
                &mut Cursor::new(b"\x00\x00"),
                MAX_FRAME_BYTES
            ),
            Err(FramingError::Truncated(_))
        ));
        let mut partial_payload = 10u32.to_be_bytes().to_vec();
        partial_payload.extend_from_slice(b"abc");
        assert!(matches!(
            read_length_prefixed_sync::<_, BusMessage>(
                &mut Cursor::new(partial_payload),
                MAX_FRAME_BYTES
            ),
            Err(FramingError::Truncated(_))
        ));
        let mut bad_proto = 2u32.to_be_bytes().to_vec();
        bad_proto.extend_from_slice(&[0xff, 0xff]);
        assert!(matches!(
            read_length_prefixed_sync::<_, BusMessage>(
                &mut Cursor::new(bad_proto),
                MAX_FRAME_BYTES
            ),
            Err(FramingError::Decode(_))
        ));
    }
}
