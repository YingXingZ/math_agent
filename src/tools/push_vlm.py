import paramiko, sys, time
HOST="222.211.217.7"; PORT=10022; USER="root"; PW="8vFdXMt@&s8cXM9D"
LOCAL=sys.argv[1]; REMOTE="/opt/math-vlm/" + sys.argv[2]
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, port=PORT, username=USER, password=PW, timeout=10)
sftp = cli.open_sftp()
sftp.put(LOCAL, REMOTE)
sftp.chmod(REMOTE, 0o644)
sftp.close()
print("uploaded", REMOTE)
cli.close()
