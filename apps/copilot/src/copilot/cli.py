"""
Mono Copilot — a friendly terminal app.

This talks to the copilot agent server (the FastAPI app in main.py) so you can
create BRDs and PRDs by answering a few questions, instead of sending raw
requests by hand.

HOW TO RUN
----------
1. Start the server in ONE terminal (leave it running):
       APP_ENV=development uv run --package copilot start
   Wait for: "Sandbox ready. Persistent workspace at /workspace/projects"

2. In a SECOND terminal, run this app:
       uv run --package copilot app

That's it — follow the on-screen menu.

Note: this app only uses Python's standard library, so there is nothing extra
to install.
"""

import json
import re
import sys
import urllib.error
import urllib.request

# Where the server is listening. Matches main.py's development host/port.
BASE_URL = "http://127.0.0.1:8000"
AGENT_URL = f"{BASE_URL}/agent"

# Agent runs can be slow (it thinks, writes files, etc.), so give it room.
TIMEOUT_SECONDS = 300


# --------------------------------------------------------------------------
# Talking to the server
# --------------------------------------------------------------------------

def call_agent(user_input: str) -> str:
    """Send a message to the agent and return its text reply.

    Raises RuntimeError with a friendly message if something goes wrong.
    """
    payload = json.dumps({"user_input": user_input}).encode("utf-8")
    request = urllib.request.Request(
        AGENT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
            return body.get("response", "(the server sent no response text)")
    except urllib.error.HTTPError as error:
        # The server ran but returned an error (e.g. 500). Try to read its detail.
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail", str(error))
        except Exception:
            detail = str(error)
        raise RuntimeError(f"The server reported an error ({error.code}): {detail}")
    except urllib.error.URLError as error:
        # Could not reach the server at all.
        raise RuntimeError(
            f"Could not reach the assistant at {BASE_URL}.\n"
            "Is the server running? Start it in another terminal with:\n"
            "    APP_ENV=development uv run --package copilot start\n"
            f"(Technical detail: {error.reason})"
        )


def server_is_up() -> bool:
    """Quick check that the server is reachable before showing the menu."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/", timeout=10) as response:
            return response.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def ask(label: str) -> str:
    """Prompt the user for a line of input, handling Ctrl+C gracefully."""
    try:
        return input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye!")
        sys.exit(0)


def slugify(name: str) -> str:
    """Turn 'eSIM Prepaid Launch' into 'esim-prepaid-launch' for a folder name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-") or "project"


def working_message() -> None:
    print("\nWorking… this can take up to a minute.\n")


# --------------------------------------------------------------------------
# The actions
# --------------------------------------------------------------------------

def _collect_project_details() -> dict | None:
    """Ask the shared questions used by both BRD and PRD."""
    project = ask("Project name (e.g. eSIM prepaid launch)")
    if not project:
        print("Cancelled — no project name given.")
        return None
    telco = ask("Operating company / telco name")
    country = ask("Operating country")
    description = ask("Briefly, what is this about? (the initiative or feature)")
    return {
        "project": project,
        "slug": slugify(project),
        "telco": telco,
        "country": country,
        "description": description,
    }


def _send_document_request(kind: str, details: dict) -> None:
    """Build and send the request for a BRD or PRD, then print the reply."""
    path = f"/workspace/projects/{details['slug']}"
    if kind == "brd":
        user_input = (
            f"Use the create-brd skill to write a BRD for the '{details['project']}' initiative. "
            f"Operating company is {details['telco']} in {details['country']}. "
            f"Details: {details['description']}. "
            f"Save it in the project folder {path}/ as project-brd.md and tell me the exact path. "
            f"Use reasonable [TBD] placeholders for anything not provided rather than asking me questions."
        )
    else:
        user_input = (
            f"Use the create-prd skill for the '{details['project']}' project. "
            f"Operating company is {details['telco']} in {details['country']}. "
            f"Details: {details['description']}. "
            f"First read any existing BRD at {path}/project-brd.md and base the PRD on it. "
            f"Save it in {path}/ as project-prd.md and tell me the exact path. "
            f"Use reasonable [TBD] placeholders for anything not provided rather than asking me questions."
        )

    working_message()
    try:
        print(call_agent(user_input))
    except RuntimeError as error:
        print(f"[!] {error}")


def create_brd() -> None:
    print("\n=== Create a Business Requirements Document (BRD) ===")
    details = _collect_project_details()
    if details is None:
        return
    _send_document_request("brd", details)

    # Offer the BRD -> PRD chain.
    follow_up = ask("\nCreate a PRD for this same project too? (y/n)")
    if follow_up.lower().startswith("y"):
        _send_document_request("prd", details)


def create_prd() -> None:
    print("\n=== Create a Product Requirements Document (PRD) ===")
    details = _collect_project_details()
    if details is None:
        return
    _send_document_request("prd", details)


def list_projects() -> None:
    print("\n=== Your projects ===")
    working_message()
    try:
        print(call_agent(
            "List the folders inside /workspace/projects. For each one, say which "
            "documents it contains (for example project-brd.md, project-prd.md). "
            "If there are no projects yet, say so plainly."
        ))
    except RuntimeError as error:
        print(f"[!] {error}")


def free_form() -> None:
    print("\n=== Ask the assistant (type 'back' to return to the menu) ===")
    while True:
        message = ask("You")
        if message.lower() in {"back", "menu", "exit", "quit", ""}:
            return
        working_message()
        try:
            print(call_agent(message) + "\n")
        except RuntimeError as error:
            print(f"[!] {error}\n")


# --------------------------------------------------------------------------
# Menu loop
# --------------------------------------------------------------------------

BANNER = r"""
==========================================
   Mono Copilot  —  BRD / PRD Assistant
==========================================
"""

MENU = """
What would you like to do?

  1) Create a Business Requirements Document (BRD)
  2) Create a Product Requirements Document (PRD)
  3) List my projects
  4) Ask the assistant something
  5) Quit
"""


def main() -> None:
    print(BANNER)

    if not server_is_up():
        print("[!] Can't reach the assistant server at " + BASE_URL)
        print("    Start it first in another terminal:")
        print("        APP_ENV=development uv run --package copilot start")
        print("    Then run this app again.\n")
        # Let them try anyway in case it's just starting up.

    while True:
        print(MENU)
        choice = ask("Enter a number (1-5)")
        if choice == "1":
            create_brd()
        elif choice == "2":
            create_prd()
        elif choice == "3":
            list_projects()
        elif choice == "4":
            free_form()
        elif choice in {"5", "q", "quit", "exit"}:
            print("Goodbye!")
            return
        else:
            print("Please enter a number from 1 to 5.")


if __name__ == "__main__":
    main()