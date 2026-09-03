"""
Watchface (BandFace) control - SAP channel 0x06.

Ported from Samsung's own fit3plugin classes:
  com.samsung.wearable.hostmanager.bandface.messenger.worker.builder.*  (requests)
  com.samsung.wearable.hostmanager.bandface.command.data.CommandValue   (message ids)
  com.samsung.wearable.hostmanager.bandface.messenger.worker.MessageParameterValue

Wire format is the shared SAMessageData one we already use elsewhere:
    header = (format<<7) | (type<<6) | (msgId & 0x3F)
    then params, each [paramId][value...]
The bandface builders never call setNumberOfParam(), so NO_OF_PARAM stays 0 and
there is NO param-count byte - unlike the media/notification channels.

Format is always 0 (fixed) and type 0 (request) for phone->band; the band's
replies come back with the type bit set (0x40 | msgId).
"""

CHANNEL = 0x06          # serviceId 6 == channel (Samsung's hmaccessoryservices xml)

# --- message ids (CommandValue) ---
GET_ALL_INFO = 0
GET_BANDFACE_LIST = 1
GET_CURRENT_BANDFACE = 2
SET_CURRENT_BANDFACE = 3
INSTALL_BANDFACE = 4
DELETE_BANDFACE = 5
GET_EDIT_INFO = 6
SET_EDIT_INFO = 7
CANCEL_EDIT_INFO = 8
GET_CURRENT_ORDER = 9
SET_CURRENT_ORDER = 10
ADD_IMAGE_LIST = 11
DELETE_IMAGE_LIST = 12
GET_IMAGE_LIST = 14
NONE = 63

MSG_NAMES = {
    GET_ALL_INFO: "GET_ALL_INFO", GET_BANDFACE_LIST: "GET_BANDFACE_LIST",
    GET_CURRENT_BANDFACE: "GET_CURRENT_BANDFACE", SET_CURRENT_BANDFACE: "SET_CURRENT_BANDFACE",
    INSTALL_BANDFACE: "INSTALL_BANDFACE", DELETE_BANDFACE: "DELETE_BANDFACE",
    GET_EDIT_INFO: "GET_EDIT_INFO", SET_EDIT_INFO: "SET_EDIT_INFO",
    CANCEL_EDIT_INFO: "CANCEL_EDIT_INFO", GET_CURRENT_ORDER: "GET_CURRENT_ORDER",
    SET_CURRENT_ORDER: "SET_CURRENT_ORDER", ADD_IMAGE_LIST: "ADD_IMAGE_LIST",
    DELETE_IMAGE_LIST: "DELETE_IMAGE_LIST", GET_IMAGE_LIST: "GET_IMAGE_LIST",
}

# --- param ids (MessageParameterValue) ---
P_CURRENT_WF_ID = 0
P_MAX_WATCHFACE_COUNT = 1
P_WF_COUNT = 2
P_WATCHFACE_LIST = 3
P_WF_ID = 4
P_WF_VERSION = 5
P_WF_NAME = 6
P_WF_DESCRIPTION = 7
P_STYLE_ID = 8
P_POSITION_INDEX = 9
P_STYLE_INDEX = 10
P_IS_CURRENT = 11
P_IS_EDITABLE = 12
P_CLOCK_TYPE = 13
P_HOUR_INDEX = 14
P_MINUTE_INDEX = 15
P_SECOND_INDEX = 16
P_WIDGET_INDEX = 17
P_WIDGET_TYPE = 18
P_BACKGROUND = 19
P_IMAGE_ID_LIST = 20
P_WF_ID_LIST = 21
P_INSTALL_RESULT = 22
P_UNINSTALL_RESULT = 23
P_NUM_OF_PARAMS = 24
P_WF_CURRENT_ORDER = 25
P_RESULT_STATUS = 26
P_IMAGE_COUNT = 27
P_IMAGE_NAME = 28
P_WF_SAMPLER_ID = 29
P_CLOCK_COLOR = 30
P_IS_SELECTED = 31

PARAM_NAMES = {v: k[2:] for k, v in list(globals().items()) if k.startswith("P_")}


def _msg(msg_id: int, *params: bytes) -> bytes:
    """header (fixed/request) + raw params, no param-count byte."""
    return bytes([msg_id & 0x3F]) + b"".join(params)


def _p(param_id: int, value: int) -> bytes:
    return bytes([param_id & 0xFF, value & 0xFF])


# ---- requests ----------------------------------------------------------------

def get_all_info() -> bytes:
    return _msg(GET_ALL_INFO)


def get_bandface_list() -> bytes:
    return _msg(GET_BANDFACE_LIST)


def get_current_bandface() -> bytes:
    return _msg(GET_CURRENT_BANDFACE)


def set_current_bandface(wf_id: int, sampler_id: int = 0) -> bytes:
    """Switch the active watchface (SetCurrentBandFaceRequestBuilder)."""
    return _msg(SET_CURRENT_BANDFACE, _p(P_WF_ID, wf_id), _p(P_WF_SAMPLER_ID, sampler_id))


def install_bandface(wf_id: int, sampler_id: int = 0) -> bytes:
    """Ask the band to install a watchface by id (InstallBandFaceRequestBuilder).

    Note this carries only the id pair - the .bin itself goes separately over
    SAFT (appId 6). Sending this alone is how we probe whether the band opens a
    file-transfer session in response.
    """
    return _msg(INSTALL_BANDFACE, _p(P_WF_ID, wf_id), _p(P_WF_SAMPLER_ID, sampler_id))


def delete_bandface(wf_id: int, sampler_id: int = 0) -> bytes:
    return _msg(DELETE_BANDFACE, _p(P_WF_ID, wf_id), _p(P_WF_SAMPLER_ID, sampler_id))


def get_edit_info(wf_id: int) -> bytes:
    return _msg(GET_EDIT_INFO, _p(P_WF_ID, wf_id))


def get_current_order() -> bytes:
    return _msg(GET_CURRENT_ORDER)


def get_image_list(wf_id: int = 0) -> bytes:
    return _msg(GET_IMAGE_LIST, _p(P_WF_ID, wf_id))


# ---- responses ---------------------------------------------------------------

# Params whose value is length-prefixed text: [id][len][utf8]. Everything else is
# a single byte. (Confirmed from real band replies - e.g. 06 0e "wf_name-00077".)
STRING_PARAMS = {P_WF_VERSION, P_WF_NAME, P_WF_DESCRIPTION, P_IMAGE_NAME}


def _parse_entry(body: bytes, i: int):
    """Parse one watchface entry: NUM_OF_PARAMS then that many params.
    Returns (dict, new_index), or (None, i) if this isn't an entry."""
    if i >= len(body) or body[i] != P_NUM_OF_PARAMS or i + 1 >= len(body):
        return None, i
    count = body[i + 1]
    i += 2
    wf = {}
    for _ in range(count):
        if i >= len(body):
            break
        pid = body[i]
        i += 1
        if pid in STRING_PARAMS:
            if i >= len(body):
                break
            ln = body[i]
            i += 1
            wf[PARAM_NAMES.get(pid, f"p{pid}")] = (
                body[i:i + ln].decode("utf-8", "replace").rstrip("\x00"))
            i += ln
        else:
            if i >= len(body):
                break
            wf[PARAM_NAMES.get(pid, f"p{pid}")] = body[i]
            i += 1
    return wf, i


def parse_response(body: bytes) -> dict:
    """Decode a band->phone bandface reply into a dict.

    Top level is a flat [param][value] walk, except WATCHFACE_LIST whose value is
    a count followed by that many NUM_OF_PARAMS-prefixed entries.
    """
    out = {"raw": body.hex(" ")}
    if not body:
        return out
    hdr = body[0]
    out["msg_id"] = hdr & 0x3F
    out["msg"] = MSG_NAMES.get(hdr & 0x3F, f"msg{hdr & 0x3F}")
    out["is_response"] = bool(hdr >> 6 & 1)
    i = 1
    while i < len(body):
        pid = body[i]
        i += 1
        if pid == P_WATCHFACE_LIST:
            if i >= len(body):
                break
            n = body[i]
            i += 1
            faces = []
            for _ in range(n):
                wf, i = _parse_entry(body, i)
                if wf is None:
                    break
                faces.append(wf)
            out["watchfaces"] = faces
            continue
        if i >= len(body):
            break
        if pid in STRING_PARAMS:
            ln = body[i]
            i += 1
            out[PARAM_NAMES.get(pid, f"p{pid}")] = (
                body[i:i + ln].decode("utf-8", "replace").rstrip("\x00"))
            i += ln
        else:
            out[PARAM_NAMES.get(pid, f"p{pid}")] = body[i]
            i += 1
    return out


def describe(body: bytes) -> str:
    """One-line-per-watchface summary of a band reply."""
    d = parse_response(body)
    if "msg" not in d:
        return f"(empty) raw={d.get('raw','')}"
    head = f"{d['msg']}{'(resp)' if d.get('is_response') else '(req)'}"
    scalars = {k: v for k, v in d.items()
               if k not in ("raw", "msg", "msg_id", "is_response", "watchfaces")}
    lines = [head + ("  " + "  ".join(f"{k}={v}" for k, v in scalars.items()) if scalars else "")]
    for wf in d.get("watchfaces", []):
        cur = "  <-- CURRENT" if wf.get("IS_CURRENT") else ""
        lines.append(
            f"    id={wf.get('WF_ID'):<4} pos={wf.get('POSITION_INDEX')} "
            f"style={wf.get('STYLE_ID')}/{wf.get('STYLE_INDEX')} "
            f"editable={wf.get('IS_EDITABLE')} "
            f"ver={wf.get('WF_VERSION')} name={wf.get('WF_NAME')}{cur}")
    return "\n".join(lines)
