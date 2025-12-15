import subprocess
import re
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

class QueryItem(BaseModel):
    query: str
    type: str # "phone", "email", "name", "generic"

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

def generate_variations(query: str, type: str) -> List[str]:
    variations = [query]
    if type == "phone":
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
    elif type == "email":
        # Just the query for now, maybe case variations? rg is case sensitive
        pass
    return list(set(variations))

def fold_line(line: str, query_variations: List[str], max_len: int = 1000) -> str:
    # A simple fold strategy: keep the match and some surrounding context
    # This is a bit complex to do perfectly with multiple variations, 
    # so we'll start by just checking if line is too long.
    if len(line) <= max_len:
        return line
    
    # Try to find the match index
    min_idx = len(line)
    matched_var = ""
    line_lower = line.lower()
    for var in query_variations:
        idx = line_lower.find(var.lower())
        if idx != -1 and idx < min_idx:
            min_idx = idx
            matched_var = var
            
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
    all_variations = []
    for item in request.queries:
        variations = generate_variations(item.query, item.type)
        all_variations.extend(variations)
    
    # Deduplicate global variations
    all_variations = list(set(all_variations))
    
    # Escape special characters for rg regex: . ( )
    # Use list for execution
    cmd = ["rg", "--json", "-i"]
    for var in all_variations:
        # Escape for regex correctness (searching literals)
        safe_var = var.replace(".", r"\.").replace("(", r"\(").replace(")", r"\)")
        cmd.extend(["-e", safe_var])
    
    cmd.extend(["-C", str(request.context)])
    
    # Use provided search path
    cmd.append(request.search_path)
    
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
                val = cmd[j+1]
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
        results = []
        matches_count = 0
        
        # Parse rg --json
        import json
        
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
                    processed_matches.append(Match(
                        line_number=data["data"]["line_number"],
                        content=content,
                        is_match=True,
                        file_path=file_path
                    ))
                elif data["type"] == "context":
                    content = data["data"]["lines"]["text"].rstrip()
                    file_path = data["data"]["path"]["text"]
                    if request.fold:
                         # Context usually doesn't have the match, so standard truncate
                         if len(content) > 1000:
                             content = content[:1000] + "..."
                    processed_matches.append(Match(
                        line_number=data["data"]["line_number"],
                        content=content,
                        is_match=False,
                        file_path=file_path
                    ))
            except json.JSONDecodeError:
                continue
                
        return {
            "matches": processed_matches, 
            "total_matches": matches_count, 
            "original_query": str([q.query for q in request.queries]), 
            "variations": all_variations,
            "command_executed": command_str
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("static/index.html", "r") as f:
        return f.read()
