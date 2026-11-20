from pathlib import Path
from security.identity import create_private,load_private,export_public,fingerprint
ROOT=Path(__file__).resolve().parents[1]; KEYDIR=ROOT/"keys"
PRIVATE=KEYDIR/"identity.ed25519"; PUBLIC=KEYDIR/"identity.public.json"
def create_identity():
    KEYDIR.mkdir(parents=True,exist_ok=True)
    fp=create_private(PRIVATE); export_public(load_private(PRIVATE),PUBLIC); return fp
def get_fingerprint():
    return fingerprint(load_private(PRIVATE).public_key()) if PRIVATE.exists() else None
