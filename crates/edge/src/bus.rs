//! Async TCP client for V-Switch `BusMessage` communication.

use std::io;
use std::time::Duration;

use thiserror::Error;
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::TcpStream;
use tokio::time::timeout;

use crate::framing::{
    read_length_prefixed_async, write_length_prefixed_async, FramingError, MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
};
use crate::proto::{
    bus_message, now_wall_time_ns, BusMessage, ComponentRole, SessionAck, SessionHello,
};

#[derive(Debug, Error)]
pub enum BusClientError {
    #[error("framing error: {0}")]
    Framing(#[from] FramingError),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error("bus operation timed out after {0:?}")]
    Timeout(Duration),
    #[error("session handshake rejected: {0}")]
    HandshakeRejected(String),
    #[error("unexpected bus message during handshake: {0}")]
    UnexpectedMessage(String),
}

pub struct OnlineBusClient {
    reader: OwnedReadHalf,
    writer: OwnedWriteHalf,
    timeout: Duration,
    next_seq: u64,
}

impl OnlineBusClient {
    pub async fn connect(
        host: &str,
        port: u16,
        timeout_dur: Duration,
    ) -> Result<Self, BusClientError> {
        let addr = format!("{host}:{port}");
        let stream = timeout(timeout_dur, TcpStream::connect(&addr))
            .await
            .map_err(|_| BusClientError::Timeout(timeout_dur))??;
        let _ = stream.set_nodelay(true);
        let (reader, writer) = stream.into_split();
        Ok(Self {
            reader,
            writer,
            timeout: timeout_dur,
            next_seq: 1,
        })
    }

    pub fn into_parts(self) -> (OwnedReadHalf, OwnedWriteHalf, Duration, u64) {
        (self.reader, self.writer, self.timeout, self.next_seq)
    }

    pub async fn handshake(
        &mut self,
        instance_id: &str,
        session_id: &str,
        station_ids: &[String],
    ) -> Result<SessionAck, BusClientError> {
        let hello = bus_message::Payload::SessionHello(SessionHello {
            protocol_version: PROTOCOL_VERSION.to_string(),
            role: ComponentRole::Edge as i32,
            instance_id: instance_id.to_string(),
            session_id: session_id.to_string(),
            station_ids: station_ids.to_vec(),
        });
        self.send_payload(hello).await?;
        let Some(reply) = self.receive().await? else {
            return Err(BusClientError::HandshakeRejected(
                "connection closed before SessionAck".to_string(),
            ));
        };
        match reply.payload {
            Some(bus_message::Payload::SessionAck(ack)) => {
                if !ack.accepted {
                    return Err(BusClientError::HandshakeRejected(ack.reason));
                }
                Ok(ack)
            }
            other => Err(BusClientError::UnexpectedMessage(format!("{other:?}"))),
        }
    }

    pub async fn send_payload(
        &mut self,
        payload: bus_message::Payload,
    ) -> Result<(), BusClientError> {
        let seq = self.next_seq;
        self.next_seq += 1;
        let msg = BusMessage {
            bus_sequence_no: seq,
            sent_wall_time_ns: now_wall_time_ns(),
            payload: Some(payload),
        };
        timeout(
            self.timeout,
            write_length_prefixed_async(&mut self.writer, &msg, MAX_FRAME_BYTES),
        )
        .await
        .map_err(|_| BusClientError::Timeout(self.timeout))??;
        Ok(())
    }

    pub async fn receive(&mut self) -> Result<Option<BusMessage>, BusClientError> {
        let msg = timeout(
            self.timeout,
            read_length_prefixed_async(&mut self.reader, MAX_FRAME_BYTES),
        )
        .await
        .map_err(|_| BusClientError::Timeout(self.timeout))??;
        Ok(msg)
    }
}
