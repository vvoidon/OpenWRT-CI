#!/usr/bin/env bash
# Shared helpers; callers enable errexit/nounset/pipefail.

retry() {
	local attempt
	for attempt in 1 2 3; do
		if "$@"; then
			return 0
		fi
		echo "Attempt $attempt/3 failed: $1" >&2
		if ((attempt < 3)); then
			sleep "${RETRY_DELAY:-5}"
		fi
	done
	return 1
}

locked_revision() {
	local repo=$1 ref=$2 kind=$3 sha
	: "${SOURCE_STATE_FILE:?SOURCE_STATE_FILE is required}"
	sha=$(awk -F'|' -v repo="$repo" -v ref="$ref" -v kind="$kind" \
		'$1 == repo && $2 == ref && $3 == kind { print $4 }' "$SOURCE_STATE_FILE")
	[[ $sha =~ ^[0-9a-f]{40}$ ]] || {
		echo "Missing or ambiguous source lock: $repo ($ref, $kind)" >&2
		return 1
	}
	printf '%s\n' "$sha"
}

checkout_locked() {
	local repo=$1 ref=$2 kind=$3 directory=$4 sha
	sha=$(locked_revision "$repo" "$ref" "$kind") || return 1
	if [[ ${REUSE_LOCKED_CHECKOUT:-false} == true && -d $directory/.git ]] &&
		[[ $(git -C "$directory" rev-parse HEAD) == "$sha" ]]; then
		return 0
	fi
	[[ ! -e $directory ]] || {
		echo "Refusing to overwrite checkout: $directory" >&2
		return 1
	}
	# Retry fetches in an empty checkout without deleting partial clones.
	git init -q "$directory" || return 1
	git -C "$directory" remote add origin "https://github.com/$repo.git" || return 1
	retry git -C "$directory" fetch --no-tags --depth=1 origin "$sha" || return 1
	git -C "$directory" -c advice.detachedHead=false checkout --detach "$sha" || return 1
	[[ $(git -C "$directory" rev-parse HEAD) == "$sha" ]]
}
