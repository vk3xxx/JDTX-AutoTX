import struct

def decode_jtdx_tx_enable(payload: bytes):
    """
    Decodes a JTDX UDP Status message and returns "on" or "off" for TX enable state.
    Returns None if the message is not a JTDX Status message.
    """
    def read_qt_utf8(data, offset):
        length = struct.unpack_from('>I', data, offset)[0]
        offset += 4
        if length == 0xFFFFFFFF:
            return None, offset
        s = data[offset:offset+length].decode('utf-8')
        return s, offset + length

    try:
        offset = 0
        # Header
        magic, schema = struct.unpack_from('>II', payload, offset)
        offset += 8
        if magic != 0xadbccbda:
            return None  # Not a JTDX message
        # Message type
        msg_type = struct.unpack_from('>I', payload, offset)[0]
        offset += 4
        if msg_type != 1:
            return None  # Not a Status message
        # Id
        _, offset = read_qt_utf8(payload, offset)
        # Dial Frequency
        offset += 8
        # Mode
        _, offset = read_qt_utf8(payload, offset)
        # DX call
        _, offset = read_qt_utf8(payload, offset)
        # Report
        _, offset = read_qt_utf8(payload, offset)
        # Tx Mode
        _, offset = read_qt_utf8(payload, offset)
        # Tx Enabled (this is what we want)
        tx_enabled = struct.unpack_from('>?', payload, offset)[0]
        return "on" if tx_enabled else "off"
    except Exception:
        return None  # Not a valid or expected message