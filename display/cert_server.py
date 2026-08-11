#!/usr/bin/env python3
"""Serve only the public display certificate and exit with the display process."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import threading
import time


MAX_PEM_BYTES = 64 * 1024
_PRIVATE_KEY_RE = re.compile(
    br"\A-----BEGIN (?:RSA |EC )?PRIVATE KEY-----\r?\n.+\r?\n"
    br"-----END (?:RSA |EC )?PRIVATE KEY-----\r?\n?\Z",
    re.DOTALL,
)


def _read_pem(path: Path, *, private: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"unsafe PEM file type: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError(f"PEM file is not owned by the current user: {path}")
        permissions = stat.S_IMODE(metadata.st_mode)
        if private and permissions & 0o077:
            raise ValueError(f"private key permissions must not grant group/other access: {path}")
        if not private and permissions & 0o022:
            raise ValueError(f"certificate must not be group/other writable: {path}")
        if metadata.st_size <= 0 or metadata.st_size > MAX_PEM_BYTES:
            raise ValueError(f"PEM file size is invalid: {path}")
        chunks = []
        remaining = MAX_PEM_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != metadata.st_size:
            raise ValueError(f"PEM file changed while being read: {path}")
        return content
    finally:
        os.close(descriptor)


def load_certificate(path: Path) -> bytes:
    content = _read_pem(path)
    try:
        ssl.PEM_cert_to_DER_cert(content.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid certificate PEM: {path}") from exc
    return content


def validate_private_key(path: Path) -> None:
    content = _read_pem(path, private=True)
    if not _PRIVATE_KEY_RE.fullmatch(content):
        raise ValueError(f"invalid private key PEM envelope: {path}")
    try:
        checked = subprocess.run(
            ["openssl", "pkey", "-check", "-noout"], input=content,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError as exc:
        raise ValueError("openssl is required to validate the private key") from exc
    if checked.returncode != 0:
        raise ValueError(f"private key validation failed: {path}")


def validate_tls_material(cert_path: Path, key_path: Path) -> bytes:
    certificate = load_certificate(cert_path)
    validate_private_key(key_path)
    key = _read_pem(key_path, private=True)
    try:
        cert_public = subprocess.run(
            ["openssl", "x509", "-pubkey", "-noout"], input=certificate,
            capture_output=True, check=False,
        )
        key_public = subprocess.run(
            ["openssl", "pkey", "-pubout"], input=key,
            capture_output=True, check=False,
        )
    except OSError as exc:
        raise ValueError("openssl is required to validate TLS material") from exc
    if (
        cert_public.returncode != 0 or key_public.returncode != 0
        or not cert_public.stdout or cert_public.stdout != key_public.stdout
    ):
        raise ValueError("certificate and private key do not match")
    return certificate


def process_identity(pid: int) -> str | None:
    """Return a value that changes if a PID is recycled."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        return f"proc:{fields[19]}" if len(fields) > 19 else None
    except OSError:
        pass
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], text=True,
        capture_output=True, check=False,
    )
    started = result.stdout.strip()
    return f"ps:{hashlib.sha256(started.encode('utf-8')).hexdigest()}" if result.returncode == 0 and started else None


class CertificateHandler(http.server.BaseHTTPRequestHandler):
    certificate: bytes = b""

    def do_GET(self) -> None:
        if self.path != "/cert.pem":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-pem-file")
        self.send_header("Content-Length", str(len(self.certificate)))
        self.send_header("Content-Disposition", 'attachment; filename="cert.pem"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(self.certificate)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _monitor_parent(
    server: http.server.ThreadingHTTPServer, parent_pid: int, parent_identity: str,
) -> None:
    while True:
        if process_identity(parent_pid) != parent_identity:
            server.shutdown()
            return
        time.sleep(1)


class CertificateServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--parent-identity")
    parser.add_argument("--print-process-identity", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.validate_only:
        if args.cert is None or args.key is None:
            parser.error("--validate-only requires --cert and --key")
        validate_tls_material(args.cert, args.key)
        return 0
    if args.parent_pid is None:
        parser.error("--parent-pid is required")
    identity = process_identity(args.parent_pid)
    if args.print_process_identity:
        if identity is None:
            return 1
        print(identity)
        return 0
    if args.cert is None or args.key is None or not args.parent_identity:
        parser.error("--cert, --key, and --parent-identity are required")
    if identity != args.parent_identity:
        raise SystemExit("parent process identity does not match")
    CertificateHandler.certificate = validate_tls_material(args.cert, args.key)
    expected_hash = hashlib.sha256(CertificateHandler.certificate).hexdigest()
    print(f"certificate_sha256={expected_hash}", file=sys.stderr, flush=True)
    server = CertificateServer(("0.0.0.0", args.port), CertificateHandler)
    threading.Thread(
        target=_monitor_parent, args=(server, args.parent_pid, args.parent_identity), daemon=True,
    ).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
