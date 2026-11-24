//! 4-byte big-endian unsigned length-prefixed Protobuf wire framing.

use std::io;

use prost::Message;
use thiserror::Error;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

/// Maximum single frame payload allowed on the wire (64 MiB).
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
