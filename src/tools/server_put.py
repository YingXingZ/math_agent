#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SFTP upload helper for the A100 box. Usage: python server_put.py <local> <remote>"""
import sys
import paramiko

HOST, PORT, USER, PW = "222.211.217.7", 10022, "root", "8vFdXMt@&s8cXM9D"


def main():
    local, remote = sys.argv[1], sys.argv[2]
    t = paramiko.Transport((HOST, PORT))
    t.connect(username=USER, password=PW)
    sftp = paramiko.SFTPClient.from_transport(t)
    sftp.put(local, remote)
    sftp.close()
    t.close()
    print("uploaded", local, "->", remote)


if __name__ == "__main__":
    main()
