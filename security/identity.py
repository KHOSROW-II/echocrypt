from pathlib import Path
import hashlib,json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

def raw_public(key): return key.public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
def fingerprint(key): return hashlib.sha256(raw_public(key)).hexdigest()
def create_private(path):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists(): raise FileExistsError("Refusing to overwrite existing private identity")
    k=Ed25519PrivateKey.generate()
    p.write_bytes(k.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()))
    try: p.chmod(0o600)
    except OSError: pass
    return fingerprint(k.public_key())
def load_private(path):
    b=Path(path).read_bytes()
    if len(b)!=32: raise ValueError("Invalid private key")
    return Ed25519PrivateKey.from_private_bytes(b)
def export_public(key,path):
    raw=raw_public(key.public_key())
    obj={"format":"ECHOCRYPT-IDENTITY-v1","public_key":raw.hex(),"fingerprint":hashlib.sha256(raw).hexdigest()}
    Path(path).write_text(json.dumps(obj,indent=2),encoding="utf-8")
    return obj["fingerprint"]
def load_public(path):
    o=json.loads(Path(path).read_text(encoding="utf-8"))
    if o.get("format")!="ECHOCRYPT-IDENTITY-v1": raise ValueError("Unsupported identity format")
    raw=bytes.fromhex(o["public_key"]); fp=hashlib.sha256(raw).hexdigest()
    if fp!=o.get("fingerprint"): raise ValueError("Fingerprint mismatch")
    return raw,fp
