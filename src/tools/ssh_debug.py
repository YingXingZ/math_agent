#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Debug SSH connection - show full banner and policy."""
import sys
import socket
import paramiko
import traceback

HOST = "222.211.217.7"
PORT = 10022
USER = "root"
PW = "8vFdXMt"


def main() -> int:
    # Probe TCP first
    try:
        s = socket.create_connection((HOST, PORT), timeout=8)
        print(f"TCP_OK host={HOST} port={PORT}")
        s.close()
    except Exception as e:
        print(f"TCP_FAIL: {e}")
        return 1

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, port=PORT, username=USER, password=PW,
                    timeout=10, banner_timeout=10, auth_timeout=10,
                    allow_agent=False, look_for_keys=False)
        print("AUTH_OK")
        cli.close()
        return 0
    except Exception as e:
        print(f"AUTH_FAIL type={type(e).__name__}")
        print(str(e)[:500])
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
