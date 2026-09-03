"""
Samsung Accessory File Transfer (SAFT) — the transport album art rides on.

Ported from Samsung's own com.samsung.accessory (SAFTProvider/CommandManager/
BinaryDataSender). Two SAP channels, declared in the framework's
accessoryservices.xml under service id "/system/filetransfer":

    channel 100 = command   (UTF-8 JSON, one message per SAP message)
    channel 101 = data      ([0x01] + raw file bytes, repeated until fileSize)

Flow (phone -> band):
    -> filetransfer-setup-req      {fileName, fileSize, transId, ...}
    <- filetransfer-setup-rsp      {result: 0=ok}
    -> data packets on ch 101
    -> filetransfer-complete-req   {fileName}
    <- filetransfer-complete-rsp

Note on chunk size: Samsung sends 32KB data packets, but that needs SAP
fragmentation, which this band reassembles unreliably (the same thing that broke
big icons). The receiver just appends bytes until fileSize, so the packet size is
ours to choose - we use single-SAP-frame packets instead.
"""

import json

CH_COMMAND = 100
CH_DATA = 101

CHUNK_MSG_ID = 0x01     # first byte of every data packet

# msgIds from FileTransferUtil
SETUP_REQ = "filetransfer-setup-req"
SETUP_RSP = "filetransfer-setup-rsp"
COMPLETE_REQ = "filetransfer-complete-req"
COMPLETE_RSP = "filetransfer-complete-rsp"
CANCEL_REQ = "filetransfer-cancel-req"
CANCEL_RSP = "filetransfer-cancel-rsp"
PROGRESS = "filetransfer-receive-progress"
SEND_ERROR = "filetransfer-send-error"
APPALIVE_REQ = "filetransfer-appalive-req"
APPALIVE_RSP = "filetransfer-appalive-rsp"

RESULT_SUCCESS = 0
RESULT_FAILURE = 1


# --- SAP service-connection creation (opens the FT channels) ---------------
# From Samsung's SAServiceFrameUtils.composeServiceConnectionRequest:
#   [0]     messageType (1 = creation request)
#   [1..2]  acceptorId  (BE u16)
#   [3..4]  initiatorId (BE u16)
#   [5..]   profileId ASCII, terminated with ';' (0x3B)
#   [..]    nSessions (BE u16)
#   then four parallel arrays, one entry per session:
#       sessionId (BE u16) xN | channelId (BE u16) xN
#       qos(type, dataRate, classType) xN | payloadType xN
SAP_MSG_SERVICE_CONNECTION_REQUEST = 1
SAP_MSG_SERVICE_CONNECTION_RESPONSE = 2

PROFILE_FILETRANSFER = "/system/filetransfer"

# SAFrameworkServiceChannelDescription constants
QOS_CLASS_FILETRANSFER, QOS_CLASS_OTHER, QOS_CLASS_STREAMING = 0, 1, 2
QOS_DATARATE_LOW, QOS_DATARATE_HIGH = 0, 1
QOS_RELIABILITY_DISABLE, QOS_RELIABILITY_ENABLE = 4, 5


def build_service_connection_request(acceptor_id: int, initiator_id: int,
                                     profile_id: str, channels: list) -> bytes:
    """`channels` = [(session_id, channel_id, qos_type, data_rate, class_type,
    payload_type), ...]"""
    out = bytearray()
    out.append(SAP_MSG_SERVICE_CONNECTION_REQUEST)
    out += int(acceptor_id).to_bytes(2, "big")
    out += int(initiator_id).to_bytes(2, "big")
    out += profile_id.encode("ascii")
    out.append(0x3B)                            # ';' terminator
    out += len(channels).to_bytes(2, "big")
    for c in channels:
        out += int(c[0]).to_bytes(2, "big")     # sessionId
    for c in channels:
        out += int(c[1]).to_bytes(2, "big")     # channelId
    for c in channels:
        out += bytes([c[2] & 0xFF, c[3] & 0xFF, c[4] & 0xFF])   # qos
    for c in channels:
        out.append(c[5] & 0xFF)                 # payloadType
    return bytes(out)


def ft_channels(sess_a: int, sess_b: int):
    """The two channels /system/filetransfer declares, with the XML's QoS."""
    return [
        # ch 100: command  - dataRate low, reliability enable, class other
        (sess_a, CH_COMMAND, QOS_RELIABILITY_ENABLE, QOS_DATARATE_LOW, QOS_CLASS_OTHER, 0),
        # ch 101: data     - class filetransfer
        (sess_b, CH_DATA, QOS_RELIABILITY_ENABLE, QOS_DATARATE_LOW, QOS_CLASS_FILETRANSFER, 0),
    ]


# --- SAP capability exchange -------------------------------------------------
# From Samsung's SACapexFrameUtils.composeCapabilityDiscoveryQueryMessage +
# SACapabilityDiscoveryMessageConstants. This is how a peer enumerates the other
# side's services AND their acceptor ids - the value a service-connection request
# needs and that we otherwise have no way to learn.
#
#   [0] messageType : 1 = QUERY, 2 = RESPONSE
#   [1] queryType   : 0 = NORMAL, 2 = PERSISTENT, 3 = PERSISTENT_WITH_CHECKSUM
#   [2] numRecords  (normal)   | [2..5] checksum + [6] numRecords (persistent)
#   then per filter: profileId ASCII + ';' (0x3B)   - none = ask for everything
CAPEX_QUERY = 1
CAPEX_RESPONSE = 2
CAPEX_QUERY_NORMAL = 0


def build_capability_query(profile_ids=None) -> bytes:
    """Ask the peer to enumerate its services. No filters = everything."""
    profile_ids = profile_ids or []
    out = bytearray([CAPEX_QUERY, CAPEX_QUERY_NORMAL, len(profile_ids) & 0xFF])
    for pid in profile_ids:
        out += pid.encode("ascii") + b";"
    return bytes(out)


def looks_like_capability_response(body: bytes) -> bool:
    return len(body) >= 2 and body[0] == CAPEX_RESPONSE


def build_setup_req(trans_id: int, file_name: str, file_size: int,
                    file_path: str = "", peer_id: str = "", container_id: str = "",
                    acc_id: int = 0, last_modified_ms: int = None,
                    file_author: str = None) -> bytes:
    """SetupRequest.toJson() - the exact key set Samsung sends."""
    d = {
        "msgId": SETUP_REQ,
        "transId": trans_id,
        "fileName": file_name,
        "filePath": file_path or file_name,
        "fileSize": file_size,
        "peerId": peer_id,
        "containerId": container_id,
        "accId": acc_id,
    }
    if last_modified_ms is not None:
        d["fileLastModified"] = last_modified_ms
    if file_author is not None:
        d["fileAuthor"] = file_author
    return json.dumps(d).encode("utf-8")


def build_ctrl_req(msg_id: str, file_name: str) -> bytes:
    """CtrlRequest.toJson(): complete-req / cancel-req."""
    return json.dumps({"msgId": msg_id, "fileName": file_name}).encode("utf-8")


def build_ctrl_rsp(msg_id: str, file_name: str, result: int = RESULT_SUCCESS,
                   reason: int = -1) -> bytes:
    """CtrlResponse.toJson()."""
    return json.dumps({"msgId": msg_id, "result": result,
                       "reason": reason, "fileName": file_name}).encode("utf-8")


def parse_command(body: bytes):
    """Any command-channel message -> dict, or None if it isn't JSON."""
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def data_packets(data: bytes, cap: int):
    """Split a file into data-channel packets: [0x01] + up to (cap-1) bytes."""
    body = max(1, cap - 1)
    return [bytes([CHUNK_MSG_ID]) + data[i:i + body]
            for i in range(0, len(data), body)]
