"""Build release notes from commits since the previous tag and CHANGELOG.md."""
import argparse
from pathlib import Path
import subprocess

def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

def main(argv: list[str] | None = None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--ref",required=True)
    parser.add_argument("--tag",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    tags=[x for x in git("tag","--merged",args.ref,"--sort=-version:refname").splitlines() if x and x!=args.tag]
    previous=tags[0] if tags else None
    rev=f"{previous}..{args.ref}" if previous else args.ref
    commits=git("log",rev,"--no-merges","--format=%h %s").splitlines()
    commits.reverse()
    full=(Path("CHANGELOG.md").read_text(encoding="utf-8"))
    lines=[f"# ECHOCRYPT {args.tag}","",f"Changes since {previous}" if previous else "Complete repository history",""]
    lines += ["## Commits",""] + ([f"- {x}" for x in commits] or ["- No new non-merge commits."])
    lines += ["","---","","## Complete project changelog","",full,"","## Validation and packaging","","- Full cross-platform test matrix passed before build jobs started.","- Desktop packages were built on native OS runners.","- Chrome, Edge and Firefox extension archives were generated.","- This release is a Draft; it is not published automatically.",""]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text("\n".join(lines),encoding="utf-8")
    print(f"Generated release notes ({len(commits)} commits).")
if __name__=="__main__":
    main()
