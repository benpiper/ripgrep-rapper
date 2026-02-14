import json
import subprocess
import re
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")


class QueryItem(BaseModel):
    query: str
    type: str  # "phone", "email", "name", "generic"


class SearchRequest(BaseModel):
    queries: List[QueryItem]
    search_path: str = "."
    context: int = 1
    fold: bool = True


class Match(BaseModel):
    line_number: int
    content: str
    is_match: bool = False
    file_path: Optional[str] = None


class SearchResult(BaseModel):
    matches: List[Match]
    total_matches: int
    original_query: str
    command_executed: str


def validate_search_path(search_path: str) -> str:
    """Validate and sanitize the search path to prevent command injection."""
    # Resolve to absolute path and normalize
    resolved = Path(search_path).resolve()

    # Block paths that don't exist
    if not resolved.exists():
        raise HTTPException(
            status_code=400, detail=f"Search path does not exist: {search_path}"
        )

    # Block sensitive system directories
    blocked_prefixes = ["/etc", "/proc", "/sys", "/dev", "/boot", "/sbin"]
    resolved_str = str(resolved)
    for prefix in blocked_prefixes:
        if resolved_str == prefix or resolved_str.startswith(prefix + "/"):
            raise HTTPException(
                status_code=403, detail=f"Access to {prefix} is not allowed"
            )

    return str(resolved)


def rg_escape(text: str) -> str:
    """Escape characters that are special in ripgrep's Rust regex engine."""
    special = set(r"\.^$*+?{}[]|()")
    return "".join("\\" + c if c in special else c for c in text)


def generate_variations(query: str, query_type: str) -> List[str]:
    variations = [query]
    if query_type == "phone":
        # Remove all non-digits
        digits = re.sub(r"\D", "", query)
        if len(digits) >= 10:
            # Assuming US format for now: 1234567890
            # 123-456-7890
            variations.append(f"{digits[:3]}-{digits[3:6]}-{digits[6:]}")
            # 1234567890
            variations.append(digits)
            # (123)456-7890
            variations.append(f"({digits[:3]}){digits[3:6]}-{digits[6:]}")
            # (123) 456-7890
            variations.append(f"({digits[:3]}) {digits[3:6]}-{digits[6:]}")
            # 123.456.7890
            variations.append(f"{digits[:3]}.{digits[3:6]}.{digits[6:]}")
            # 123 456-7890
            variations.append(f"{digits[:3]} {digits[3:6]}-{digits[6:]}")
    elif query_type == "name":
        parts = query.strip().split()
        if len(parts) == 2:
            first, last = parts
            variations.append(f"{first} {last}")
            variations.append(f"{last}, {first}")
            variations.append(f"{last},{first}")
        elif len(parts) >= 3:
            first, middle, last = parts[0], " ".join(parts[1:-1]), parts[-1]
            mi = parts[1][0]
            # First + last only (no middle)
            variations.append(f"{first} {last}")
            variations.append(f"{last}, {first}")
            variations.append(f"{last},{first}")
            # Space-separated orderings with middle
            variations.append(f"{first} {middle} {last}")
            variations.append(f"{first} {mi}. {last}")
            variations.append(f"{first} {mi} {last}")
            variations.append(f"{last}, {first} {middle}")
            variations.append(f"{last}, {first} {mi}")
            variations.append(f"{last}, {first} {mi}.")
            # Comma-separated (CSV) orderings
            variations.append(f"{first},{middle},{last}")
            variations.append(f"{first},{mi},{last}")
            variations.append(f"{last},{first},{middle}")
            variations.append(f"{last},{first},{mi}")
    elif query_type == "email":
        # Just the query for now, maybe case variations? rg is case sensitive
        pass
    elif query_type == "generic":
        # Auto-detect: if mostly digits, treat as phone; if 2-3 words, treat as name
        stripped = query.strip()
        digits_only = re.sub(r"\D", "", stripped)
        words = stripped.split()
        if len(digits_only) >= 7 and len(digits_only) / max(len(stripped), 1) > 0.5:
            variations = generate_variations(query, "phone")
        elif len(words) in (2, 3) and all(w.replace(".", "").isalpha() for w in words):
            variations = generate_variations(query, "name")
    return list(set(variations))


def fold_line(line: str, query_variations: List[str], max_len: int = 1000) -> str:
    # A simple fold strategy: keep the match and some surrounding context
    # This is a bit complex to do perfectly with multiple variations,
    # so we'll start by just checking if line is too long.
    if len(line) <= max_len:
        return line

    # Try to find the match index
    min_idx = len(line)
    line_lower = line.lower()
    for var in query_variations:
        idx = line_lower.find(var.lower())
        if idx != -1 and idx < min_idx:
            min_idx = idx

    if min_idx == len(line):
        # No match found in this line (might be context line), just truncate
        return line[:max_len] + "..."

    # We found a match. ensure we show plenty of context up to max_len.
    # Center the match in the window if possible.
    half_window = max_len // 2

    # Initial start/end calculation
    start = max(0, min_idx - half_window)
    end = min(len(line), start + max_len)

    # If we hit the end, we might have more space at the beginning
    if (end - start) < max_len:
        start = max(0, end - max_len)

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(line) else ""

    return f"{prefix}{line[start:end]}{suffix}"


def prepare_search_command(request: SearchRequest):
    # Validate and sanitize the search path
    safe_path = validate_search_path(request.search_path)

    all_variations = []
    regex_patterns = []  # Additional regex patterns (e.g. name wildcards)

    for item in request.queries:
        variations = generate_variations(item.query, item.type)
        all_variations.extend(variations)

        # For name queries, add regex wildcards to catch any middle name/initial
        # Apply for explicit "name" type or generic auto-detected as name
        is_name_query = item.type == "name"
        if item.type == "generic":
            words = item.query.strip().split()
            is_name_query = len(words) in (2, 3) and all(
                w.replace(".", "").isalpha() for w in words
            )

        if is_name_query:
            parts = item.query.strip().split()
            first, last = parts[0], parts[-1]
            ef, el = rg_escape(first), rg_escape(last)
            # "John <middle> Smith" or "John,<middle>,Smith"
            regex_patterns.append(f"{ef}[,\\s]+\\S+[,\\s]+{el}")
            # "Smith, John <middle>" or "Smith,John,<middle>"
            regex_patterns.append(f"{el}[,\\s]+{ef}[,\\s]+\\S+")

    # Deduplicate global variations
    all_variations = list(set(all_variations))

    cmd = ["rg", "--json", "-i", "--max-count", "10000"]
    # Add literal variations escaped for rg regex
    for var in all_variations:
        cmd.extend(["-e", rg_escape(var)])
    # Add regex wildcard patterns (for name matching with unknown middle names)
    for pat in regex_patterns:
        cmd.extend(["-e", pat])

    cmd.extend(["-C", str(request.context)])

    # Use the validated search path
    cmd.append(safe_path)

    return cmd, all_variations


def format_command_for_display(cmd: List[str]) -> str:
    # Custom formatter to enforce single quotes around -e arguments' values for display
    # Re-impl strategy: iterate and format
    formatted = []
    skip_next = False
    for j, arg in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue

        if arg == "-e":
            formatted.append("-e")
            if j + 1 < len(cmd):
                val = cmd[j + 1]
                formatted.append(f"'{val}'")
                skip_next = True
        else:
            # Simple handling for other args
            if " " in arg:
                formatted.append(f'"{arg}"')
            else:
                formatted.append(arg)

    return " ".join(formatted)


@app.post("/search/preview")
async def search_preview(request: SearchRequest):
    try:
        cmd, _ = prepare_search_command(request)
        command_str = format_command_for_display(cmd)
        return {"command_executed": command_str}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search")
async def search(request: SearchRequest):
    try:
        cmd, all_variations = prepare_search_command(request)

        # Create a display string for the command
        command_str = format_command_for_display(cmd)

        process = subprocess.run(cmd, capture_output=True, text=True, cwd=".")

        output_lines = process.stdout.splitlines()
        matches_count = 0

        # Parse rg --json output

        processed_matches = []

        for line in output_lines:
            try:
                data = json.loads(line)
                if data["type"] == "match":
                    matches_count += 1
                    content = data["data"]["lines"]["text"].rstrip()
                    file_path = data["data"]["path"]["text"]
                    if request.fold:
                        content = fold_line(content, all_variations)
                    processed_matches.append(
                        Match(
                            line_number=data["data"]["line_number"],
                            content=content,
                            is_match=True,
                            file_path=file_path,
                        )
                    )
                elif data["type"] == "context":
                    content = data["data"]["lines"]["text"].rstrip()
                    file_path = data["data"]["path"]["text"]
                    if request.fold:
                        # Context usually doesn't have the match, so standard truncate
                        if len(content) > 1000:
                            content = content[:1000] + "..."
                    processed_matches.append(
                        Match(
                            line_number=data["data"]["line_number"],
                            content=content,
                            is_match=False,
                            file_path=file_path,
                        )
                    )
            except json.JSONDecodeError:
                continue

        return {
            "matches": processed_matches,
            "total_matches": matches_count,
            "original_query": str([q.query for q in request.queries]),
            "variations": all_variations,
            "command_executed": command_str,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search/stream")
async def search_stream(request: SearchRequest):
    """Stream search results as newline-delimited JSON events."""
    cmd, all_variations = prepare_search_command(request)
    command_str = format_command_for_display(cmd)

    def generate():
        # First event: preview with command and variations
        yield (
            json.dumps(
                {
                    "event": "preview",
                    "command_executed": command_str,
                    "variations": all_variations,
                }
            )
            + "\n"
        )

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=".",
            bufsize=1,
        )

        matches_count = 0
        try:
            for line in process.stdout:
                try:
                    data = json.loads(line)
                    if data["type"] == "match":
                        matches_count += 1
                        content = data["data"]["lines"]["text"].rstrip()
                        file_path = data["data"]["path"]["text"]
                        if request.fold:
                            content = fold_line(content, all_variations)
                        yield (
                            json.dumps(
                                {
                                    "event": "match",
                                    "line_number": data["data"]["line_number"],
                                    "content": content,
                                    "is_match": True,
                                    "file_path": file_path,
                                    "count": matches_count,
                                }
                            )
                            + "\n"
                        )
                    elif data["type"] == "context":
                        content = data["data"]["lines"]["text"].rstrip()
                        file_path = data["data"]["path"]["text"]
                        if request.fold and len(content) > 1000:
                            content = content[:1000] + "..."
                        yield (
                            json.dumps(
                                {
                                    "event": "context",
                                    "line_number": data["data"]["line_number"],
                                    "content": content,
                                    "is_match": False,
                                    "file_path": file_path,
                                    "count": matches_count,
                                }
                            )
                            + "\n"
                        )
                except json.JSONDecodeError:
                    continue

            process.wait()
        except Exception:
            process.kill()
            process.wait()

        # Final event: done
        yield (
            json.dumps(
                {
                    "event": "done",
                    "total_matches": matches_count,
                    "original_query": str([q.query for q in request.queries]),
                    "variations": all_variations,
                    "command_executed": command_str,
                }
            )
            + "\n"
        )

    from starlette.responses import StreamingResponse

    return StreamingResponse(generate(), media_type="application/x-ndjson")


class PathInfoRequest(BaseModel):
    search_path: str = "."


@app.post("/search/pathinfo")
async def path_info(request: PathInfoRequest):
    """Return resolved path, total size, file count, and estimated search time."""
    try:
        safe_path = validate_search_path(request.search_path)
        resolved = Path(safe_path)

        total_size = 0
        file_count = 0

        if resolved.is_file():
            total_size = resolved.stat().st_size
            file_count = 1
        elif resolved.is_dir():
            for f in resolved.rglob("*"):
                if f.is_file():
                    try:
                        total_size += f.stat().st_size
                        file_count += 1
                    except (PermissionError, OSError):
                        continue

        # 10k RPM disk: ~150 MB/s sequential read
        disk_speed_bps = 150 * 1024 * 1024
        est_seconds = total_size / disk_speed_bps if total_size > 0 else 0

        return {
            "resolved_path": safe_path,
            "total_size_bytes": total_size,
            "file_count": file_count,
            "est_search_seconds": round(est_seconds, 2),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("static/index.html", "r") as f:
        return f.read()
