import hashlib
import importlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / ".tools"


def load_environment():
    environment = os.environ.copy()
    source = ROOT / ".env"
    allowed = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "WEBSITE_BUCKET",
        "WEBSITE_SOURCE_CIDR",
        "PRODUCTS_API_ID",
        "LAMBDA_ROLE_NAME",
        "LAMBDA_RUNTIME",
    }
    if source.exists():
        for line in source.read_text().splitlines():
            key, separator, value = line.partition("=")
            key = key.strip().upper()
            if separator and key in allowed:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                environment[key] = value
    environment.setdefault(
        "TF_VAR_aws_region",
        environment.get("AWS_REGION", environment.get("AWS_DEFAULT_REGION", "us-east-1")),
    )
    return environment


def find_terraform():
    existing = shutil.which("terraform")
    if not existing:
        existing = next((str(path) for path in sorted(ROOT.parent.glob("Module-*/.tools/terraform")) if path.is_file()), None)
    if existing:
        return existing
    binary = "terraform.exe" if platform.system() == "Windows" else "terraform"
    executable = TOOLS / binary
    if executable.exists():
        return str(executable)
    operating_system = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(platform.system())
    architecture = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if not operating_system or not architecture:
        raise RuntimeError("Install Terraform >= 1.7 and add it to PATH.")
    version = "1.13.5"
    filename = f"terraform_{version}_{operating_system}_{architecture}.zip"
    base_url = f"https://releases.hashicorp.com/terraform/{version}"
    TOOLS.mkdir(exist_ok=True)
    print("Downloading Terraform...", flush=True)
    with tempfile.TemporaryDirectory(dir=TOOLS) as directory:
        archive_path = Path(directory) / filename
        with urllib.request.urlopen(f"{base_url}/{filename}", timeout=30) as response:
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        with urllib.request.urlopen(f"{base_url}/terraform_{version}_SHA256SUMS", timeout=30) as response:
            checksums = response.read().decode()
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == filename)
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Terraform download checksum does not match.")
        with zipfile.ZipFile(archive_path) as archive:
            archive.extract(binary, TOOLS)
    executable.chmod(0o755)
    return str(executable)


def aws_session(environment):
    dependencies = TOOLS / "python"
    for path in sorted(ROOT.parent.glob("Module-*/.tools/python")):
        sys.path.insert(0, str(path))
    sys.path.insert(0, str(dependencies))
    try:
        import boto3
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--target", str(dependencies), "-r", str(ROOT / "requirements.txt")],
            check=True,
        )
        importlib.invalidate_caches()
        import boto3
    return boto3.Session(
        aws_access_key_id=environment.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=environment.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=environment.get("AWS_SESSION_TOKEN"),
        region_name=environment["TF_VAR_aws_region"],
    )
