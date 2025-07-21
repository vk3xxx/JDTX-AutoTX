#!/usr/bin/env python3
import cgi
import cgitb
import json
import os

cgitb.enable()
status_file = '/tmp/autotx73_status.json'
command_file = '/tmp/autotx73_command.txt'

print("Content-Type: application/json\n")

form = cgi.FieldStorage()
action = form.getvalue('action')

# Handle control commands
if action in ['enable', 'disable', 'quit']:
    with open(command_file, 'w') as f:
        f.write(action)
    print(json.dumps({'result': 'ok', 'action': action}))
    exit(0)

# Return status
if os.path.exists(status_file):
    with open(status_file) as f:
        status = json.load(f)
else:
    status = {'enabled': False, 'tx': False, 'qso_partner': None, 'messages': []}

print(json.dumps(status)) 