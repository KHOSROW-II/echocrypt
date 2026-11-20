from core.crypto import SecureChannel


class Session:
    def __init__(self, tx_key, rx_key, peer_fingerprint, hopping_key=None,
                 *, padding_block=0, security_profile=None):
        if tx_key == rx_key:
            raise ValueError("Use distinct directional keys")
        if hopping_key is not None and len(hopping_key) != 32:
            raise ValueError("Hopping key must be exactly 32 bytes")
        self.tx = SecureChannel(tx_key, padding_block=padding_block)
        self.rx = SecureChannel(rx_key, padding_block=padding_block)
        self.peer_fingerprint = peer_fingerprint
        self.hopping_key = bytes(hopping_key) if hopping_key is not None else None
        self.security_profile = security_profile

    def encrypt_text(self, text):
        return self.tx.encrypt(text.encode("utf-8"), b"ECHOCRYPT/TEXT/v2")

    def decrypt_text(self, packet):
        return self.rx.decrypt(packet, b"ECHOCRYPT/TEXT/v2").decode("utf-8")
