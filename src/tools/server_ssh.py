#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Robust SSH/SFTP helper for the A100 box (retries on flaky banner errors).

Usage:
    python server_ssh.py run <timeout_seconds> <command...>
    python server_ssh.py put <local_path> <remote_path>
    python server_ssh.py get <remote_path> <local_path>
"""
import sys
import time

import paramiko

# Remote OCR output contains LaTeX symbols such as U+2212.  Do not let a
# Windows GBK console turn an otherwise successful worker invocation into a
# false failure while printing its result.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

HOST, PORT, USER, PW = "222.211.217.7", 10022, "root", "8vFdXMt@&s8cXM9D"


def _connect_client():
    last = None
    for i in range(8):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HOST, port=PORT, username=USER, password=PW,
                      timeout=15, banner_timeout=15, auth_timeout=15)
            return c
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 2 * i)
    raise RuntimeError(f"ssh connect failed after retries: {last}")


def run(cmd, timeout=120):
    c = _connect_client()
    try:
        _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        c.close()


def put(local, remote):
    c = _connect_client()
    try:
        sftp = c.open_sftp()
        sftp.put(local, remote)
        sftp.close()
    finally:
        c.close()


def get(remote, local):
    c = _connect_client()
    try:
        sftp = c.open_sftp()
        sftp.get(remote, local)
        sftp.close()
    finally:
        c.close()


def main():
    mode = sys.argv[1]
    if mode == "run":
        timeout = int(sys.argv[2])
        cmd = " ".join(sys.argv[3:])
        rc, out, err = run(cmd, timeout=timeout)
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        if err:
            sys.stderr.write(err)
            if not err.endswith("\n"):
                sys.stderr.write("\n")
        sys.exit(rc)
    elif mode == "put":
        put(sys.argv[2], sys.argv[3])
        print("uploaded", sys.argv[2], "->", sys.argv[3])
    elif mode == "get":
        get(sys.argv[2], sys.argv[3])
        print("downloaded", sys.argv[2], "->", sys.argv[3])
    else:
        print("unknown mode", mode, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
