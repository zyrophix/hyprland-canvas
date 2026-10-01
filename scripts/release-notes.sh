#!/usr/bin/env bash
# release-notes.sh — a changelog entry, written from the commit subjects.
#
# Usage: release-notes.sh <version> [<since-ref> [<until-ref>]]
#   release-notes.sh 1.7.0              # since the most recent tag, up to HEAD
#   release-notes.sh 1.7.0 v1.6.0       # since a specific tag, up to HEAD
#   release-notes.sh 1.5.0 v1.4.2 v1.5.0 # a closed range, for back-filling
#
# The third argument is what you need when you are rewriting a release that is
# already tagged: <since>..HEAD would swallow every commit made since, because
# the tag is behind HEAD and HEAD moves.
#
# It prints the block. Paste it at the top of CHANGELOG.md under the newest
# version, or pipe it wherever you keep your notes.
#
# The commit subject IS the entry. That is the whole idea and the whole cost:
# the changelog inherits the quality of your subjects exactly, with no room to
# polish it afterwards. So write subjects that would make sense to somebody who
# did not write them — what changed, and when the reason is not obvious, why.
#
# Merge commits are skipped: they are plumbing, not a change a reader cares
# about. Squash before merging instead of recording the plumbing.
set -euo pipefail

VERSION="${1:-}"
if [ "$#" -lt 1 ] || [ -z "$VERSION" ]; then
  echo "usage: release-notes.sh <version> [<since-ref> [<until-ref>]]" >&2
  exit 1
fi

# Operate on the repository that CONTAINS this script, not on whatever happens to
# be the working directory. Run from a subdirectory of another repo, the first
# version of this printed that repo's commits into your changelog, silently.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
git rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git repository: $REPO" >&2; exit 1; }

if [ "$#" -ge 3 ]; then
  SINCE="$2"; UNTIL="$3"; RANGE="$SINCE..$UNTIL"
elif [ "$#" -ge 2 ]; then
  SINCE="$2"; UNTIL="HEAD"; RANGE="$SINCE..HEAD"
else
  SINCE="$(git describe --tags --abbrev=0 2>/dev/null || true)"
  UNTIL="HEAD"
  if [ -n "$SINCE" ]; then RANGE="$SINCE..HEAD"; else RANGE="HEAD"; fi
fi

# A ref that does not resolve must stop the script. The first version printed a
# version heading with an empty list under it, which is worse than failing: it
# looks exactly like a release in which nothing changed.
# Validate the refs themselves, not the range — `rev-parse --verify` rejects
# range expressions, so checking the range rejects every valid range.
for ref in "$SINCE" "$UNTIL"; do
  [ -n "$ref" ] || continue
  git rev-parse --verify --quiet "$ref^{commit}" >/dev/null \
    || { echo "no such ref: $ref (known tags: $(git tag | tr '\n' ' '))" >&2; exit 1; }
done

COUNT="$(git rev-list --count --no-merges "$RANGE" 2>/dev/null || true)"
if [ -z "$COUNT" ]; then
  echo "cannot count commits in $RANGE" >&2
  exit 1
fi
if [ "$COUNT" -eq 0 ]; then
  echo "no commits in $RANGE" >&2
  exit 1
fi

printf '### %s\n\n' "$VERSION"
git log --no-merges --format='- %s' "$RANGE"
