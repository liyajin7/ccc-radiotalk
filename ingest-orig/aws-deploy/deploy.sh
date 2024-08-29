#!/bin/bash

## Create repos (if necessary), build, tag and push
declare -a target_repos=("worker")
export AWS_ACCOUNT_ID="$(aws sts get-caller-identity | jq -r .Account)"

declare -a current_repos
lines="$(aws ecr describe-repositories | jq -r '.repositories[] | .repositoryName')"
mapfile -t current_repos <<< "$lines"

$(aws ecr get-login --no-include-email) #  docker login for push

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

