#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Check available SSH auth methods and try kbd-interactive."""
import sys, paramiko

HOST, PORT, USER, PW = "222.211.217.7", 10022, "root", "8vFdXMt"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
t = paramiko.Transport((HOST, PORT))
t.banner_timeout = 10
t.start_client()
try:
    print("negotiated:", t.is_active())
    auths = t.auth_none(USER)
    print(f"server allows: {auths}")
finally:
    t.close()

# Try with look_for_keys off, but allow keyboard-interactive
cli2 = paramiko.SSHClient()
cli2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    cli2.connect(HOST, port=PORT, username=USER, password=PW,
                 timeout=10, banner_timeout=10, auth_timeout=10,
                 allow_agent=False, look_for_keys=False)
    print("AUTH_OK")
    cli2.close()
except paramiko.AuthenticationException as e:
    print(f"AUTH_FAIL: {e}")
except Exception as e:
    print(f"OTHER: {type(e).__name__}: {e}")
