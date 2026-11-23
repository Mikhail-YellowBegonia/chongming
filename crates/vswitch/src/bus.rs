//! Online TCP bus (`BusMessage`) and Test-Framework channel (`TestFrameworkMessage`)
//! with `SessionHello` / `SessionAck` handshake validation.

use std::collections::HashSet;
use std::io;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use thiserror::Error;
use tokio::net::tcp::OwnedWriteHalf;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::timeout;

use crate::framing::{
    read_length_prefixed_async, write_length_prefixed_async, FramingError, MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
};
use crate::proto::{
    bus_message, now_wall_time_ns, BusMessage, ComponentRole, SessionAck, SessionHello,
    TestFrameworkMessage,
};

pub const DEFAULT_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(5);
pub const DEFAULT_PEER_QUEUE_CAPACITY: usize = 1024;

#[derive(Debug, Error)]
pub enum BusError {
    #[error("framing error: {0}")]
    Framing(#[from] FramingError),
    #[error("handshake timed out")]
    HandshakeTimeout,
    #[error("handshake failed: {0}")]
    HandshakeRejected(String),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

#[derive(Debug, Clone)]
pub struct PeerInfo {
    pub peer_id: u64,
    pub role: ComponentRole,
    pub instance_id: String,
    pub session_id: String,
    pub station_ids: Vec<String>,
    pub remote_addr: SocketAddr,
}

#[derive(Debug)]
pub enum BusEvent {
    PeerConnected(PeerHandle),
    Message {
        peer_id: u64,
        role: ComponentRole,
        instance_id: String,
        message: Box<BusMessage>,
    },
    PeerDisconnected {
        peer_id: u64,
        role: ComponentRole,
        instance_id: String,
    },
}

#[derive(Debug, Error)]
#[error("peer outbound queue closed or full")]
pub struct PeerSendError;

#[derive(Debug, Clone)]
pub struct PeerHandle {
    pub info: PeerInfo,
    tx: mpsc::Sender<BusMessage>,
}

impl PeerHandle {
    pub async fn send(&self, message: BusMessage) -> Result<(), PeerSendError> {
        self.tx.send(message).await.map_err(|_| PeerSendError)
    }

    pub fn try_send(&self, message: BusMessage) -> Result<(), PeerSendError> {
        self.tx.try_send(message).map_err(|_| PeerSendError)
    }
}

/// Validate an incoming `SessionHello` against protocol version, allowed roles, and required identity fields.
pub fn validate_session_hello(
    hello: &SessionHello,
    allowed_roles: &[ComponentRole],
) -> Result<ComponentRole, String> {
    if hello.protocol_version != PROTOCOL_VERSION {
        return Err(format!(
            "unsupported protocol_version {:?}, expected {:?}",
            hello.protocol_version, PROTOCOL_VERSION
        ));
    }
    let role = ComponentRole::try_from(hello.role).unwrap_or(ComponentRole::Unspecified);
    if role == ComponentRole::Unspecified {
        return Err("role must not be COMPONENT_ROLE_UNSPECIFIED".to_string());
    }
    if role == ComponentRole::Viewer {
        return Err("viewer must connect to the viewer push listener, not the online bus".into());
    }
    if !allowed_roles.is_empty() && !allowed_roles.contains(&role) {
        return Err(format!("role {role:?} is not allowed in this hub mode"));
    }
    if hello.instance_id.trim().is_empty() {
        return Err("instance_id must be non-empty".to_string());
    }
    if hello.session_id.trim().is_empty() {
        return Err("session_id must be non-empty".to_string());
    }
    if role == ComponentRole::Edge && hello.station_ids.is_empty() {
        return Err("EDGE peer must declare at least one station_id".to_string());
    }
    let mut seen = HashSet::new();
    for st in &hello.station_ids {
        if st.trim().is_empty() || !seen.insert(st.as_str()) {
            return Err(format!("invalid or duplicate station_id {st:?}"));
        }
    }
    Ok(role)
}

pub struct BusServer {
    local_addr: SocketAddr,
    accept_handle: JoinHandle<()>,
}

impl BusServer {
    pub async fn bind(
        addr: SocketAddr,
        run_id: String,
        allowed_roles: Vec<ComponentRole>,
        event_tx: mpsc::Sender<BusEvent>,
    ) -> io::Result<Self> {
        let listener = TcpListener::bind(addr).await?;
        let local_addr = listener.local_addr()?;
        let next_peer_id = Arc::new(AtomicU64::new(1));
        let next_seq = Arc::new(AtomicU64::new(1));
        let allowed_roles = Arc::new(allowed_roles);

        let accept_handle = tokio::spawn(async move {
            while let Ok((stream, remote_addr)) = listener.accept().await {
                let _ = stream.set_nodelay(true);
                let peer_id = next_peer_id.fetch_add(1, Ordering::Relaxed);
                let run_id = run_id.clone();
                let allowed_roles = Arc::clone(&allowed_roles);
                let event_tx = event_tx.clone();
                let next_seq = Arc::clone(&next_seq);
                tokio::spawn(async move {
                    handle_bus_connection(
                        stream,
                        remote_addr,
                        peer_id,
                        run_id,
                        allowed_roles,
                        event_tx,
                        next_seq,
                    )
                    .await;
                });
            }
        });

        Ok(Self {
            local_addr,
            accept_handle,
        })
    }

    pub fn local_addr(&self) -> SocketAddr {
        self.local_addr
    }

    pub fn shutdown(self) {
        self.accept_handle.abort();
    }
}

async fn handle_bus_connection(
    mut stream: TcpStream,
    remote_addr: SocketAddr,
    peer_id: u64,
    run_id: String,
    allowed_roles: Arc<Vec<ComponentRole>>,
    event_tx: mpsc::Sender<BusEvent>,
    next_seq: Arc<AtomicU64>,
) {
    let first_msg = match timeout(
        DEFAULT_HANDSHAKE_TIMEOUT,
        read_length_prefixed_async::<_, BusMessage>(&mut stream, MAX_FRAME_BYTES),
    )
    .await
    {
        Ok(Ok(Some(msg))) => msg,
        _ => return,
    };

    let hello = match first_msg.payload {
        Some(bus_message::Payload::SessionHello(h)) => h,
        _ => {
            let ack = BusMessage {
                bus_sequence_no: next_seq.fetch_add(1, Ordering::Relaxed),
                sent_wall_time_ns: now_wall_time_ns(),
                payload: Some(bus_message::Payload::SessionAck(SessionAck {
                    accepted: false,
                    reason: "first message must be SessionHello".into(),
                    run_id,
                })),
            };
            let _ = write_length_prefixed_async(&mut stream, &ack, MAX_FRAME_BYTES).await;
            return;
        }
    };

    let role = match validate_session_hello(&hello, &allowed_roles) {
        Ok(role) => role,
        Err(reason) => {
            let ack = BusMessage {
                bus_sequence_no: next_seq.fetch_add(1, Ordering::Relaxed),
                sent_wall_time_ns: now_wall_time_ns(),
                payload: Some(bus_message::Payload::SessionAck(SessionAck {
                    accepted: false,
                    reason,
                    run_id,
                })),
            };
            let _ = write_length_prefixed_async(&mut stream, &ack, MAX_FRAME_BYTES).await;
            return;
        }
    };

    let ack = BusMessage {
        bus_sequence_no: next_seq.fetch_add(1, Ordering::Relaxed),
        sent_wall_time_ns: now_wall_time_ns(),
        payload: Some(bus_message::Payload::SessionAck(SessionAck {
            accepted: true,
            reason: "ok".into(),
            run_id,
        })),
    };
    if write_length_prefixed_async(&mut stream, &ack, MAX_FRAME_BYTES)
        .await
        .is_err()
    {
        return;
    }

    let info = PeerInfo {
        peer_id,
        role,
        instance_id: hello.instance_id,
        session_id: hello.session_id,
        station_ids: hello.station_ids,
        remote_addr,
    };
    let (out_tx, mut out_rx) = mpsc::channel::<BusMessage>(DEFAULT_PEER_QUEUE_CAPACITY);
    let handle = PeerHandle {
        info: info.clone(),
        tx: out_tx,
    };
    if event_tx
        .send(BusEvent::PeerConnected(handle))
        .await
        .is_err()
    {
        return;
    }

    let (mut reader, mut writer) = stream.into_split();
    let writer_task = tokio::spawn(async move {
        while let Some(msg) = out_rx.recv().await {
            if write_length_prefixed_async(&mut writer, &msg, MAX_FRAME_BYTES)
                .await
                .is_err()
            {
                break;
            }
        }
    });

    while let Ok(Some(message)) =
        read_length_prefixed_async::<_, BusMessage>(&mut reader, MAX_FRAME_BYTES).await
    {
        if event_tx
            .send(BusEvent::Message {
                peer_id,
                role: info.role,
                instance_id: info.instance_id.clone(),
                message: Box::new(message),
            })
            .await
            .is_err()
        {
            break;
        }
    }

    writer_task.abort();
    let _ = event_tx
        .send(BusEvent::PeerDisconnected {
            peer_id,
            role: info.role,
            instance_id: info.instance_id,
        })
        .await;
}

// --- TestFrameworkMessage TCP server (Scorer TruthFrame/ScoreEvent & Producer TruthFrame) ---

#[derive(Debug)]
pub enum TruthEvent {
    PeerConnected(TruthPeerHandle),
    Message {
        peer_id: u64,
        message: TestFrameworkMessage,
    },
    PeerDisconnected {
        peer_id: u64,
    },
}

#[derive(Debug, Clone)]
pub struct TruthPeerHandle {
    pub peer_id: u64,
    pub remote_addr: SocketAddr,
    tx: mpsc::Sender<TestFrameworkMessage>,
}

impl TruthPeerHandle {
    pub async fn send(&self, message: TestFrameworkMessage) -> Result<(), PeerSendError> {
        self.tx.send(message).await.map_err(|_| PeerSendError)
    }
}

pub struct TruthServer {
    local_addr: SocketAddr,
    accept_handle: JoinHandle<()>,
}

impl TruthServer {
    pub async fn bind(addr: SocketAddr, event_tx: mpsc::Sender<TruthEvent>) -> io::Result<Self> {
        let listener = TcpListener::bind(addr).await?;
        let local_addr = listener.local_addr()?;
        let next_peer_id = Arc::new(AtomicU64::new(1));

        let accept_handle = tokio::spawn(async move {
            while let Ok((stream, remote_addr)) = listener.accept().await {
                let _ = stream.set_nodelay(true);
                let peer_id = next_peer_id.fetch_add(1, Ordering::Relaxed);
                let event_tx = event_tx.clone();
                tokio::spawn(async move {
                    handle_truth_connection(stream, remote_addr, peer_id, event_tx).await;
                });
            }
        });

        Ok(Self {
            local_addr,
            accept_handle,
        })
    }

    pub fn local_addr(&self) -> SocketAddr {
        self.local_addr
    }

    pub fn shutdown(self) {
        self.accept_handle.abort();
    }
}

async fn handle_truth_connection(
    stream: TcpStream,
    remote_addr: SocketAddr,
    peer_id: u64,
    event_tx: mpsc::Sender<TruthEvent>,
) {
    let (mut reader, writer) = stream.into_split();
    let (out_tx, out_rx) = mpsc::channel::<TestFrameworkMessage>(DEFAULT_PEER_QUEUE_CAPACITY);
    let handle = TruthPeerHandle {
        peer_id,
        remote_addr,
        tx: out_tx,
    };
    if event_tx
        .send(TruthEvent::PeerConnected(handle))
        .await
        .is_err()
    {
        return;
    }

    let writer_task = tokio::spawn(run_truth_writer(writer, out_rx));
    while let Ok(Some(message)) =
        read_length_prefixed_async::<_, TestFrameworkMessage>(&mut reader, MAX_FRAME_BYTES).await
    {
        if event_tx
            .send(TruthEvent::Message { peer_id, message })
            .await
            .is_err()
        {
            break;
        }
    }
    writer_task.abort();
    let _ = event_tx
        .send(TruthEvent::PeerDisconnected { peer_id })
        .await;
}

async fn run_truth_writer(
    mut writer: OwnedWriteHalf,
    mut out_rx: mpsc::Receiver<TestFrameworkMessage>,
) {
    while let Some(msg) = out_rx.recv().await {
        if write_length_prefixed_async(&mut writer, &msg, MAX_FRAME_BYTES)
            .await
            .is_err()
        {
            break;
        }
    }
}

/// Client handshake helper for tests and tooling.
pub async fn connect_bus_client(
    addr: SocketAddr,
    hello: SessionHello,
) -> Result<(TcpStream, SessionAck), BusError> {
    let mut stream = TcpStream::connect(addr).await?;
    let _ = stream.set_nodelay(true);
    let msg = BusMessage {
        bus_sequence_no: 1,
        sent_wall_time_ns: now_wall_time_ns(),
        payload: Some(bus_message::Payload::SessionHello(hello)),
    };
    write_length_prefixed_async(&mut stream, &msg, MAX_FRAME_BYTES).await?;
    let reply = read_length_prefixed_async::<_, BusMessage>(&mut stream, MAX_FRAME_BYTES)
        .await?
        .ok_or_else(|| BusError::HandshakeRejected("connection closed before SessionAck".into()))?;
    match reply.payload {
        Some(bus_message::Payload::SessionAck(ack)) => {
            if ack.accepted {
                Ok((stream, ack))
            } else {
                Err(BusError::HandshakeRejected(ack.reason))
            }
        }
        _ => Err(BusError::HandshakeRejected(
            "expected SessionAck response".into(),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn handshake_accepts_valid_and_rejects_invalid_version() {
        let (tx, mut rx) = mpsc::channel(16);
        let server = BusServer::bind(
            "127.0.0.1:0".parse().unwrap(),
            "run-1".into(),
            vec![ComponentRole::Edge],
            tx,
        )
        .await
        .unwrap();
        let addr = server.local_addr();

        let bad = connect_bus_client(
            addr,
            SessionHello {
                protocol_version: "wrong.v0".into(),
                role: ComponentRole::Edge as i32,
                instance_id: "e1".into(),
                session_id: "s1".into(),
                station_ids: vec!["st1".into()],
            },
        )
        .await;
        assert!(matches!(bad, Err(BusError::HandshakeRejected(_))));

        let (stream, ack) = connect_bus_client(
            addr,
            SessionHello {
                protocol_version: PROTOCOL_VERSION.into(),
                role: ComponentRole::Edge as i32,
                instance_id: "e1".into(),
                session_id: "s1".into(),
                station_ids: vec!["st1".into()],
            },
        )
        .await
        .unwrap();
        assert!(ack.accepted);
        assert_eq!(ack.run_id, "run-1");

        match rx.recv().await.unwrap() {
            BusEvent::PeerConnected(peer) => {
                assert_eq!(peer.info.instance_id, "e1");
                assert_eq!(peer.info.station_ids, vec!["st1"]);
            }
            other => panic!("unexpected event: {other:?}"),
        }
        drop(stream);
        server.shutdown();
    }
}
