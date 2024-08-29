#!/bin/bash

set -e

function get_param() {
    if [[ "$#" -ne 1 ]]; then
        (>&2 echo "$0: incorrect number of arguments to ${FUNCNAME[0]}")
        exit 1
    fi

    local raw
    raw="$(aws ssm get-parameter --name "$1" --with-decryption)"

    # if the parameter name is bad or something else goes wrong, aws ssm
    # will print a message to stderr and $raw will be empty
    if [[ -z "$raw" ]]; then
        exit 2
    fi

    # parse them and write to stdout
    local cred
    cred="$(echo "$raw" | jq -r .Parameter.Value)"

    echo "${cred//,/\n}"
}

# The database master password
if [ -n "$DATABASE_PASSWORD_KEY" ] && [ -n "$DATABASE_PASSWORD" ]; then
    echo "Cannot set both DATABASE_PASSWORD_KEY and DATABASE_PASSWORD"
    exit 1
fi

if [ -n "$DATABASE_PASSWORD_KEY" ]; then
    DATABASE_PASSWORD="$(get_param "$DATABASE_PASSWORD_KEY")"
fi

# Non-password variables here are assumed to be in the environment
cat > ~/.odbc.ini << EOF
[Database]
Driver = /usr/lib/$(uname -m)-linux-gnu/odbc/psqlodbcw.so
Servername = $DATABASE_HOST
Port = $DATABASE_PORT
Database = $DATABASE_DBNAME
UserName = $DATABASE_USERNAME
Password = $DATABASE_PASSWORD
BoolsAsChar = 0
EOF

exec "$@"

