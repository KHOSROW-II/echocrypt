"""Register ECHOCRYPT Native Messaging for Chrome, Edge, or Firefox."""
import argparse
import json
import os
from pathlib import Path
import sys

HOST = "org.echocrypt.tab_audio"

def paths(browser):
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        if browser == "firefox": return base / "Mozilla/NativeMessagingHosts", base / "ECHOCRYPT"
        local = Path(os.environ.get("LOCALAPPDATA", base))
        vendor = "Google/Chrome/User Data" if browser == "chrome" else "Microsoft/Edge/User Data"
        return local / vendor / "NativeMessagingHosts", base / "ECHOCRYPT"
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
        if browser == "firefox": return base / "Mozilla/NativeMessagingHosts", base / "ECHOCRYPT"
        vendor = {"chrome":"Google/Chrome","edge":"Microsoft Edge"}[browser]
        return base / vendor / "NativeMessagingHosts", base / "ECHOCRYPT"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home()/".config"))
    vendor = {"chrome":"google-chrome","edge":"microsoft-edge","firefox":"mozilla"}[browser]
    return base / vendor / "NativeMessagingHosts", base / "ECHOCRYPT"

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--browser",required=True,choices=("chrome","edge","firefox"))
    parser.add_argument("--extension-id",required=True)
    parser.add_argument("--native-host",required=True,type=Path)
    args=parser.parse_args()
    if args.browser=="firefox":
        if args.extension_id!="echocrypt@khosrow-ii.local": raise SystemExit("Invalid Firefox add-on ID")
    elif len(args.extension_id)!=32 or any(c not in "abcdefghijklmnop" for c in args.extension_id):
        raise SystemExit("Chromium extension ID must be 32 characters in a-p")
    executable=args.native_host.expanduser().resolve()
    if not executable.is_file(): raise SystemExit(f"Native host executable not found: {executable}")
    host_dir,config_dir=paths(args.browser)
    host_dir.mkdir(parents=True,exist_ok=True);config_dir.mkdir(parents=True,exist_ok=True)
    manifest={"name":HOST,"description":"ECHOCRYPT selected-tab audio bridge","path":str(executable),"type":"stdio"}
    if args.browser=="firefox": manifest["allowed_extensions"]=[args.extension_id]
    else: manifest["allowed_origins"]=[f"chrome-extension://{args.extension_id}/"]
    (host_dir/f"{HOST}.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    config_path=config_dir/"browser-native-host.json"
    try: config=json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError,ValueError,TypeError): config={}
    extensions=config.get("extensions",{})
    if not isinstance(extensions,dict): extensions={}
    extensions[args.browser]=args.extension_id
    config["extensions"]=extensions
    config_path.write_text(json.dumps(config,indent=2)+"\n",encoding="utf-8")
    print(f"Registered {HOST} for {args.browser}: {host_dir}")
if __name__=="__main__": main()
