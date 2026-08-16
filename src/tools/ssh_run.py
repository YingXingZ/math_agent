#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Small helper: SSH into the 80GA100 box and run a shell command."""
import sys
import paramiko

HOST = "222.211.217.7"
PORT = 10022
USER = "root"
PW = "8vFdXMt@&s8cXM9D"


def run(cmd: str, timeout: int = 30) -> int:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER, password=PW,
                timeout=10, banner_timeout=10, auth_timeout=10)
    try:
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        if err:
            sys.stderr.write(err)
            if not err.endswith("\n"):
                sys.stderr.write("\n")
        return stdout.channel.recv_exit_status()
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(run(" ".join(sys.argv[1:])))
