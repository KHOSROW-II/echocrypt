from prompt_toolkit import prompt
from interface.terminal import Menu
from interface.dashboard import render as render_app_header
from security.keystore import create_identity,get_fingerprint,PRIVATE,PUBLIC
from security.identity import load_private,export_public
from core.audio import list_devices,play,record,default_device_names
from core.config import load
from core.modem import modulate,demodulate
from core.crypto import SecureChannel
from core.frequency import validate
from interface.communication import start_communication
from interface.settings import settings_menu
from interface.audio_tests import run_microphone_level_test, run_speaker_test
import secrets

def pause(): prompt("\nPress Enter to return...")
def identity_menu():
    while True:
        c=Menu("MANAGE IDENTITY",[("create","Create identity"),("export","Export public identity"),
            ("fp","Show fingerprint"),("back","Back")]).run()
        if c in (None,"back"): return
        render_app_header(session_status="IDENTITY SETTINGS")
        try:
            if c=="create": print("Created:",create_identity())
            elif c=="export":
                if not PRIVATE.exists(): raise RuntimeError("Create identity first")
                print("Exported:",export_public(load_private(PRIVATE),PUBLIC))
            elif c=="fp": print("Fingerprint:",get_fingerprint() or "No identity")
        except Exception as e: print("ERROR:",e)
        pause()
def audio_menu():
    while True:
        c=Menu("AUDIO CONFIGURATION",[("devices","List audio devices"),
            ("speaker-test","Test speakers (LEFT then RIGHT, 2 seconds each)"),
            ("mic-test","Test microphone (live level meter)"),
            ("record","Record 3-second input test"),("back","Back")]).run()
        if c in (None,"back"): return
        render_app_header(session_status="AUDIO SETTINGS")
        try:
            if c=="speaker-test":
                run_speaker_test()
            elif c=="mic-test":
                run_microphone_level_test()
            elif c=="devices":
                cfg = load()
                try:
                    default_in, default_out = default_device_names(allow_install=False)
                except Exception:
                    default_in = default_out = "Unavailable"
                print(f"System default input : {default_in} ({'active' if cfg.input_device is None else 'overridden'})")
                print(f"System default output: {default_out} ({'active' if cfg.output_device is None else 'overridden'})")
                for i,n,ni,no in list_devices():
                    marks = []
                    if n == default_in and ni > 0: marks.append("DEFAULT INPUT")
                    if n == default_out and no > 0: marks.append("DEFAULT OUTPUT")
                    suffix = f" | {' / '.join(marks)}" if marks else ""
                    print(f"{i:>3} | {n} | IN {ni} | OUT {no}{suffix}")
            else:
                cfg=load(); a=record(3,cfg.sample_rate,cfg.input_device)
                print("Captured",len(a),"samples; peak",round(float(abs(a).max()),4))
        except Exception as e: print("AUDIO ERROR:",e)
        pause()
def modem_test():
    render_app_header(session_status="MODEM DIAGNOSTICS")
    cfg=load(); msg=prompt("Message for local modem test: ")
    key=secrets.token_bytes(32); tx,rx=SecureChannel(key),SecureChannel(key)
    encrypted=tx.encrypt(msg.encode(),b"text")
    wave=modulate(encrypted,cfg.sample_rate,cfg.symbol_rate,cfg.mark_hz,cfg.space_hz,cfg.gain,cfg.max_payload)
    print(f"Packet {len(encrypted)} bytes | waveform {len(wave)/cfg.sample_rate:.2f} sec")
    c=Menu("MODEM TEST",[("decode","Decode in memory"),("play","Play on output"),("record","Record from microphone and decode"),("back","Back")]).run()
    try:
        if c=="decode":
            pkt=demodulate(wave,cfg.sample_rate,cfg.symbol_rate,cfg.mark_hz,cfg.space_hz,cfg.max_payload)
            print("Recovered:",rx.decrypt(pkt,b"text").decode()); print("PASS: local software loopback only.")
        elif c=="play":
            play(wave,cfg.sample_rate,cfg.output_device)
            print("Playback complete. This does not confirm remote reception.")
        elif c=="record":
            print("Start recording; play the signal from the other device during the recording.")
            audio=record(cfg.record_seconds,cfg.sample_rate,cfg.input_device)
            pkt=demodulate(audio,cfg.sample_rate,cfg.symbol_rate,cfg.mark_hz,cfg.space_hz,cfg.max_payload)
            print("Acoustic packet received:",len(pkt),"bytes. Decryption requires an established peer session.")
    except Exception as e: print("MODEM ERROR:",e)
    pause()
def main():
    while True:
        c=Menu("MAIN MENU",[("start","Start Communication"),("identity","Manage Identity"),
            ("audio","Audio Configuration"),("config","Configuration"),
            ("security","Security Settings"),("diagnostics","Diagnostics"),("exit","Exit")]).run()
        if c in (None,"exit"): return
        if c=="start":
            start_communication()
        elif c=="identity": identity_menu()
        elif c=="audio": audio_menu()
        elif c=="config":
            settings_menu()
        elif c=="security":
            render_app_header(session_status="SECURITY SETTINGS")
            print("Identity: Ed25519 with pinned peer fingerprint")
            print("Key agreement: ephemeral X25519 + transcript-bound HKDF-SHA256")
            print("Traffic protection: directional ChaCha20-Poly1305 keys")
            print("Key confirmation: mutual encrypted FINISH messages")
            print("Replay defense: bounded 1024-packet receive window")
            print("Transport: acoustic BFSK, half-duplex, authenticated ACK/retry.")
            print("No server, sockets, or direct network connection are used."); pause()
        elif c=="diagnostics":
            render_app_header(session_status="DIAGNOSTICS")
            cfg=load(); validate(cfg.mark_hz,cfg.space_hz,cfg.sample_rate)
            print("Configuration: OK")
            try:
                default_in, default_out = default_device_names(allow_install=False)
                print("System default input :", default_in)
                print("System default output:", default_out)
            except Exception:
                print("Audio runtime: not loaded (device selection remains SYSTEM DEFAULT)")
            print("Security: Ed25519 identity, ephemeral X25519, directional ChaCha20-Poly1305.")
            print("Text: authenticated acoustic packets with ACK/retry and replay protection.")
            print("Files: encrypted binary chunks, SHA-256 verification, resume after restart.")
            print("Voice: narrowband ADPCM voice notes and delayed 2-second PTT bursts.")
            print("Modem: BFSK, keyed per-symbol FHSS after handshake, Hamming(7,4).")
            print("Retry adaptation: Repetition-3 FEC after missing ACK.")
            print("Hybrid: experimental dual-band FDM for simultaneous text + media chunks.")
            print("Not implemented: true full-duplex/real-time voice. PTT is store-and-forward.")
            print("Hardware microphone/speaker reliability still requires two-device testing.")
            pause()
