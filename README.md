# Ripgrep Wrapper (Rapper)

A web interface for [ripgrep](https://github.com/BurntSushi/ripgrep) (rg) that allows you to search for data (like phone numbers) with automatic variation generation and context display.

## Features

- **Multi-Flow Search**: Search for multiple terms simultaneously (e.g., a name AND a phone number).
- **Flexible Path**: Configure the search directory or file directly from the UI.
- **PII Search**: Specialized handling for phone numbers (auto-generates variations like `123-456-7890`, `(123) 456-7890`, etc.).
- **Context Awareness**: Configurable context lines around matches.
- **Smart Folding**: Folds long lines (>1000 chars) while keeping the matched content visible.
- **Command Preview**: See the exact `rg` command being executed.

## Prerequisites

- **Python**: 3.10 or higher.
- **ripgrep**: The `rg` command line tool must be installed and available in your system's PATH.
  - MacOS: `brew install ripgrep`
  - Ubuntu/Debian: `sudo apt-get install ripgrep`
  - Windows: `choco install ripgrep`

## Installation

1.  Clone the repository or navigate to the project directory.

2.  Install the required Python dependencies:

    ```bash
    pip install fastapi uvicorn jinja2 requests
    ```
    
    *Or if you are using `uv` or another package manager, install from `pyproject.toml`.*

## Data Setup

You can search any file or directory. To test quickly:

1.  Create a file named `data.txt` in the project root.
2.  Add some text content you want to search through.
3.  Use `.` (current directory) or `data.txt` in the "search path" input.

## Usage

1.  Start the FastAPI server:

    ```bash
    uvicorn main:app --reload
    ```

    *The `--reload` flag is useful for development as it restarts the server on code changes.*

2.  Open your web browser and navigate to:

    ```
    http://127.0.0.1:8000
    ```

3.  **The Hood (Search Path)**: Enter `.` to search the current directory or a specific file path.
4.  **Freestyle (Query)**: Enter your search term. Click **"+ Add Another Flow"** to search for multiple items at once.
5.  **Mic Check**: Click search. The executed command will appear immediately, followed by results grouped by file.

## API Endpoints

-   `GET /`: Serves the main search interface.
-   `POST /search`: JSON API to perform searches.
    -   Body: 
        ```json
        {
          "queries": [
            {"query": "1234567890", "type": "phone"},
            {"query": "john", "type": "name"}
          ],
          "search_path": ".",
          "context": 5,
          "fold": true
        }
        ```
-   `POST /search/preview`: Returns the `rg` command string that would be executed for a given request without running it.
