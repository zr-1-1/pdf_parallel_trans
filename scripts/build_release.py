"""在专用 Conda 环境构建 Windows 包、源码包与校验清单。"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow import __version__

SOURCE_FILES = [".gitignore", ".gitattributes", "LICENSE", "README.md", "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md", "environment.yml", "requirements.txt", "requirements-ocr.txt",
    "requirements-build.txt", "constraints.txt", "run.bat", "PDFParallelTrans.spec", "release-resources.json",
    "docs/ROADMAP.md", "docs/RELEASING.md", "scripts/build_release.py", "scripts/frozen_entry.py",
    "scripts/build_layout_demo.py", "scripts/smoke_release.py"]


def source_files():
    paths = [ROOT / name for name in SOURCE_FILES]
    for directory, pattern in (("paperflow", "*.py"), ("paperflow/static", "*"), ("tests", "test_*.py"), ("licenses", "*")):
        paths.extend((ROOT / directory).glob(pattern))
    for path in paths:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"发布文件缺失或路径不安全：{path}")
    return sorted(set(paths))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resources():
    for name, info in json.loads((ROOT / "release-resources.json").read_text(encoding="utf-8")).items():
        path = ROOT / "build/resources" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".download")
            print(f"下载构建资源：{name}", flush=True)
            with urllib.request.urlopen(info["url"], timeout=120) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            if sha256(temporary) != info["sha256"]:
                raise ValueError(f"资源校验失败：{name}")
            temporary.replace(path)
        if sha256(path) != info["sha256"]:
            raise ValueError(f"已有资源校验失败：{name}")


def collect_licenses(destination):
    destination.mkdir(parents=True, exist_ok=True)
    packages = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        package_dir = destination / name
        for entry in dist.files or []:
            if any(word in str(entry).lower() for word in ("license", "copying", "notice", "copyright")):
                source = Path(dist.locate_file(entry))
                if source.is_file() and source.suffix not in {".py", ".pyc"}:
                    package_dir.mkdir(exist_ok=True)
                    target = package_dir / str(entry).replace("/", "__").replace("\\", "__")
                    shutil.copy2(source, target)
        packages.append({"name": name, "version": dist.version,
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", "See upstream")})
    for name in ("LICENSE_PYTHON.txt", "LICENSE.txt"):
        path = Path(sys.prefix) / name
        if path.is_file():
            shutil.copy2(path, destination / ("Python-" + name))
    shutil.copy2(ROOT / "LICENSE", destination / "PyMuPDF-MuPDF-AGPL-3.0.txt")
    shutil.copy2(ROOT / "build/resources/fonts/OFL.txt", destination / "Noto-OFL.txt")
    for path in (ROOT / "licenses").glob("*"):
        if path.is_file():
            shutil.copy2(path, destination / path.name)
    (destination / "build-environment.json").write_text(json.dumps(packages, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true", help="只生成白名单源码包，不下载或构建二进制")
    args = parser.parse_args()
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise SystemExit("请在专用 Conda 环境运行构建脚本。")
    output = ROOT / "dist/release"
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    if not args.source_only:
        if sys.platform != "win32":
            raise SystemExit("Windows 包必须在 Windows 上构建。")
        if metadata.version("PyMuPDF") != "1.25.2":
            raise SystemExit("请安装 constraints.txt 中的 PyMuPDF 版本，以匹配随包对应源码。")
        resources()
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "PDFParallelTrans.spec"], cwd=ROOT, check=True)
        bundle = ROOT / "dist/PDFParallelTrans"
        collect_licenses(bundle / "licenses")
        for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"):
            shutil.copy2(ROOT / name, bundle / name)
        (bundle / "docs").mkdir(exist_ok=True)
        for name in ("RELEASING.md", "ROADMAP.md"):
            shutil.copy2(ROOT / "docs" / name, bundle / "docs" / name)
        archive = output / f"PDFParallelTrans-{__version__}-windows-x64.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    z.write(path, path.relative_to(bundle.parent))
        artifacts.append(archive)
        for name in json.loads((ROOT / "release-resources.json").read_text(encoding="utf-8")):
            if name.endswith((".tar.gz", ".tar.bz2")):
                upstream = output / name
                shutil.copy2(ROOT / "build/resources" / name, upstream)
                artifacts.append(upstream)
    source = output / f"PDFParallelTrans-{__version__}-source.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as z:
        for path in source_files():
            z.write(path, f"pdf_parallel_trans-{__version__}/" + path.relative_to(ROOT).as_posix())
    artifacts.append(source)
    (output / "SHA256SUMS.txt").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in artifacts), encoding="ascii")
    for path in artifacts:
        print(f"{path.name}: {path.stat().st_size / 1024 / 1024:.2f} MiB")


if __name__ == "__main__":
    main()
