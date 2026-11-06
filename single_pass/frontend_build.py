"""Keep the Windows launcher's served Vue bundle in sync with this checkout."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def fingerprints(directory, paths):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def inputs(frontend):
    files = [p for folder in ("src", "public") for p in (frontend / folder).rglob("*")]
    files.extend(frontend.glob("*.html"))
    files.extend(frontend.glob("vite.config.*"))
    files.extend(frontend / name for name in ("package.json", "package-lock.json"))
    return fingerprints(frontend, files)


def ensure(frontend):
    dist = frontend / "dist"
    marker = dist / ".workbench-build.json"
    current = inputs(frontend)
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        outputs = state["outputs"]
        if (current == state["inputs"] and "index.html" in outputs
                and outputs == fingerprints(dist, (dist / name for name in outputs))):
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm or not (frontend / "node_modules/vite/package.json").is_file():
        raise SystemExit("Frontend needs rebuilding. Install Node.js and run npm --prefix frontend ci.")

    print("Building the frontend for the current checkout.", flush=True)
    # Finish the build before serving any changed file. Old tabs can still fetch
    # their original hashed assets, and unrelated static files stay in place.
    with tempfile.TemporaryDirectory(prefix=".workbench-build-", dir=frontend) as temporary:
        stage = Path(temporary)
        subprocess.run([npm, "run", "build", "--", "--outDir", str(stage)], cwd=frontend, check=True)
        outputs = fingerprints(stage, stage.rglob("*"))
        if "index.html" not in outputs or not any(name.endswith(".js") for name in outputs):
            raise RuntimeError("Incomplete frontend build")
        if inputs(frontend) != current:
            raise RuntimeError("Frontend inputs changed during the build. Restart to retry.")
        for name in sorted(outputs, key=lambda n: (n.endswith(".html"), n)):
            target = dist / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / name, target)
        stamp = stage / marker.name
        stamp.write_text(json.dumps({"inputs": current, "outputs": outputs}), encoding="utf-8")
        os.replace(stamp, marker)


if __name__ == "__main__":
    ensure(Path(__file__).resolve().parents[1] / "frontend")
