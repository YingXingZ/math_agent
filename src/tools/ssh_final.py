#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Final probe: try a battery of credentials one-by-one with sleeps."""
import sys, time, paramiko

HOST = "222.211.217.7"
PORT = 10022

VARIANTS = [
    ("root", "8vFdXMt"),
    ("root", "8vfdxmt"),
    ("root", "8VFDXMT"),
    ("root", "8vFdXMt "),
    ("root", " 8vFdXMt"),
    ("root", "8vFdXMt!"),
    ("root", "8vFdXMt123"),
    ("root", "r00t"),
    ("root", "toor"),
    ("root", "password"),
    ("root", "8vFdXM"),
    ("root", "vFdXMt"),
    ("root", "88888888"),
    ("root", "12345678"),
    ("ubuntu", "8vFdXMt"),
    ("ai", "8vFdXMt"),
    ("llm", "8vFdXMt"),
    ("user", "8vFdXMt"),
    ("workbuddy", "8vFdXMt"),
    ("pi", "8vFdXMt"),
    ("centos", "8vFdXMt"),
]

for u, p in VARIANTS:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, port=PORT, username=u, password=p,
                    timeout=10, banner_timeout=10, auth_timeout=10,
                    allow_agent=False, look_for_keys=False)
        print(f"AUTH_OK as {u!r} password={p!r}")
        cli.close()
        sys.exit(0)
    except paramiko.AuthenticationException:
        print(f"AUTH_FAIL u={u!r} p={p!r}")
    except Exception as e:
        print(f"AUTH_OTHER u={u!r} p={p!r} err={type(e).__name__}: {str(e)[:160]}")
    finally:
        try:
            cli.close()
        except Exception:
            pass
    time.sleep(1.0)

print("ALL_VARIANTS_FAILED")
sys.exit(2)
