#!/usr/bin/env bash

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

WORKSPACE=${GITHUB_WORKSPACE:-$(pwd)}
STATE_FILE=${SOURCE_STATE_FILE:-$WORKSPACE/source-state.txt}
PREVIOUS_STATE=${PREVIOUS_SOURCE_STATE:-}
FORCE_BUILD=${FORCE_BUILD:-false}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"
PREVIOUS_COVERAGE=${PREVIOUS_SOURCE_COVERAGE:-$(dirname "${PREVIOUS_STATE:-.}")/source-coverage.json}

mkdir -p "$(dirname "$STATE_FILE")"
current_tmp=$(mktemp)
ignored_tmp=$(mktemp)
trap 'rm -f "$current_tmp" "$ignored_tmp"' EXIT
sources=$(python3 "$SCRIPT_DIR/source_inputs.py" sources)

while IFS= read -r source; do
	IFS='|' read -r repo ref kind <<< "$source"
	if [[ $ref == HEAD ]]; then
		remote_ref=HEAD
	else
		remote_ref="refs/heads/$ref"
	fi

	sha=$(retry git ls-remote "https://github.com/$repo.git" "$remote_ref" | awk 'NR == 1 { print $1 }')
	if [[ ! $sha =~ ^[0-9a-f]{40}$ ]]; then
		echo "Unable to resolve $repo ($ref)" >&2
		exit 1
	fi
	printf '%s|%s|%s|%s\n' "$repo" "$ref" "$kind" "$sha" >> "$current_tmp"
done <<< "$sources"

# CI 配置本身也是 RE-CS-08 构建输入。
printf '%s|main|ci|%s\n' "$GITHUB_REPOSITORY" "$GITHUB_SHA" >> "$current_tmp"
LC_ALL=C sort -u "$current_tmp" > "$STATE_FILE"
fingerprint=$(sha256sum "$STATE_FILE" | awk '{ print $1 }')

has_relevant_diff() {
	local repo=$1
	local kind=$2
	local old_sha=$3
	local new_sha=$4
	local compare_dir result

	compare_dir=$(mktemp -d)
	git -C "$compare_dir" init -q

	if ! retry git -C "$compare_dir" fetch -q --no-tags --depth=1 \
		"https://github.com/$repo.git" "$old_sha"; then
		rm -rf "$compare_dir"
		return 0
	fi
	git -C "$compare_dir" tag old-source FETCH_HEAD

	if ! retry git -C "$compare_dir" fetch -q --no-tags --depth=1 \
		"https://github.com/$repo.git" "$new_sha"; then
		rm -rf "$compare_dir"
		return 0
	fi
	git -C "$compare_dir" tag new-source FETCH_HEAD

	# --no-renames includes both sides of moves. A diff/filter failure rebuilds.
	if ! git -C "$compare_dir" diff --no-renames --name-only -z old-source new-source \
		> "$compare_dir/changed-paths"; then
		rm -rf "$compare_dir"
		return 0
	fi
	result=0
	python3 "$SCRIPT_DIR/source_inputs.py" relevant --repo "$repo" --kind "$kind" \
		--state "$PREVIOUS_STATE" --coverage "$PREVIOUS_COVERAGE" \
		--paths "$compare_dir/changed-paths" --classify --root "$compare_dir" || result=$?
	rm -rf "$compare_dir"
	[[ $result -eq 10 ]] && return 1
	[[ $result -eq 20 ]] && { change_mode=repository; return 0; }
	change_mode=firmware
	return 0
}

should_build=false
build_mode=none
declare -a reasons=()

if [[ ${FORCE_BUILD,,} == true ]]; then
	should_build=true
	build_mode=firmware
	reasons+=("manual force build")
elif [[ ! -s $PREVIOUS_STATE ]]; then
	should_build=true
	build_mode=firmware
	reasons+=("no previous successful source state")
else
	while IFS='|' read -r repo ref kind new_sha; do
		old_sha=$(awk -F'|' -v repo="$repo" -v ref="$ref" -v kind="$kind" \
			'$1 == repo && $2 == ref && $3 == kind { print $4; exit }' "$PREVIOUS_STATE")

		if [[ ! $old_sha =~ ^[0-9a-f]{40}$ ]]; then
			should_build=true
			build_mode=firmware
			reasons+=("new or invalid previous build input: $repo ($ref)")
			continue
		fi
		if [[ $old_sha == "$new_sha" ]]; then
			continue
		fi

		if [[ $kind == ci ]]; then
			should_build=true
			build_mode=firmware
			reasons+=("CI configuration changed")
		else
			change_mode=firmware
			if has_relevant_diff "$repo" "$kind" "$old_sha" "$new_sha"; then
				should_build=true
				if [[ $change_mode == firmware ]]; then
					build_mode=firmware
				elif [[ $build_mode == none ]]; then
					build_mode=repository
				fi
				reasons+=("relevant source changed: $repo ($ref)")
			else
				echo "Ignored documentation or unrelated target changes: $repo ($ref)"
				# Preserve the actual build lock rather than claiming ignored sources were built.
				printf '%s|%s\n' "$repo" "$old_sha" >> "$ignored_tmp"
			fi
		fi
	done < "$STATE_FILE"
fi
awk -F'|' -v OFS='|' 'FILENAME == ARGV[1] {old[$1]=$2; next} {if ($1 in old) $4=old[$1]; print}' \
	"$ignored_tmp" "$STATE_FILE" > "$current_tmp"
cp "$current_tmp" "$STATE_FILE"
fingerprint=$(sha256sum "$STATE_FILE" | awk '{ print $1 }')

{
	echo "should_build=$should_build"
	echo "build_mode=$build_mode"
	echo "fingerprint=$fingerprint"
	echo "reason<<SOURCE_CHECK_EOF"
	if ((${#reasons[@]})); then
		printf '%s\n' "${reasons[@]}"
	else
		echo "no RE-CS-08 related source changes"
	fi
	echo "SOURCE_CHECK_EOF"
} >> "$GITHUB_OUTPUT"

echo "Source fingerprint: $fingerprint"
printf 'Build decision: %s\n' "$should_build"
printf 'Reason: %s\n' "${reasons[*]:-no RE-CS-08 related source changes}"
