class_name ProducerBusClient
extends RefCounted
## Pure GDScript TCP client for `Godot Producer -> V-Switch Record`.
## Manages both the online `BusMessage` stream (`--bus`) and the privileged
## `TestFrameworkMessage` truth stream (`--truth`) with 4-byte BE Protobuf framing.

const ProtoWire = preload("res://core/proto_wire.gd")

var _bus_peer: StreamPeerTCP
var _truth_peer: StreamPeerTCP
var _bus_seq: int = 1
var _truth_seq: int = 1
var _last_error: String = ""


func last_error() -> String:
	return _last_error


func is_connected_both() -> bool:
	return (
		_bus_peer != null
		and _truth_peer != null
		and _bus_peer.get_status() == StreamPeerTCP.STATUS_CONNECTED
		and _truth_peer.get_status() == StreamPeerTCP.STATUS_CONNECTED
	)


func connect_and_handshake(
	bus_host: String,
	bus_port: int,
	truth_host: String,
	truth_port: int,
	instance_id: String,
	session_id: String,
	station_ids: Array,
	timeout_ms: int = 10000
) -> bool:
	_last_error = ""
	_bus_seq = 1
	_truth_seq = 1

	_bus_peer = _connect_tcp(bus_host, bus_port, timeout_ms)
	if _bus_peer == null:
		return false

	var hello_bytes := ProtoWire.encode_session_hello(
		instance_id,
		session_id,
		station_ids,
		ProtoWire.COMPONENT_ROLE_PRODUCER
	)
	var bus_hello := ProtoWire.encode_bus_message(
		_next_bus_seq(),
		_wall_time_ns(),
		10,
		hello_bytes
	)
	if not _send_framed(_bus_peer, bus_hello, timeout_ms):
		return false
	var bus_ack_payload := _read_framed(_bus_peer, timeout_ms)
	if bus_ack_payload.is_empty():
		return false
	var bus_ack := ProtoWire.decode_bus_message_session_ack(bus_ack_payload)
	if bus_ack.has("error") or not bool(bus_ack.get("accepted", false)):
		_last_error = "Bus handshake rejected: %s" % JSON.stringify(bus_ack)
		return false

	_truth_peer = _connect_tcp(truth_host, truth_port, timeout_ms)
	if _truth_peer == null:
		return false

	var truth_hello_bus := ProtoWire.encode_bus_message(
		1,
		_wall_time_ns(),
		10,
		hello_bytes
	)
	var truth_hello := ProtoWire.encode_test_framework_message(
		_next_truth_seq(),
		12,
		truth_hello_bus
	)
	if not _send_framed(_truth_peer, truth_hello, timeout_ms):
		return false
	var truth_ack_payload := _read_framed(_truth_peer, timeout_ms)
	if truth_ack_payload.is_empty():
		return false
	var truth_ack := ProtoWire.decode_test_framework_session_ack(truth_ack_payload)
	if truth_ack.has("error") or not bool(truth_ack.get("accepted", false)):
		_last_error = "Truth handshake rejected: %s" % JSON.stringify(truth_ack)
		return false
	return true


func send_station_config(cfg: Dictionary, timeout_ms: int = 10000) -> bool:
	var payload := ProtoWire.encode_station_config(cfg)
	var msg := ProtoWire.encode_bus_message(_next_bus_seq(), _wall_time_ns(), 12, payload)
	return _send_framed(_bus_peer, msg, timeout_ms)


func send_video_packet(pkt: Dictionary, timeout_ms: int = 30000) -> bool:
	var payload := ProtoWire.encode_framed_video_packet(pkt)
	var msg := ProtoWire.encode_bus_message(_next_bus_seq(), _wall_time_ns(), 13, payload)
	return _send_framed(_bus_peer, msg, timeout_ms)


func send_ray_event(ray: Dictionary, timeout_ms: int = 10000) -> bool:
	var payload := ProtoWire.encode_ray_event(ray)
	var msg := ProtoWire.encode_bus_message(_next_bus_seq(), _wall_time_ns(), 16, payload)
	return _send_framed(_bus_peer, msg, timeout_ms)


func send_progress_report(pr: Dictionary, timeout_ms: int = 10000) -> bool:
	var payload := ProtoWire.encode_progress_report(pr)
	var msg := ProtoWire.encode_bus_message(_next_bus_seq(), _wall_time_ns(), 20, payload)
	return _send_framed(_bus_peer, msg, timeout_ms)


func send_truth_frame(tf: Dictionary, timeout_ms: int = 10000) -> bool:
	var payload := ProtoWire.encode_truth_frame(tf)
	var msg := ProtoWire.encode_test_framework_message(_next_truth_seq(), 10, payload)
	return _send_framed(_truth_peer, msg, timeout_ms)


func close() -> void:
	if _bus_peer != null:
		_bus_peer.disconnect_from_host()
		_bus_peer = null
	if _truth_peer != null:
		_truth_peer.disconnect_from_host()
		_truth_peer = null


func _next_bus_seq() -> int:
	var seq := _bus_seq
	_bus_seq += 1
	return seq


func _next_truth_seq() -> int:
	var seq := _truth_seq
	_truth_seq += 1
	return seq


static func _wall_time_ns() -> int:
	return int(Time.get_unix_time_from_system() * 1000000000.0)


func _connect_tcp(host: String, port: int, timeout_ms: int) -> StreamPeerTCP:
	var peer := StreamPeerTCP.new()
	var err := peer.connect_to_host(host, port)
	if err != OK:
		_last_error = "TCP connect_to_host(%s:%d) failed: err=%d" % [host, port, err]
		return null
	var deadline := Time.get_ticks_msec() + timeout_ms
	while Time.get_ticks_msec() <= deadline:
		peer.poll()
		var st := peer.get_status()
		if st == StreamPeerTCP.STATUS_CONNECTED:
			peer.set_no_delay(true)
			return peer
		if st == StreamPeerTCP.STATUS_ERROR or st == StreamPeerTCP.STATUS_NONE:
			_last_error = "TCP connect(%s:%d) failed with status=%d" % [host, port, st]
			return null
		OS.delay_msec(5)
	_last_error = "TCP connect(%s:%d) timed out after %d ms" % [host, port, timeout_ms]
	return null


func _send_framed(peer: StreamPeerTCP, payload: PackedByteArray, timeout_ms: int) -> bool:
	if peer == null:
		_last_error = "TCP peer is null"
		return false
	var framed := ProtoWire.encode_length_prefixed(payload)
	if framed.is_empty():
		_last_error = "Failed to encode length-prefixed payload"
		return false
	var sent := 0
	var total := framed.size()
	var deadline := Time.get_ticks_msec() + timeout_ms
	while sent < total:
		peer.poll()
		var st := peer.get_status()
		if st != StreamPeerTCP.STATUS_CONNECTED:
			_last_error = "TCP disconnected while writing (%d/%d bytes, status=%d)" % [sent, total, st]
			return false
		var chunk := framed if sent == 0 else framed.slice(sent, total)
		var res := peer.put_partial_data(chunk)
		var err := int(res[0])
		var written := int(res[1])
		if err != OK and err != ERR_BUSY:
			_last_error = "TCP write error %d after %d/%d bytes" % [err, sent, total]
			return false
		if written > 0:
			sent += written
			continue
		if Time.get_ticks_msec() > deadline:
			_last_error = "TCP write timed out (%d/%d bytes)" % [sent, total]
			return false
		OS.delay_msec(1)
	return true


func _read_framed(peer: StreamPeerTCP, timeout_ms: int) -> PackedByteArray:
	var header := _read_exact(peer, 4, timeout_ms)
	if header.size() != 4:
		return PackedByteArray()
	var length := ProtoWire.decode_be_u32(header, 0)
	if length <= 0 or length > ProtoWire.MAX_FRAME_BYTES:
		_last_error = "Invalid incoming frame length: %d" % length
		return PackedByteArray()
	return _read_exact(peer, length, timeout_ms)


func _read_exact(peer: StreamPeerTCP, count: int, timeout_ms: int) -> PackedByteArray:
	var out := PackedByteArray()
	var deadline := Time.get_ticks_msec() + timeout_ms
	while out.size() < count:
		peer.poll()
		var st := peer.get_status()
		if st != StreamPeerTCP.STATUS_CONNECTED and peer.get_available_bytes() <= 0:
			_last_error = "TCP closed while reading (%d/%d bytes)" % [out.size(), count]
			return PackedByteArray()
		var avail := peer.get_available_bytes()
		if avail > 0:
			var need := mini(avail, count - out.size())
			var res := peer.get_partial_data(need)
			var err := int(res[0])
			var chunk: PackedByteArray = res[1]
			if err != OK:
				_last_error = "TCP read error %d" % err
				return PackedByteArray()
			out.append_array(chunk)
			continue
		if Time.get_ticks_msec() > deadline:
			_last_error = "TCP read timed out (%d/%d bytes)" % [out.size(), count]
			return PackedByteArray()
		OS.delay_msec(1)
	return out
