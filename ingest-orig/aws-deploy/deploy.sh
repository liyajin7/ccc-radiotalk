#!/bin/bash

## Create repos (if necessary), build, tag and push
declare -a target_repos=("worker")
export AWS_ACCOUNT_ID="$(aws sts get-caller-identity | jq -r .Account)"

declare -a current_repos
lines="$(aws ecr describe-repositories | jq -r '.repositories[] | .repositoryName')"
mapfile -t current_repos <<< "$lines"
# mapfile: built-in Bash command that reads lines from the standard input (or from a file) and stores them in an array
# -t: tells mapfile to omit the trailing newline characters from each line it reads
# current_repos: name of the array variable that will store the lines of input
# <<<: passes the content of a string to the command on the left side

## New docker login for push
$(aws ecr get-login-password)
# aws ecr get-login-password | docker login --username AWS --password-stdin 021891577602.dkr.ecr.us-east-1.amazonaws.com
#$(aws ecr get-login --no-include-email) #  docker login for push
                                         #  deprection of the command get-login --no-include-email in awscli version 1.7.10
                                         #  aws ecr get-login-password | docker login --username AWS --password-stdin 1234567890.dkr.ecr.us-west-2.amazonaws.com/reponame

for target in "${target_repos[@]}"; do
    echo "Processing $target"
    rname="${ApplicationName}/${target}"
    repo_url="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$rname:latest"

    crt=1
    for c in "${current_repos[@]}"; do
        if [ "$c" == "$rname" ]; then
            crt=0 #already exists
        fi
    done

    if [ "$crt" -ne 0 ]; then
        echo "Creating repo $rname"
        aws ecr create-repository --repository-name "$rname"
    fi

    docker build -t "$rname" images/"$target"
    docker tag "$rname:latest" "$repo_url"
    docker push "$repo_url"
done

