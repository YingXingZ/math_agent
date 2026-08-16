import paramiko, sys
HOST="222.211.217.7"; PORT=10022; USER="root"; PW="8vFdXMt@&s8cXM9D"
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, port=PORT, username=USER, password=PW, timeout=10)
sftp = cli.open_sftp()
remote, local = sys.argv[1], sys.argv[2]
sftp.get(remote, local)
sftp.close()
print("OK", local)
cli.close()
