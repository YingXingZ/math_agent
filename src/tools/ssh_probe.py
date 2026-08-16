#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Probe SSH: get banner + try keyboard-interactive + alternate usernames."""
import sys
import socket
import paramiko

HOST = "222.211.217.7"
PORT = 10022


def banner():
    s = socket.create_connection((HOST, PORT), timeout=8)
    s.settimeout(5)
    try:
        b = s.recv(512).decode("utf-8", "replace")
    except Exception:
        b = "<no banner in time>"
    s.close()
    print(f"BANNER: {b.strip()!r}")


def try_auth(user, pw):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, port=PORT, username=user, password=pw,
                    timeout=10, banner_timeout=10, auth_timeout=10,
                    allow_agent=False, look_for_keys=False)
        print(f"AUTH_OK as {user}")
        cli.close()
        return True
    except paramiko.AuthenticationException:
        print(f"AUTH_FAIL password as {user}")
    except Exception as e:
        print(f"AUTH_OTHER {type(e).__name__} as {user}: {e}")
    return False


def main():
    banner()
    for u in ["root", "ubuntu", "admin", "user", "deploy", "workbuddy", "ai", "llm"]:
        for pw in [
            "8vFdXMt",
            "8vFdXMt" + "\n",
            " 8vFdXMt",
        ]:
            if try_auth(u, pw):
                return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
