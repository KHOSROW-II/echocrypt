import os,json,hashlib
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

def _raw(key): return key.public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
def create_hello(identity_private):
    eph=X25519PrivateKey.generate(); nonce=os.urandom(16)
    body=json.dumps({"v":1,"ephemeral":_raw(eph.public_key()).hex(),"nonce":nonce.hex()},
                    sort_keys=True,separators=(",",":")).encode()
    return {"body":body.hex(),"signature":identity_private.sign(body).hex()},eph,nonce
def verify_hello(hello,peer_ed25519_raw,pinned_fingerprint):
    if hashlib.sha256(peer_ed25519_raw).hexdigest()!=pinned_fingerprint: raise ValueError("Untrusted peer fingerprint")
    body=bytes.fromhex(hello["body"])
    Ed25519PublicKey.from_public_bytes(peer_ed25519_raw).verify(bytes.fromhex(hello["signature"]),body)
    o=json.loads(body)
    if o.get("v")!=1: raise ValueError("Unsupported handshake version")
    return X25519PublicKey.from_public_bytes(bytes.fromhex(o["ephemeral"])),bytes.fromhex(o["nonce"])
def derive_keys(own_eph,peer_eph,initiator_nonce,responder_nonce,is_initiator):
    shared=own_eph.exchange(peer_eph); transcript=b"ECHOCRYPT-HS-v1"+initiator_nonce+responder_nonce
    material=HKDF(algorithm=hashes.SHA256(),length=64,salt=hashlib.sha256(transcript).digest(),
                  info=b"ECHOCRYPT directional keys").derive(shared)
    ab,ba=material[:32],material[32:]
    return (ab,ba) if is_initiator else (ba,ab)
