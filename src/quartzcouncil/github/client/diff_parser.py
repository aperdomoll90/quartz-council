from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DiffLineRange:
    """Valid line range for inline comments in a file's diff."""
    valid_lines: set[int]


# Matches hunk headers like: @@ -10,5 +12,8 @@
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_patch_valid_lines(patch: str) -> set[int]:
    """
    Parse a unified diff patch and return the set of valid line numbers
    for inline comments (new-side lines: additions and context).

    GitHub's review API only accepts lines that appear in the diff's "new" side:
    - Lines starting with '+' (additions)
    - Lines starting with ' ' (context)
    - NOT lines starting with '-' (deletions)
    """
    if not patch:
        return set()

    valid_lines: set[int] = set()
    current_new_line = 0
    in_hunk = False

    for line in patch.split("\n"):
        # Check for hunk header
        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match:
            # Start of new hunk - get the starting line number for new file
            current_new_line = int(hunk_match.group(1))
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if not line:
            continue

        prefix = line[0] if line else ""

        if prefix == "+":
            # Addition - valid for comments, increment new line counter
            valid_lines.add(current_new_line)
            current_new_line += 1
        elif prefix == "-":
            # Deletion - NOT valid for comments, don't increment new line counter
            pass
        elif prefix == " ":
            # Context line - valid for comments, increment new line counter
            valid_lines.add(current_new_line)
            current_new_line += 1
        elif prefix == "\\":
            # "\ No newline at end of file" - skip
            pass
        else:
            # Unknown line type or end of hunk
            pass

    return valid_lines


def build_file_line_map(files: list[dict]) -> dict[str, set[int]]:
    """
    Build a mapping of filename -> valid line numbers from PR files.

    Args:
        files: List of dicts with 'filename' and 'patch' keys
               (matches PullRequestFile structure)

    Returns:
        Dict mapping filename to set of valid line numbers
    """
    result: dict[str, set[int]] = {}

    for file_data in files:
        filename = file_data.get("filename") or ""
        patch = file_data.get("patch") or ""

        if filename and patch:
            result[filename] = parse_patch_valid_lines(patch)

    return result


def is_comment_line_valid(
    filename: str,
    line_number: int,
    file_line_map: dict[str, set[int]],
) -> bool:
    """
    Check if a comment's line number is valid for the given file.

    Returns True if the line exists in the diff's new side.
    Returns False if the file isn't in the map or line isn't valid.
    """
    valid_lines = file_line_map.get(filename)
    if valid_lines is None:
        return False
    return line_number in valid_lines


def snap_to_nearest_valid_line(
    filename: str,
    line_number: int,
    file_line_map: dict[str, set[int]],
    max_distance: int = 5,
) -> int | None:
    """
    Find the closest valid line number for a comment.

    If the exact line is valid, returns it unchanged.
    Otherwise finds the nearest valid line within max_distance.
    Returns None if no valid line is close enough (to avoid misplaced comments).
    """
    valid_lines = file_line_map.get(filename)
    if not valid_lines:
        return None

    # If exact line is valid, use it
    if line_number in valid_lines:
        return line_number

    # Find the closest valid line within max_distance
    sorted_lines = sorted(valid_lines)
    best_line = None
    best_distance = float("inf")

    for valid_line in sorted_lines:
        distance = abs(valid_line - line_number)
        # Only consider lines within max_distance
        if distance <= max_distance and distance < best_distance:
            best_distance = distance
            best_line = valid_line

    return best_line


def extract_line_from_patch(patch: str, target_line: int) -> str | None:
    """
    Extract the source code at a specific line number from a patch.
    Returns the line content without the diff prefix (+/- / ).
    """
    if not patch:
        return None

    current_new_line = 0
    in_hunk = False

    for line in patch.split("\n"):
        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match:
            current_new_line = int(hunk_match.group(1))
            in_hunk = True
            continue

        if not in_hunk or not line:
            continue

        prefix = line[0] if line else ""

        if prefix == "+":
            if current_new_line == target_line:
                return line[1:]  # Remove the + prefix
            current_new_line += 1
        elif prefix == "-":
            pass  # Deletions don't have line numbers
        elif prefix == " ":
            if current_new_line == target_line:
                return line[1:]  # Remove the space prefix
            current_new_line += 1

    return None
